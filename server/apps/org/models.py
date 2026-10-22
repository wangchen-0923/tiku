"""机构、专业、班级、学生名册 · 数据模型。

层级关系::

    org（机构）
     └─ major（专业，职业教育特有；K12 留空）
         └─ classroom（班级）
              └─ classroom_student（名册，答题卡靠学号认人）

★ 本模块的模型都不继承 `BaseModel`：`Org` 是平台级表（无 org_id），
  其余三个的 org 外键语义各有差异，显式声明更清楚。
"""

from __future__ import annotations

from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import GradeBand, MajorLevel
from apps.common.managers import OrgScopedManager, PlatformModel, SoftDeleteModel


# ══════════════════════════════════════════════════════════════
#  3.1 org — 机构
# ══════════════════════════════════════════════════════════════


class OrgType(models.TextChoices):
    """机构类型。

    ★ `PLATFORM`（v2.0 新增）不是「一种机构」，而是**系统层的锚点行**：
      公共题库、公共知识库、平台级提示词模板都挂在它下面，
      因为 `question.org_id` 是 `NOT NULL`，系统级资产必须有一个 org 可指。

    ★★ 三条使用纪律：
      1. **不能把平台管理员账号的 `org_id` 改到这一行**——
         改了之后他立刻看不到任何学校的业务数据（`for_org` 只按 org 过滤）。
      2. 平台管理员的**跨机构权限走 `role=PLATFORM_ADMIN` 判定**，
         不依赖 `org_id` 归属。
      3. 这一行**不参与**任何业务统计：报表、容量配额、机构列表
         默认都要 `exclude(type=PLATFORM)`，否则会出现「第 2 所学校」。
    """

    PLATFORM = "PLATFORM", "平台本体"
    SCHOOL = "SCHOOL", "学校"
    TRAINING = "TRAINING", "培训机构"


#: 平台机构行的固定名称。
#:
#: ★ 迁移与 `OrgService.ensure_platform_org()` 都以 `type=PLATFORM` 为准来查找，
#:   名称只用于展示；**不要**用名称做 get_or_create 的键（改个名字就多一行）。
PLATFORM_ORG_NAME = "知否平台"
PLATFORM_ORG_SHORT_NAME = "知否"


class OrgStatus(models.TextChoices):
    """机构状态。"""

    ACTIVE = "ACTIVE", "正常"
    SUSPENDED = "SUSPENDED", "已停用"


class Org(PlatformModel):
    """机构（多租户的租户）。"""

    name = models.CharField(_("全称"), max_length=200)
    short_name = models.CharField(
        _("简称"), max_length=50, blank=True, default="",
        help_text=_("用于试卷页眉"),
    )
    logo_url = models.URLField(_("Logo"), max_length=500, blank=True, default="")
    type = models.CharField(
        _("类型"), max_length=20, choices=OrgType.choices, default=OrgType.SCHOOL
    )

    ai_config = models.JSONField(_("AI 配置"), default=dict, blank=True)
    grading_config = models.JSONField(_("批改配置"), default=dict, blank=True)
    quota_config = models.JSONField(_("容量配额"), default=dict, blank=True)

    sheet_default_style = models.ForeignKey(
        "sheet.SheetStyle",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name=_("默认答题卡样式"),
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=OrgStatus.choices, default=OrgStatus.ACTIVE
    )

    objects = models.Manager()

    class Meta:
        db_table = "org"
        verbose_name = _("机构")
        verbose_name_plural = _("机构")

    def __str__(self) -> str:
        return self.short_name or self.name

    # ── 配置读取（★ 机构级覆盖，缺省回落到平台默认）──
    def get_ai_config(self, key: str, default=None):
        return (self.ai_config or {}).get(key, default)

    def get_grading_config(self, key: str, default=None):
        return (self.grading_config or {}).get(key, default)

    def get_student_no_digits(self) -> int:
        """★ 该机构的默认学号位数。答题卡与名册都以它为准。"""
        return int(self.get_grading_config("student_no_default_digits", 8))

    def get_omr_thresholds(self, channel: str) -> dict:
        """取某采集通道的 OMR 阈值。

        ★ 双通道阈值必须独立（见 08 文档）——手机拍摄与扫描仪的光照条件不同。
        """
        omr = self.get_grading_config("omr", {}) or {}
        return omr.get(channel, {}) or {}


