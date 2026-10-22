"""题库 · 数据模型。

六张表：
  - `QuestionGroup`     题组（材料/分析题的「大题」，下挂多道小题）
  - `Question`          题目（含难度四字段、状态机）
  - `QuestionOption`    选项
  - `QuestionImage`     配图（含逐图确认状态机）
  - `QuestionKnowledge` 题目↔知识点
  - `QuestionRelation`  题目关系（变式、同源、相似、前置）

★★ 本模块有三条**不可绕过**的硬约束（设计文档「三个闸门」之一）：
  1. **DRAFT 题不可组卷** —— 取题必须走 `Question.objects.usable_for_paper()`
  2. **配图未确认不可组卷** —— 同上，且题干提到图却无图时阻断定稿
  3. **AI 生成题默认 DRAFT** —— 必须经人审核才能转 APPROVED

★★ 题组（2026-10-17 需求方确认「方案一」）：
  机械试卷里的「识读零件图」是**一道大题 + 多个填空小题**。小题各有答案与知识点，
  学情要能落到「哪个小问错得多」，所以不能塞成一道题的 `blanks`：
  单独的 `QuestionGroup` 存材料（题干/配图），`Question.group` 指向它，
  `group_seq` 是小题序号。★ 组卷按**组**抽（不允许把小题拆开单独抽）。
"""

from __future__ import annotations

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    DIFFICULTY_RATIO,
    FIGURE_GATE_PASSED_STATUSES,
    QUESTION_USABLE_STATUSES,
    Difficulty,
    FigureConfirmMethod,
    FigureQualityFlag,
    FigureReviewStatus,
    GradeBand,
    QuestionRelationType,
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
)
from apps.common.managers import OrgScopedManager, SoftDeleteModel


# ══════════════════════════════════════════════════════════════
#  QuerySet / Manager
# ══════════════════════════════════════════════════════════════

#: ★ 配图「阻断组卷」的状态集合。
#:   它是「通过集合」的补集——只有 CONFIRMED / DELETED 才放行。
#:   单独抽成常量，供 QuerySet 与单题属性两处共用，避免判据分叉。
FIGURE_BLOCKING_STATUSES = (
    FigureReviewStatus.PENDING,
    FigureReviewStatus.RECROPPED,
    FigureReviewStatus.REPLACED,
)


def _figure_blocked_statuses() -> list[str]:
    """阻断组卷的配图状态（字符串值）。"""
    return [s.value for s in FIGURE_BLOCKING_STATUSES]


#: 题干中被判为「提到了图」的关键词。
#:
#: ★★ 必须只有这一份：`Question.referenced_figure_missing`（单题属性）与
#:    `QuestionQuerySet.usable_for_paper()`（组卷取数）两处都读它。
#:
#:    这两处判据一旦分叉，会出现最糟糕的一类故障——
#:    组卷放行了某道题（因为组卷侧的判据没覆盖），
#:    定稿时又拦住它（因为定稿侧判据覆盖了），
#:    老师看到「定稿失败：第 3 题题干提到图但无配图」却不知道该怎么绕过。
#:    实测 2026-10-21 就是这个状态：1945 道 APPROVED 题里有 1 道命中。
FIGURE_MENTION_KEYWORDS = ("下图", "如图", "见图", "图中", "图1", "图 1")


