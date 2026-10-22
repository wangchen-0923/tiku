"""用户与认证 · 数据模型。

设计要点：
  - 用户属于**一个机构**（`org_id`），机构间数据隔离靠它；
  - 平台管理员 `org_id` 为空（跨机构）；
  - 用户名唯一性范围是**机构内**，不同学校可以有同名账号。

★ 本模型**不继承** `BaseModel`：用户表要先于机构上下文可用（登录时还没有机构），
  且 `org_id` 允许为空。
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.common.managers import SoftDeleteModel



class Role(models.TextChoices):
    """角色 · 三级账户体系（2026-10-21 定稿）。

    ★★ 三级账户与 `role` 值的对应关系::

        L3  系统管理员      PLATFORM_ADMIN   org 必须为空
        L2  学校平台账户    ORG_ADMIN        org 必填，每校 1 主 + 至多 2 备
        L1  教师账户        TEACHER          org 必填
        ─   学生身份行      STUDENT          **不是账户**，等级恒 0

    ★★ 为什么 `role` 值不改名（仍是 `PLATFORM_ADMIN` / `ORG_ADMIN`）：

      后端 18 处视图判断、9 个回归脚本、前端 3 个文件都硬编码了
      这两个字符串。改 DB 值要连带改数据、代码、脚本、前端四层，
      收益只是「名字好听」。三级语义已完全对应，所以
      **值不动，只统一显示名**（见 `role_label()`），
      新代码请用模块级别名 `SYS_ADMIN` / `SCHOOL_ADMIN`。

    ★ `STUDENT` 是**任务参与者身份行**（2026-10-16 新增）：

      每个名册学生（学号）一个身份行，作答/评分/成绩/学情全部挂在它上面。
      这样「学生没绑账号也能用学号 + 姓名线上作答」不需要给五张业务表
      换身份键 —— 业务表仍然按 `student_id` 取数，只是这一行的含义
      从「已登录的账号」变成「这个人」。

    ★ 它与「教师账号」的差别**只有一点**：一期**没有登录凭据**
      （`set_unusable_password()`），所以登录一定失败。
      日后「绑定账号」就是把凭据补给这一行（或与已有账号合并），
      历史数据天然在它名下，不需要迁移。
    """

    STUDENT = "STUDENT", "学生"
    PLATFORM_ADMIN = "PLATFORM_ADMIN", "平台管理员"
    ORG_ADMIN = "ORG_ADMIN", "机构管理员"
    TEACHER = "TEACHER", "教师"


# ★★ 不要在 `Role` 类体里写名为 `label` 或 `value` 的方法。
#
#   `Choices` 基类用 `DynamicClassAttribute` 实现成员上的 `.label`，
#   类体里一旦定义同名属性，它会**遮蔽**掉所有成员的 label ——
#   `Role.choices` 会变成
#   `[('STUDENT', <function Role.label>), ...]`，
#   于是 `makemigrations` 每次都认为 `User.role` 的 choices 变了，
#   无限生成 `00xx_alter_user_role.py`。我踩过一次，就在这里记下。
#   所以取显示名的函数放在**模块级**：`role_label()`。

#: 三级账户显示名。★ 与数据库值解耦 —— 值不变，叫法变。
_ROLE_LABELS: dict[str, str] = {
    "PLATFORM_ADMIN": "系统管理员",
    "ORG_ADMIN": "学校平台账户",
    "TEACHER": "教师账户",
    "STUDENT": "学生",
}


def role_label(role: str | None) -> str:
    """取角色的**三级显示名**：系统管理员 / 学校平台账户 / 教师账户。

    ★ 前端菜单、审计日志、错误提示一律用这个，
      不要在别处再写一份 `{"ORG_ADMIN": "机构管理员"}` 的映射表 ——
      那样改名时必然漏掉一两处，界面上就会同时出现
      「机构管理员」和「学校平台账户」两种叫法。
    """
    key = str(role or "")
    if key in _ROLE_LABELS:
        return _ROLE_LABELS[key]
    try:
        return str(Role(key).label)
    except (ValueError, KeyError):
        return key or "未知角色"


def role_level(role: str | None) -> int:
    """取角色等级（0/10/20/30）。数值来源是 `ROLE_RANK`，此处只是转发。"""
    from apps.common.permissions import role_rank

    return role_rank(str(role or ""))


#: 三级账户别名（★ 新代码请用这三个，语义比旧名清楚）。
#:
#: ★ 定义在**模块级**而非 `Role` 类体内：类体内写同值常量会被 `enum`
#:   当成别名成员，`ChoicesMeta` 会给每个别名再 append 一次 label，
#:   `Role.choices` 从 4 项变 7 项，Django 立刻生成一条无意义的迁移。
SYS_ADMIN = Role.PLATFORM_ADMIN
SCHOOL_ADMIN = Role.ORG_ADMIN
TEACHER_ACCOUNT = Role.TEACHER


class UserManager(BaseUserManager):
    """用户 Manager。★ 不用 `OrgScopedManager`：用户表要先于机构之外可用。"""

    use_in_migrations = True

    def _create_user(self, username: str, password: str | None, **extra):
        if not username:
            raise ValueError("用户名不能为空")
        username = self.model.normalize_username(username)
        user = self.model(username=username, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, username: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        extra.setdefault("role", Role.TEACHER)
        return self._create_user(username, password, **extra)

    def create_superuser(self, username: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.PLATFORM_ADMIN)
        extra.setdefault("org", None)
        if extra.get("is_staff") is not True:
            raise ValueError("超级用户必须 is_staff=True")
        if extra.get("is_superuser") is not True:
            raise ValueError("超级用户必须 is_superuser=True")
        return self._create_user(username, password, **extra)

    def for_org(self, org_id: int):
        """★ 机构内取数入口。"""
        return self.get_queryset().filter(org_id=org_id, deleted_at__isnull=True)


class User(AbstractBaseUser, PermissionsMixin, SoftDeleteModel):
    """用户。

    ★ **用户名全局唯一**（`username` 字段 `unique=True`）。
      原设计想做「机构内唯一」（不同学校可同名），但 Django 的 `auth.E003`
      系统检查强制要求 `USERNAME_FIELD` 唯一，而该检查无法通过
      `Meta.constraints` 满足。取舍如下：

      - 保留全局唯一：实现简单、登录无需先选机构；
      - 代价：跨机构不能有同名账号。对职业学校场景可接受
        （实际账号多用工号/手机号，本身就不重名）。

      若将来确需机构内同名，应改为**用户名 + 机构**两段登录
      （`org_code + username`），而不是放宽唯一性——否则登录时无法确定
      查哪个机构。
    """

    username = models.CharField(_("用户名"), max_length=150, unique=True)
    real_name = models.CharField(_("真实姓名"), max_length=100, blank=True, default="")
    phone = models.CharField(_("手机号"), max_length=20, blank=True, default="")

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        verbose_name=_("所属机构"),
        help_text=_("平台管理员为空"),
    )
    role = models.CharField(
        _("角色"), max_length=20, choices=Role.choices, default=Role.TEACHER
    )

    is_active = models.BooleanField(_("是否启用"), default=True)
    is_staff = models.BooleanField(_("可登录后台"), default=False)

    last_login_ip = models.GenericIPAddressField(_("最后登录 IP"), null=True, blank=True)

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "user"
        verbose_name = _("用户")
        verbose_name_plural = _("用户")
        constraints = [
            # ★ 用户名已由字段级 unique=True 保证（见类 docstring 的取舍说明）。
            #   这里只补「软删除后仍占用」的问题：已删除账号的用户名不释放，
            #   避免同名新账号继承旧账号的历史数据关联。
            models.UniqueConstraint(
                fields=["org", "username"],
                name="uq_user_org_username",
                condition=models.Q(deleted_at__isnull=True),
            ),
            # ★★ 三级账户铁律：系统管理员属于**平台**，不属于任何一所学校。
            #
            #   这条约束是 2026-10-21 加的核心价值所在：它能防住
            #   「一个账号同时是平台方和校方」——那种账号既能看到全校
            #   每个学生的成绩，又能跨校操作，所有权限规则都被它绕过。
            #
            #   ★ 加约束之前必须先修数据（迁移 0005 的第一个 RunPython），
            #     否则迁移会直接报 violates check constraint。
            models.CheckConstraint(
                condition=(
                    ~models.Q(role="PLATFORM_ADMIN")
                    | models.Q(org__isnull=True)
                ),
                name="ck_platform_admin_no_org",
                violation_error_message="系统管理员不属于任何学校（org 必须为空）",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "role"], name="idx_user_org_role"),
            models.Index(fields=["phone"], name="idx_user_phone"),
        ]

    def __str__(self) -> str:
        return self.real_name or self.username

    # ── 便捷属性 ──
    @property
    def is_student(self) -> bool:
        """是否学生身份行。"""
        return self.role == Role.STUDENT

    @property
    def can_login(self) -> bool:
        """能否登录。

        ★★ 判据是**「有没有一份真实可用的密码哈希」**，不是角色：

          - 学生身份行由发布任务时自动建，`set_unusable_password()`
            → 哈希以 `!` 开头，登录必定失败；
          - ★ 也要排除**空哈希**：测试/脚本常用
            `User.objects.create(...)`（不带密码），此时 `password=''`，
            而 Django 的 `has_usable_password()` 对空串**返回 True**
            （它只判 `!` 前缀）—— 用它会把「没有凭据」显示成「已绑定」。
          - 日后「绑定账号」的动作就是**给这一行设密码/绑手机**
            → 同一判据自动翻转，不需要改表、不需要迁移历史数据。

        ★ 界面上的「已绑定 / 未绑定」必须用这个属性，
          不要用 `ClassroomStudent.student IS NULL` ——
          身份行建好之后那个字段永远非空，用它判断会显示成「全班都已绑定」。
        """
        return bool(self.password) and self.has_usable_password() \
            and not self.is_deleted

    @property
    def display(self) -> str:
        """列表/日志里的展示名：优先姓名，其次用户名。"""
        return self.real_name or self.username

    # ── 三级账户判定（★ 新代码用这两个）──
    @property
    def level(self) -> int:
        """角色等级：0 学生 / 10 教师 / 20 学校平台账户 / 30 系统管理员。"""
        from apps.common.permissions import role_rank

        return role_rank(self)

    @property
    def role_label(self) -> str:
        """三级显示名：系统管理员 / 学校平台账户 / 教师账户。"""
        return role_label(self.role)

    @property
    def is_system_admin(self) -> bool:
        """L3 系统管理员（`org` 应为空，跨校）。"""
        return self.role == Role.PLATFORM_ADMIN or self.is_superuser

    @property
    def is_school_admin(self) -> bool:
        """L2 学校平台账户及以上（含 L3）。"""
        from apps.common.permissions import LEVEL_SCHOOL

        return self.level >= LEVEL_SCHOOL

    # ── 旧属性名（保留兼容，内部委托到新名字）──
    @property
    def is_platform_admin(self) -> bool:
        return self.is_system_admin

    @property
    def is_org_admin(self) -> bool:
        return self.is_school_admin

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    # ── 软删除 ──
    def delete(self, using=None, keep_parents=False):  # type: ignore[override]
        self.deleted_at = timezone.now()
        self.is_active = False
        self.save(update_fields=["deleted_at", "is_active", "updated_at"])

    def hard_delete(self, using=None, keep_parents=False):
        """物理删除。★ 仅测试与清理任务可用。

        ★★ 这里**不能**写 `super().delete()`。

          `User` 的基类是 `(AbstractBaseUser, PermissionsMixin,
          SoftDeleteModel)`，MRO 里 `delete` 的最近实现是
          **`SoftDeleteModel.delete`（软删）** —— 于是
          `super().delete()` 只把 `deleted_at` 写了，行还在库里，
          「hard_delete」名不副实。

          我在回归脚本里就被它坑过一次：一个软删过的
          `testql_s0` 残留，下一轮 `create(username="testql_s0")`
          直接撞 `UNIQUE constraint failed: user.username`，
          而报错点在**建数据**的地方，看起来跟清理逻辑毫不相干。

          显式调 `Model.delete` 才能落到真正的 DELETE 语句上。
        """
        from django.db.models import Model

        return Model.delete(self, using=using, keep_parents=keep_parents)

    @transaction.atomic
    def set_password_and_save(self, raw_password: str) -> None:
        """设置密码并保存。★ 单独提供，避免误把明文写进 `password` 字段。"""
        self.set_password(raw_password)
        self.save(update_fields=["password", "updated_at"])
