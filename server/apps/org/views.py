"""机构、专业、班级、学生名册 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 名册导入的接口设计要点：**先校验、后导入，两步分开**。

    POST /org/classrooms/{id}/roster/validate/   预检（不写库）
    POST /org/classrooms/{id}/roster/            导入

  「预检」不是可有可无的：一份 45 人的名册里「学号 4 位与 8 位
  混用」这种问题，如果在导入时才报错，老师已经点过一次「确定」了；
  而预检能让他先在界面上看到「3 条阻断、2 条提示」，再决定修还是强导。
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import (
    IsSystemAdmin,
    IsSchoolAdmin,
    IsTeacherAccount,
    require_school_admin,
)
from apps.common.scoping import (
    Scope,
    assert_classroom_manage,
    assert_classroom_scope,
    scope_of,
    teacher_classroom_ids,
)
from apps.common.responses import created, ok
from apps.org.selectors import (
    ClassroomSelector,
    MajorSelector,
    OrgSelector,
    RosterSelector,
)
from apps.org.serializers import (
    ClassroomCreateSerializer,
    ClassroomSerializer,
    ClassroomStudentSerializer,
    ClassroomUpdateSerializer,
    MajorCreateSerializer,
    MajorSerializer,
    OrgCreateSerializer,
    OrgSerializer,
    RosterStudentInputSerializer,
    RosterUploadSerializer,
)
from apps.common.exceptions import ValidationFailed
from apps.org.services import (
    ClassroomService,
    MajorService,
    OrgService,
    RosterService,
)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


# ══════════════════════════════════════════════════════════════
#  机构（平台管理员）
# ══════════════════════════════════════════════════════════════


class OrgListCreateView(APIView):
    """`GET/POST /api/v1/org/orgs/` 机构列表 / 新建。

    ★ **平台管理员专属**：机构列表本身就是跨机构数据。
      机构管理员调它应该 403——不是「只返回自己那个」，
      而是明确拒绝。返回一个只含自己的列表会让他以为
      「系统里只有一个学校」。
    """

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def get(self, request: Request) -> Response:
        qs = OrgSelector.list_active()
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            OrgSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        s = OrgCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ `create_org` 的真实签名是
        #   `(name, short_name, org_type, ai_config, grading_config)`——
        #   没有 `code` / `stage` / `contact_*`，那些字段在 `Org` 上不存在。
        #   学号位数通过 `grading_config` 传进去。
        org = OrgService.create_org(
            name=v["name"],
            short_name=v.get("short_name", ""),
            org_type=v.get("org_type", "SCHOOL"),
            grading_config={
                "student_no_default_digits": v.get("student_no_digits", 8)
            },
        )
        return created(OrgSerializer(org).data, message="机构已创建")


class OrgDetailView(APIView):
    """`GET /api/v1/org/orgs/{id}/` 机构详情。

    ★ 机构管理员能看**自己那个**；平台管理员能看任意。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        mine = getattr(request.user, "org_id", None)
        if not request.user.is_platform_admin and mine != pk:
            raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                          detail="无权访问其他机构的信息")
        org = OrgSelector.get(pk)
        if org is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="机构不存在")
        return ok(OrgSerializer(org).data)


