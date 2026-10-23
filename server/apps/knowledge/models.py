"""知识体系 · 数据模型。

层级（职业教育形态）::

    专业 major
     └─ 课程 course           ← 原 subject 字段升级为独立实体
         └─ 章 chapter        ← 「篇」是 chapter 的属性，不独立成层
             └─ 任务 task      ← ★ 组卷的最小粒度；题库只标到这一级
                 └─ 知识点 knowledge_node
                     └─ 知识库条目 knowledge_entry（AI 生题的依据来源）

★ 两个关键约定：
  1. **题库章节号 `X.Y` 的 Y 是任务号，不是节号**。导入必须按
     `(chapter.seq=X, task.seq=Y)` 映射校验，映射不到就进异常清单。
  2. **`code` 是内部稳定标识**（改名不变、可跨机构对齐），
     `name` 是展示名（与纸质教材一致）。两者都存，不允许只存其一。
"""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    KB_MIN_ENTRIES,
    KnowledgeEntryStatus,
    KnowledgeEntryType,
    KnowledgeSourceType,
    MajorLevel,
    QuestionStyle,
    GradeBand,
)
from apps.common.managers import OrgScopedManager, SoftDeleteModel
from apps.org.models import CODE_VALIDATOR


# ══════════════════════════════════════════════════════════════
#  4.1 course — 课程
# ══════════════════════════════════════════════════════════════


class CourseStatus(models.TextChoices):
    """课程状态。"""

    ACTIVE = "ACTIVE", "启用"
    ARCHIVED = "ARCHIVED", "归档"


class Course(SoftDeleteModel):
    """课程。

    ★ `org` 为空表示**平台模板课程**（可被机构深拷贝）。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="courses",
        verbose_name=_("机构"),
        help_text=_("为空表示平台模板"),
    )
    major = models.ForeignKey(
        "org.Major",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="courses",
        verbose_name=_("所属专业"),
        help_text=_("K12 场景留空"),
    )
    name = models.CharField(_("课程名"), max_length=200)
    code = models.CharField(
        _("课程编码"), max_length=80, validators=[CODE_VALIDATOR],
        help_text=_("如 JXJC（机械基础）。全部知识点编码以此开头，确定后不要改"),
    )
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )
    level = models.CharField(
        _("层次"), max_length=20, choices=MajorLevel.choices,
        default=MajorLevel.SECONDARY,
    )

    textbook = models.JSONField(
        _("教材信息"), default=dict, blank=True,
        help_text=_("{name, publisher, edition, isbn}"),
    )
    total_hours = models.IntegerField(_("总课时"), null=True, blank=True)

    style_config = models.JSONField(
        _("命题风格"), default=dict, blank=True,
        help_text=_("含 context_required 情境规则；见 13 文档"),
    )

    sort_order = models.IntegerField(_("排序"), default=0)
    is_template = models.BooleanField(_("平台模板"), default=False)
    status = models.CharField(
        _("状态"), max_length=20, choices=CourseStatus.choices,
        default=CourseStatus.ACTIVE,
    )

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "course"
        verbose_name = _("课程")
        verbose_name_plural = _("课程")
        constraints = [
            models.UniqueConstraint(
                models.functions.Coalesce("org_id", models.Value(0)),
                "code",
                name="uq_course_org_code",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]
        indexes = [
            models.Index(
                fields=["major", "sort_order"], name="idx_course_major",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.name

    # ── 情境规则（★ 职教硬规则）──
    @property
    def context_required(self) -> bool:
        """命题是否必须嵌入真实岗位/企业情境。

        ★ 机械基础的题库**每一题都有真实工业情境**（宝武集团机床床身、
          比亚迪电池外壳冲压、减速器维护…）。AI 生题与改写**必须保留情境**，
          不得去情境化，否则产出与存量题库风格不一致。
        """
        return bool((self.style_config or {}).get("context_required", False))

    @property
    def question_style(self) -> str:
        return (self.style_config or {}).get(
            "question_style", QuestionStyle.TASK_DRIVEN
        )

    # ── 编码生成 ──
    def chapter_code(self, seq: int) -> str:
        return f"{self.code}-C{seq:02d}"

    def task_code(self, chapter_seq: int, task_seq: int) -> str:
        return f"{self.code}-C{chapter_seq:02d}-T{task_seq:02d}"

    def node_code(self, chapter_seq: int, task_seq: int, k_seq: int) -> str:
        return f"{self.code}-C{chapter_seq:02d}-T{task_seq:02d}-K{k_seq:02d}"


# ══════════════════════════════════════════════════════════════
#  4.5 chapter — 章
# ══════════════════════════════════════════════════════════════


class Chapter(SoftDeleteModel):
    """章。

    ★ **「篇」不独立成层**，而是 `part_seq` + `part_name` 两个属性列。
      理由：篇只起分组显示作用（共 3 篇），建成独立实体会让树深一层、
      增加 join 与维护成本，而业务上没有「对篇整体操作」的需求。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="chapters",
        verbose_name=_("机构"),
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="chapters",
        verbose_name=_("课程"),
    )
    subject = models.CharField(
        _("课程编码"), max_length=20,
        help_text=_("冗余存 course.code，兼容既有查询"),
    )
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )

    seq = models.SmallIntegerField(
        _("章号"), help_text=_("对应题库章节号 X.Y 的 X")
    )
    name = models.CharField(_("章名"), max_length=200)

    # ── 「篇」作为属性 ──
    part_seq = models.SmallIntegerField(_("篇号"), null=True, blank=True)
    part_name = models.CharField(_("篇名"), max_length=100, blank=True, default="")

    code = models.CharField(
        _("章编码"), max_length=80, blank=True, default="",
        validators=[CODE_VALIDATOR],
    )
    path = models.CharField(_("物化路径"), max_length=500, blank=True, default="")

    sort_order = models.IntegerField(_("排序"), default=0)
    textbook = models.CharField(_("教材名"), max_length=100, blank=True, default="")
    volume = models.CharField(_("册次"), max_length=50, blank=True, default="")
    is_template = models.BooleanField(_("平台模板"), default=False)
    status = models.CharField(_("状态"), max_length=20, default="ACTIVE")

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "chapter"
        verbose_name = _("章")
        verbose_name_plural = _("章")
        constraints = [
            models.UniqueConstraint(
                "course", "seq", name="uq_chapter_course_seq",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.UniqueConstraint(
                models.functions.Coalesce("org_id", models.Value(0)),
                "code",
                name="uq_chapter_org_code",
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(code=""),
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "course", "part_seq", "sort_order"],
                name="idx_chapter_tree",
            ),
        ]
        ordering = ["course", "seq"]

    def __str__(self) -> str:
        return self.name