# ══════════════════════════════════════════════════════════════
#  3.4 major — 专业
# ══════════════════════════════════════════════════════════════

#: 编码校验：字母、数字、下划线、短横线、点、中文；1–80 位（README 五之二）
CODE_VALIDATOR = RegexValidator(
    regex=r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]{0,79}$",
    message=_("编码只允许字母、数字、下划线、短横线、点与中文，且不能以点或短横线开头"),
)


class Major(SoftDeleteModel):
    """专业。

    ★ **K12 场景不需要建专业**，`classroom.major_id` 留空即可。
      系统以 `major_id` 是否为空判断是否启用专业维度——同一套代码支持两种形态。
    """

    org = models.ForeignKey(
        Org, on_delete=models.PROTECT, related_name="majors", verbose_name=_("机构")
    )
    name = models.CharField(_("专业名称"), max_length=100)
    code = models.CharField(
        _("专业代码"), max_length=80, validators=[CODE_VALIDATOR],
        help_text=_("可用教育部专业代码，如 660101"),
    )
    short_name = models.CharField(_("简称"), max_length=50, blank=True, default="")
    level = models.CharField(
        _("层次"), max_length=20, choices=MajorLevel.choices,
        default=MajorLevel.SECONDARY,
    )
    sort_order = models.IntegerField(_("排序"), default=0)
    status = models.CharField(_("状态"), max_length=20, default="ACTIVE")

    # ★ created_at / updated_at / deleted_at 由 SoftDeleteModel 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   且会让 hard_delete() 等基类方法的行为与字段定义脱节。
    objects = OrgScopedManager()

    class Meta:
        db_table = "major"
        verbose_name = _("专业")
        verbose_name_plural = _("专业")
        constraints = [
            models.UniqueConstraint(
                "org", "code",
                name="uq_major_org_code",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "sort_order"], name="idx_major_org",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.short_name or self.name


# ══════════════════════════════════════════════════════════════
#  3.3 classroom — 班级
# ══════════════════════════════════════════════════════════════


class ClassroomStatus(models.TextChoices):
    """班级状态。"""

    ACTIVE = "ACTIVE", "在读"
    GRADUATED = "GRADUATED", "已毕业"
    ARCHIVED = "ARCHIVED", "归档"


class Classroom(SoftDeleteModel):
    """班级。

    ★★ **归属教师是班级的主人，不是学校**（2026-10-21 定稿）。

      业务规则：学校不统一建班，只授权教师账号；班级由教师自行创建，
      教师只能看到自己创建的班。

      所以 `owner` 不是「附加的审计字段」，而是**数据隔离的判据**：
      教师端所有班级查询都以它过滤。学校平台账户（`ORG_ALL`）仍然
      能看到本校全部班级 —— 那是管理视角（离职移交、纠错），
      不代表学校的日常流程要建班。
    """

    org = models.ForeignKey(
        Org, on_delete=models.PROTECT, related_name="classrooms", verbose_name=_("机构")
    )
    owner = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="owned_classrooms",
        verbose_name=_("归属教师"),
        help_text=_("创建者。教师只能看到自己创建的班级；学校账户可见本校全部"),
    )
    name = models.CharField(_("班级名称"), max_length=100)
    grade = models.CharField(_("年级"), max_length=20)
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )
    major = models.ForeignKey(
        Major,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="classrooms",
        verbose_name=_("所属专业"),
        help_text=_("K12 场景留空"),
    )
    head_teacher = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="headed_classrooms",
        verbose_name=_("班主任"),
    )
    academic_year = models.CharField(_("学年"), max_length=20)
    status = models.CharField(
        _("状态"), max_length=20, choices=ClassroomStatus.choices,
        default=ClassroomStatus.ACTIVE,
    )

    # ★ created_at / updated_at / deleted_at 由 SoftDeleteModel 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   且会让 hard_delete() 等基类方法的行为与字段定义脱节。
    objects = OrgScopedManager()

    class Meta:
        db_table = "classroom"
        verbose_name = _("班级")
        verbose_name_plural = _("班级")
        constraints = [
            # ★ 班级归教师个人后，唯一键必须带上 `owner`：
            #   两位老师各自建一个「机械1班」是完全正常的，
            #   若沿用 (org, name, year) 全校唯一，第二个老师会被拒绝，
            #   而且报错信息会说「该班已存在」—— 他看不到那个班，无从理解。
            models.UniqueConstraint(
                "org", "owner", "name", "academic_year",
                name="uq_classroom_owner_name_year",
                condition=models.Q(deleted_at__isnull=True,
                                   owner__isnull=False),
            ),
            # 历史遗留的「学校公共班」（owner 为空）仍按全校唯一兜底。
            # ★ PG 的唯一约束把 NULL 视为互不相等，不加这条就会出现
            #   两个同名同年的无主班，且谁都查不出来。
            models.UniqueConstraint(
                "org", "name", "academic_year",
                name="uq_classroom_org_name_year_no_owner",
                condition=models.Q(deleted_at__isnull=True,
                                   owner__isnull=True),
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "grade_band"], name="idx_classroom_org_grade",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(
                fields=["major"], name="idx_classroom_major",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]
        ordering = ["academic_year", "name"]

    def __str__(self) -> str:
        return f"{self.name}（{self.academic_year}）"

    @property
    def student_count(self) -> int:
        return self.students.filter(status="ACTIVE", deleted_at__isnull=True).count()


# ══════════════════════════════════════════════════════════════
#  3.5 classroom_student — 名册
# ══════════════════════════════════════════════════════════════


class ClassroomStudentStatus(models.TextChoices):
    """名册条目状态。"""

    ACTIVE = "ACTIVE", "在读"
    LEFT = "LEFT", "已转出"


class ClassroomStudent(SoftDeleteModel):
    """班级学生名册。

    ★ **这是免登录认人与答题卡学号匹配的基础表**。
      学号在班内唯一——重复会导致答题卡成绩记错人。
    """

    org = models.ForeignKey(
        Org, on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    classroom = models.ForeignKey(
        Classroom, on_delete=models.CASCADE, related_name="students",
        verbose_name=_("班级"),
    )
    student = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="classroom_entries",
        verbose_name=_("学生账号"),
        help_text=_("无账号学生也在名册，此字段可为空"),
    )
    student_no = models.CharField(
        _("学号"), max_length=50, help_text=_("与答题卡学号区对应")
    )
    name = models.CharField(_("姓名"), max_length=100)
    display_name = models.CharField(
        _("展示名"), max_length=100, blank=True, default="",
        help_text=_("处理重名，如「张三(大)」"),
    )
    seat_no = models.CharField(_("座位号"), max_length=20, blank=True, default="")

    join_at = models.DateTimeField(_("加入时间"), default=timezone.now)
    leave_at = models.DateTimeField(_("离开时间"), null=True, blank=True)
    status = models.CharField(
        _("状态"), max_length=20, choices=ClassroomStudentStatus.choices,
        default=ClassroomStudentStatus.ACTIVE,
    )

    # ★ created_at / updated_at / deleted_at 由 SoftDeleteModel 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   且会让 hard_delete() 等基类方法的行为与字段定义脱节。
    objects = OrgScopedManager()

    class Meta:
        db_table = "classroom_student"
        verbose_name = _("班级学生")
        verbose_name_plural = _("班级学生")
        constraints = [
            # ★ 学号同班内唯一：答题卡靠学号认人，重复会记错成绩
            models.UniqueConstraint(
                "classroom", "student_no",
                name="uq_classroom_student_no",
                condition=models.Q(deleted_at__isnull=True),
            ),
            # 一个学生在同一班只能有一条 ACTIVE 记录
            models.UniqueConstraint(
                "classroom", "student",
                name="uq_classroom_student_active",
                condition=models.Q(
                    status="ACTIVE", deleted_at__isnull=True, student__isnull=False
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["classroom", "name"], name="idx_classroom_student_name"),
            models.Index(fields=["org", "student_no"], name="idx_cs_org_no"),
        ]

    def __str__(self) -> str:
        return f"{self.effective_name}({self.student_no})"

    @property
    def effective_name(self) -> str:
        """展示用姓名：优先 display_name。"""
        return self.display_name or self.name