class OrgStudentNoDigitsView(APIView):
    """`POST /api/v1/org/orgs/{id}/student-no-digits/` 设学号位数。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        mine = _org_id(request)
        if pk != mine and not request.user.is_platform_admin:
            raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                          detail="只能修改本机构的设置")
        s = OrgStudentNoDigitsSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        org = OrgService.update_student_no_digits(
            org_id=pk, digits=s.validated_data["digits"])
        return ok(OrgSerializer(org).data, message="已更新学号位数")


# ══════════════════════════════════════════════════════════════
#  专业
# ══════════════════════════════════════════════════════════════


class MajorListCreateView(APIView):
    """`GET/POST /api/v1/org/majors/` 专业列表 / 新建。"""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        qs = MajorSelector.list_active(org_id)
        return ok(MajorSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        s = MajorCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        m = MajorService.create_major(
            org_id=org_id, name=v["name"], code=v["code"],
            short_name=v.get("short_name", ""),
            level=v.get("level", "SECONDARY_VOCATIONAL"),
        )
        return created(MajorSerializer(m).data, message="专业已创建")


# ══════════════════════════════════════════════════════════════
#  班级
# ══════════════════════════════════════════════════════════════


class ClassroomListCreateView(APIView):
    """`GET/POST /api/v1/org/classrooms/` 班级列表 / 新建。

    ★★ 班级归教师个人（2026-10-21）：

      - `GET`：教师只返回**自己**的班；学校平台账户返回本校全部
      - `POST`：教师可直接建班，`owner` 自动记为自己

      学校不再统一建班 —— 它的职责收窄为「授权教师账号」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = ClassroomSelector.with_student_counts(
            org_id, academic_year=q.get("academic_year") or None)
        # ★ 教师作用域按**归属**过滤，而不是按机构。
        #   不写这段，教师列表里就会出现全校所有班级 —— 这是本次规则
        #   最容易被漏掉的一处：接口本身合法，只是「多返回了」。
        if scope_of(request.user) == Scope.OWN_TEACHING.value:
            ids = teacher_classroom_ids(request.user)
            qs = qs.filter(pk__in=list(ids)) if ids else qs.none()
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            ClassroomSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = ClassroomCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ `create_classroom` **没有** `student_no_digits` 参数；
        #   `Classroom` 上也没这个字段——位数是按名册推断的。
        c = ClassroomService.create_classroom(
            org_id=org_id, name=v["name"], grade=v["grade"],
            academic_year=v["academic_year"],
            grade_band=v.get("grade_band", "SECONDARY_VOCATIONAL"),
            major_id=v.get("major_id"),
            head_teacher_id=v.get("head_teacher_id"),
            # ★ 归属人 = 操作者。不能从 body 收 `owner_id`：
            #   那等于允许替别人建班（建出来的班他自己也看不见）。
            owner_id=request.user.id,
        )
        return created(ClassroomSerializer(c).data, message="班级已创建")


class ClassroomDetailView(APIView):
    """`GET` · `PATCH` · `DELETE` `/api/v1/org/classrooms/{id}/` 详情 / 编辑 / 删除。

    ★★ `PATCH` 与 `DELETE` 是**补出来的**（P1-13b）：

      原来只实现了 `get` —— 于是**班级改不了、删不掉**，
      老师建错一个班就只能重建（还得先把学生一个个转出）。
      一个「管理页」没有改删就不是管理页。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = ClassroomSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")
        # ★ 详情里带 `roster_brief`（学生姓名与学号）——
        #   教师只能看自己所教的班，不能靠改 URL 里的 id 翻别班名单。
        assert_classroom_scope(request.user, pk)
        data = ClassroomSerializer(c).data
        data["roster_brief"] = _brief_payload(
            RosterSelector.brief(org_id, pk))
        return ok(data)

    def patch(self, request: Request, pk: int) -> Response:
        # ★ 班级归教师个人：建它的人能改它；学校账户可接管纠错。
        assert_classroom_manage(request.user, pk)
        org_id = _org_id(request)
        s = ClassroomUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        if not v:
            raise ValidationFailed(detail="没有要修改的字段")
        # ★★ 归属人只有学校平台账户能改（指派历史班、离职转接）。
        #   教师能改自己的班名、年级，但**不能把班送给别人** ——
        #   那既交出名单，也把责任推走。
        if "owner_id" in v and scope_of(request.user) != Scope.ORG_ALL.value:
            raise ZfError(
                ErrorCode.AUTH_FORBIDDEN,
                detail="只有学校平台账户可以变更班级归属",
            )
        c = ClassroomService.update_classroom(
            org_id=org_id, classroom_id=pk,
            name=v.get("name"), grade=v.get("grade"),
            academic_year=v.get("academic_year"),
            grade_band=v.get("grade_band"),
            major_id=v.get("major_id"),
            head_teacher_id=v.get("head_teacher_id"),
            owner_id=v.get("owner_id"),
            status=v.get("status"),
            operator_id=request.user.id,
        )
        return ok(ClassroomSerializer(c).data, message="已保存")

    def delete(self, request: Request, pk: int) -> Response:
        assert_classroom_manage(request.user, pk)
        org_id = _org_id(request)
        # ★ `reason` 走 body（与 `DELETE /questions/{id}/` 同一约定）
        reason = ""
        if isinstance(request.data, dict):
            reason = str(request.data.get("reason") or "")
        ClassroomService.delete_classroom(
            org_id=org_id, classroom_id=pk,
            operator_id=request.user.id, reason=reason,
        )
        return ok({"id": pk}, message="已删除班级")


class RosterStudentView(APIView):
    """名册单个学生的增 / 改 / 转出。

        `POST   /api/v1/org/classrooms/{id}/roster/students/`        加一名
        `PATCH  /api/v1/org/classrooms/{id}/roster/students/{sid}/`  改一名
        `DELETE /api/v1/org/classrooms/{id}/roster/students/{sid}/`  转出一名

    ★★ 为什么需要独立的单条接口（原来只有整表导入）：

      `import_roster(replace=True)` 是**整表替换** —— 为了加一个学生
      而重传全班名册，风险远大于收益（传错一次全班变样）。
      而日常最常用的操作恰恰是「加一个转学生」「把一个人转出」。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        assert_classroom_manage(request.user, pk)
        org_id = _org_id(request)
        s = RosterStudentInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        cs = ClassroomService.add_roster_student(
            org_id=org_id, classroom_id=pk,
            student_no=v["student_no"], name=v["name"],
            seat_no=v.get("seat_no", ""),
            operator_id=request.user.id,
        )
        return created(ClassroomStudentSerializer(cs).data,
                       message="已加入名册")

    def patch(self, request: Request, pk: int, sid: int) -> Response:
        assert_classroom_manage(request.user, pk)
        org_id = _org_id(request)
        s = RosterStudentInputSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        cs = ClassroomService.update_roster_student(
            org_id=org_id, classroom_id=pk, student_id=sid,
            name=v.get("name"), seat_no=v.get("seat_no"),
            status=v.get("status"), operator_id=request.user.id,
        )
        return ok(ClassroomStudentSerializer(cs).data, message="已保存")

    def delete(self, request: Request, pk: int, sid: int) -> Response:
        assert_classroom_manage(request.user, pk)
        org_id = _org_id(request)
        reason = ""
        if isinstance(request.data, dict):
            reason = str(request.data.get("reason") or "")
        ClassroomService.remove_roster_student(
            org_id=org_id, classroom_id=pk, student_id=sid,
            operator_id=request.user.id, reason=reason,
        )
        return ok({"id": sid},
                  message="已转出（不物理删除，保留历史成绩）")


