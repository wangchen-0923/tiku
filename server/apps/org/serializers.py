"""机构、专业、班级、学生名册 · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 我第一版这里凭直觉写了 `code` / `org_type` / `stage` /
  `student_no_digits` / `years` 这几个字段，**全都不存在**。

  实际是：

    `Org`             `name` / `short_name` / `type` / `status`
                      学号位数在 **`grading_config`** 这个 JSON 里
                      （键 `student_no_default_digits`）
    `Major`           `name` / `code` / `short_name` / `level` / `status`
    `Classroom`       没有 `student_no_digits`（位数是**推断**出来的）
    `ClassroomStudent` 有 `name` / `display_name` / `seat_no`

  ★ 教训：写序列化器前必须先读模型或 `dataclasses.fields()`。
    「字段名看起来应该是什么」在这类项目里错得离谱，
    而 DRF 的报错（`Field name ... is not valid`）只在
    我把字段列进 `ModelSerializer.Meta.fields` 时才出现；
    写成 `Serializer` + 手写 `JSONField` 时它**不报错**，
    只是静默地永远返回空值。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.org.models import Classroom, ClassroomStudent, Major, Org


class OrgSerializer(serializers.ModelSerializer):
    """机构输出。

    ★ `student_no_digits` 从这个 JSON 里**提出来**作为一个平铺字段，
      不让前端去 `grading_config.student_no_default_digits` 里翻——
      那是实现细节，而前端到处都在用它。
    """

    student_no_digits = serializers.SerializerMethodField()
    org_type = serializers.CharField(source="type", read_only=True)

    class Meta:
        model = Org
        fields = [
            "id", "name", "short_name", "type", "org_type", "status",
            "student_no_digits",
            "created_at", "updated_at",
        ]

    def get_student_no_digits(self, obj) -> int:
        """机构默认学号位数。回落到 8（`create_org` 的默认值）。"""
        gc = obj.grading_config or {}
        try:
            return int(gc.get("student_no_default_digits", 8))
        except (TypeError, ValueError):
            return 8



class MajorSerializer(serializers.ModelSerializer):
    """专业输出。

    ★ 没有 `years`——实际字段是 `level`（培养层次）。
    """

    class Meta:
        model = Major
        fields = ["id", "name", "code", "short_name", "level",
                  "sort_order", "status", "created_at", "updated_at"]


class ClassroomSerializer(serializers.ModelSerializer):
    """班级输出。"""

    major_name = serializers.CharField(
        source="major.name", read_only=True, default="")
    grade_band = serializers.CharField(read_only=True)
    #: 在读人数。★★ 必须走 `SerializerMethodField`，**不能**写成
    #: `IntegerField(read_only=True)` —— 见 `get_student_count` 的说明。
    student_count = serializers.SerializerMethodField()
    #: 归属教师（班级归个人后，前端列表要能显示「谁的班」）
    owner_name = serializers.CharField(
        source="owner.real_name", read_only=True, default="")

    class Meta:
        model = Classroom
        fields = [
            "id", "name", "grade", "grade_band", "academic_year",
            "major_id", "major_name",
            "owner_id", "owner_name", "head_teacher_id",
            "status", "student_count",
            "created_at", "updated_at",
        ]

    def get_student_count(self, obj) -> int:
        """在读人数。

        ★★★ 为什么不能直接用同名字段读注解值（这是一个**线上 500**）：

          `Classroom.student_count` 是模型上的**只读 property**
          （它自己会查一次库）。而 `ClassroomSelector.with_student_counts()`
          用 `.annotate(student_count=Count(...))` 挂了个**同名注解**。

          于是 Django 在实例化模型时执行
          `setattr(obj, "student_count", row[col])` ——
          撞上只有 getter 的 property →
          `AttributeError: property 'student_count' of 'Classroom'
           object has no setter`，**整个班级列表 500**。

        ★ 修法（两处一起改，缺一不可）：
          ① 选择器把注解**改名**为 `active_student_count`（避开冲突）
          ② 这里用方法字段：**优先读注解值**（一次查询拿到全部班级的人数），
             读不到再回退到 property（单条查询，只在没走选择器时发生）

          ★ 只改一处都不行：只改① 则序列化器读到 property（N+1 查询）；
            只改② 则注解仍在 setattr 时炸。
        """
        annotated = getattr(obj, "active_student_count", None)
        if annotated is not None:
            return int(annotated)
        return int(obj.student_count)


class ClassroomStudentSerializer(serializers.ModelSerializer):
    """名册条目输出。"""

    student_name = serializers.CharField(
        source="student.real_name", read_only=True, default="")
    is_bound = serializers.SerializerMethodField()

    class Meta:
        model = ClassroomStudent
        fields = [
            "id", "classroom_id", "student_no", "name", "display_name",
            "seat_no", "student_id", "student_name", "is_bound",
            "status", "created_at", "updated_at",
        ]

    def get_is_bound(self, obj) -> bool:
        """是否已绑**登录凭据**（能自己登录看成绩）。

        ★★ 判据是「有没有可用密码」，**不是** `student_id 是否为空`：

          发布任务时会为每个学号建**学生身份行**（`role=STUDENT`，
          无可用密码），所以 `student_id` 建好后永远非空 ——
          用它判断会把全班显示成「已绑定」。

        ★ 学生**没绑账号也能答题**（学号 + 姓名认人 + `session_token`）；
          绑定只影响「能否自己登录看个人报告」，不是参加考试的前提。
        """
        student = obj.student
        return bool(student is not None and student.can_login)


class RosterUploadSerializer(serializers.Serializer):
    """名册导入输入。

    ★ 只收**结构化行**（`rows`），不收 CSV 文件本身。
      CSV 解析放到前端——服务端解析 CSV 要处理编码、分隔符、
      表头变体，那些是「导入体验」的问题，不该混进名册校验。

    ★ 学号位数**不作为入参**：`RosterService.validate()` 会
      **推断**它（`inferred_digits`），而老师自报的位数
      与数据实际不符时，推断的结果才是对的。
    """

    rows = serializers.ListField(
        child=serializers.DictField(), allow_empty=False)
    #: 导入时是否**替换**整个名册（默认追加/更新）
    replace = serializers.BooleanField(required=False, default=False)


class OrgCreateSerializer(serializers.Serializer):
    """建机构输入（平台管理员）。"""

    name = serializers.CharField(max_length=100)
    short_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=40)
    org_type = serializers.CharField(
        required=False, allow_blank=True, default="SCHOOL", max_length=20)
    student_no_digits = serializers.IntegerField(
        required=False, default=8, min_value=3, max_value=8)


class OrgStudentNoDigitsSerializer(serializers.Serializer):
    """设机构默认学号位数输入。"""

    digits = serializers.IntegerField(min_value=3, max_value=8)


class ClassroomCreateSerializer(serializers.Serializer):
    """建班级输入。

    ★ 没有 `student_no_digits`——`Classroom` 上没这个字段，
      位数是**按名册推断**的（`RosterSelector.brief().inferred_digits`）。
      让老师手工指定一个与实际数据不符的位数，正是
      「扫描时整片学号读错」的成因。
    """

    name = serializers.CharField(max_length=100)
    grade = serializers.CharField(max_length=20)
    academic_year = serializers.CharField(max_length=20)
    grade_band = serializers.CharField(
        required=False, allow_blank=True, default="SECONDARY_VOCATIONAL",
        max_length=30)
    major_id = serializers.IntegerField(required=False, allow_null=True)
    head_teacher_id = serializers.IntegerField(required=False,
                                               allow_null=True)


class MajorCreateSerializer(serializers.Serializer):
    """建专业输入。"""

    name = serializers.CharField(max_length=100)
    code = serializers.CharField(max_length=40)
    short_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=40)
    level = serializers.CharField(
        required=False, allow_blank=True, default="SECONDARY_VOCATIONAL",
        max_length=30)
    sort_order = serializers.IntegerField(
        required=False, default=0, min_value=0)


class ClassroomUpdateSerializer(serializers.Serializer):
    """改班级输入（P1-13b 补）。

    ★ **全部字段可选** —— 只改传入的那些。
      `PATCH` 的语义就是部分更新，要求传全量会把「改个名字」
      变成「重填一张表」。

    ★ 唯一键 `(org, name, academic_year)` 的查重在服务层做，
      且必须**排除自己**（否则「改成原名」会自撞）。
    """

    name = serializers.CharField(
        required=False, allow_blank=False, max_length=100)
    grade = serializers.CharField(
        required=False, allow_blank=False, max_length=20)
    academic_year = serializers.CharField(
        required=False, allow_blank=False, max_length=20)
    grade_band = serializers.CharField(
        required=False, allow_blank=False, max_length=30)
    major_id = serializers.IntegerField(required=False, allow_null=True)
    head_teacher_id = serializers.IntegerField(required=False,
                                               allow_null=True)
    owner_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.ChoiceField(
        required=False, choices=["ACTIVE", "ARCHIVED"])


class RosterStudentInputSerializer(serializers.Serializer):
    """名册单个学生的增 / 改输入（P1-13b 补）。

    ★★ **`student_no` 只在 `POST` 时可传，`PATCH` 时会被忽略。**

      原因是它在服务层被刻意排除：学号是答题卡**认人的唯一依据**
      （`classroom_student` 上有 `(classroom, student_no)` 唯一约束），
      改学号等于换一个人 —— 应「转出旧的 + 加入新的」，
      否则历史成绩会错挂到别人身上。
      ★ 与其让前端猜，不如在输入校验层就**不接收**它。

      `PATCH` 允许的字段：`name` / `seat_no` / `status`。
    """

    student_no = serializers.CharField(
        required=False, allow_blank=False, max_length=32)
    name = serializers.CharField(
        required=False, allow_blank=False, max_length=50)
    seat_no = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    status = serializers.ChoiceField(
        required=False, choices=["ACTIVE", "LEFT"])

    def validate(self, attrs: dict) -> dict:
        """POST（新建）必须给 `student_no` 与 `name`。

        ★ 用 `self.partial` 区分 `POST` 与 `PATCH`：
          `partial=True` 时所有字段都可缺，`partial=False` 时
          `student_no` / `name` 必填 —— 而它们本身声明为
          `required=False`（因为在 `PATCH` 里确实可以缺）。
          这个「同一个序列化器两种必填口径」的差别，
          只能在 `validate()` 里表达。
        """
        if not self.partial:
            if not attrs.get("student_no"):
                raise serializers.ValidationError(
                    {"student_no": "学号必填"})
            if not attrs.get("name"):
                raise serializers.ValidationError({"name": "姓名必填"})
        return attrs