# ══════════════════════════════════════════════════════════════
#  4.3 task — 任务
# ══════════════════════════════════════════════════════════════


class Task(SoftDeleteModel):
    """任务。

    ★ **组卷的最小粒度**，也是题目标签的最细粒度（存量题库只标到这一级）。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="tasks",
        verbose_name=_("机构"),
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="tasks", verbose_name=_("课程")
    )
    chapter = models.ForeignKey(
        Chapter, on_delete=models.CASCADE, related_name="tasks", verbose_name=_("所属章")
    )
    seq = models.SmallIntegerField(
        _("任务号"), help_text=_("对应题库章节号 X.Y 的 Y")
    )
    name = models.CharField(_("任务名"), max_length=200)
    code = models.CharField(
        _("任务编码"), max_length=80, validators=[CODE_VALIDATOR]
    )
    path = models.CharField(_("物化路径"), max_length=500, blank=True, default="")

    objectives = models.JSONField(
        _("教学目标"), default=list, blank=True,
        help_text=_('如 ["了解…", "能…"]'),
    )
    is_practical = models.BooleanField(
        _("是否实训任务"), default=False, help_text=_("职教特有")
    )
    sort_order = models.IntegerField(_("排序"), default=0)
    status = models.CharField(_("状态"), max_length=20, default="ACTIVE")

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "task"
        verbose_name = _("任务")
        verbose_name_plural = _("任务")
        constraints = [
            # ★ 唯一键是 **(chapter, seq)**，不是 (course, seq)。
            #
            #   实测数据：`task.seq`（即题库章节号 `X.Y` 的 Y）**在每章内从 1 重排**——
            #   第1章有 T01–T07，第5章也有 T01–T06。若按 `(course, seq)` 约束，
            #   各章同序号任务会互相覆盖（实测把 23 个任务压成 6 个），
            #   且与「Y = 章内任务号」这个题库约定直接冲突。
            #
            #   本条与 02 文档 DDL 4.3 的写法不同，以**实际数据与用途为准**，
            #   文档已同步更正。
            models.UniqueConstraint(
                "chapter", "seq", name="uq_task_chapter_seq",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.UniqueConstraint(
                models.functions.Coalesce("org_id", models.Value(0)),
                "code",
                name="uq_task_org_code",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=["chapter", "seq"], name="idx_task_chapter"),
        ]
        ordering = ["chapter", "seq"]

    def __str__(self) -> str:
        return self.name


# ══════════════════════════════════════════════════════════════
#  4.2 knowledge_node — 知识点
# ══════════════════════════════════════════════════════════════


class KnowledgeNode(SoftDeleteModel):
    """知识点节点。

    ★ `parent_id` **废弃**：原设计用两级父子表达「章 → 节」，
      实际数据里「节」的角色由 `task` 承担。层级改由 `task` 外键表达。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="knowledge_nodes",
        verbose_name=_("机构"),
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="knowledge_nodes",
        null=True, blank=True, verbose_name=_("课程"),
    )
    subject = models.CharField(
        _("课程编码"), max_length=20, blank=True, default="",
        help_text=_("冗余存 course.code，兼容既有查询"),
    )
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="knowledge_nodes",
        verbose_name=_("所属任务"),
    )

    name = models.CharField(_("知识点名"), max_length=200)
    code = models.CharField(
        _("知识点编码"), max_length=80, blank=True, default="",
        validators=[CODE_VALIDATOR],
        help_text=_("分层生成，如 JXJC-C01-T01-K01"),
    )
    path = models.CharField(_("物化路径"), max_length=500, blank=True, default="")
    level = models.SmallIntegerField(_("层级"), default=1)
    sort_order = models.IntegerField(_("排序"), default=0)

    exam_weight = models.DecimalField(
        _("考纲权重"), max_digits=3, decimal_places=2, default=1.0,
        # ★ `9.99` 要写成 `Decimal`，不能是 float。
        #   DRF 会把模型的 `MaxValueValidator` 搬成序列化器的 `max_value`，
        #   而 `DecimalField` 拿到 float 会打警告：
        #     "max_value should be an integer or Decimal instance"
        #   （实测 2026-10-16：每次加载 `KnowledgeNodeSerializer` 都刷一条）。
        validators=[MinValueValidator(0), MaxValueValidator(Decimal("9.99"))],
        help_text=_("学情组卷加权用；建议教研按课时比例填"),
    )
    seq_in_task = models.SmallIntegerField(
        _("教材原始序号"), null=True, blank=True,
        help_text=_("如「1.」，用于教师对照纸质书"),
    )

    is_template = models.BooleanField(_("平台模板"), default=False)
    template_source_id = models.BigIntegerField(_("模板来源"), null=True, blank=True)
    status = models.CharField(_("状态"), max_length=20, default="ACTIVE")
    #: ★ 21 文档任务五：「节点 id 稳定，不删除；废弃用 `deprecated_at`」。
    #:   `status=ARCHIVED` 是「已并入别处」，`deprecated_at` 是「这个考点
    #:   课标已删」——两者语义不同：前者还能查历史，后者不再参与组卷。
    deprecated_at = models.DateTimeField(
        _("废弃时间"), null=True, blank=True,
        help_text=_("非空表示该考点已废弃：不参与组卷与覆盖统计，但保留历史引用"),
    )

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_node"
        verbose_name = _("知识点")
        verbose_name_plural = _("知识点")
        constraints = [
            models.UniqueConstraint(
                models.functions.Coalesce("org_id", models.Value(0)),
                "code",
                name="uq_knowledge_node_org_code",
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(code=""),
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "course", "sort_order"], name="idx_knowledge_node_tree"
            ),
            models.Index(fields=["task", "sort_order"], name="idx_knowledge_node_task"),
            models.Index(fields=["path"], name="idx_knowledge_node_path"),
        ]
        ordering = ["course", "task", "sort_order"]

    def __str__(self) -> str:
        return self.name

    # ── 知识库门禁 ──
    def active_entry_count(self) -> int:
        """该知识点下 ACTIVE 的知识库条目数。★ L1 门禁的判据之一。

        ★★ `entry__deleted_at__isnull=True` **不可省**：
          `knowledge_entry_node` 是纯关联表（没有 `deleted_at`），
          `KnowledgeEntryNode.objects` 因此**不会**排除已软删的条目；
          而 `entry__status=ACTIVE` 只是 join 上的过滤条件，
          Django 不会顺带套用 `KnowledgeEntry.objects` 的默认管理器。
          少了这一句，软删掉的条目会一直算作「有依据」——
          门禁说够、检索却拿不到，AI 会以「无依据」失败而不是
          `KB_INSUFFICIENT`，排查时完全看不出原因。
        """
        return KnowledgeEntryNode.objects.filter(
            node=self,
            entry__status=KnowledgeEntryStatus.ACTIVE,
            entry__deleted_at__isnull=True,
        ).count()

    @property
    def is_kb_sufficient(self) -> bool:
        """★ AI 生题门禁：知识库依据是否充足。

        不足时 AI 必须**拒绝生成**（`AI.KB_INSUFFICIENT`），
        不允许凭空编造。
        """
        return self.active_entry_count() >= KB_MIN_ENTRIES


