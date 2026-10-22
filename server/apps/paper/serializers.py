"""组卷（约束求解、等值、排版导出） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 试卷序列化的三个取舍：

  1. **快照按需返回**。`paper_item.snapshot_json` 是整道题的完整副本
     （含题干、选项、答案、知识点），一份 25 题的卷子约 150–400KB。
     列表接口返回它会让响应体膨胀百倍且没人需要；
     只有「试卷预览」与「导出」接口才要。

  2. **不返回 `answer`**（除非显式要）。学生端会拿到试卷结构——
     虽然是教师端接口，但一旦答案跟着结构一起返回，
     任何一次「把教师接口的响应转发给学生页」的疏忽都是泄题。

  3. **`layout_flags` 原样返回**。它是排版检查的产物，
     前端要按它高亮「这题图太大放不下」。
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.common.mathtext import truncate_keep_math
from apps.paper.models import Paper, PaperEquivalence, PaperItem


# ══════════════════════════════════════════════════════════════
#  输出
# ══════════════════════════════════════════════════════════════


class PaperListSerializer(serializers.ModelSerializer):
    """试卷列表（轻量）。"""

    item_count = serializers.IntegerField(read_only=True)
    course_name = serializers.CharField(
        source="course.name", read_only=True, default="")

    class Meta:
        model = Paper
        fields = [
            "id", "uuid", "title", "purpose", "status", "subject",
            "grade_band", "course_id", "course_name",
            "total_score", "item_count", "difficulty_expect",
            "target_total_score",
            "compose_mode", "is_personalized", "owner_student_id",
            "variant_label", "paper_group_id",
            "finalized_at", "created_at", "updated_at",
        ]


class PaperItemSerializer(serializers.ModelSerializer):
    """卷面题。

    `with_snapshot=True` 时带快照（预览/导出用）；
    默认只给结构信息。
    """

    question_type = serializers.SerializerMethodField()
    stem_preview = serializers.SerializerMethodField()
    difficulty = serializers.SerializerMethodField()

    class Meta:
        model = PaperItem
        fields = [
            "id", "seq", "section_title", "score", "group_key",
            "question_id", "question_type", "stem_preview", "difficulty",
            "option_order", "layout_flags", "figure_display_scale",
        ]

    def get_question_type(self, obj) -> str:
        """题型取**快照优先**。

        ★ 与判分侧一致：试卷定稿时已固化题型，
          题库事后改题型不该改变已定稿卷子的结构。
        """
        snap = obj.snapshot_json or {}
        return snap.get("type") or getattr(obj.question, "type", "")

    def get_stem_preview(self, obj) -> str:
        snap = obj.snapshot_json or {}
        s = (snap.get("stem_text") or getattr(obj.question, "stem_text", "")
             or "").strip()
        # ★ 不能裸切 `[:60]`：落在 `$\frac{1}{2}$` 中间会让前端拿到
        #   不配对的定界符，整段退化成字面文本。
        return truncate_keep_math(s, 60)

    def get_difficulty(self, obj) -> float | None:
        snap = obj.snapshot_json or {}
        v = snap.get("difficulty")
        if v is None:
            v = getattr(obj.question, "difficulty_effective", None)
        return float(v) if v is not None else None


class PaperDetailSerializer(serializers.ModelSerializer):
    """试卷详情（含卷面题）。"""

    items = serializers.SerializerMethodField()
    course_name = serializers.CharField(
        source="course.name", read_only=True, default="")

    class Meta:
        model = Paper
        fields = [
            "id", "uuid", "title", "purpose", "status", "subject",
            "grade_band", "course_id", "course_name",
            "total_score", "item_count", "difficulty_expect",
            "target_total_score",
            "compose_mode", "compose_config", "compose_rationale",
            "knowledge_coverage", "layout_config",
            "random_seed",
            "is_personalized", "owner_student_id", "source_paper_id",
            "variant_label", "paper_group_id",
            "finalized_at", "created_at", "updated_at",
            "items",
        ]

    def get_items(self, obj) -> list[dict]:
        # ★ 不返回 snapshot_json 全文——见模块 docstring 第 1 条。
        #   需要快照走 `/papers/{id}/preview/`。
        qs = obj.items.all().select_related("question").order_by("seq")
        return PaperItemSerializer(qs, many=True).data


class PaperPreviewSerializer(serializers.ModelSerializer):
    """试卷**预览**：含每题的完整快照（题干、选项、图）。

    ★ 这个序列化器**不含答案**（`answer` / `answer_text` /
      `explanation_*` 都被排除）。教师预览用不到；
      而一旦它带上答案，「把预览响应转发给学生」就是一次泄题。
      需要答案的场景（如教师端答案页）走单独的
      `/papers/{id}/answer-key/` 接口，权限与用途更明确。
    """

    items = serializers.SerializerMethodField()
    sections = serializers.SerializerMethodField()

    class Meta:
        model = Paper
        fields = [
            "id", "uuid", "title", "subject", "status",
            "total_score", "item_count", "target_total_score",
            "sections", "items",
        ]

    def get_items(self, obj) -> list[dict]:
        out = []
        for it in obj.items.all().select_related("question").order_by("seq"):
            snap = it.snapshot_json or {}
            out.append({
                "id": it.id,
                "seq": it.seq,
                "section_title": it.section_title,
                "score": str(it.score),
                "type": snap.get("type") or it.question.type,
                "stem_html": snap.get("stem_html") or it.question.stem_html,
                "stem_text": snap.get("stem_text") or it.question.stem_text,
                "options": [
                    {
                        "label": o.get("label"),
                        "content_html": o.get("content_html", ""),
                        "content_text": o.get("content_text", ""),
                        "image_asset_id": o.get("image_asset_id"),
                    }
                    for o in (snap.get("options") or [])
                ],
                "figures": snap.get("figures") or [],
                "option_order": it.option_order,
                "layout_flags": it.layout_flags,
                # ★ `figure_display_scale` 是 **JSONField**（dict），
                #   不是数值——我第一版按 FloatField 写了 `float(...)`，
                #   于是 `float() argument must be ... not 'dict'`。
                #   它是每张图各自的缩放比，形如 `{"1": 0.8, "2": 1.0}`，
                #   原样透传即可。
                "figure_display_scale": it.figure_display_scale or {},
            })
        return out

    def get_sections(self, obj) -> list[dict]:
        """按大题分组（前端渲染用）。

        ★ 顺序以**卷面出现顺序**为准，不用 `section_title` 排序——
          老师排版时可能把「二、填空」放在「一、选择」前面，
          按标题排序会让预览与打印件不一致。
        """
        seen: list[str] = []
        for it in obj.items.all().order_by("seq"):
            t = it.section_title or ""
            if t not in seen:
                seen.append(t)
        return [{"title": t} for t in seen]


class PaperItemInputSerializer(serializers.Serializer):
    """添加卷面题的单项输入。"""

    question_id = serializers.IntegerField()
    score = serializers.FloatField(min_value=0.5, max_value=100)
    section_title = serializers.CharField(
        required=False, allow_blank=True, default="")
    group_key = serializers.CharField(
        required=False, allow_blank=True, default="")


class PaperCreateSerializer(serializers.Serializer):
    """新建试卷输入。"""

    course_id = serializers.IntegerField()
    title = serializers.CharField(max_length=200)
    purpose = serializers.ChoiceField(
        choices=[c[0] for c in Paper._meta.get_field("purpose").choices],
        required=False, default="QUIZ")
    compose_mode = serializers.ChoiceField(
        choices=[c[0] for c in Paper._meta.get_field("compose_mode").choices],
        required=False, default="MANUAL")
    compose_config = serializers.JSONField(required=False, default=dict)
    difficulty_expect = serializers.FloatField(
        required=False, allow_null=True, min_value=1.0, max_value=5.0)
    layout_config = serializers.JSONField(required=False, default=dict)
    # ★★ `random_seed` 必须能被外部钉死。
    #
    #   12 文档 TC-PAP-107 / A11 是 P0 验收项：
    #   「同一 seed + 同一候选池两次组卷，题目顺序、选项顺序、
    #     `paper_item.seq` 完全一致」。
    #
    #   不暴露这个字段的话，种子只能由服务端随机生成——
    #   验收时**无法构造「同一 seed」这个前提**，
    #   那项就只能靠「信任服务端」通过，而不是真的测过。
    random_seed = serializers.IntegerField(
        required=False, allow_null=True, min_value=0,
        max_value=2**63 - 1)


class PaperComposeSerializer(serializers.Serializer):
    """规则组卷输入。

    ★ 只有两个参数，且都是「怎么执行」而不是「组什么」——
      组什么由试卷自己的 `compose_config` 决定。
      它是**快照**：审计里要能看出「当时用哪份配置组出了这份卷」。
    """

    #: 候选池上限。默认 2 万，与 12 文档 TC-SYS-105 的性能场景一致。
    candidate_limit = serializers.IntegerField(
        required=False, default=20000, min_value=1, max_value=200000)
    #: `True` 时缺口只记警告不抛错（用于「试组卷」看效果）
    allow_partial = serializers.BooleanField(required=False, default=False)


class PaperUpdateSerializer(serializers.Serializer):
    """更新试卷输入（只允许改元信息与抽题规则，不改卷面题）。

    ★★ `compose_config` 必须在列表里。

      原来这里没有它，而视图也没有对应参数 —— 于是
      `PATCH {"compose_config": ...}` 返回 **200 且静默忽略**：
      老师改了配置、点组卷、拿到旧配置的结果，界面还显示「已保存」。

      ★ 教训：凡是「前端会传」的字段，后端要么处理、要么**明确报错**
        （`build_unknown_field`）。静默忽略是最坏的一种——
        DRF 的 `Serializer` 默认就会忽略未声明字段，
        所以这里需要靠 code review / 测试来兜。
    """

    title = serializers.CharField(required=False, max_length=200)
    purpose = serializers.ChoiceField(
        choices=[c[0] for c in Paper._meta.get_field("purpose").choices],
        required=False)
    difficulty_expect = serializers.FloatField(
        required=False, allow_null=True, min_value=1.0, max_value=5.0)
    layout_config = serializers.JSONField(required=False)
    # ★ 抽题规则快照。定稿后不可改（服务层拦，理由见 `update_paper`）。
    compose_config = serializers.JSONField(required=False)


class PaperAddItemsSerializer(serializers.Serializer):
    """批量添加卷面题输入。"""

    items = PaperItemInputSerializer(many=True)
    section_title = serializers.CharField(
        required=False, allow_blank=True, default="")
    #: 是否允许加入未审核的题（**仅供草稿试排**）
    allow_unapproved = serializers.BooleanField(required=False, default=False)


class PaperFinalizeSerializer(serializers.Serializer):
    """定稿输入。"""

    #: 忽略非阻断告警（阻断项仍会拒绝）
    force = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class PaperTargetTotalSerializer(serializers.Serializer):
    """设「期望总分」输入（`P1-58b`）。

    ★ 允许 `null` —— 表示**清空**（取消总分校验）。
      与「设为 0」不同：0 表示「要求卷面总分为 0」。
      `allow_null=True` 是必要的，否则前端无法「取消设定」。
    """

    target_total_score = serializers.DecimalField(
        max_digits=6, decimal_places=2,
        required=False, allow_null=True, min_value=Decimal("0"),
    )

    def validate_target_total_score(self, v):
        """★ 上限校验：一张卷子超过 1000 分几乎一定是误输入。

        ★ 下界用 `min_value=0`（字段级）而不是「必须 > 0」：
          0 是一个**合法**的目标（要求卷面总分为 0，用于空卷模板）。
        """
        if v is not None and v > Decimal("1000"):
            raise serializers.ValidationError(
                "期望总分不能超过 1000（当前卷子实算总分通常不超过 300）")
        return v


class PaperApplyAllocationSerializer(serializers.Serializer):
    """按建议写入配分输入（`P1-58b`）。

    `scores` 是 `{卷面题号: 分值}`，键用 `seq` ——
    老师看到的是「第 3 题给 5 分」，不是内部 id。
    """

    scores = serializers.DictField(
        child=serializers.DecimalField(
            max_digits=6, decimal_places=2, min_value=Decimal("0.01"),
        ),
        allow_empty=False,
    )

    def validate_scores(self, v):
        """★ 键必须是**正整数字符串**（卷面题号）。

        ★ 为什么在这里校验键：`DictField` 只校验**值**，
          键原样传下来。若前端误传 `paper_item_id` 或空键，
          会到服务层才报「卷面没有第 X 题」——
          而那时错误信息已经指不到「你传错了什么」。
        """
        out: dict[int, Decimal] = {}
        for k, val in v.items():
            try:
                seq = int(k)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    f"`scores` 的键必须是卷面题号（整数），收到 {k!r}")
            if seq <= 0:
                raise serializers.ValidationError(
                    f"卷面题号必须为正整数，收到 {seq}")
            out[seq] = val
        return out


class PaperDuplicateSerializer(serializers.Serializer):
    """派生 AB 卷 / 补考卷输入。"""

    variant_label = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=10)
    title = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)
    share_group = serializers.BooleanField(required=False, default=True)


class EquivalenceCheckSerializer(serializers.Serializer):
    """等值检查输入。"""

    paper_a_id = serializers.IntegerField()
    paper_b_id = serializers.IntegerField()