def _brief_payload(b) -> dict:
    """名册摘要（含**推断位数**与位数分布）。

    ★★ 字段名以 `RosterBrief` 为准：

        `classroom_id` / `classroom_name` / `student_count`
        `digit_distribution` / `inferred_digits`

      我第一版凭直觉写了 `total` / `bound` / `unbound` /
      `student_no_digits`，那些**都不存在**——`getattr(b, ..., default)`
      带默认值不会报错，只会静默返回 0/None，
      于是接口「成功」而字段全是空的。

    ★ `inferred_digits` 是答题卡**排版的依据**（出现最多的位数）；
      `digit_distribution` 让老师看到「4 位 2 人、8 位 43 人」的实情。
    """
    return {
        "classroom_id": getattr(b, "classroom_id", None),
        "classroom_name": getattr(b, "classroom_name", ""),
        "student_count": getattr(b, "student_count", 0),
        "inferred_digits": getattr(b, "inferred_digits", None),
        "digit_distribution": getattr(b, "digit_distribution", {}),
        # ★ 「位数一致」是**告警**级信息，不是阻断级——
        #   短学号左侧留空格，读出来仍然正确。
        "digit_consistent": b.digit_consistent,
    }


class ClassroomAcademicYearsView(APIView):
    """`GET /api/v1/org/classrooms/academic-years/` 学年列表。

    ★ 静态路径，必须排在 `<int:pk>/` 之前。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        return ok(ClassroomSelector.academic_years(org_id))


# ══════════════════════════════════════════════════════════════
#  名册
# ══════════════════════════════════════════════════════════════


class RosterListView(APIView):
    """`GET /api/v1/org/classrooms/{id}/roster/` 名册列表。"""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = ClassroomSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")
        assert_classroom_scope(request.user, pk, what="该班级")
        qs = RosterSelector.active_of_classroom(org_id, pk)
        if request.query_params.get("unbound") in ("1", "true", "True"):
            qs = qs.filter(student__isnull=True)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            ClassroomStudentSerializer(page, many=True).data)


class RosterValidateView(APIView):
    """`POST /api/v1/org/classrooms/{id}/roster/validate/` **预检（不写库）**。

    ★ 返回阻断项与提示项**分开**，与定稿门禁同一套约定：
      前端要「阻断项红色不可忽略、提示黄色可忽略」。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = ClassroomSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")
        # ★ 预检虽然不写库，但会把「哪几个学号冲突」吐出来 ——
        #   那是别人的名册内容，必须同样校验归属。
        assert_classroom_manage(request.user, pk)
        s = RosterUploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ 真实签名：`validate(*, rows, existing_nos=None)`。
        #   **没有** `org_id` / `student_no_digits` / `classroom_id` 参数——
        #   位数由它自己从数据里推断（`inferred_digits`）。
        existing = list(
            RosterSelector.active_of_classroom(org_id, pk)
            .values_list("student_no", flat=True)
        )
        val = RosterService.validate(rows=v["rows"], existing_nos=existing)
        return ok({
            "can_import": val.can_import,
            "summary": val.summary(),
            "total": val.total,
            "valid": val.valid,
            "inferred_digits": val.inferred_digits,
            "digit_distribution": val.digit_distribution,
            "digit_consistent": val.digit_consistent,
            "all_numeric": val.all_numeric,
            # ★ `RosterIssue` 的字段是 `detail` 不是 `message`
            "blocking": [
                {"code": i.code, "row": i.row, "detail": i.detail,
                 "student_no": i.student_no, "name": i.name}
                for i in val.blocking
            ],
            "warnings": [
                {"code": i.code, "row": i.row, "detail": i.detail,
                 "student_no": i.student_no, "name": i.name}
                for i in val.issues if not i.is_blocking
            ],
        })


