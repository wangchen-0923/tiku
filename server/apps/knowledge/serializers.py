"""课程、章、任务、知识点树、知识库 · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 我第一版这里凭直觉写了 `subject` / `part` / `difficulty_expect` /
  `source_ref` 这些字段，与真实模型对不上的有六处：

    `Course`          `subject` **不存在**；有 `grade_band` / `level`
                      / `textbook` / `sort_order` / `status` / `is_template`
    `Chapter`         是 `part_seq` + `part_name`（**两个**字段），
                      不是 `part`；还有 `path` / `textbook` / `volume`
    `KnowledgeNode`   **没有** `difficulty_expect`；有 `sort_order` /
                      `is_template`
    `KnowledgeEntry`  **没有** `is_adopted`；有 `entry_type` /
                      `adopted_count` / `embedding_model`
    `NodeBrief`       字段是 `entry_count`，不是 `kb_entry_count`
    `CourseTree`      字段是 `chapters`（列表），不是各层计数

  ★ 更糟的是：这些错**不会报错**。我写的是普通 `Serializer` +
  `SerializerMethodField`，`get_kb_sufficient()` 里 `getattr(obj,
  "kb_entry_count", None)` 带默认值 → 静默返回 `False`，
  于是接口「成功」而字段永远是错的。
  只有把字段列进 `ModelSerializer.Meta.fields` 时 DRF 才会拦。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.common.constants import KnowledgeEntryStatus
from apps.knowledge.models import (
    Chapter,
    Course,
    KnowledgeEntry,
    KnowledgeNode,
    Task,
)


class _StrictFieldsMixin:
    """**拒绝未声明字段**，而不是静默丢掉。

    ★★ 这一条是必须的，不是洁癖：

      DRF 的 `Serializer.to_internal_value()` 只遍历**自己声明的字段**，
      请求体里多出来的键**不会报错，直接消失**。于是：

          老师提交 `{"name": "新名", "org_id": 2}`
          → 服务层根本收不到 `org_id`（被序列化器吃了）
          → 返回 200「已更新」
          → 老师以为跨机构搬过去了 / 以为改成功了

      这个代码库里已经被同一机制坑过一次：`EntryCreateSerializer`
      漏声明 `node_ids`，导致多知识点条目**静默只挂一个**
      （见该类的 docstring，写得很长就是因为那次踩得狠）。

      所以这里的口径是：**多传的键就是 400**，让调用方当场知道
      「这个字段我改不了」，而不是回去发现数据没变。
    """

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({
                    "detail": (
                        f"不支持修改这些字段：{'、'.join(unknown)}。"
                        f"可用字段：{'、'.join(sorted(self.fields))}"
                    ),
                })
        return super().to_internal_value(data)


class CourseSerializer(serializers.ModelSerializer):
    """课程输出。"""

    major_name = serializers.CharField(
        source="major.name", read_only=True, default="")
    chapter_count = serializers.IntegerField(read_only=True, default=0)
    task_count = serializers.IntegerField(read_only=True, default=0)
    node_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Course
        fields = [
            "id", "name", "code", "grade_band", "level",
            "textbook", "total_hours", "sort_order", "is_template",
            "status", "major_id", "major_name",
            "chapter_count", "task_count", "node_count",
            "created_at", "updated_at",
        ]


class ChapterSerializer(serializers.ModelSerializer):
    """章输出。"""

    course_name = serializers.CharField(
        source="course.name", read_only=True, default="")
    task_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Chapter
        fields = [
            "id", "name", "code", "seq", "part_seq", "part_name",
            "course_id", "course_name", "path", "textbook", "volume",
            "status", "task_count", "created_at", "updated_at",
        ]


class TaskSerializer(serializers.ModelSerializer):
    """任务输出。"""

    chapter_name = serializers.CharField(
        source="chapter.name", read_only=True, default="")
    node_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Task
        fields = [
            "id", "name", "code", "seq", "chapter_id", "chapter_name",
            "course_id", "path", "is_practical", "objectives",
            "status", "node_count", "created_at", "updated_at",
        ]


class KnowledgeNodeSerializer(serializers.ModelSerializer):
    """知识点输出。

    `entry_count` / `kb_sufficient` 是 L1 闸门的可见面
    （见模块 docstring）。
    """

    task_name = serializers.CharField(
        source="task.name", read_only=True, default="")
    #: 知识库**生效**条目数（由视图或 Selector 注解）
    entry_count = serializers.IntegerField(read_only=True, default=0)
    kb_sufficient = serializers.SerializerMethodField()
    question_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = KnowledgeNode
        fields = [
            "id", "name", "code", "path", "level", "seq_in_task",
            "sort_order", "course_id", "task_id", "task_name",
            "exam_weight", "is_template", "status",
            "entry_count", "kb_sufficient", "question_count",
            "created_at", "updated_at",
        ]

    def get_kb_sufficient(self, obj) -> bool:
        """知识库条数是否够 AI 生题。

        ★ 阈值来自配置（`ai.kb_min_entries`），不写死在序列化器里——
          写死会让「调阈值」变成一次发版。

        ★★ 第一版这里读的是 `obj.kb_entry_count`——**字段名错了**
          （真实是 `entry_count`），而 `getattr(..., None)` 带默认值
          让它静默返回 `False`。于是每个知识点都显示「知识库不足」，
          而老师会去补一堆本来已经够的内容。
        """
        from apps.common.config import get_config

        # ★★ 键名是 `ai.kb_min_hits_for_generate`，**不是**
        #   `ai.kb_min_entries`——后者未注册，`get_config` 会
        #   直接抛 `ConfigError`，把「打开知识点列表」变成 500。
        #
        #   `get_config` 对未注册键**抛错**是刻意的（静默用默认值
        #   会让「配置没生效」永远发现不了），代价是键名写错时
        #   报错位置离出错点很远。
        need = int(get_config("ai.kb_min_hits_for_generate"))
        return int(getattr(obj, "entry_count", 0) or 0) >= need


class KnowledgeEntrySerializer(serializers.ModelSerializer):
    """知识库条目输出。

    ★★ `KnowledgeEntry` **没有** `knowledge_node` 外键——知识点
      关联走中间表 `entry_links`（多对多）。所以这里给
      `node_ids`（列表）而不是 `node_name` 单值。

      我第一版写了 `source="knowledge_node.name"`，DRF 报
      `The field 'node_name' was declared ... but has not been
      included in the 'fields' option`——那个报错信息误导性很强
      （看起来像漏列字段，实际是关系不存在）。
    """

    node_ids = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeEntry
        fields = [
            "id", "title", "content", "entry_type", "source_type",
            "source_ref", "course_id", "chapter_id",
            "node_ids", "status", "adopted_count",
            "created_at", "updated_at",
        ]

    def get_node_ids(self, obj) -> list[int]:
        """本条链到的知识点 id 列表。"""
        return [
            link.node_id for link in obj.entry_links.all()
            if link.node_id
        ]


# ══════════════════════════════════════════════════════════════
#  输入
# ══════════════════════════════════════════════════════════════


class ChapterCreateSerializer(serializers.Serializer):
    """建章输入。

    ★ `part_seq` + `part_name` 是**两个**字段（篇号与篇名）——
      用 `part_seq` 编号（如 `第一篇`）而 `part_name` 存标题。
      我第一版写成一个 `part`，那是凭直觉。
    """

    course_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    seq = serializers.IntegerField(min_value=1, max_value=99)
    part_seq = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=99)
    part_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=60)
    #: 不传则由 `KnowledgeCodeService` 按 `seq` 生成
    code = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=60)


class TaskCreateSerializer(serializers.Serializer):
    """建任务输入。"""

    chapter_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    seq = serializers.IntegerField(min_value=1, max_value=99)
    is_practical = serializers.BooleanField(required=False, default=False)
    objectives = serializers.ListField(
        child=serializers.CharField(max_length=200),
        required=False, default=list)


class NodeCreateSerializer(_StrictFieldsMixin, serializers.Serializer):
    """建知识点输入。

    ★ `seq_in_task` 决定编码里的 `K` 序号（`X.Y.K`），
      所以它必须显式给——自动分配会让「插一个知识点」
      把后面所有知识点的**编码**都改掉，而那些编码已经被
      题目引用、被老师写在教案里了。
    """

    task_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    seq_in_task = serializers.IntegerField(min_value=1, max_value=99)
    exam_weight = serializers.FloatField(
        required=False, default=1.0, min_value=0.0, max_value=10.0)


# ══════════════════════════════════════════════════════════════
#  ★ 课程与知识点的写操作（U08 / U09，2026-10-21）
# ══════════════════════════════════════════════════════════════
#
# ★★ 为什么全部用 `partial` 语义 + 显式白名单，而不是 `ModelSerializer`：
#
#   `ModelSerializer` 会把模型上所有可写字段都放进来，于是
#   `org_id` / `is_template` / `deleted_at` 这些字段一旦被传进来就
#   直接落库 —— 改机构等于跨租户搬家，改 `deleted_at` 等于绕过删除流程。
#   白名单在服务层再加一道（`COURSE_WRITABLE` / `NODE_WRITABLE`），
#   序列化器这一道只是让报错更早、更明确。


class CourseCreateSerializer(_StrictFieldsMixin, serializers.Serializer):
    """建课程输入。

    ★ `code` 必填：它是全部知识点编码的前缀，不能从中文课程名猜拼音。
    ★ `major_id` 传 0 或 null 表示不挂专业（K12 场景）。
    """

    name = serializers.CharField(max_length=200)
    code = serializers.CharField(max_length=80)
    major_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    grade_band = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    level = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    textbook = serializers.JSONField(required=False, default=dict)
    total_hours = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=0, max_value=2000)
    style_config = serializers.JSONField(required=False, default=dict)
    sort_order = serializers.IntegerField(required=False, default=0)


class CourseUpdateSerializer(_StrictFieldsMixin, serializers.Serializer):
    """改课程输入（全部可选，`partial` 语义）。

    ★ `code` 允许出现但**未必允许生效**：课程已长出章/题/卷时，
      服务层会拒（409 `KNOWLEDGE.CODE_LOCKED`）并说明被什么卡住。
      在这里直接禁掉反而是错的——空课程改编码是完全合理的动作。
    """

    name = serializers.CharField(required=False, max_length=200)
    code = serializers.CharField(required=False, max_length=80)
    major_id = serializers.IntegerField(required=False, allow_null=True)
    grade_band = serializers.CharField(required=False, max_length=20)
    level = serializers.CharField(required=False, max_length=20)
    textbook = serializers.JSONField(required=False)
    total_hours = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=2000)
    style_config = serializers.JSONField(required=False)
    sort_order = serializers.IntegerField(required=False)
    status = serializers.CharField(required=False, max_length=20)


class NodeUpdateSerializer(_StrictFieldsMixin, serializers.Serializer):
    """改知识点 / 移动层级输入（全部可选）。

    ★★ `task_id` 就是「移动」：换任务 = 换章（甚至换课程）。
       服务层会把 `course` / `subject` / `grade_band` / `path` 四个冗余列
       一起改，但**不改 `code`**——编码是稳定标识，不是位置描述。

    ★ `deprecated` 是布尔开关，不是 `deprecated_at` 时间戳：
      让前端传时间戳是在要求它知道后端用哪个时区。
    """

    name = serializers.CharField(required=False, max_length=200)
    code = serializers.CharField(required=False, max_length=80)
    seq_in_task = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=99)
    sort_order = serializers.IntegerField(required=False)
    exam_weight = serializers.FloatField(
        required=False, min_value=0.0, max_value=9.99)
    status = serializers.CharField(required=False, max_length=20)
    task_id = serializers.IntegerField(required=False)
    deprecated = serializers.BooleanField(required=False)


class PrerequisiteSerializer(serializers.Serializer):
    """加前置知识点输入。"""

    node_id = serializers.IntegerField()
    prerequisite_id = serializers.IntegerField()
    weight = serializers.FloatField(
        required=False, default=1.0, min_value=0.0, max_value=1.0)


class EntryCreateSerializer(serializers.Serializer):
    """建知识库条目输入。

    ★ `entry_type` 与 `source_type` 是两个不同的维度：
      前者是「这条内容是什么」（概念/例题/常见错误），
      后者是「它从哪来」（教师录入/教材摘录/AI 生成）。

    ★★ 知识点关联有**两种写法**（这是本条接口唯一容易写错的地方）：

        `node_ids`: [1, 2, 3]   多知识点（列表，**推荐**）
        `node_id`:  1           单个知识点（**向后兼容**的旧写法）

      `KnowledgeEntry` **没有** `knowledge_node` 外键，关联走中间表
      `entry_links`（多对多），所以「一条条目挂多个知识点」是模型
      本来就支持的（`create_entry(node_ids=[...])` 一直收列表）。
      P1-18c 的「AI 拆条归类的批量建条」正是靠它一次挂多个知识点。

      ★★ 曾经这里**只声明了 `node_id`**，而视图读的是
        `v.get("node_ids") or [v["node_id"]]` —— DRF 对**未声明**字段是
        **静默忽略**，所以 `node_ids` 永远取不到，多知识点条目
        **在建条接口上根本做不了**（服务层与模型都支持列表）。
        实测（把序列化器换回旧字段集跑真库）的两种表现：

            只传 `node_ids=[A,B,C]`        → 400「必须至少关联一个知识点」
                                             ★ 误导：调用方明明给了知识点
            `node_id=A` + `node_ids=[A,B,C]` → **201，但 `entry_links` 只有 1 条**
                                             ★ 静默丢数据，最坏的一种

        所以这里显式声明它：让「挂了几个知识点」由**参数本身**决定，
        而不是由「哪个字段被静默丢了」决定。

    ★ 两件必须**显式拒绝**、不能静默忽略的事：

      ① `chapter_id`：本条接口**不接受**。`create_entry` 的签名里没有
         它——条目的 `course_id` / `chapter_id` 由第一个知识点的
         `task.chapter` **反推**。静默忽略会让调用方以为章节写进去了，
         而检索与健康度统计都按知识点走，这条数据就永远是错的。
      ② `node_ids` 与 `node_id` **同时传且不一致**：没有安全的优先级
         （按哪个都可能写错归属），所以要求 `node_id` 必须包含在
         `node_ids` 里（等价写法），否则 400。

    ★ 其余口径：

       - `node_ids` 里的**重复 id 去重**（`uq_kb_entry_node` 唯一约束下
         重复插入会直接 `IntegrityError`；去重后只建 1 条关联）；
       - 两者都不传 / 传空列表 → 400（不挂知识点的条目**检索不到**，
         等于没录）；
       - id 是否存在、是否属于本机构，由服务层校验（`create_entry`
         对无效 id 抛 400，**不会**留下悬空关联）。
    """

    #: ★ 多知识点关联（推荐写法；AI 拆条归类批量建条用这个）
    node_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        # ★ 空列表在**字段层**就拒：让「什么都没关联」的意图在
        #   进入业务层之前就被明确拒绝，且文案可照做。
        allow_empty=False,
        error_messages={
            "empty": "node_ids 不能是空列表：必须至少关联一个知识点"
                     "（不挂知识点的条目检索不到）",
        },
        help_text="知识点 id 列表；重复 id 自动去重",
    )
    #: ★ 向后兼容的单数写法（等价于 `node_ids=[node_id]`）
    node_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="单个知识点 id（旧写法，等价于 node_ids=[node_id]）",
    )
    #: ★ 显式声明只为了**报错**（不声明就会被 DRF 静默忽略）
    chapter_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="★ 不接受：章节归属由知识点反推",
    )
    title = serializers.CharField(max_length=200)
    content = serializers.CharField()
    entry_type = serializers.CharField(
        required=False, allow_blank=True, default="CONCEPT", max_length=30)
    source_type = serializers.CharField(
        required=False, allow_blank=True, default="TEACHER_INPUT",
        max_length=30)
    # ★★ 2026-10-17 补：`source_ref` 原来**没有声明**，而视图读的是
    #   `v.get("source_ref", "")` —— DRF 对**未声明字段是静默忽略**的，
    #   于是「AI 拆条 批次#12」这类来源标注传进来会被**悄悄丢掉**，
    #   接口返回 201、库里却是空串。与当初 `node_ids` 未声明是**同一个坑**。
    #
    #   ★ 同一个字段在两条入口上行为还不一致：`EntryUpdateSerializer`（PATCH）
    #     声明了它，所以 PATCH 能写进去 —— 「POST 丢、PATCH 不丢」。
    #     同一字段两种行为，比两处都丢更难发现。
    source_ref = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=300)

    def validate(self, attrs: dict) -> dict:
        """归一化成**去重后的 `node_ids`**，并拒绝上面那两种写法。

        ★ 归一化后的 `node_ids` 一定非空且唯一，视图可以直接
          `v["node_ids"]` 传给服务层（视图里也保留了 `node_id` 兜底，
          见 `EntryListCreateView.post`）。

        ★ `ValidationError` 传 **dict**（`{字段: [说明]}`），不传字符串：
          字符串会让 DRF 在 `is_valid(raise_exception=True)` 里
          构造 `ReturnDict` 失败 → `ValueError` → 500。
          `_map_drf_exception` 取 `next(iter(detail.values()))[0]` 当
          `message`，所以 dict 的**第一个键**决定老师看到哪句话。
        """
        errors: dict[str, list[str]] = {}

        # ── ① `chapter_id` 不接受 ──（先判，让提示优先指向根因）
        if attrs.get("chapter_id") is not None:
            errors["chapter_id"] = [
                "本接口不接受 chapter_id：条目的课程/章节归属由所关联的"
                "知识点反推（第一个知识点的 task.chapter）。"
                "请改用 node_ids / node_id 指定知识点。"
            ]

        # ── ② `node_ids` 与 `node_id` 归一化 ──
        raw_ids = [int(x) for x in (attrs.get("node_ids") or [])]
        single = attrs.get("node_id")
        if single is not None:
            single = int(single)
            if raw_ids and single not in raw_ids:
                errors["non_field_errors"] = [
                    f"node_id={single} 与 node_ids={raw_ids} 冲突："
                    f"同时传两者时 node_id 必须包含在 node_ids 里"
                    f"（等价写法）。请只传 node_ids。"
                ]
                errors["node_id"] = ["与 node_ids 不一致（不在列表里）"]
            elif not raw_ids:
                raw_ids = [single]

        # 保序去重：`uq_kb_entry_node` 唯一约束下重复 id 会 IntegrityError
        seen: set[int] = set()
        ids: list[int] = []
        for i in raw_ids:
            if i not in seen:
                seen.add(i)
                ids.append(i)

        # ── ③ 一个都没挂 → 拒绝（不静默建成无关联条目） ──
        if not ids and "non_field_errors" not in errors:
            errors["node_ids"] = [
                "必须至少关联一个知识点：传 node_ids（列表）或"
                " node_id（单个，向后兼容）。不挂知识点的条目检索不到。"
            ]

        if errors:
            raise serializers.ValidationError(errors)

        attrs["node_ids"] = ids
        return attrs


class EntryAdoptSerializer(serializers.Serializer):
    """采纳条目输入。"""

    entry_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False)


class EntryUpdateSerializer(serializers.Serializer):
    """**更新**知识库条目输入（`PATCH .../entries/{id}/`）。

    ★ `partial=True` 调用：只校验传了的字段，未传的字段不动
      （与 `create` 的「未传就落默认值」语义不同）。

    ★★ **只收可编辑字段**。下面这些**刻意不在这里**，传了就是 400
      `Unexpected field`（DRF 对 `Serializer` 的既成行为）：

        `id` / `course_id` / `chapter_id`   —— 外键归属，由知识点反推
        `org_id`                            —— 跨机构改数据
        `adopted_count` / `embedding`       —— 统计与派生字段
        `created_by` / `created_at` / `updated_at` / `deleted_at`

      ★ 用**显式白名单**而不是 `ModelSerializer(partial=True)`：
        `ModelSerializer` 遇到未声明的字段是**静默忽略**（改不了也不报错），
        而这条接口的调用方（条目编辑器）需要知道「这个字段不能改」。
        实测过一次「PATCH 返回 200 但标题没变」就是这类静默造成的。

    ★ `node_ids` 传了就**整体替换**关联（不传则保持原样）；
      替换后为空会被服务层拒绝——不挂知识点的条目检索不到，等于删了一半。
    """

    title = serializers.CharField(
        required=False, allow_blank=True, max_length=300)
    content = serializers.CharField(required=False, allow_blank=False)
    entry_type = serializers.CharField(required=False, max_length=20)
    source_type = serializers.CharField(required=False, max_length=20)
    source_ref = serializers.CharField(
        required=False, allow_blank=True, max_length=300)
    status = serializers.ChoiceField(
        required=False,
        choices=[c.value for c in KnowledgeEntryStatus],
        # ★ 默认只到 `ARCHIVED`：条目有独立、语义明确的删除接口
        #   （软删 + 审计），不该从这条通用更新路径进去。
        help_text="DRAFT / ACTIVE / ARCHIVED（删除请用 DELETE）",
    )
    #: 整体替换知识点关联
    node_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=False)

    def to_internal_value(self, data):
        """★ 未声明字段**报错**，不静默丢弃。

        ★★ 两点都是实测踩出来的：

        ① **不能指望 DRF 自己拦**。DRF 的 `Serializer` 对**未声明**的
           字段是**静默忽略**：`{"course_id": 999}` 会被滤成 `{}`，
           于是服务层只看到「没有需要更新的字段」，调用方拿到的是
           **另一条**错误信息。更坏的是 `{"title": "x", "course_id":
           999}` 会返回 **200 且 course_id 没变**——调用方以为改成功了。

        ② **`ValidationError` 必须传 dict，不能传 str**。
           我原来写 `raise serializers.ValidationError("...")`，DRF 在
           `is_valid(raise_exception=True)` 里会 `raise ValidationError(
           self.errors)`，而 `self.errors` 要把一个**字符串**当成
           `ReturnDict` 的初始化序列 → `ValueError: dictionary update
           sequence element #0 has length 48; 2 is required` →
           **500「服务内部错误」**。

           ★ 这条最坑的地方是：它只在「同时传了未声明字段」时才出现，
             而报错是个看不出与本处有关的 `ValueError`。
             正确写法是 `{字段名: 说明}`（DRF 的标准字段错误结构）。

        ★ 用 `non_field_errors` 承载**整句话**：项目的
          `_map_drf_exception` 取 `next(iter(detail.values()))[0]`
          作为 `message`，只有整句话才让老师看到「到底哪几个字段不能改」。
        """
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({
                    "non_field_errors": [
                        "以下字段不可修改（外键归属与统计字段由系统维护）："
                        + "、".join(unknown)
                    ],
                    **{k: ["该字段不可修改"] for k in unknown},
                })
        return super().to_internal_value(data)


# ══════════════════════════════════════════════════════════════
#  文档导入 · 文本抽取（需求 3 / `P1-18b` 接线）
# ══════════════════════════════════════════════════════════════


class KnowledgeImportExtractSerializer(serializers.Serializer):
    """`POST /api/v1/knowledge/import/extract/` 的上传输入。

    ★ **只收一个** `file`：多文件由导入向导在客户端**逐个**调用本端点。
      不在这里发明「一次多文件」——那会立刻带出「第 3 个失败时前 2 个
      算成功吗」的部分失败语义，而 `P1-18c` 的拆条本来就是逐文档进行的。

    ★★ `allow_empty_file=True` 是**刻意**的：

      空文件（0 字节）的判定留在**抽取层**（`extract.extract_document`）。
      它的文案、`extra`（`filename` / `byte_size`）与「先判后缀再判内容、
      最后判空」的顺序，已有 39 项回归覆盖；在这里再判一次，
      就会出现两套「空文件」文案，迟早不一致。

      同理，**扩展名与内容（魔数）的比对、大小上限都不在这里**：
      唯一真源是 `apps.knowledge.extract`。

    ★ 这里只管一件事：**`file` 在不在、是不是一个文件**。
      这两条若不显式给文案，DRF 的默认提示是「没有提交文件。」与
      「提交的数据不是文件。请检查表单上的编码类型。」—— 后者会让一个
      只想传文档的老师去「检查表单编码类型」，等于没说。
    """

    file = serializers.FileField(
        allow_empty_file=True,
        error_messages={
            "required": (
                "缺少 file 字段：请用 multipart/form-data 上传一个文件"
                "（字段名 file），支持后缀 .docx / .pdf / .txt"
            ),
            "invalid": (
                "file 字段不是一个文件：本接口只收 multipart/form-data 的"
                "文件上传（不要用 JSON 传文件名、路径或 base64 内容）"
            ),
            "no_name": (
                "file 字段没有文件名：无法判定格式（抽取层按"
                "「扩展名 + 魔数」双重判定，两者都要）"
            ),
            "empty": (
                "file 字段是空的：请确认真的选中了一个文件再上传"
            ),
        },
    )


# ══════════════════════════════════════════════════════════════
#  文档导入 · AI 拆条归类（需求 3 / `P1-18c`）
# ══════════════════════════════════════════════════════════════


class KnowledgeImportParseSerializer(serializers.Serializer):
    """`POST /api/v1/knowledge/import/parse/` 的输入。

    ★★ 为什么收 `text` 而不是 `file`（**成本口径**）：

      「不能把整份文档的 AI 费用花在老师没看过文本之前」。
      收文件的端点（`/import/extract/`）是**免费**的；一旦这个端点也收文件，
      选错文件 / 扫描件 PDF / 旧教材都会在老师看清内容之前触发几十次模型调用。
      所以流程被切成两步：**先 extract（免费、看见文本）→ 再 parse（花钱）**。
      ★ 附带好处：`parse` 是输入的纯函数，候选可复现、可离线验证。

    ★ 这里**只判「字段在不在、类型对不对」**：长度上限（块数、字符数）
      与知识点过滤都在服务层（`import_service`）——
      唯一真源放一处，免得出现两套「太长」的文案。
    """

    text = serializers.CharField(
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": (
                "缺少 text 字段：本端点收**已抽取的文本**，"
                "请先用 POST /api/v1/knowledge/import/extract/ 上传文档拿到 text，"
                "确认内容无误后再调用本端点"
            ),
            "blank": (
                "text 不能为空：请先上传文档抽取文本"
                "（本端点不接受文件，只接受文本）"
            ),
            "invalid": "text 必须是字符串",
        },
    )
    filename = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255,
        error_messages={
            "max_length": "filename 最长 255 个字符（只用于溯源与回显）",
        },
    )
    node_hints = serializers.ListField(
        child=serializers.IntegerField(
            error_messages={"invalid": "node_hints 的元素必须是知识点 id（整数）"},
        ),
        required=False, default=list,
        error_messages={
            "not_a_list": "node_hints 必须是知识点 id 的数组",
        },
    )
    batch_ref = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200,
        error_messages={
            "max_length": "batch_ref 最长 200 个字符",
        },
    )

    def validate_node_hints(self, value: list) -> list:
        """去重保序 + 上限。

        ★ 上限放在这里而不是服务层：它纯粹是**请求体积**的护栏
          （提示词长度），与「知识点是否存在」是两件事，
          后者必须查库、只能在服务层做。
        """
        from apps.knowledge.import_service import MAX_NODE_HINTS

        out: list[int] = []
        for v in value:
            if v not in out:
                out.append(v)
        if len(out) > MAX_NODE_HINTS:
            raise serializers.ValidationError(
                f"node_hints 最多 {MAX_NODE_HINTS} 个知识点，收到 {len(out)} 个"
                f"（提示词长度与成本的护栏；请缩小到相关章节的知识点）"
            )
        return out


class KnowledgeImportCommitSerializer(serializers.Serializer):
    """`POST /api/v1/knowledge/import/commit/` 的输入。

    ★★ 刻意收得**很松**（`candidates` 只要求「是对象数组」）：

      逐条校验属于**服务层**，失败的那条进 `failed` 桶。
      若在这里做字段级校验，一条候选写错就会 `400` 打断整批——
      而老师点「确认入库」时最需要的是「这 20 条里哪 3 条没成、为什么」，
      不是「整批都没进去，请你自己找哪条错」。
      （单条录入端点 `POST /entries/` 用「整请求 400」是**对**的，
       因为那里一次只有一条；口径不同，不是不一致。）
    """

    candidates = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        error_messages={
            "required": (
                "缺少 candidates 字段：请把 parse 返回（并已由老师确认）的"
                "候选条目数组原样提交"
            ),
            "empty": "candidates 不能为空：没有要入库的候选条目",
            "not_a_list": "candidates 必须是数组",
        },
    )
    batch_ref = serializers.CharField(
        max_length=200,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": (
                "缺少 batch_ref 字段：它是这一批的批次标识，"
                "用于逐条审计与对账（parse 的响应里原样回显了它）"
            ),
            "blank": "batch_ref 不能为空",
            "max_length": "batch_ref 最长 200 个字符",
        },
    )
