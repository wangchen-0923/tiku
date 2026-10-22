"""DRF 权限类 · 三级账户体系。

★ 机构隔离是**数据层**的事（`OrgScopedManager` / `apps.common.scoping`），
  这里只管「谁能调用这个接口」。

═══════════════════════════════════════════════════════════════════
 ★★ 三级账户（2026-10-21 定稿）
═══════════════════════════════════════════════════════════════════

  L3  `SYSTEM`   系统管理员       `PLATFORM_ADMIN`  org 必须为空
  L2  `SCHOOL`   学校平台账户     `ORG_ADMIN`       org 必填，每校 1 主 + 至多 2 备
  L1  `TEACHER`  教师账户         `TEACHER`         org 必填
  ─   `STUDENT`  学生身份行       `STUDENT`         **不是账户**，等级恒 0

★★ `STUDENT` 的等级必须是 0 且**永远不加**：

  它是「每个学号一个身份行」的载体（发布任务时建），
  一期**没有登录凭据**（`set_unusable_password()`），
  作答走 `/s/{publish_token}/` + `session_token`，不走 JWT。
  等级一旦 > 0，学生就会自动获得教师端接口权限。

★★ 为什么 DB 里的 `role` 值保持 `PLATFORM_ADMIN` / `ORG_ADMIN` 不变：

  后端 18 处视图判断、9 个回归脚本、前端 3 个文件都硬编码了这两个字符串。
  改 DB 值要连带改数据、代码、脚本、前端四层，收益只是「名字好听」。
  三级语义已完全对应，所以**值不动，显示名与文档术语统一为新三级**
  （见 `Role.label()` → 实际是模块级 `role_label()`）。
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

# ══════════════════════════════════════════════════════════════
#  角色等级表 —— 全项目唯一的权限数值来源
# ══════════════════════════════════════════════════════════════

#: 三级账户的等级常量。★ 加角色时**只改这里和 `ROLE_RANK`**，
#: 不要在任何视图里写 `if role == "ORG_ADMIN"`。
LEVEL_STUDENT = 0
LEVEL_TEACHER = 10
LEVEL_SCHOOL = 20
LEVEL_SYSTEM = 30

#: 角色层级（数值越大权限越高）。
#:
#: ★ 用**显式层级表**而不是一串 `if role == ...`：
#:   加角色时只改这一处；写成条件链时必然有人漏加分支，
#:   而漏加的表现是「新角色什么都干不了」或「新角色能干什么都行」。
ROLE_RANK: dict[str, int] = {
    "STUDENT": LEVEL_STUDENT,
    "TEACHER": LEVEL_TEACHER,
    "ORG_ADMIN": LEVEL_SCHOOL,
    "PLATFORM_ADMIN": LEVEL_SYSTEM,
}

#: 教师端「用户管理」可见的角色。
#:
#: ★ 学生身份行**不进用户管理列表**：它们不是登录账号，
#:   一个班 45 人、一所学校上千个，混进来会把「成员管理」冲垮。
#:   要管学生请去「班级与名册」页。
STAFF_ROLES: tuple[str, ...] = ("TEACHER", "ORG_ADMIN", "PLATFORM_ADMIN")

#: 每所学校允许的「学校平台账户」上限（1 主 + 至多 2 备）。
#:
#: ★ 为什么不是无限：这是**法人代表账号**，绑着服务额度与数据归属。
#:   放开数量会让「谁代表学校」变得含糊，出事时互相推诿。
#:   第 2、3 个只能由系统管理员创建（见 `can_create_role`）。
MAX_SCHOOL_ADMINS_PER_ORG = 3


# ══════════════════════════════════════════════════════════════
#  纯函数：权限判定（供 permissions 类与 service 层共用）
# ══════════════════════════════════════════════════════════════


def role_rank(user_or_role) -> int:
    """取角色等级。未知角色按 0（最低）处理。

    可传 user 对象，也可直接传角色字符串。
    """
    role = (
        user_or_role if isinstance(user_or_role, str)
        else str(getattr(user_or_role, "role", "") or "")
    )
    return ROLE_RANK.get(role, 0)


def is_system_admin(user) -> bool:
    """L3 系统管理员。★ `is_superuser` 也算——Django admin 的逃生口。"""
    if not (user and getattr(user, "is_authenticated", False)):
        return False
    return (
        getattr(user, "role", "") == "PLATFORM_ADMIN"
        or bool(getattr(user, "is_superuser", False))
    )


def is_school_admin(user) -> bool:
    """L2 学校平台账户（含 L3，因为高级别天然可做低级别的事）。"""
    return role_rank(user) >= LEVEL_SCHOOL


def is_teacher(user) -> bool:
    """L1 教师账户（含 L2/L3）。"""
    return role_rank(user) >= LEVEL_TEACHER


def can_create_role(operator, role: str) -> tuple[bool, str]:
    """能否创建/授予某角色。返回 `(是否允许, 拒绝原因)`。

    ★★ 铁律 1：**只能管低一级**（`rank(目标) < rank(操作者)`）。

       原实现用的是 `>`（允许同级），于是机构管理员可以建第二个
       机构管理员，两个平级账号可以互相改密码、互相停用 ——
       实际操作中一旦出现争执，学校层面就没有仲裁者了。

       唯一放行的例外是系统管理员：多个 sysadmin 之间允许同级，
       否则唯一那个 sysadmin 密码丢了就整站失守。

    ★ 铁律 2：学校平台账户**不能**创建任何高于教师的角色。
    """
    if operator is None:
        return True, ""       # 系统任务 / 迁移脚本 / 平台初始化
    target_rank = ROLE_RANK.get(role, 0)
    op_rank = role_rank(operator)

    if target_rank == LEVEL_STUDENT:
        return False, "学生身份行由发布任务时自动创建，不能手工建"
    if target_rank >= LEVEL_SYSTEM:
        # 仅系统管理员可建同级（多个 sysadmin 互为备份）
        if op_rank < LEVEL_SYSTEM:
            return False, "无权创建系统管理员账号"
        return True, ""
    if target_rank >= op_rank:
        return False, (
            f"无权创建「{_role_label(role)}」账号（该角色不低于你自己的等级）"
        )
    return True, ""


def can_manage_target(operator, target) -> tuple[bool, str]:
    """能否操作某个已存在的账号（改资料 / 改角色 / 停用 / 重置密码）。

    ★★ 铁律 3：**不能改自己**。

       一条命令把自己降成教师、或者停用自己，会导致该校
       **再没有人能恢复它**（另一个学校账户也不存在时）。
       改自己的资料（姓名、手机号）走 `PATCH /auth/me/`，不在这条路上。

    ★ 同级不可互管：两个学校平台账户之间不能互相停用/改密码。
      要处理请找系统管理员。
    """
    if operator is None or target is None:
        return True, ""
    if getattr(operator, "pk", None) and operator.pk == getattr(target, "pk", None):
        return False, "不能修改自己的账号（角色、状态），请找上级管理员"
    op_rank = role_rank(operator)
    target_rank = role_rank(target)
    if op_rank >= LEVEL_SYSTEM:
        return True, ""       # 系统管理员可管同级（互为备份）
    if target_rank >= op_rank:
        return False, (
            f"无权操作「{_role_label(getattr(target, 'role', ''))}」账号"
            "（该角色不低于你自己的等级）"
        )
    return True, ""


def can_view_scope(user, scope: str) -> bool:
    """能否使用某个数据作用域。见 `apps.common.scoping.Scope`。"""
    rank = role_rank(user)
    return {
        "SELF": rank >= LEVEL_STUDENT,
        "OWN_TEACHING": rank >= LEVEL_TEACHER,
        "ORG_ALL": rank >= LEVEL_SCHOOL,
        "PLATFORM": rank >= LEVEL_SYSTEM,
    }.get(scope, False)


def _role_label(role: str) -> str:
    from apps.accounts.models import role_label

    return role_label(role)


def has_role_at_least(user, role: str) -> bool:
    """判断用户是否达到某角色等级。★ 供视图内部做细粒度判断。"""
    return role_rank(user) >= ROLE_RANK.get(role, 0)


# ══════════════════════════════════════════════════════════════
#  视图层断言助手 —— 替代散落各处的 `if not request.user.is_org_admin`
# ══════════════════════════════════════════════════════════════


def require_system_admin(request):
    """要求 L3。不满足则抛 `AUTH_FORBIDDEN`。"""
    from apps.common.exceptions import ErrorCode, ZfError

    if not is_system_admin(request.user):
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="仅系统管理员可操作")


def require_school_admin(request):
    """要求 L2 及以上。不满足则抛 `AUTH_FORBIDDEN`。"""
    from apps.common.exceptions import ErrorCode, ZfError

    if not is_school_admin(request.user):
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="仅学校平台账户可操作")


def require_teacher(request):
    """要求 L1 及以上。不满足则抛 `AUTH_FORBIDDEN`。"""
    from apps.common.exceptions import ErrorCode, ZfError

    if not is_teacher(request.user):
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="需要教师及以上权限")


# ══════════════════════════════════════════════════════════════
#  DRF 权限类
# ══════════════════════════════════════════════════════════════


class IsSystemAdmin(BasePermission):
    """仅 L3 系统管理员。★ 等价于旧名 `IsPlatformAdmin`。"""

    message = "仅系统管理员可操作"

    def has_permission(self, request, view) -> bool:
        return is_system_admin(getattr(request, "user", None))


class IsSchoolAdmin(BasePermission):
    """L2 学校平台账户及以上。★ 等价于旧名 `IsOrgAdmin`。"""

    message = "仅学校平台账户可操作"

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return is_school_admin(user)


class IsTeacherAccount(BasePermission):
    """L1 教师账户及以上。

    ★★ 判据是**角色等级**，不是「有没有机构」。

      我最初把它写成「已认证 + 有 org_id」——那样**任何有机构的账号
      都能通过**。一期虽只有教师/管理员三种角色，但这个写法意味着
      将来一加 `STUDENT` 角色，学生就会自动获得出题、改题、审核题的权限，
      而且没有任何地方会报错或提示。

      实测暴露了它：一个 role 传了非法值（不在 choices 内）的账号
      照样能建题。
    """

    message = "需要教师及以上权限"

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if not getattr(user, "is_active", True):
            return False
        return is_teacher(user)


class IsAssistantOrAbove(BasePermission):
    """助教及以上。★ 预留给「只能录分/只能扫描」这类受限岗位。"""

    message = "需要助教及以上权限"

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return role_rank(user) >= 5


class CanManageRole(BasePermission):
    """对象级：能否操作目标账号。

    ★ 用在 `has_object_permission` 上（DRF 只对 detail 路由调用），
      实际业务判断请调 `UserService` 里的同名逻辑，两者共用
      `can_manage_target()`，不会各写一份。
    """

    message = "无权操作该账号"

    def has_object_permission(self, request, view, obj) -> bool:
        ok, reason = can_manage_target(request.user, obj)
        if not ok:
            self.message = reason
        return ok


class IsSameOrg(BasePermission):
    """对象级：只能操作本机构数据。

    ★ 与 `OrgScopeMiddleware` 配合：中间件已把 `request.org_id` 设好，
      这里做对象级兜底校验。
    """

    message = "无权访问其他机构的数据"

    def has_object_permission(self, request, view, obj) -> bool:
        org_id = getattr(request, "org_id", None)
        if org_id is None:
            return False
        return getattr(obj, "org_id", None) == org_id


class ReadOnly(BasePermission):
    """仅允许读。"""

    def has_permission(self, request, view) -> bool:
        return request.method in SAFE_METHODS


class IsOwnerStudent(BasePermission):
    """学生端：只能访问自己的数据。

    用于学生作答、个人练习、个人学情。
    """

    message = "无权访问他人的数据"

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        owner = (
            getattr(obj, "owner_student_id", None)
            or getattr(obj, "student_id", None)
            or getattr(obj, "user_id", None)
        )
        return owner == user.id


# ══════════════════════════════════════════════════════════════
#  旧名兼容别名 —— 现有视图/脚本 import 这两个名字，保留不破
# ══════════════════════════════════════════════════════════════

IsPlatformAdmin = IsSystemAdmin
IsOrgAdmin = IsSchoolAdmin
IsTeacherOrAbove = IsTeacherAccount