class RosterImportView(APIView):
    """`POST /api/v1/org/classrooms/{id}/roster/` 导入名册。

    ★ 有阻断项时**拒绝**（`import_roster` 内部抛校验错误）。

    ★★ 「学号位数不一致」是**告警不是阻断**——这一点我一开始
      猜反了，去看实现才发现判据是「会不会让答题卡**认错人**」：

        位数不一致 → 短学号左侧留空格 → 读出来仍然正确 → 告警
        位数超范围（3–8 之外）→ 排不下 → 阻断（`OUT_OF_RANGE`）

      所以这里**不该**用位数一致性去拒绝导入；
      老师要看的是 `inferred_digits`（推断出的主位数）——
      答题卡按它排版。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = ClassroomSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")
        # ★ 只有班级归属人（或学校账户）能改它的名册。
        assert_classroom_manage(request.user, pk)
        s = RosterUploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ 真实签名：`import_roster(*, org_id, classroom_id, rows,
        #   replace=False, operator_id=None)`。没有 `student_no_digits`
        #   与 `force`——位数由 `validate()` 推断后写入，
        #   而阻断项由 `validate()` 上报（见预检接口），
        #   导入本身不会因为位数混用而拒（那是预检的职责）。
        res = RosterService.import_roster(
            org_id=org_id, classroom_id=pk, rows=v["rows"],
            replace=v.get("replace", False),
            operator_id=request.user.id,
        )
        return ok(res, message=(
            f"已导入 {res.get('created', 0)} 人"
            + (f"，更新 {res.get('updated', 0)} 人"
               if res.get("updated") else "")
        ))


class RosterDigitMismatchView(APIView):
    """`GET /api/v1/org/roster/digit-mismatch/` 位数不一致的班级。

    ★ 这是一个**运维巡检接口**：老师不会主动去看，
      但「学号位数混用」会在扫描时变成整片读错。
      所以它要能被一次列出来，在扫描前处理掉。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        rows = RosterSelector.classes_with_digit_mismatch(org_id)
        # ★ 巡检结果带班级名与学号分布 —— 教师只该看见自己那些班。
        if scope_of(request.user) == Scope.OWN_TEACHING.value:
            mine = teacher_classroom_ids(request.user)
            rows = [r for r in rows
                    if getattr(r, "classroom_id", None) in mine]
        return ok({
            "count": len(rows),
            "items": [
                {"classroom_id": getattr(r, "classroom_id", None),
                 "classroom_name": getattr(r, "classroom_name", ""),
                 "student_no_digits": getattr(r, "student_no_digits", None),
                 "digit_groups": getattr(r, "digit_groups", {}),
                 "total": getattr(r, "total", 0)}
                for r in rows
            ],
        })