class QuestionQuerySet(models.QuerySet):
    """题目查询集。

    ★ 所有组卷相关的取数**必须**用 `usable_for_paper()`，
      它把「状态」与「配图门禁」两个条件一起封住。

    ★★ 双轨可见性（v2.0）的**唯一收口点**是 `for_org()`，见其文档。
      除此之外的每个方法都默认「已经过收口」，不再重复判可见性。
    """

    # ── 双轨可见性（★ 全项目风险最高的判定，改动前请读完整段）──
    def for_org(self, org_id: int):
        """★ 本机构可见题目 = **本机构的题** ∪ **公共库已审核的题**。

        ═══════════════════════════════════════════════════════════
         为什么必须收口在这里，而不是各调用点自己拼
        ═══════════════════════════════════════════════════════════

        `for_org()` 是 57 张表共用的唯一取数入口，业务代码里有
        **39 处** `Question.objects.for_org(...)`。如果每处都自己写
        「本机构 or 公共」，那么漏掉一处的结果是**跨机构教材不可见**
        （老师抱怨「题明明在公共库却看不见」）；而如果反过来
        有人图省事写了 `.all()`，结果就是**跨机构泄露**。

        同一个判据写 39 遍，必然分叉。所以只写一遍。

        ═══════════════════════════════════════════════════════════
         两个条件分别拦住什么
        ═══════════════════════════════════════════════════════════

        1. `org_id=org_id`
           —— 本机构的全部题，**含草稿与待审**（题库管理页要看得到）。

        2. `bank.kind=PUBLIC AND status=APPROVED`
           —— 公共库**只放行已审核的**。
           公共库里的 `DRAFT` / `PENDING_REVIEW` / `REJECTED` 属于
           「提交者与审核人之间」，别的机构看到就是泄漏未定稿内容。
           ★ 注意这里用的是 `status` 而不是「审核人非空」：
             `approve()` 是唯一能置 APPROVED 的入口，它同时写
             `reviewed_by`；判 `status` 与判 `reviewed_by` 等价，
             但前者有索引（`db_index=True`）。

        ═══════════════════════════════════════════════════════════
         绝对不要做的事
        ═══════════════════════════════════════════════════════════

        ★ **不要改基类 `OrgScopedQuerySet.for_org`**。那会同时改变
          57 张表的语义——作业、试卷、班级的「公共」含义完全不同，
          改完不会有任何报错，只会在某张表上静默多出别人的数据。

        ★ **不要在业务模块里直接 `.filter(org_id=...)` 取题**。
          `scripts/verify_question_bank.py` 里有静态断言守着这条
          （它扫 `apps/` 下除 `questionbank` 外的所有 `.py`）。

        ★ **组卷不走这里**。决策 3 定的是「引用公共题 = 先下载到私有库」，
          所以 `usable_for_paper()` 会再把公共库的题排除掉。
          两条路径合起来的效果是：
            题库页 / 搜题 / 补标队列  → 看得见公共题（本方法放开）
            组卷池                    → 只有本机构私有题（`usable_for_paper` 收紧）
        """
        from apps.common.managers import OrgScopeRequired

        if not org_id:
            raise OrgScopeRequired(
                f"Question.objects.for_org() 需要有效的 org_id，收到 {org_id!r}"
            )
        return self.filter(
            Q(org_id=org_id)
            | Q(
                bank__kind=QuestionBankKind.PUBLIC,
                status=QuestionStatus.APPROVED,
            )
        )

    def private_only(self):
        """只要本机构的私有题（**统计口径专用**）。

        ★ 用在「本校题库有多少题」「还有多少题没打难度标」这类数字上。
          这些数字如果算进公共题，会立刻失真——公共题不属于本校资产，
          也不能被本校老师改状态。

        ★ `bank__isnull=True` 归入私有：迁移期的兼容分支。
          回填后不该再出现空 `bank_id`，验收脚本里有断言。
        """
        return self.filter(
            Q(bank__kind=QuestionBankKind.PRIVATE) | Q(bank__isnull=True)
        )

    def approved(self):
        return self.filter(status__in=QUESTION_USABLE_STATUSES)

    def usable_for_paper(self):
        """★ 唯一合法的取题入口。

        条件：
          0. 落在**本机构私有库**（`private_only()`，双轨之后新增）
          1. `status = APPROVED`
          2. 不存在「未通过门禁的配图」——即每张配图都是 CONFIRMED 或 DELETED

        ★★ 实现为什么用**集合运算**而不是 `exclude(id__in=子查询)`：

          两者语义看起来相同，但 Django 会把 `exclude(id__in=<子查询>)`
          渲染成 `NOT (id IN (SELECT ...))`。当子查询的结果集里含 NULL 行时，
          SQL 的三值逻辑会让 `NOT (x IN (...))` 对某些行求值为 NULL（而非 TRUE），
          于是**该放行的题被静默排除**。

          实测到的现象：一道配图已确认的题被误排除。
          这个 bug 极其隐蔽——SQL 看起来完全正确，只有跑数据才发现。

          集合运算没有这个问题，且判据一目了然。

        ★★ 第三个条件（2026-10-21 补）：**题干提到图但一张配图都没有**
          的题也不可用。理由：这类题能通过这里、被组进卷子，但会在
          定稿时被 B-04（`FIGURE.MISSING_REFERENCED`）拦住——
          等于"能抽到、却永远定不了稿"，老师只能靠试错找哪道题有问题。

          这个条件用 `Exists` 子查询而不是 python 集合：`Exists` 恒返回布尔、
          永不为 NULL，不会踩上面那个三值逻辑的坑；也不会把整表读进内存。
        """
        passed = {s.value for s in FIGURE_GATE_PASSED_STATUSES}
        bad_question_ids = set(
            QuestionImage.objects.filter(deleted_at__isnull=True)
            .exclude(review_status__in=passed)
            .values_list("question_id", flat=True)
        )
        bad_question_ids.discard(None)
        # ★★ 条件 0（2026-10-21 双轨）：**只从本机构私有库取题**。
        #
        #   决策 3 定的是「引用公共题 = 先下载到私有库」，所以组卷池
        #   **不收**公共题。如果不加这一条，`for_org()` 放开之后
        #   组卷会直接把别的机构的题抽进本校试卷，而卷面上不会显示来源，
        #   等于绕过「下载副本 → 自己审核」这条追溯链。
        #
        #   ★ 为什么不在 `for_org()` 里就排除公共题、让组卷天然干净：
        #     那样题库页/搜题也看不见公共题，双轨就白做了。
        #     两个口径必须分开，且都要显式写出来。
        qs = (
            self.private_only()
            .filter(status=QuestionStatus.APPROVED, group__isnull=True)
            .exclude(id__in=bad_question_ids)
        )

        # ★★ 题组的小题**不单独进组卷池**：材料/分析题必须整组抽，
        #   否则会抽出「没有材料的小题」。组级组卷在下一步接入
        #   （PaperItem 引题组、渲染成一道大题 + 若干小问）。

        # 条件 3：题干提到图但无任何配图 → 不可组卷（否则定稿必挂）
        has_image = QuestionImage.objects.filter(
            question_id=OuterRef("pk"), deleted_at__isnull=True
        )
        mention = Q()
        for kw in FIGURE_MENTION_KEYWORDS:
            mention |= Q(stem_text__contains=kw)
        return qs.annotate(
            _has_any_image=Exists(has_image)
        ).exclude(mention & Q(_has_any_image=False))

    def with_figures(self):
        return self.filter(has_figure=True)

    def by_difficulty(self, level: str):
        return self.filter(difficulty_band=level)


