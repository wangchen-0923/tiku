"""用户、认证、JWT、角色 · 业务写操作。

职责边界（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response

★★ 本模块是**攻击面最大**的一个：登录、改密、建号。

  三条必须守住的：

    1. **失败信息不区分「用户不存在」与「密码错误」**
       否则攻击者可以拿一份用户名列表逐个试，把「哪些账号存在」
       问出来，再去猜密码。提示统一是「用户名或密码错误」。

    2. **登录失败次数要限流**（按用户名 + IP）
       不限流的话，弱密码账号会被在线爆破。

    3. **不能建/不能改出比自己高的角色**
       否则机构管理员建一个平台管理员账号，就是一条提权路径。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.authentication import issue_tokens
from apps.accounts.models import Role, User, role_label
from apps.audit.models import AuditAction
from apps.audit.services import audit_log
from apps.common.config import get_config
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)

#: 审计动作
A_LOGIN_OK = "user.login"
A_LOGIN_FAIL = "user.login_failed"
A_USER_CREATE = "user.create"
A_USER_UPDATE = "user.update"
A_USER_DISABLE = "user.disable"
A_PASSWORD_CHANGE = "user.password_change"
A_PASSWORD_RESET = "user.password_reset"


#: Django 的「不可用密码」标记（`set_unusable_password()` 会写成 `!<随机>`）。
_UNUSABLE_PASSWORD_PREFIX = "!"


def has_usable_password_hash(password_hash: str | None) -> bool:
    """从**密码哈希字符串**判断这个账号能不能登录。

    ★ 为什么需要它（而不是直接用 `user.can_login`）：

      批量场景（如发布结果里的身份覆盖率）走的是
      `values_list("student__password")`，只拿到哈希串，没有模型实例 ——
      为了一个布尔值去逐行取对象会变成 N+1。

    ★ 判据与 `User.can_login` 一致（都是「有没有可用密码」），
      所以「学生身份行」在这里会被正确识别为**不能登录**。
    """
    return bool(password_hash) and not str(password_hash).startswith(
        _UNUSABLE_PASSWORD_PREFIX)


def _fail_bucket(username: str, ip: str | None) -> str:
    """登录失败计数的 key。

    ★ 按 **用户名 + IP** 双维度：只按 IP 会让整个学校共用出口 IP
      时一个学生输错就锁住所有人；只按用户名则攻击者换 IP 就绕过。
      两个都算，任一超限即拒。
    """
    return f"login_fail:{username}:{ip or '-'}"


def _too_many_failures(username: str, ip: str | None) -> bool:
    from django.core.cache import cache

    max_try = int(get_config("auth.login_max_attempts"))
    n = cache.get(_fail_bucket(username, ip)) or 0
    try:
        return int(n) >= max_try
    except (TypeError, ValueError):
        return False


def _bump_failures(username: str, ip: str | None) -> int:
    from django.core.cache import cache

    window = int(get_config("auth.login_lock_seconds"))
    key = _fail_bucket(username, ip)
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, window)
        n = 1
    try:
        cache.touch(key, window)
    except Exception:      # noqa: BLE001
        # ★ 某些后端不支持 touch；退化为重设一次，保证窗口滑动。
        cache.set(key, n, window)
    return int(n)


def _clear_failures(username: str, ip: str | None) -> None:
    from django.core.cache import cache

    cache.delete(_fail_bucket(username, ip))


class AuthService:
    """登录与令牌。"""

    @staticmethod
    def login(
        *,
        username: str,
        password: str,
        ip: str | None = None,
        user_agent: str = "",
    ) -> dict[str, Any]:
        """用户名 + 密码登录。

        ★★ 三重防护：

            1. 限流：同一「用户名 + IP」失败超过阈值直接拒，
               **不再比对密码**（省掉一次昂贵的 argon2 校验）
            2. 统一失败提示：「用户名或密码错误」——
               不区分「用户不存在」与「密码不对」
            3. 停用与软删账号一律拒，且提示同样是统一文案
               （提示「账号已停用」等于告诉攻击者这个账号存在）
        """
        from apps.accounts.selectors import UserSelector

        uname = (username or "").strip()
        if not uname or not password:
            raise ZfError(ErrorCode.AUTH_INVALID_CREDENTIALS,
                          "请输入用户名与密码")

        if _too_many_failures(uname, ip):
            # ★ `target` 不能传 `None`：`AuditService._resolve_target()`
            #   会抛 `ValueError: target 必须是模型实例或 (type, id) 元组`。
            #   登录失败时还没有用户对象，用 `("User", 0)` 表示
            #   「针对用户表、但没有具体对象」——审计表本身
            #   就要求有 target 字段。
            audit_log.record(
                A_LOGIN_FAIL, ("User", 0), None,
                org_id=None,
                after={"username": uname, "ip": ip, "reason": "RATE_LIMITED"},
            )
            raise ZfError(
                ErrorCode.COMMON_RATE_LIMITED,
                f"登录失败次数过多，请 "
                f"{int(get_config('auth.login_lock_seconds')) // 60} "
                f"分钟后再试",
            )

        user = UserSelector.by_username(uname)
        # ★★ `not user.can_login`：**学生身份行**（`role=STUDENT`，发布任务时
        #   按学号自动建）没有可用密码，`check_password` 必定 False。
        #   放在这里是为了**少做一次 argon2 校验**，并确保提示也是统一的
        #   「用户名或密码错误」——不泄漏「这个学号存在但没有凭据」。
        #   ★ 日后「绑定账号」= 给这一行设密码 → 同一判据自动放行。
        if user is None or not user.can_login or not user.check_password(password):
            n = _bump_failures(uname, ip)
            audit_log.record(
                A_LOGIN_FAIL, ("User", 0), None,
                org_id=getattr(user, "org_id", None),
                after={"username": uname, "ip": ip, "attempt": n},
            )
            max_try = int(get_config("auth.login_max_attempts"))
            left = max(0, max_try - n)
            raise ZfError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "用户名或密码错误"
                + (f"（还可尝试 {left} 次）" if left <= 3 else ""),
            )

        if not user.is_active or user.deleted_at is not None:
            # ★ 提示与密码错误一致，不泄漏「账号存在但被停用」
            _bump_failures(uname, ip)
            audit_log.record(
                A_LOGIN_FAIL, user, None,
                after={"username": uname, "ip": ip, "reason": "DISABLED"},
            )
            raise ZfError(ErrorCode.AUTH_INVALID_CREDENTIALS,
                          "用户名或密码错误")

        if user.org_id is None and not user.is_platform_admin:
            raise ZfError(
                ErrorCode.AUTH_FORBIDDEN,
                "该账号未绑定机构，请联系管理员",
            )

        _clear_failures(uname, ip)
        tokens = issue_tokens(user)
        User.objects.filter(pk=user.pk).update(
            last_login=timezone.now(), last_login_ip=ip or None)
        audit_log.record(
            A_LOGIN_OK, user, user.id,
            after={"ip": ip, "user_agent": user_agent[:200]},
        )
        return {
            **tokens,
            "user": {
                "id": user.id,
                "username": user.username,
                "real_name": user.real_name,
                "role": user.role,
                "org_id": user.org_id,
            },
        }

    @staticmethod
    def refresh(refresh_token: str) -> dict[str, Any]:
        """用刷新令牌换新令牌。

        ★★ 一期**不做刷新令牌轮换**（rotation）。

          设计文档第 4 节写的是「刷新令牌一次性使用（rotation）」，
          但轮换需要服务端存「已用过的 jti」，否则无法判断一个
          刷新令牌是否已被用过——而那只在**被盗用**时才有意义
          （攻击者用了旧令牌，合法用户被挤下线，从而暴露）。

          一期的取舍：**不做轮换，把寿命压到 14 天**，
          响应里返回新的 `refresh_token`（滚动续期）。

          ★ 这是一条需要回写的偏差：03 开发计划里应记为
            「刷新轮换延后到二期（需 Redis 存 jti 黑名单）」。
        """
        import jwt as _jwt

        from apps.accounts.authentication import (
            ALGORITHM,
            TOKEN_TYPE_REFRESH,
            _secret,
        )

        try:
            payload = _jwt.decode(refresh_token, _secret(),
                                  algorithms=[ALGORITHM])
        except _jwt.ExpiredSignatureError as exc:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          "登录已过期，请重新登录") from exc
        except _jwt.PyJWTError as exc:
            raise ZfError(ErrorCode.AUTH_INVALID_CREDENTIALS,
                          "刷新凭证无效，请重新登录") from exc

        if payload.get("type") != TOKEN_TYPE_REFRESH:
            raise ZfError(ErrorCode.AUTH_INVALID_CREDENTIALS,
                          "凭证类型不正确，请重新登录")

        from apps.accounts.selectors import UserSelector

        user = UserSelector.me(int(payload.get("sub") or 0))
        if user is None or not user.is_active or user.deleted_at is not None:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          "账号已停用，请重新登录")
        # ★ 机构变更后旧令牌失效（与 JWTAuthentication 同一判据）
        if payload.get("org_id") != user.org_id:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          "账号机构已变更，请重新登录")

        # ★★ 改密码后旧**刷新**令牌同样失效（U26）
        #
        #   只让访问令牌失效是不够的：攻击者拿旧的 refresh 就能换一个
        #   新的 access，整条失效机制等于被绕过。
        #
        #   ★ 这个漏点是靠「跨路径用例」发现的——访问令牌那条路径全绿，
        #     只有把同一个旧令牌的两个用途都测一遍才暴露。
        #     凡是"同一判据要在两处生效"的地方，都要各写一条用例。
        from apps.accounts.authentication import password_fingerprint

        if payload.get("pv") != password_fingerprint(user):
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          "密码已变更，请重新登录")

        return issue_tokens(user)


class UserService:
    """用户管理。"""

    #: 允许创建的角色（一期只有三种）
    CREATABLE_ROLES = frozenset({
        Role.TEACHER, Role.ORG_ADMIN, Role.PLATFORM_ADMIN,
    })

    #: 离职移交时要改写 `created_by` 的业务表。
    #:
    #: ★ 用 `(app_label, model_name)` 字符串而不是直接 import 模型：
    #:   这些 app 大多间接依赖 accounts，顶层导入会形成循环。
    #:
    #: ★ 只列**教师会自己创建**的东西。成绩、作答、学生身份行不在列 ——
    #:   它们属于学生，不属于教师，换老师不该影响它们。
    TRANSFER_TARGETS: tuple[tuple[str, str], ...] = (
        ("paper", "Paper"),                 # 试卷
        ("questionbank", "Question"),       # 题目
        ("knowledge", "KnowledgeEntry"),    # 知识库条目
        ("assignment", "Assignment"),       # 作业
        ("sheet", "SheetTemplate"),         # 答题卡
    )
    #
    # ★ `knowledge.KnowledgeAlias` 不在列：它继承的是 `models.Model`
    #   （没有 `deleted_at`，也不是 BaseModel），而且别名是**学科级**的
    #   公共资产，通常由学校平台账户维护，不属于某个教师的个人产出。
    #   放进来的话 `.filter(deleted_at__isnull=True)` 会直接报
    #   FieldError。

    @staticmethod
    @transaction.atomic
    def create_user(
        *,
        org_id: int | None,
        username: str,
        password: str,
        real_name: str = "",
        role: str = Role.TEACHER,
        phone: str = "",
        operator_id: int | None = None,
        operator=None,
    ) -> User:
        """建账号。

        ★★ **不能建比自己高的角色**：

            机构管理员只能建教师与（同级）机构管理员，
            **不能建平台管理员**——那是一条提权路径：
            他建一个平台管理员账号，然后用那个账号跨机构访问。

          判据是 `ROLE_RANK`（见 `permissions.py`），
          不是「角色名是否相等」——加角色时只改层级表。

        ★ 密码强度在这里校验（长度 + 常见弱口令），不放序列化器：
          建号与改密两条路都要过同一套规则，写两遍必然漏改一处。
        """
        uname = (username or "").strip()
        if not uname:
            raise ValidationFailed(detail="用户名不能为空")
        if User._base_manager.filter(username=uname).exists():
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"用户名 {uname} 已被占用",
                extra={"username": uname},
            )

        if role not in UserService.CREATABLE_ROLES:
            raise ValidationFailed(
                detail=f"未知角色 {role!r}，可选 "
                       f"{sorted(UserService.CREATABLE_ROLES)}")
        UserService._check_role_ceiling(role, operator)

        # ★ 系统管理员的 org 必须为空：它属于平台，不属于任何一所学校。
        #   DB 层还有 `ck_platform_admin_no_org` 兜底，这里先给可读的提示。
        if role == Role.PLATFORM_ADMIN and org_id is not None:
            raise ValidationFailed(
                detail="系统管理员不属于任何学校，请不填所属机构")
        # ★ 学校平台账户的 org 必须填：它是「学校在平台上的代表」，
        #   没有归属的学校账户等于一个能建账号却没人认领的孤儿。
        if role in (Role.ORG_ADMIN, Role.TEACHER) and org_id is None:
            raise ValidationFailed(
                detail=f"{role_label(role)}必须绑定所属机构")

        # ★ 每校的「学校平台账户」有上限（1 主 + 至多 2 备）。
        #   这是**法人代表账号**，绑着服务额度与数据归属；放开数量会让
        #   「谁代表学校」变得含糊，出事时互相推诿。
        if role == Role.ORG_ADMIN:
            UserService._check_school_admin_quota(org_id)

        UserService._check_password_strength(password)

        user = User.objects.create_user(
            username=uname, password=password, real_name=real_name.strip(),
            role=role, phone=phone.strip(), org_id=org_id,
        )
        audit_log.record_strict(
            A_USER_CREATE, user, operator_id,
            after={"username": uname, "role": role, "org_id": org_id},
        )
        return user

    @staticmethod
    def _check_role_ceiling(role: str, operator) -> None:
        """能否**授予**某角色。

        ★★ 铁律 1：只能管低一级（`rank(目标) < rank(操作者)`）。

          原实现用的是 `>`（允许同级），于是学校平台账户可以建
          **第二个学校平台账户**，两个平级账号能互相改密码、互相停用。
          实际运营中一旦两人有分歧，学校层面就没有仲裁者了，
          只能找到平台方才能解开。

          唯一放行的例外是系统管理员：多个 sysadmin 之间允许同级，
          否则唯一那个 sysadmin 密码丢了就整站失守。

        ★ 具体判据在 `apps.common.permissions.can_create_role()`，
          这里只负责把 `(bool, reason)` 转成异常 —— 视图、脚本、
          迁移都能复用同一套规则，不会各写一份。
        """
        if operator is None:
            return      # 系统任务 / 迁移脚本 / 平台初始化
        from apps.common.permissions import can_create_role

        allowed, reason = can_create_role(operator, role)
        if not allowed:
            raise ZfError(
                ErrorCode.AUTH_FORBIDDEN,
                detail=reason,
                extra={"requested_role": role,
                       "your_role": getattr(operator, "role", "")},
            )

    @staticmethod
    def _check_manage_target(target, operator, action: str = "操作") -> None:
        """能否操作某个已存在的账号（改角色 / 停用 / 重置密码）。

        ★★ 铁律 3：**不能改自己** —— 一条命令把自己降级或停用，
          会导致该校再没有人能恢复（另一个学校账户也不存在时）。

        ★ 同级不可互管：两个学校平台账户之间不能互相停用。
        """
        if operator is None or target is None:
            return
        from apps.common.permissions import can_manage_target

        allowed, reason = can_manage_target(operator, target)
        if not allowed:
            raise ZfError(
                ErrorCode.AUTH_FORBIDDEN,
                detail=reason or f"无权{action}该账号",
                extra={"target_id": target.pk,
                       "target_role": getattr(target, "role", "")},
            )

    @staticmethod
    @transaction.atomic
    def transfer_assets(
        *,
        org_id: int,
        from_user_id: int,
        to_user_id: int,
        operator=None,
        operator_id: int | None = None,
        reason: str = "",
    ) -> dict[str, int]:
        """移交离职教师的教学资产。

        ★★ 为什么这是 L2 存在的**真正理由**（铁律 6）：

          教师会离职。如果试卷、题库、知识库条目只归教师个人，
          人一走这些数据就成孤儿 —— 全组老师都看不见、学校也收不回，
          最后的处理办法通常是「让他把账号密码留给教研组长」，
          而这正是权限体系失效的起点。

          所以「**撤销权留给学校**」比「能建账号」更关键：
          学校必须能在人走之后，把他在平台上留下的东西转给别人。

        ★ 只移交**归属关系**，不动业务状态：
          试卷仍是已定稿的试卷、作业仍是已关闭的作业、
          学生成绩与作答一条不动（它们挂在学生的身份行上，与教师无关）。

        ★ 不移交审计日志。`audit_log.actor` 记的是「当时是谁做的」，
          改写它等于篡改历史。原账号停用后，日志里显示的名字仍然可查。

        ★ 不自动停用原账号：移交与停用是两件事。先移交、核对无误、
          再停用 —— 合成一步的话，一旦移交漏了某张表，
          原账号已经登不进去，只能改数据库救回来。
        """
        from django.apps import apps as django_apps

        src = User._base_manager.filter(
            pk=from_user_id, org_id=org_id, deleted_at__isnull=True).first()
        if src is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="被移交的账号不存在")
        dst = User._base_manager.filter(
            pk=to_user_id, org_id=org_id, deleted_at__isnull=True).first()
        if dst is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="接收账号不存在")
        if src.pk == dst.pk:
            raise ValidationFailed(detail="不能移交给自己")
        if dst.role == Role.STUDENT:
            raise ValidationFailed(
                detail="接收人必须是教师账号，学生身份行不能接收教学资产")
        UserService._check_manage_target(src, operator, action="移交")
        if operator is not None:
            UserService._check_manage_target(dst, operator, action="移交给")

        counts: dict[str, int] = {}
        for app_label, model_name in UserService.TRANSFER_TARGETS:
            model = django_apps.get_model(app_label, model_name)
            n = model.objects.filter(
                org_id=org_id, created_by_id=src.pk,
                deleted_at__isnull=True,
            ).update(created_by_id=dst.pk)
            counts[f"{app_label}.{model_name}"] = n

        # ★★ 班级单独处理：它的归属字段叫 `owner_id`，不是 `created_by_id`。
        #   而班级恰恰是**最必须移交**的一类 —— 教师离职后，他建的班
        #   连同名册一起没人管，学校只能重建一个，历史成绩全部脱钩。
        #   （`TRANSFER_TARGETS` 是 `created_by_id` 的批量清单，
        #     字段名不同，硬塞进去会 FieldError。）
        from apps.org.models import Classroom

        counts["org.Classroom(owner)"] = Classroom.objects.filter(
            org_id=org_id, owner_id=src.pk, deleted_at__isnull=True,
        ).update(owner_id=dst.pk)

        # ★ 班主任也一起转：否则班移交了、学生成绩也跟过去了，
        #   但「班主任」还挂着离职的人，后续按班主任口径的查询全错。
        counts["org.Classroom(head_teacher)"] = Classroom.objects.filter(
            org_id=org_id, head_teacher_id=src.pk, deleted_at__isnull=True,
        ).update(head_teacher_id=dst.pk)

        audit_log.record_strict(
            AuditAction.USER_TRANSFER, dst, operator_id,
            before={"from_username": src.username, "from_id": src.pk},
            after={"to_username": dst.username, "to_id": dst.pk,
                   "counts": counts},
            reason=reason or f"移交 {src.display} 的教学资产给 {dst.display}",
        )
        return counts

    @staticmethod
    def _check_school_admin_quota(org_id: int | None) -> None:
        """每校「学校平台账户」数量上限（默认 3：1 主 + 2 备）。"""
        from apps.common.permissions import MAX_SCHOOL_ADMINS_PER_ORG

        if org_id is None:
            return
        used = User._base_manager.filter(
            org_id=org_id, role=Role.ORG_ADMIN, deleted_at__isnull=True,
        ).count()
        if used >= MAX_SCHOOL_ADMINS_PER_ORG:
            raise ValidationFailed(
                detail=(
                    f"该校已有 {used} 个学校平台账户，达到上限 "
                    f"{MAX_SCHOOL_ADMINS_PER_ORG} 个。"
                    "请先停用不用的账号，或联系系统管理员调整"
                )
            )

    @staticmethod
    def _check_password_strength(password: str) -> None:
        """密码强度校验。

        ★ 只做**长度 + 常见弱口令**两道。不强制大小写混合——
          强制混合会让人写成 `Abc12345` 然后贴在显示器上，
          而 12 位纯小写短语更安全也更好记。
        """
        pwd = password or ""
        min_len = int(get_config("auth.password_min_length"))
        if len(pwd) < min_len:
            raise ValidationFailed(
                detail=f"密码长度不能少于 {min_len} 位")
        weak = get_config("auth.password_weak_list") or []
        if pwd.lower() in {str(w).lower() for w in weak}:
            raise ValidationFailed(detail="该密码过于常见，请换一个")

    @staticmethod
    @transaction.atomic
    def update_user(
        *,
        org_id: int,
        user_id: int,
        operator_id: int | None = None,
        operator=None,
        real_name: str | None = None,
        phone: str | None = None,
        role: str | None = None,
    ) -> User:
        """改用户资料 / 角色。

        ★ 改角色同样受**等级上限**约束——否则学校平台账户可以把
          同机构的某个老师提成系统管理员，等于绕过建号那道检查。

        ★★ 铁律 3：不能改自己的**角色**。
          改自己的资料（姓名、手机号）是日常操作，放行；
          但把自己降级会导致该校再没有人能恢复（另一个学校账户
          也不存在时），所以那条必须拦住。
        """
        user = User._base_manager.filter(
            pk=user_id, org_id=org_id, deleted_at__isnull=True).first()
        if user is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")

        before: dict[str, Any] = {}
        changed: list[str] = []
        if real_name is not None:
            before["real_name"] = user.real_name
            user.real_name = real_name.strip()
            changed.append("real_name")
        if phone is not None:
            before["phone"] = user.phone
            user.phone = phone.strip()
            changed.append("phone")
        if role is not None and role != user.role:
            # ★★ 只有**改角色**才受「不能改自己/同级」约束。
            #   改自己的姓名、手机号是日常操作，不该被拦——
            #   拦住的话老师连自己名字写错了都改不了。
            UserService._check_manage_target(user, operator, action="修改角色")
            if role not in UserService.CREATABLE_ROLES:
                raise ValidationFailed(detail=f"未知角色 {role!r}")
            UserService._check_role_ceiling(role, operator)
            if role == Role.ORG_ADMIN:
                UserService._check_school_admin_quota(org_id)
            # ★ 也要挡住「把高等级的人降下来」之外的情况：
            #   改**别人**的角色时，对方当前等级也不能高于自己
            UserService._check_role_ceiling(user.role, operator)
            before["role"] = user.role
            user.role = role
            changed.append("role")

        if changed:
            changed.append("updated_at")
            user.save(update_fields=list(dict.fromkeys(changed)))
            audit_log.record_strict(
                A_USER_UPDATE, user, operator_id,
                before=before, after={"changed": changed},
            )
        return user

    @staticmethod
    @transaction.atomic
    def update_self(
        *,
        user_id: int,
        real_name: str | None = None,
        phone: str | None = None,
    ) -> User:
        """改**自己的**资料（个人中心 · U26）。

        ★ 与 `update_user` 的三点区别：

          1. **不收 `role`**：改自己的角色不在本接口范围内。
             `update_user` 里那条「不能改自己的角色」是铁律，
             与其在这里再判一次，不如让这个参数根本不存在——
             不存在的参数不存在绕过。
          2. **不收 `org_id` 做归属校验**：改的是自己，归属天然成立。
          3. 审计里记 `self: True`：事后查「谁把张三的手机号改了」时，
             能一眼分清是本人改的还是管理员改的。
        """
        user = User._base_manager.filter(
            pk=user_id, deleted_at__isnull=True).first()
        if user is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")

        before: dict[str, Any] = {}
        changed: list[str] = []
        if real_name is not None:
            name = real_name.strip()
            if len(name) > 100:
                raise ValidationFailed(detail="姓名不能超过 100 个字符")
            before["real_name"] = user.real_name
            user.real_name = name
            changed.append("real_name")
        if phone is not None:
            phone_clean = phone.strip()
            if len(phone_clean) > 20:
                raise ValidationFailed(detail="手机号不能超过 20 个字符")
            before["phone"] = user.phone
            user.phone = phone_clean
            changed.append("phone")

        if changed:
            changed.append("updated_at")
            user.save(update_fields=list(dict.fromkeys(changed)))
            audit_log.record_strict(
                A_USER_UPDATE, user, user_id,
                before=before, after={"changed": changed, "self": True},
            )
        return user

    @staticmethod
    @transaction.atomic
    def disable_user(
        *,
        org_id: int,
        user_id: int,
        operator_id: int | None = None,
        operator=None,
        reason: str = "",
    ) -> User:
        """停用账号（软删）。

        ★★ 停用是**软删**（`deleted_at` + `is_active=False`）：

            `User` 被大量外键引用（题目 `created_by`、成绩
            `graded_by`、审计 `actor`）。硬删会让这些引用悬空
            或触发 PROTECT。

        ★ 铁律 3：**不能停用自己** —— 那会把自己锁在门外。
        ★ 铁律 1：**不能停用同级或更高级**的人。
          两个学校平台账户互停，学校就没人管了。
        """
        if operator_id and int(operator_id) == int(user_id):
            raise ValidationFailed(
                detail="不能停用自己的账号（会导致无法登录）")
        user = User._base_manager.filter(
            pk=user_id, org_id=org_id, deleted_at__isnull=True).first()
        if user is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")
        UserService._check_manage_target(user, operator, action="停用")

        before = {"is_active": user.is_active, "role": user.role}
        user.delete()      # 软删：置 deleted_at + is_active=False
        audit_log.record_strict(
            A_USER_DISABLE, user, operator_id,
            before=before, after={"is_active": False},
            reason=reason or "停用账号",
        )
        return user

    @staticmethod
    @transaction.atomic
    def change_password(
        *,
        user_id: int,
        old_password: str,
        new_password: str,
        operator_id: int | None = None,
    ) -> None:
        """改自己的密码。**必须验旧密码**。

        ★ 不验旧密码的话，一个被偷了会话的攻击者可以直接改密
          把账号锁死（合法用户再也登不进来）。
        """
        user = User._base_manager.filter(
            pk=user_id, deleted_at__isnull=True).first()
        if user is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")
        if not user.check_password(old_password or ""):
            raise ZfError(ErrorCode.AUTH_INVALID_CREDENTIALS,
                          "原密码不正确")
        UserService._check_password_strength(new_password)
        if new_password == old_password:
            raise ValidationFailed(detail="新密码不能与原密码相同")
        user.set_password_and_save(new_password)
        audit_log.record_strict(
            A_PASSWORD_CHANGE, user, operator_id or user.id,
            after={"changed": True},
        )

    @staticmethod
    @transaction.atomic
    def reset_password(
        *,
        org_id: int,
        user_id: int,
        new_password: str,
        operator_id: int | None = None,
        operator=None,
    ) -> None:
        """管理员重置他人密码（**不需要旧密码**）。

        ★★ 这是**高权限动作**，所以有两道约束：

            1. 不能重置**高于自己等级**的人的密码
               （否则机构管理员可以重置平台管理员的密码再登录）
            2. 必写审计，且记 `operator_id`
        """
        target = User._base_manager.filter(
            pk=user_id, org_id=org_id, deleted_at__isnull=True).first()
        if target is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")
        # ★ 走这条路**不需要旧密码**，所以它是提权路径：
        #   必须挡住「重置自己」绕过旧密码校验，也要挡住同级互改。
        UserService._check_manage_target(target, operator, action="重置密码")
        UserService._check_password_strength(new_password)
        target.set_password_and_save(new_password)
        audit_log.record_strict(
            A_PASSWORD_RESET, target, operator_id,
            after={"target_username": target.username,
                   "target_role": target.role},
            reason="管理员重置密码",
        )


# ══════════════════════════════════════════════════════════════
#  ★ 学生身份行（发布任务时按学号自动建）
# ══════════════════════════════════════════════════════════════


class StudentIdentityService:
    """学生身份行：**每个学号一行**，作答数据都挂在它名下。

    ★★★ 为什么需要它（2026-10-16，需求方口径）：

        「教师发布任务 → 生成考试链接 → 学生用**学号 + 姓名**开启答题 →
         作答数据存到该班级对应学号的**学生账号**上」。

        即：**学生不需要先绑账号也能答题**，但数据必须挂在一个稳定身份上。

      原来 `accounts.User` 只表示「能登录的账号」，于是 45 个没绑账号的学生
      `student_id` 全是 `NULL`；而 `response` / `score_result` /
      `student_total_score` / `knowledge_mastery` / `diagnosis_snapshot` /
      `practice_set` / `scan_card` 七张表都按 `student_id` 取数 ——
      不给身份，整条链路（作答→批改→成绩→学情）都落不了库。

    ★ 做法：把「**他是谁**」与「**他能不能自己登录**」拆开。

      | 概念 | 载体 | 一期状态 |
      | --- | --- | --- |
      | 这个人是谁（作答/成绩/学情挂谁） | 本服务建的 `User` 行（`role=STUDENT`） | 发布任务时自动建 |
      | 他能不能自己登录看数据 | 该行**有没有可用密码**（`User.can_login`） | 没有 → 不能登录 |
      | 他是几号、叫什么 | `ClassroomStudent` / `AssignmentStudent` 的学号姓名快照 | 发布即冻结 |

      → 七张业务表**一个字都不用改**，作答/批改/成绩/学情照旧工作；
      → 日后「绑定账号」= 给这一行设密码（或与已有账号合并），
        历史数据天然在它名下，**不需要迁移历史**。

    ★ 用户名规则 `stu-{org_id}-{学号}`：全局唯一（`User.username` 是 unique），
      带机构前缀避免不同学校同学号撞车，且**可反推**回学号。
    """

    USERNAME_TEMPLATE = "stu-{org_id}-{student_no}"
    USERNAME_PREFIX = "stu-"

    # ── 用户名 ──
    @staticmethod
    def username_for(org_id: int, student_no: str) -> str:
        """学号 → 身份行用户名。★ 纯函数，幂等（不含时间/随机）。"""
        return User.normalize_username(
            StudentIdentityService.USERNAME_TEMPLATE.format(
                org_id=int(org_id), student_no=(student_no or "").strip()))

    @staticmethod
    def is_identity_username(username: str) -> bool:
        return str(username or "").startswith(StudentIdentityService.USERNAME_PREFIX)

    @staticmethod
    def student_no_from_username(username: str) -> str:
        """反解学号（`stu-1-25251401` → `25251401`）。

        ★ 只在能反解时返回；格式不对返回空串，**不抛错**——
          调用方用它做展示兜底，抛错会把「用户名不合规则」升级成 500。
        """
        uname = str(username or "")
        parts = uname.split("-", 2)
        if len(parts) != 3 or parts[0] != "stu":
            return ""
        return parts[2]

    # ── 建 / 取 ──
    @staticmethod
    @transaction.atomic
    def ensure(*, org_id: int, student_no: str, name: str = "") -> User:
        """保证「该机构 + 该学号」有一个身份行，返回它。

        ★ **幂等**：已存在则只维护展示名，**绝不碰登录凭据**
          （碰了就等于替学生设了密码 / 清了密码）。
        """
        no = (student_no or "").strip()
        if not no:
            raise ValidationFailed(detail="学号为空，无法建学生身份行")

        uname = StudentIdentityService.username_for(org_id, no)
        user = User._base_manager.filter(username=uname).first()
        if user is None:
            return StudentIdentityService._create(
                org_id=org_id, student_no=no, name=name)
        StudentIdentityService._assert_is_identity(user, student_no=no)
        StudentIdentityService._refresh(user, name=name)
        return user

    @staticmethod
    @transaction.atomic
    def ensure_many(
        *, org_id: int, rows: Iterable[tuple[str, str]],
    ) -> dict[str, Any]:
        """批量保证身份行存在。

        `rows` = `[(学号, 姓名), ...]`。返回::

            {"created": 3, "reused": 42, "students": {"25251401": <User>, ...}}

        ★ 一次查询取回已存在的行（不是 45 次 SELECT），新建逐条走
          `create_user`（它会 `set_unusable_password()`）。
        """
        wanted: dict[str, str] = {}
        for no, nm in rows:
            s = (no or "").strip()
            if s:
                wanted[s] = (nm or "").strip()

        if not wanted:
            return {"created": 0, "reused": 0, "students": {}}

        uname_of = {
            no: StudentIdentityService.username_for(org_id, no) for no in wanted
        }
        existing = {
            u.username: u
            for u in User._base_manager.filter(username__in=list(uname_of.values()))
        }

        created = reused = 0
        students: dict[str, User] = {}
        for no, nm in wanted.items():
            user = existing.get(uname_of[no])
            if user is None:
                user = StudentIdentityService._create(
                    org_id=org_id, student_no=no, name=nm)
                created += 1
            else:
                StudentIdentityService._assert_is_identity(user, student_no=no)
                StudentIdentityService._refresh(user, name=nm)
                reused += 1
            students[no] = user

        return {"created": created, "reused": reused, "students": students}

    # ── 内部 ──
    @staticmethod
    def _create(*, org_id: int, student_no: str, name: str) -> User:
        """建身份行。★ 无密码 → `set_unusable_password()`。"""
        user = User.objects.create_user(
            username=StudentIdentityService.username_for(org_id, student_no),
            password=None,                      # ★ None → 不可用密码
            real_name=(name or "").strip(),
            role=Role.STUDENT,
            org_id=org_id,
        )
        logger.debug(
            "建立学生身份行 org=%s 学号=%s user=%s", org_id, student_no, user.id)
        return user

    @staticmethod
    def _assert_is_identity(user: User, *, student_no: str) -> None:
        """已存在的同名用户必须就是身份行。

        ★ 两个必须拦的情况（都宁可报错也不能将错就错）：

          1. **用户名被非学生账号占用** —— 若把教师账号挂成学生名册行，
             他的成绩会记到教师名下；
          2. **身份行已被停用/软删** —— `username` 是字段级 unique，
             删掉也占着名字，此时"重建"会撞唯一约束；正确处置是先恢复。
        """
        if user.role != Role.STUDENT or not StudentIdentityService.is_identity_username(
                user.username):
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"用户名 {user.username} 已被非学生账号占用，"
                       f"无法为学号 {student_no} 建立学生身份行",
                extra={"username": user.username, "student_no": student_no,
                       "role": user.role},
            )
        if user.deleted_at is not None or not user.is_active:
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"学号 {student_no} 的学生身份行已被停用，请先恢复后再发布",
                extra={"username": user.username, "student_no": student_no},
            )

    @staticmethod
    def _refresh(user: User, *, name: str) -> None:
        """同步展示名。★ 只改姓名，不碰密码、不碰角色。"""
        nm = (name or "").strip()
        if nm and user.real_name != nm:
            user.real_name = nm
            user.save(update_fields=["real_name", "updated_at"])
