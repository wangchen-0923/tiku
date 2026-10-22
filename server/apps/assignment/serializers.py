"""发布任务、逐生乱序映射 · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 本模块最需要小心的一件事：**学生端序列化绝不能带答案**。

  教师端与学生端共用同一批模型，最容易的写法是
  「一个 `AssignmentDetailSerializer` 到处用」——
  那样学生端拿到的 JSON 里就带着 `answer`，
  而任何一个学生按 F12 就能看到全部答案。

  所以这里把两者**物理分开**：

    `AssignmentDetailSerializer`   教师端（含乱序映射、统计、学生名单）
    `StudentPaperSerializer`       学生端（**题面 + 选项，绝无答案**）

  两者的字段集合是**显式列举**的，不共用基类——
  共用基类意味着将来给教师端加字段时会自动泄漏到学生端。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.assignment.models import Assignment, AssignmentStudent


# ══════════════════════════════════════════════════════════════
#  教师端
# ══════════════════════════════════════════════════════════════


class AssignmentListSerializer(serializers.ModelSerializer):
    """任务列表（轻量）。"""

    classroom_name = serializers.CharField(
        source="classroom.name", read_only=True, default="")
    paper_title = serializers.CharField(
        source="paper.title", read_only=True, default="")
    sheet_template_name = serializers.CharField(
        source="sheet_template.name", read_only=True, default="")
    sheet_template_version = serializers.IntegerField(
        source="sheet_template.version", read_only=True, default=0)
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = [
            "id", "uuid", "title", "status", "status_label",
            "classroom_id", "classroom_name",
            "paper_id", "paper_title", "sheet_template_id",
            "sheet_template_name", "sheet_template_version",
            "start_at", "end_at", "published_at",
            "shuffle_questions", "shuffle_options",
            "answer_visibility", "score_visibility",
            "allow_late", "pass_ratio", "auto_practice",
            "auto_grade_on_submit",
            "created_at",
        ]

    def get_status_label(self, obj) -> str:
        from apps.common.labels import unknown_label

        return {
            "DRAFT": "草稿", "PUBLISHED": "已发布",
            "CLOSED": "已关闭", "ARCHIVED": "已归档",
        }.get(obj.status, unknown_label(obj.status))


class AssignmentDetailSerializer(serializers.ModelSerializer):
    """教师端任务详情。

    ★ 含 `publish_token`（扫码入口链接要用），
      所以**这个序列化器绝不能用于学生端接口**。
    """

    classroom_name = serializers.CharField(
        source="classroom.name", read_only=True, default="")
    paper_title = serializers.CharField(
        source="paper.title", read_only=True, default="")
    sheet_template_name = serializers.CharField(
        source="sheet_template.name", read_only=True, default="")
    sheet_template_version = serializers.IntegerField(
        source="sheet_template.version", read_only=True, default=0)
    sheet_validation = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = [
            "id", "uuid", "title", "status", "status_label",
            "classroom_id", "classroom_name",
            "paper_id", "paper_title", "sheet_template_id",
            "sheet_template_name", "sheet_template_version",
            "sheet_validation",
            "start_at", "end_at", "published_at",
            "shuffle_questions", "shuffle_options",
            "answer_visibility", "score_visibility",
            "allow_late", "pass_ratio", "auto_practice",
            "auto_grade_on_submit",
            "publish_token",
            "created_at", "updated_at",
        ]

    def get_status_label(self, obj) -> str:
        from apps.common.labels import unknown_label

        return {
            "DRAFT": "草稿", "PUBLISHED": "已发布",
            "CLOSED": "已关闭", "ARCHIVED": "已归档",
        }.get(obj.status, unknown_label(obj.status))

    def get_sheet_validation(self, obj):
        """实时校验结果（未绑卡时为 None）。

        ★ 详情页要显示「这张卡能不能用于这份卷」，所以直接复用
          `BindingService.validate`，不在前端另写一套判据。
        """
        if not obj.sheet_template_id:
            return None
        from apps.sheet.services import BindingService

        try:
            return BindingService.validate(
                org_id=obj.org_id, paper_id=obj.paper_id,
                template_id=obj.sheet_template_id)
        except Exception:      # noqa: BLE001 —— 详情不因校验失败而 500
            return {"ok": False, "blocking": [
                {"seq": None, "code": "VALIDATE_ERROR",
                 "message": "校验失败，请检查答题卡状态"}]}


class AssignmentStudentSerializer(serializers.ModelSerializer):
    """名单条目（教师端）。

    ★★ 学号与姓名取 **`assignment_student` 上的身份快照**，不取账号：

      ① **没绑账号的学生也必须显示学号姓名**。原来姓名取
         `student.real_name`（账号为空 → 永远空串）、学号取名册表里
         按 `student_id` 建的映射（未绑账号的学生被 `if sid` 过滤掉）
         —— 结果是**整列空白**：老师看不到名单，学生也进不去
         （2026-10-16 实测的线上阻塞）。
      ② 快照是**发布时冻结**的，账号改名/学生转班都不影响已发布任务的历史。

    ★ **不返回** `question_order` / `option_order` 全文：
      一份 25 题的任务 × 45 人 = 上千条映射，列表接口带上它
      会让响应体到几 MB，而老师根本不需要逐条看。
      需要核对走 `/{id}/students/{sid}/` 单生详情。
    """

    #: 该生是否已绑**登录凭据**（能自己登录看成绩）。
    #: ★ 与「名单里有没有他」是两件事：一期所有人都能答题，
    #:   但不是所有人都能登录（见 `accounts.StudentIdentityService`）。
    student_login_bound = serializers.SerializerMethodField()
    #: 是否已写入乱序映射（发布后才有）
    has_mapping = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = AssignmentStudent
        fields = [
            "id", "student_id", "student_no", "student_name",
            "student_login_bound",
            "status", "status_label", "entered_at", "submitted_at",
            "has_mapping",
        ]

    def get_status_label(self, obj) -> str:
        from apps.common.labels import student_status_label

        return student_status_label(obj.status)

    def get_has_mapping(self, obj) -> bool:
        """是否已有乱序映射。

        ★ 这个标记对老师很重要：**没映射 = 任务没发布成功或漏了这个学生**。
          没有它的话，老师只能看到「名单里有 45 人」，
          却不知道有 3 人没有映射（他们答题时会被判越界）。
        """
        return bool(obj.question_order)

    def get_student_login_bound(self, obj) -> bool:
        """有没有登录凭据（与 `User.can_login` 同一判据）。"""
        student = obj.student
        return bool(student is not None and student.can_login)


class AssignmentStudentDetailSerializer(AssignmentStudentSerializer):
    """单生详情（含乱序映射，供教师核对）。"""

    class Meta(AssignmentStudentSerializer.Meta):
        fields = AssignmentStudentSerializer.Meta.fields + [
            "question_order", "option_order", "device_fingerprint",
            "session_token",
        ]


# ══════════════════════════════════════════════════════════════
#  输入（教师端）
# ══════════════════════════════════════════════════════════════


class AssignmentCreateSerializer(serializers.Serializer):
    """新建任务输入。"""

    paper_id = serializers.IntegerField()
    classroom_id = serializers.IntegerField()
    title = serializers.CharField(max_length=200)
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    # ★ 2026-10-19：新建作业不再选答题卡；绑定只在作业详情完成。
    shuffle_questions = serializers.BooleanField(required=False, default=False)
    shuffle_options = serializers.BooleanField(required=False, default=False)
    answer_visibility = serializers.ChoiceField(
        choices=[c[0] for c in
                 Assignment._meta.get_field("answer_visibility").choices],
        required=False, default="AFTER_GRADE")
    score_visibility = serializers.ChoiceField(
        choices=[c[0] for c in
                 Assignment._meta.get_field("score_visibility").choices],
        required=False, default="TOTAL")
    allow_late = serializers.BooleanField(required=False, default=False)
    pass_ratio = serializers.FloatField(
        required=False, default=0.6, min_value=0.0, max_value=1.0)
    auto_practice = serializers.BooleanField(required=False, default=True)
    #: ★ 学生一交卷就自动批改他这一份（2026-10-17 新增）
    auto_grade_on_submit = serializers.BooleanField(
        required=False, default=False)

    def validate(self, attrs):
        """只做**形状**校验：时间窗顺序。

        ★ `start_at >= end_at` 在这里就拦，因为它是**表单级**错误——
          让老师立刻看到「结束时间必须晚于开始时间」比
          服务层报一个 `COMMON.VALIDATION_ERROR` 更直接。
          但「试卷是否已定稿」这类需要查库的规则仍留给服务层。
        """
        if attrs.get("start_at") and attrs.get("end_at"):
            if attrs["start_at"] >= attrs["end_at"]:
                raise serializers.ValidationError(
                    {"end_at": "结束时间必须晚于开始时间（时间窗左闭右开）"}
                )
        return attrs


class AssignmentPublishSerializer(serializers.Serializer):
    """发布输入。"""

    #: 不传则取班级全部在读学生
    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True)


class AssignmentUpdateSerializer(serializers.Serializer):
    """作业设置修改（`PATCH /api/v1/assignments/{id}/`）。

    ★★ 只允许**非结构性**的开关与时间窗：

      | 允许改 | 为什么 |
      | --- | --- |
      | `auto_grade_on_submit` | 需求方口径：「设置作业详情时可勾选交卷自动批改」 |
      | `answer_visibility` / `score_visibility` | 学生能看到什么，随时可调 |
      | `allow_late` / `pass_ratio` / `auto_practice` | 判分与练习的口径开关 |
      | `start_at` / `end_at` | 延长作答时间是最常见的真实需求 |

    ★ **不允许改**（刻意排除）：

      · 已发布/已绑卡的 `shuffle_questions` / `shuffle_options` ——
        乱序在发布时已逐生冻结进 `assignment_student`；
        但**草稿且未绑卡**时允许改（否则老师无法在绑定答题卡前关闭乱序）。
      · `paper_id` / `classroom_id` / `channel` —— 换卷、换班、换通道
        等于换一场考试，应当**重建任务**而不是改这一条。

    ★ 已 `CLOSED` / `ARCHIVED` 的任务不允许再改：成绩已定稿，
      此时改设置无法自洽（服务层拦）。
    """

    start_at = serializers.DateTimeField(required=False)
    end_at = serializers.DateTimeField(required=False)
    answer_visibility = serializers.ChoiceField(
        choices=[c[0] for c in
                 Assignment._meta.get_field("answer_visibility").choices],
        required=False)
    score_visibility = serializers.ChoiceField(
        choices=[c[0] for c in
                 Assignment._meta.get_field("score_visibility").choices],
        required=False)
    allow_late = serializers.BooleanField(required=False)
    pass_ratio = serializers.FloatField(
        required=False, min_value=0.0, max_value=1.0)
    auto_practice = serializers.BooleanField(required=False)
    auto_grade_on_submit = serializers.BooleanField(required=False)
    #: ★ 草稿且未绑卡时允许改乱序；一旦绑卡 / 发布即冻结。
    shuffle_questions = serializers.BooleanField(required=False)
    shuffle_options = serializers.BooleanField(required=False)
    #: ★ 发布前允许改绑答题卡（需求 6）
    sheet_template_id = serializers.IntegerField(
        required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("没有要修改的字段")
        # ★ 两边都传时在这里先拦一次（只传一边由服务层与现值合并后再校验）
        if attrs.get("start_at") and attrs.get("end_at"):
            if attrs["start_at"] >= attrs["end_at"]:
                raise serializers.ValidationError(
                    {"end_at": "结束时间必须晚于开始时间（时间窗左闭右开）"}
                )
        return attrs


class AssignmentCloseSerializer(serializers.Serializer):
    """关闭输入。"""

    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class MarkAbsentSerializer(serializers.Serializer):
    """标记缺考输入。

    ★ `student_ids` 必填且非空：缺考必须**显式指定**，
      不提供「全部未交的都算缺考」这种批量动作——
      那会把「卡还没扫」的学生一起判缺考。
    """

    student_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


# ══════════════════════════════════════════════════════════════
#  ★ 学生端（扫码入口）
# ══════════════════════════════════════════════════════════════


class StudentBindSerializer(serializers.Serializer):
    """学生绑定身份（扫码后第一步）。

    ★ 学号 + 姓名**双因子**：只凭学号的话，任何一个学生
      输入同学的学号就能替人答题；而学号在班上是公开的。
      姓名是第二道便宜的防线（不是强认证，但足以挡住随手冒名）。
    """

    student_no = serializers.CharField(max_length=20)
    name = serializers.CharField(max_length=50, required=False, allow_blank=True)
    device_fingerprint = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default="")


class StudentAnswerItemSerializer(serializers.Serializer):
    """单题作答。

    ★ `sheet_seq` 是**卷面题号**，不是原题号——
      学生看到的是打乱后的卷面，前端只知道卷面题号。
      乱序还原由 `ResponseService.record_online` 内部完成，
      **调用方绝不能自己换算**（那是全班串号的成因）。

    ★★ 两种作答形态，按题型二选一：

        `labels`  选项题（选择/判断）——**卷面字母**（不是原题字母）
        `blanks`  填空题——按空顺序的**文本列表**

      ★ 原来只有 `labels`，于是填空题无路可走：
        一期承诺的「在线作答 + 客观题/**填空题**自动批改」
        （v2.1 功能清单第 12 项、03 计划 P1-74）只能覆盖客观题。

      ★ 两者都为空时后端会报「作答内容为空」——
        这比静默存一条空作答好：空作答会让学生以为「我填了」。
    """

    sheet_seq = serializers.IntegerField(min_value=1)
    labels = serializers.ListField(
        child=serializers.CharField(max_length=4),
        required=False, default=list)
    #: 填空题的逐空答案。★ 键名与判分侧一致（`answer_json["blanks"]`）。
    blanks = serializers.ListField(
        child=serializers.CharField(max_length=200, allow_blank=True),
        required=False, default=list)


class StudentSubmitSerializer(serializers.Serializer):
    """整卷提交。"""

    answers = StudentAnswerItemSerializer(many=True, required=False,
                                          default=list)
    #: 提前交卷时传 True（默认按提交动作即交卷）
    final = serializers.BooleanField(required=False, default=True)