class QuestionManager(OrgScopedManager.from_queryset(QuestionQuerySet)):  # type: ignore[misc]
    """题目 Manager。

    ★ 取数入口：
        `Question.objects.for_org(org_id).usable_for_paper()`

    ★★ 这里**必须**显式转调 QuerySet 的 `for_org`，不能省。

      `Manager.from_queryset()` 只复制「基类上没有同名属性」的方法
      （见 Django `_get_queryset_methods`）。而 `OrgScopedManager`
      **自带**一个 `for_org`（`filter(org_id=...)`），于是：

        · QuerySet 里覆写的 `for_org` 会被**静默忽略**；
        · `Question.objects.for_org(x)` 仍走基类那版，
          双轨可见性**根本没生效**。

      ★ 这个坑的形态极坏：没有报错、没有警告，`usable_for_paper()`
        也照常工作，只有「公共题看不见」这一个可观察差异——
        而它看起来像「公共库里没题」。

      ★ 覆盖率：`Question.objects.for_org` 共 39 处调用，只有走 Manager
        的这 39 处会受影响；直接对 QuerySet 调 `for_org` 的地方不受影响。
    """

    def for_org(self, org_id: int):  # type: ignore[override]
        return self.get_queryset().for_org(org_id)


# ══════════════════════════════════════════════════════════════
#  5.1 question — 题目
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
#  5.0 question_bank — 题库（双轨：私有 / 公共）
# ══════════════════════════════════════════════════════════════


class QuestionBankKind(models.TextChoices):
    """题库类型。★ 这是「题目双轨」的唯一判据。"""

    PRIVATE = "PRIVATE", "机构私有库"
    PUBLIC = "PUBLIC", "平台公共库"


class QuestionBankStatus(models.TextChoices):
    """题库状态。"""

    ACTIVE = "ACTIVE", "启用"
    ARCHIVED = "ARCHIVED", "已归档"