# ══════════════════════════════════════════════════════════════
#  4.9 knowledge_entry — 知识库条目
# ══════════════════════════════════════════════════════════════


class KnowledgeEntry(SoftDeleteModel):
    """知识库条目。

    ★ **AI 生题的依据来源**。召回为空必须拒绝生成。
      实测初稿：110 条（来自知识点清单「核心内容」列，类型 CONCEPT）。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="knowledge_entries",
        verbose_name=_("机构"),
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="knowledge_entries",
        null=True, blank=True, verbose_name=_("课程"),
    )
    chapter = models.ForeignKey(
        Chapter, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="entries", verbose_name=_("所属章"),
    )

    title = models.CharField(_("标题"), max_length=300, blank=True, default="")
    content = models.TextField(_("内容"))
    entry_type = models.CharField(
        _("条目类型"), max_length=20, choices=KnowledgeEntryType.choices,
        default=KnowledgeEntryType.CONCEPT,
    )
    source_type = models.CharField(
        _("来源类型"), max_length=20, choices=KnowledgeSourceType.choices,
        default=KnowledgeSourceType.TEACHER_INPUT,
    )
    source_ref = models.CharField(
        _("来源引用"), max_length=300, blank=True, default="",
        help_text=_("如教材页码、文档名；供引证与追溯"),
    )

    # ── 向量检索 ──
    embedding = models.JSONField(
        _("向量"), null=True, blank=True,
        help_text=_("pgvector 列；迁移时改为 vector 类型"),
    )
    embedding_model = models.CharField(_("向量模型"), max_length=100, blank=True, default="")

    status = models.CharField(
        _("状态"), max_length=20, choices=KnowledgeEntryStatus.choices,
        default=KnowledgeEntryStatus.ACTIVE,
    )
    #: 被 AI 生成采纳的次数（健康度「采纳率」用）
    adopted_count = models.IntegerField(_("被采纳次数"), default=0)

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_entry"
        verbose_name = _("知识库条目")
        verbose_name_plural = _("知识库条目")
        indexes = [
            models.Index(
                fields=["org", "entry_type", "status"], name="idx_kb_entry_org_type"
            ),
            models.Index(fields=["chapter", "status"], name="idx_kb_entry_chapter"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title or self.content[:40]


# ══════════════════════════════════════════════════════════════
#  4.10 knowledge_entry_node — 条目与知识点映射
# ══════════════════════════════════════════════════════════════


class KnowledgeEntryNode(models.Model):
    """知识库条目 ↔ 知识点映射。

    ★ 按 DDL 4.10 实现：带 `weight`（检索排序用）与 `source`（AI/教师标注）。
      早期版本误用 `is_primary` 且漏了 `org`，与 DDL 不符，已更正。

    ★ 关系名说明：本表两条外键都用 `related_name="entry_links"` / `"node_links"`，
      避免与 `KnowledgeEntry.chapter` 的 `entries` 关系名冲突
      （`KnowledgeNode.entries` 已在别处用于查条目）。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    entry = models.ForeignKey(
        KnowledgeEntry, on_delete=models.CASCADE, related_name="entry_links",
        verbose_name=_("条目"),
    )
    node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="entry_links",
        db_column="knowledge_node_id",
        verbose_name=_("知识点"),
    )
    weight = models.DecimalField(
        _("相关性权重"), max_digits=3, decimal_places=2, default=1.0,
        help_text=_("检索排序用"),
    )
    source = models.CharField(
        _("标注来源"), max_length=20, default="TEACHER",
        choices=[("AI", "AI 标注"), ("TEACHER", "教师标注")],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_entry_node"
        verbose_name = _("条目知识点映射")
        verbose_name_plural = _("条目知识点映射")
        constraints = [
            models.UniqueConstraint(
                "entry", "node", name="uq_kb_entry_node",
            ),
        ]
        indexes = [
            models.Index(
                fields=["node", "-weight"], name="idx_kb_entry_node_node"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entry_id}↔{self.node_id}"


# ══════════════════════════════════════════════════════════════
#  4.4 knowledge_edge — 知识点关系
# ══════════════════════════════════════════════════════════════


class KnowledgeRelation(models.TextChoices):
    """知识点关系类型。"""

    PREREQUISITE = "PREREQUISITE", "前置知识"
    RELATED = "RELATED", "相关"
    CONTAINS = "CONTAINS", "包含"


class KnowledgeEdge(models.Model):
    """知识点关系。

    ★ `PREREQUISITE` 必须**无环**。校验在应用层做（`apps.knowledge.services`），
      并由每日定时任务检测——**发现环只告警，不自动修复**，
      因为自动修复可能删掉正确的边。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    from_node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="edges_from",
        verbose_name=_("起点"),
    )
    to_node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="edges_to",
        verbose_name=_("终点"),
    )
    relation = models.CharField(
        _("关系"), max_length=20, choices=KnowledgeRelation.choices
    )
    weight = models.DecimalField(_("权重"), max_digits=3, decimal_places=2, default=1.0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_edge"
        verbose_name = _("知识点关系")
        verbose_name_plural = _("知识点关系")
        constraints = [
            models.UniqueConstraint(
                "from_node", "to_node", "relation", name="uq_knowledge_edge"
            ),
            # ★ 自环无意义，库层拦掉
            models.CheckConstraint(
                condition=~models.Q(from_node=models.F("to_node")),
                name="ck_knowledge_edge_no_self_loop",
            ),
        ]
        indexes = [
            models.Index(
                fields=["to_node", "relation"], name="idx_knowledge_edge_to"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.from_node_id}-{self.relation}->{self.to_node_id}"


# ══════════════════════════════════════════════════════════════
#  4.7 task_knowledge — 任务与知识点映射
# ══════════════════════════════════════════════════════════════


class TaskKnowledge(models.Model):
    """任务与知识点映射（多对多）。

    ★ **为什么必须有这张表**：知识点按名称去重后，**一个知识点可能被多个
      任务关联**（实测 106 行中 64 处是同名跨任务复用）。
      而 `knowledge_node.task` 是单值外键，表达不了多任务关系。

    ★ 使用约定：
      - `knowledge_node.task` = **主任务**（首次出现位置），用于展示与默认查询
      - `task_knowledge`     = **完整的多任务关系**，
        **组卷按知识点抽题与学情聚合时必须用它**，不能只看 `knowledge_node.task`
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="knowledge_links",
        verbose_name=_("任务"),
    )
    knowledge_node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="task_links",
        verbose_name=_("知识点"),
    )
    seq_in_task = models.SmallIntegerField(
        _("任务内序号"), null=True, blank=True,
        help_text=_("该知识点在此任务下的教材原始序号，用于与纸质教材对照"),
    )
    is_primary = models.BooleanField(
        _("主归属任务"), default=False,
        help_text=_("该任务是此知识点的主归属"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "task_knowledge"
        verbose_name = _("任务知识点映射")
        verbose_name_plural = _("任务知识点映射")
        constraints = [
            models.UniqueConstraint(
                "task", "knowledge_node", name="uq_task_knowledge"
            ),
            # 一个知识点只能有一个主归属任务
            models.UniqueConstraint(
                "knowledge_node", name="uq_task_knowledge_primary",
                condition=models.Q(is_primary=True),
            ),
        ]
        indexes = [
            models.Index(
                fields=["knowledge_node"], name="idx_task_knowledge_node"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_id}↔{self.knowledge_node_id}"


# ══════════════════════════════════════════════════════════════
#  4.8 chapter_knowledge — 章与知识点映射
# ══════════════════════════════════════════════════════════════


class ChapterKnowledge(models.Model):
    """章与知识点映射（多对多）。

    ★ 用于**跨任务合并**的场景：按章统计覆盖率、按章组卷时，
      需要章维度的知识点全集，而不是逐任务累加。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    chapter = models.ForeignKey(
        Chapter, on_delete=models.CASCADE, related_name="knowledge_links",
        verbose_name=_("章"),
    )
    knowledge_node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="chapter_links",
        verbose_name=_("知识点"),
    )
    is_primary = models.BooleanField(
        _("主覆盖"), default=False,
        help_text=_("该章主覆盖的知识点"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "chapter_knowledge"
        verbose_name = _("章知识点映射")
        verbose_name_plural = _("章知识点映射")
        constraints = [
            models.UniqueConstraint(
                "chapter", "knowledge_node", name="uq_chapter_knowledge"
            ),
        ]
        indexes = [
            models.Index(
                fields=["knowledge_node"], name="idx_chapter_knowledge_node"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chapter_id}↔{self.knowledge_node_id}"


# ══════════════════════════════════════════════════════════════
#  4.11 knowledge_alias — 知识点别名（21 文档任务五）
# ══════════════════════════════════════════════════════════════


class KnowledgeAliasSource(models.TextChoices):
    """别名来源。"""

    MANUAL = "MANUAL", "教师录入"
    IMPORT = "IMPORT", "导入"
    MERGE = "MERGE", "合并时自动生成"


class KnowledgeAlias(models.Model):
    """知识点的别名 / 同义词 / 错写。

    ★★ 为什么别名要单独建表，而不是塞进 `KnowledgeNode.name`：

      「带传动」与「皮带传动」是同一个考点，但教材、试卷、导入文件里
      三种写法都有。如果把别名当节名放进树里，树会长出一堆重复节点，
      而组卷按节点抽题会各抽一半——覆盖率永远上不去。

    ★ 解析规则（`KnowledgeAliasService.resolve`）：
      先精确匹配 `name`，再匹配 `alias_normalized`。
      两者都命中不同节点时**不猜**：返回冲突，由教师裁决。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+",
        verbose_name=_("机构"), null=True, blank=True,
    )
    node = models.ForeignKey(
        KnowledgeNode, on_delete=models.CASCADE, related_name="aliases",
        verbose_name=_("知识点"),
    )
    alias = models.CharField(_("别名"), max_length=200)
    #: 归一化后的检索键（去空格、全角转半角、去「的/与」等虚词）
    alias_normalized = models.CharField(
        _("归一化别名"), max_length=200, db_index=True)
    source = models.CharField(
        _("来源"), max_length=20, choices=KnowledgeAliasSource.choices,
        default=KnowledgeAliasSource.MANUAL,
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_alias"
        verbose_name = _("知识点别名")
        verbose_name_plural = _("知识点别名")
        constraints = [
            models.UniqueConstraint(
                "node", "alias_normalized", name="uq_knowledge_alias"),
        ]
        indexes = [
            models.Index(fields=["org", "alias_normalized"],
                         name="idx_knowledge_alias_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.alias} → {self.node_id}"


# ══════════════════════════════════════════════════════════════
#  4.12 knowledge_merge_log — 合并日志（可回滚）
# ══════════════════════════════════════════════════════════════


class KnowledgeMergeLog(models.Model):
    """知识点合并日志。

    ★★ 为什么必须能回滚：

      合并只改引用、不物理删除——但「改引用」本身也是破坏性的：
      一旦把「带传动」并进「摩擦轮传动」，覆盖统计立刻变了。
      如果合错了而无法反向恢复，老师只能手工重建 1944 条关联。

    ★ `detail` 存**可反向执行的快照**（迁移了哪些关联、丢弃了哪些重复），
      而不是只存计数。回滚靠它重放，不靠猜。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+",
        verbose_name=_("机构"), null=True, blank=True,
    )
    source_node_id = models.BigIntegerField(_("源知识点 id"))
    target_node_id = models.BigIntegerField(_("目标知识点 id"))
    source_name = models.CharField(_("源知识点名"), max_length=200, blank=True,
                                   default="")
    target_name = models.CharField(_("目标知识点名"), max_length=200, blank=True,
                                   default="")
    #: 各项迁移计数（`question_links_moved` 等）
    counts = models.JSONField(_("迁移计数"), default=dict, blank=True)
    #: 可重放的明细快照（见类注释）
    detail = models.JSONField(_("明细快照"), default=dict, blank=True)

    operator = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("操作人"),
    )
    reason = models.CharField(_("原因"), max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    rolled_back_at = models.DateTimeField(_("回滚时间"), null=True, blank=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "knowledge_merge_log"
        verbose_name = _("知识点合并日志")
        verbose_name_plural = _("知识点合并日志")
        indexes = [
            models.Index(fields=["org", "-created_at"],
                         name="idx_knowledge_merge_org"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"merge {self.source_node_id}→{self.target_node_id}"