class QuestionBank(SoftDeleteModel):
    """题库。

    ★★ 为什么要有这个模型（而不是给 `Question` 加一个 `is_public` 布尔）：

      1. **归属需要落到行上**：公共库挂在平台机构（`OrgType.PLATFORM`）下，
         私有库挂在学校下。用布尔标记的话，`org_id` 的语义就分叉了
         （公共题的 `org_id` 到底指谁）——而 `org_id` 是全站取数的唯一入口。
      2. **一个机构将来会有多个私有库**（按课程/按教研组建），
         布尔标记没有扩位空间。
      3. **审核状态要能按库查**：「公共库待审多少条」是审核工作台的首页数字。

    ★ 归属三层（写死在这里，不靠调用方自觉）：
      - 系统层：`kind=PUBLIC` + `org` 指向 `type=PLATFORM` 的平台行
      - 机构层：`kind=PRIVATE` + `org` 指向本机构
      - 用户层：**不单独建库**，靠 `Question.created_by` 过滤

    ★★ `is_default` 的意义：新建题目时若没有显式指定题库，落到这个库。
      每机构**只能有一个**私有默认库（DB 层用部分唯一索引兜住）。
    """

    kind = models.CharField(
        _("类型"), max_length=20, choices=QuestionBankKind.choices,
        default=QuestionBankKind.PRIVATE, db_index=True,
    )
    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="question_banks",
        verbose_name=_("归属机构"),
        help_text=_("PUBLIC 库必须是 OrgType.PLATFORM 的平台行"),
    )
    name = models.CharField(_("题库名称"), max_length=100)
    description = models.TextField(_("说明"), blank=True, default="")
    status = models.CharField(
        _("状态"), max_length=20, choices=QuestionBankStatus.choices,
        default=QuestionBankStatus.ACTIVE,
    )
    is_default = models.BooleanField(
        _("默认落库目标"), default=False,
        help_text=_("新建题目未指定题库时落到这里；每个机构仅一个私有默认库"),
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_bank"
        verbose_name = _("题库")
        verbose_name_plural = _("题库")
        ordering = ["kind", "id"]
        constraints = [
            # 同一机构下同类库不能重名（防手滑建两个「公共题库」）
            models.UniqueConstraint(
                fields=["org", "kind", "name"],
                name="uq_question_bank_org_kind_name",
            ),
            # ★ 部分唯一索引：只对「未删除的私有默认库」生效。
            #   有它之后「每机构一个默认库」不依赖应用层记得清标志位。
            #   ★ `condition` 里**不要**引用可空字段，否则 NULL 会让索引形同虚设。
            models.UniqueConstraint(
                fields=["org"],
                condition=Q(is_default=True, kind=QuestionBankKind.PRIVATE,
                            deleted_at__isnull=True),
                name="uq_question_bank_default_private",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}·{self.name}"

    @property
    def is_public(self) -> bool:
        return self.kind == QuestionBankKind.PUBLIC


class QuestionGroupKind(models.TextChoices):
    """题组类型。"""

    MATERIAL = "MATERIAL", "材料题"
    ANALYSIS = "ANALYSIS", "分析题"
    CALCULATION = "CALCULATION", "计算题"
    DRAWING = "DRAWING", "识图/读图题"


class QuestionGroup(SoftDeleteModel):
    """题组（材料/分析题的「大题」）。

    ★ 为什么单独建表而不是塞进 `Question`：
      小题要各自有答案、知识点、难度与错题统计；塞进一道题的 `blanks` 里，
      学情就只能到「这道大题」，再拆几乎等于重做。
    ★ 题组的**配图**复用 `QuestionImage(question=None, group=...)`？
      一期先存 `figure_asset_ids`（asset id 列表），等配图归属统一时再收敛。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="question_groups",
        verbose_name=_("机构"),
    )
    course = models.ForeignKey(
        "knowledge.Course", on_delete=models.PROTECT, related_name="+",
        verbose_name=_("课程"),
    )
    task = models.ForeignKey(
        "knowledge.Task", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("任务"),
        help_text=_("组卷按任务取数；题组也要能定位到任务"),
    )
    kind = models.CharField(
        _("类型"), max_length=20, choices=QuestionGroupKind.choices,
        default=QuestionGroupKind.MATERIAL,
    )
    stem_html = models.TextField(_("材料题干"), blank=True, default="")
    stem_text = models.TextField(_("材料纯文本"), blank=True, default="")
    figure_asset_ids = models.JSONField(
        _("配图资源"), default=list, blank=True,
        help_text=_("Asset id 列表（组级配图，如零件图）"),
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+",
        verbose_name=_("创建人"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_group"
        verbose_name = _("题组")
        verbose_name_plural = _("题组")
        indexes = [
            models.Index(fields=["org", "course", "task"],
                         name="idx_qgroup_org_course"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"题组{self.pk}({self.kind})"

    @property
    def sub_count(self) -> int:
        """小题数（含软删前的口径由 manager 决定）。"""
        return self.questions.count()


class DifficultySource(models.TextChoices):
    """`question.difficulty_source`。"""

    MANUAL = "MANUAL", "教师标定"
    STAT = "STAT", "统计推断"
    MANUAL_STAT_BLEND = "MANUAL_STAT_BLEND", "标定与统计混合"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE", "教师覆盖统计值"


class Question(SoftDeleteModel):
    """题目。

    ★ 表述与实体的 key 保持 `type`（与 DDL 一致），不使用 `question_type`，
      避免与 `QuestionType` 常量名混淆。
    """

    uuid = models.UUIDField(_("对外标识"), default=uuid.uuid4, unique=True, editable=False)

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="questions",
        verbose_name=_("机构"),
    )
    #: ★ 所属题库（v2.0 双轨）。
    #:
    #:   `null=True` 是**迁移期兼容**，不是允许空：
    #:   1946 道存量题已由迁移 `questionbank 0008` 全部落到本机构默认私有库，
    #:   新建题由 `QuestionService.create_question` 自动挂默认库。
    #:   业务代码**不应**再产生 `bank_id IS NULL` 的行——
    #:   `scripts/verify_question_bank.py` 里有断言守着。
    #:
    #:   ★ 为什么不设 `NOT NULL`：`bank_id` 与 `org_id` 必须一致
    #:     （私有题挂本机构库、公共题挂平台行库），这个一致性
    #:     要在服务层校验并给出可读错误；先收成 NOT NULL 会让
    #:     「迁移没跑完」与「服务层写错」两种故障表现成同一个 IntegrityError。
    bank = models.ForeignKey(
        QuestionBank, on_delete=models.PROTECT,
        null=True, blank=True, related_name="questions",
        verbose_name=_("所属题库"),
    )
    #: ★★ 下载式引用的追溯字段（决策 3）。
    #:
    #:   `source_question` 指向**公共库里的原题**；`source_version` 记录下载那一刻
    #:   原题的 `updated_at`（ISO8601 字符串）。
    #:
    #:   ★ 为什么版本用 `updated_at` 而不是新加 `revision` 自增列：
    #:     `Question` 的写路径有二十多处（含 `save(update_fields=...)` 与
    #:     批量 `update()`），自增列只要漏一处就会「改了但不涨版本」，
    #:     而这种漏法的表现是「永远不提示漂移」——正好是最难发现的方向。
    #:     `updated_at` 是 `auto_now`，写路径漏同步它至少会连带漏掉别的东西。
    #:
    #:   ★ 漂移只**提示**不自动同步：老师可能已经改过这道题。
    source_question = models.ForeignKey(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="derived_copies",
        verbose_name=_("来源公共题"),
    )
    source_version = models.CharField(
        _("来源版本"), max_length=40, blank=True, default="",
        help_text=_("下载时来源题的 updated_at（ISO8601），用于漂移提示"),
    )
    course = models.ForeignKey(
        "knowledge.Course",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="questions",
        verbose_name=_("课程"),
    )
    subject = models.CharField(
        _("课程编码"), max_length=20,
        help_text=_("冗余存 course.code；新代码请用 course 外键"),
    )
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )
    #: ★ 题库只标到「任务」这一级（存量数据如此）
    task = models.ForeignKey(
        "knowledge.Task",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="questions",
        verbose_name=_("所属任务"),
    )
    #: 题库原始章节号 `X.Y`，导入时保留以便追溯
    chapter_raw = models.CharField(
        _("题库章节号"), max_length=20, blank=True, default="",
        help_text=_("如 1.1；Y 是任务号"),
    )

    # ── 题组（材料/分析题）──
    #: ★ 小题挂到题组；为 NULL 表示独立题。
    #: `on_delete=PROTECT`：题组里还有小题时不允许删组（先删小题）。
    group = models.ForeignKey(
        QuestionGroup, on_delete=models.PROTECT,
        null=True, blank=True, related_name="questions",
        verbose_name=_("所属题组"),
    )
    #: 小题在组内的序号（从 1 开始；独立题为 0）
    group_seq = models.SmallIntegerField(
        _("组内序号"), default=0,
        validators=[MinValueValidator(0)],
        help_text=_("材料题/分析题的小题序号；(1)(2)… 的编号"),
    )

    type = models.CharField(_("题型"), max_length=30, choices=QuestionType.choices)

    # ── 题干（双存：富文本 + 纯文本）──
    stem_html = models.TextField(_("题干 HTML"))
    stem_text = models.TextField(
        _("题干纯文本"), help_text=_("用于检索与去重，不用于渲染")
    )
    stem_hash = models.CharField(
        _("题干哈希"), max_length=64, db_index=True,
        help_text=_("归一化后 MD5，精确去重"),
    )
    simhash = models.BigIntegerField(_("近似哈希"), null=True, blank=True, db_index=True)

    # ── 配图 ──
    has_figure = models.BooleanField(_("有配图"), default=False)
    figure_count = models.SmallIntegerField(_("配图数量"), default=0)

    # ── 答案与解析 ──
    answer = models.JSONField(
        _("标准答案"), default=dict, blank=True,
        help_text=_("结构随题型：选择题 {correct:['A']}；填空 {blanks:[...]}"),
    )
    answer_text = models.TextField(_("答案纯文本"), blank=True, default="")
    explanation_html = models.TextField(_("解析 HTML"), blank=True, default="")
    explanation_text = models.TextField(_("解析纯文本"), blank=True, default="")

    # ── 难度四字段（★ 组卷只用 difficulty_effective）──
    difficulty_manual = models.SmallIntegerField(
        _("教师标定难度"), null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    difficulty_ai = models.SmallIntegerField(
        _("AI 建议难度"), null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    difficulty_stat = models.DecimalField(
        _("统计难度"), max_digits=4, decimal_places=2, null=True, blank=True
    )
    difficulty_effective = models.DecimalField(
        _("有效难度"), max_digits=4, decimal_places=2, default=3.00,
        help_text=_("★ 组卷实际使用值"),
    )
    difficulty_source = models.CharField(
        _("难度来源"), max_length=20, choices=DifficultySource.choices,
        default=DifficultySource.MANUAL,
    )
    difficulty_band = models.CharField(
        _("难度档"), max_length=10, choices=Difficulty.choices,
        default=Difficulty.MEDIUM, db_index=True,
        help_text=_("由有效难度归入易/中/难，供组卷按比例抽题"),
    )

    # ── 统计 ──
    answer_count = models.IntegerField(_("作答次数"), default=0)
    correct_rate = models.DecimalField(
        _("实测得分率"), max_digits=5, decimal_places=4, null=True, blank=True
    )

    # ── 来源与状态 ──
    source_type = models.CharField(
        _("来源"), max_length=30, choices=QuestionSourceType.choices,
        default=QuestionSourceType.ORIGINAL_HUMAN,
    )
    source_ref = models.JSONField(
        _("来源引用"), default=dict, blank=True,
        help_text=_("如 {file, row} 或 {ai_call_log_id}"),
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=QuestionStatus.choices,
        default=QuestionStatus.DRAFT, db_index=True,
    )
    #: AI 生成题必须记录依据，便于追溯与引证校验
    ai_evidence = models.JSONField(_("AI 依据"), default=list, blank=True)

    reviewed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("审核人"),
    )
    reviewed_at = models.DateTimeField(_("审核时间"), null=True, blank=True)

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_questions", verbose_name=_("创建人"),
    )
    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = QuestionManager()

    class Meta:
        db_table = "question"
        verbose_name = _("题目")
        verbose_name_plural = _("题目")
        indexes = [
            # ★ 索引名上限 30 字符（Django models.E034）
            models.Index(
                fields=["org", "subject", "status"], name="idx_question_org_subj_st"
            ),
            models.Index(
                fields=["org", "type", "difficulty_band"], name="idx_question_org_type_diff"
            ),
            # ★ 双轨列表的主查询形态：本机构 + 某个库 + 状态。
            #   没有它会退化成只用 org 索引再回表过滤 bank，公共 Tab 尤其慢
            #   （公共题的 `org_id` 是平台行，与本机构不同，走不到上面的索引）。
            models.Index(fields=["org", "bank", "status"],
                         name="idx_question_org_bank_st"),
            models.Index(fields=["stem_hash"], name="idx_question_stem_hash"),
            models.Index(fields=["simhash"], name="idx_question_simhash"),
            models.Index(fields=["task"], name="idx_question_task"),
            models.Index(fields=["uuid"], name="idx_question_uuid"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.stem_text[:40] if self.stem_text else f"Question#{self.pk}"

    # ── 组卷可用性 ──
    @property
    def is_usable_for_paper(self) -> bool:
        """★ 单题层面的组卷可用性（与 QuerySet 版判据共用同一常量）。

        ★ 双轨之后**必须**与 QuerySet 版判据完全一致：公共题一律不可组卷
          （决策 3：先下载到私有库）。这两处判据历史上就分叉过一次
          （单题版少了「题干提到图但无配图」这一条），所以两处都显式写出来。
        """
        if self.status != QuestionStatus.APPROVED:
            return False
        if self.bank_id is not None and self.bank.kind == QuestionBankKind.PUBLIC:
            return False
        return not self.images.filter(
            deleted_at__isnull=True,
            review_status__in=_figure_blocked_statuses(),
        ).exists()

    @property
    def has_unconfirmed_figure(self) -> bool:
        """是否存在**待确认**配图（PENDING）。

        ★ 与 `is_usable_for_paper` 的区别：这里只判 PENDING，
          用于「审核通过」的闸门；而组卷门禁更严，连 RECROPPED/REPLACED
          这种瞬时态也不放行。
        """
        return self.images.filter(
            deleted_at__isnull=True,
            review_status=FigureReviewStatus.PENDING,
        ).exists()

    @property
    def referenced_figure_missing(self) -> bool:
        """题干提到图但实际无配图。

        ★ 该状态会**阻断试卷定稿**（`FIGURE.MISSING_REFERENCED`）。

        ★ 判据用**实时查询**而不是 `has_figure` 缓存字段。
          原因：`has_figure` 是反规范化字段，只在写路径显式同步；
          若某条写路径漏了同步（历史上就漏了，导致有图的题也被判为缺图），
          用缓存会让这个门禁**产生假阳性并拦住正常定稿**。
          门禁宁可多查一次库，也不能因为缓存过期而误判。
        """
        if self.images.filter(deleted_at__isnull=True).exists():
            return False
        text = self.stem_text or ""
        # ★ 关键词来自模块常量，与 `usable_for_paper()` 共用同一份，
        #   改判据只能改一处（见 `FIGURE_MENTION_KEYWORDS` 的说明）。
        return any(k in text for k in FIGURE_MENTION_KEYWORDS)

    def sync_figure_counters(self, save: bool = True) -> tuple[bool, int]:
        """把 `has_figure` / `figure_count` 与配图表对齐。

        ★ 这两个字段是**反规范化缓存**：查询时免去 join 配图表。
          必须在每次配图增删后调用，否则缓存会漂移。

        返回 `(has_figure, figure_count)`。

        ★ 为什么不禁用这两个字段：`with_figures()` 与题库统计会用到，
          每次 join 配图表的代价不值得。保留缓存 + 显式同步是常见取舍。
        """
        n = self.images.filter(deleted_at__isnull=True).count()
        has = n > 0
        changed = (self.has_figure != has) or (self.figure_count != n)
        if changed:
            self.has_figure = has
            self.figure_count = n
            if save:
                self.save(update_fields=["has_figure", "figure_count",
                                         "updated_at"])
        return has, n

    # ── 难度 ──
    @property
    def suggested_difficulty_band(self) -> str:
        """由有效难度归入档位。"""
        d = float(self.difficulty_effective or 3)
        if d <= 2.4:
            return Difficulty.EASY
        if d <= 3.6:
            return Difficulty.MEDIUM
        return Difficulty.HARD

    def sync_difficulty_band(self, save: bool = True) -> str:
        """把档位与有效难度对齐。"""
        band = self.suggested_difficulty_band
        if band != self.difficulty_band:
            self.difficulty_band = band
            if save:
                self.save(update_fields=["difficulty_band", "updated_at"])
        return band

    # ── 状态流转（★ 唯一允许的改状态入口）──
    def approve(self, user=None) -> None:
        """审核通过。

        ★ 有未确认配图时**不允许**通过——否则配图门禁形同虚设。
        """
        if self.has_unconfirmed_figure:
            from apps.common.exceptions import ErrorCode, ZfError

            raise ZfError(
                ErrorCode.FIGURE_PENDING_CONFIRM,
                detail="存在未确认的配图，不能审核通过",
                extra={"question_id": self.pk},
            )
        self.status = QuestionStatus.APPROVED
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.save(
            update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"]
        )

    def reject(self, user=None, reason: str = "") -> None:
        self.status = QuestionStatus.REJECTED
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        if reason:
            self.source_ref = {**(self.source_ref or {}), "reject_reason": reason}
        self.save(
            update_fields=["status", "reviewed_by", "reviewed_at", "source_ref",
                           "updated_at"]
        )


# ══════════════════════════════════════════════════════════════
#  5.2 question_option — 选项
# ══════════════════════════════════════════════════════════════


class QuestionOption(models.Model):
    """选项。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="options",
        verbose_name=_("题目"),
    )
    label = models.CharField(_("选项标签"), max_length=5)
    content_html = models.TextField(_("选项 HTML"))
    content_text = models.TextField(_("选项纯文本"))
    sort_order = models.IntegerField(_("排序"), default=0)
    is_correct = models.BooleanField(_("是否正确答案"), default=False)

    image_asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("选项配图"),
    )
    error_reason = models.TextField(
        _("典型错因"), blank=True, default="",
        help_text=_("该选项对应的常见错误，供错因分析与 AI 干扰项生成"),
    )
    error_stat = models.JSONField(
        _("被选统计"), default=dict, blank=True,
        help_text=_("{count, rate}"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_option"
        verbose_name = _("选项")
        verbose_name_plural = _("选项")
        constraints = [
            models.UniqueConstraint(
                "question", "label", name="uq_question_option"
            ),
        ]
        indexes = [
            models.Index(
                fields=["question", "sort_order"], name="idx_question_option_question"
            ),
        ]
        ordering = ["question", "sort_order"]

    def __str__(self) -> str:
        return f"{self.label}. {self.content_text[:20]}"


# ══════════════════════════════════════════════════════════════
#  5.3 question_image — 配图
# ══════════════════════════════════════════════════════════════


class FigureAttachPoint(models.TextChoices):
    """配图挂载点。"""

    STEM = "STEM", "题干"
    MATERIAL = "MATERIAL", "材料"
    OPTION_A = "OPTION_A", "选项 A"
    OPTION_B = "OPTION_B", "选项 B"
    OPTION_C = "OPTION_C", "选项 C"
    OPTION_D = "OPTION_D", "选项 D"


class FigureSrcPath(models.TextChoices):
    """配图来源路径。"""

    PDF_OBJECT = "PDF_OBJECT", "PDF 对象抽取"
    WORD_INLINE = "WORD_INLINE", "Word 内嵌图"
    VISION_EXTRACT = "VISION_EXTRACT", "视觉抽取"
    MANUAL = "MANUAL", "人工截取"


class FigureCategory(models.TextChoices):
    """配图类别。"""

    GEOMETRY = "GEOMETRY", "几何图"
    FUNCTION_GRAPH = "FUNCTION_GRAPH", "函数图像"
    CIRCUIT = "CIRCUIT", "电路图"
    FORCE_DIAGRAM = "FORCE_DIAGRAM", "受力图"
    APPARATUS = "APPARATUS", "装置图"
    CHEM_STRUCTURE = "CHEM_STRUCTURE", "化学结构"
    MAP = "MAP", "地图"
    CHART = "CHART", "图表"
    PHOTO = "PHOTO", "照片"
    TABLE = "TABLE", "表格"
    OTHER = "OTHER", "其它"


class QuestionImage(SoftDeleteModel):
    """题目配图。

    ★ 逐图确认状态机：`PENDING` → `CONFIRMED` / `DELETED`。
      RECROPPED / REPLACED 是瞬时态，处理完立即转 CONFIRMED。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="images",
        verbose_name=_("题目"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="images",
        verbose_name=_("卷面题"),
        help_text=_("材料题多题共用一图时使用"),
    )
    attach_point = models.CharField(
        _("挂载点"), max_length=20, choices=FigureAttachPoint.choices,
        default=FigureAttachPoint.STEM,
    )
    asset = models.ForeignKey(
        "media.Asset", on_delete=models.PROTECT, related_name="question_images",
        verbose_name=_("图片资源"),
    )
    seq_in_question = models.SmallIntegerField(_("题内序号"), default=1)

    src_path = models.CharField(
        _("来源路径"), max_length=20, choices=FigureSrcPath.choices,
        default=FigureSrcPath.MANUAL,
    )
    source_ref = models.JSONField(
        _("来源定位"), default=dict, blank=True,
        help_text=_("{page, bbox, anchor_id}"),
    )
    caption = models.CharField(_("题注"), max_length=100, blank=True, default="")
    figure_category = models.CharField(
        _("类别"), max_length=30, choices=FigureCategory.choices,
        blank=True, default="",
    )
    description = models.TextField(_("图片描述"), blank=True, default="")
    text_inside = models.JSONField(
        _("图内文字"), default=list, blank=True,
        help_text=_('如 ["R1", "6V"]；AI 生题与检索会用到'),
    )

    # ── 质检 ──
    quality_score = models.DecimalField(
        _("质量分"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    quality_flags = models.JSONField(
        _("质量标记"), default=list, blank=True,
        help_text=_(f"取值 {[c.value for c in FigureQualityFlag]}"),
    )

    # ── 归属判定 ──
    binding_confidence = models.DecimalField(
        _("归属置信度"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    binding_method = models.CharField(
        _("归属方法"), max_length=20, blank=True, default="",
        help_text=_("SEQUENCE / REFERENCE_TEXT / CAPTION / OPTION_POS / CROSS_PAGE / MANUAL"),
    )

    # ── ★ 逐图确认（门禁字段）──
    review_status = models.CharField(
        _("确认状态"), max_length=20, choices=FigureReviewStatus.choices,
        default=FigureReviewStatus.PENDING, db_index=True,
    )
    confirm_method = models.CharField(
        _("确认方式"), max_length=20, choices=FigureConfirmMethod.choices,
        blank=True, default="",
    )
    confirmed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("确认人"),
    )
    confirmed_at = models.DateTimeField(_("确认时间"), null=True, blank=True)
    #: 重裁历史，便于回溯与「撤销」
    crop_history = models.JSONField(_("裁切历史"), default=list, blank=True)

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_image"
        verbose_name = _("题目配图")
        verbose_name_plural = _("题目配图")
        indexes = [
            models.Index(
                fields=["question", "attach_point"], name="idx_qimage_question_attach"
            ),
            models.Index(
                fields=["review_status"], name="idx_qimage_review_status",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(
                fields=["asset"], name="idx_qimage_asset",
            ),
        ]
        ordering = ["question", "seq_in_question"]

    def __str__(self) -> str:
        return f"图{self.seq_in_question}@{self.attach_point}"

    @property
    def gate_passed(self) -> bool:
        """是否满足配图门禁。"""
        return self.review_status in FIGURE_GATE_PASSED_STATUSES

    # ── 状态流转（★ 唯一允许的确认入口）──
    def confirm(self, method: str, user=None, crop_box: dict | None = None) -> None:
        """确认该配图。

        ★ 重裁与换图**同时完成确认**——不设「裁完还要再点确认」的两步，
          减少一次点击与一次状态。
        """
        if crop_box:
            self.crop_history = [*(self.crop_history or []), crop_box]
        self.review_status = FigureReviewStatus.CONFIRMED
        self.confirm_method = method
        self.confirmed_by = user
        self.confirmed_at = timezone.now()
        self.save(
            update_fields=[
                "review_status", "confirm_method", "confirmed_by",
                "confirmed_at", "crop_history", "updated_at",
            ]
        )

    def mark_deleted(self, user=None) -> None:
        """判定为非配图（也算通过门禁）。"""
        self.review_status = FigureReviewStatus.DELETED
        self.confirmed_by = user
        self.confirmed_at = timezone.now()
        self.save(
            update_fields=["review_status", "confirmed_by", "confirmed_at",
                           "updated_at"]
        )


# ══════════════════════════════════════════════════════════════
#  5.4 question_knowledge — 题目↔知识点
# ══════════════════════════════════════════════════════════════


class KnowledgeLinkSource(models.TextChoices):
    """知识点关联来源。

    ★ 21 文档任务五把「人工」叫 `manual`，本项目既有取值是 `TEACHER`。
      **不改**：改值要迁移 1944 条历史数据的语义，而且前端、
      序列化器、导入服务里都有字面量 `"TEACHER"`；
      改名的收益是零，风险是一整批数据的 `source` 变成脏值。
    """

    AI = "AI", "AI 标注"
    TEACHER = "TEACHER", "教师标注"
    IMPORT = "IMPORT", "导入"


class QuestionReviewState(models.TextChoices):
    """知识点标注的人工确认状态（21 文档 5.2）。

    ★★ 只有 `CONFIRMED` 计入覆盖统计。
      AI 标注默认 `PENDING`——它只是一个**候选**，不是结论。
      否则 AI 一跑，覆盖率立刻「达标」，而实际上没人看过。
    """

    PENDING = "PENDING", "待确认"
    CONFIRMED = "CONFIRMED", "已确认"
    REJECTED = "REJECTED", "已驳回"


class QuestionKnowledge(models.Model):
    """题目与知识点的关联。

    ★ 每题**有且仅有一个**主知识点（`is_primary=True`，见唯一索引）。
      组卷按知识点抽题依赖这张表。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="knowledge_links",
        verbose_name=_("题目"),
    )
    knowledge_node = models.ForeignKey(
        "knowledge.KnowledgeNode",
        on_delete=models.CASCADE,
        related_name="question_links",
        verbose_name=_("知识点"),
    )
    weight = models.DecimalField(
        _("权重"), max_digits=3, decimal_places=2, default=1.0,
        help_text=_("主知识点 1.0，次要 0.3–0.5"),
    )
    is_primary = models.BooleanField(_("主知识点"), default=False)
    source = models.CharField(
        _("标注来源"), max_length=20, choices=KnowledgeLinkSource.choices,
        default=KnowledgeLinkSource.TEACHER,
    )
    #: ★ 21 文档任务五：AI 标注必须带置信度与人工确认状态。
    confidence = models.DecimalField(
        _("标注置信度"), max_digits=4, decimal_places=3, null=True, blank=True,
        help_text=_("仅 AI / 导入标注有值；教师手工标注为空"),
    )
    review_state = models.CharField(
        _("确认状态"), max_length=20, choices=QuestionReviewState.choices,
        default=QuestionReviewState.CONFIRMED, db_index=True,
        help_text=_("★ 只有 CONFIRMED 计入覆盖率；AI 标注默认 PENDING"),
    )
    reviewed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("确认人"),
    )
    reviewed_at = models.DateTimeField(_("确认时间"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_knowledge"
        verbose_name = _("题目知识点关联")
        verbose_name_plural = _("题目知识点关联")
        constraints = [
            models.UniqueConstraint(
                "question", "knowledge_node", name="uq_question_knowledge"
            ),
            models.UniqueConstraint(
                "question",
                name="uq_question_primary_node",
                condition=models.Q(is_primary=True),
            ),
        ]
        indexes = [
            models.Index(
                fields=["knowledge_node", "question"], name="idx_qk_node"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.question_id}→{self.knowledge_node_id}"


# ══════════════════════════════════════════════════════════════
#  5.5 question_relation — 题目关系
# ══════════════════════════════════════════════════════════════


class QuestionRelation(models.Model):
    """题目关系。

    ★ `VARIANT`（变式题）在个性化练习里**优先选用**——
      「同一知识点的变式题」比「同题重做」更有练习价值。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    from_question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="relations_from",
        verbose_name=_("源题"),
    )
    to_question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="relations_to",
        verbose_name=_("目标题"),
    )
    relation = models.CharField(
        _("关系类型"), max_length=30, choices=QuestionRelationType.choices
    )
    similarity = models.DecimalField(
        _("相似度"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    created_by = models.CharField(_("创建者"), max_length=20, default="AI")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_relation"
        verbose_name = _("题目关系")
        verbose_name_plural = _("题目关系")
        constraints = [
            models.UniqueConstraint(
                "from_question", "to_question", "relation",
                name="uq_question_relation",
            ),
        ]
        indexes = [
            models.Index(fields=["from_question", "relation"], name="idx_qrel_from"),
            models.Index(fields=["to_question", "relation"], name="idx_qrel_to"),
        ]

    def __str__(self) -> str:
        return f"{self.from_question_id}-{self.relation}->{self.to_question_id}"
