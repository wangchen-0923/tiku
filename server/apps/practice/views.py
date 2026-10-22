"""个性化练习（结构约束生成、批量导出） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 本模块是「**一生一卷**」的落点，所以有三条刻意的设计：

    1. **`dry_run` 是默认可用的**：老师先预演再真出。
       没有预演的话，任何一次「题量调错了」都会在库里
       留下一批垃圾练习集（而它们会出现在学生端）。

    2. **生成 ≠ 发布**：生成后状态是 `GENERATED`，必须经
       `approve` 才是 `TEACHER_APPROVED`。老师要看一眼
       「给这个学生出了什么、为什么」。

    3. **学生端与教师端分开**：学生端不含 `reason`（选题理由）
       与 `target_node`（哪个知识点弱）——见序列化器的说明。
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.practice.selectors import PracticeSetSelector
from apps.practice.serializers import (
    ApproveSerializer,
    GenerateBatchSerializer,
    GenerateForStudentSerializer,
    MarkStaleSerializer,
    PracticeSetDetailSerializer,
    PracticeSetSerializer,
    StudentPracticeSerializer,
)
from apps.practice.services import PracticeService


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _must_get(org_id: int, practice_set_id: int):
    ps = PracticeSetSelector.get(org_id, practice_set_id)
    if ps is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="练习集不存在")
    return ps


# ══════════════════════════════════════════════════════════════
#  生成
# ══════════════════════════════════════════════════════════════


class GenerateForStudentView(APIView):
    """`POST /api/v1/practice/generate/` 为单个学生生成。

    ★ `dry_run=true` 时**零副作用**——不写库，只返回计划
      （会出哪些题、每题的理由、策略、目标知识点）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = GenerateForStudentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = PracticeService.generate_for_student(
            org_id=org_id, student_id=v["student_id"],
            source_assignment_id=v["source_assignment_id"],
            created_by_id=request.user.id,
            dry_run=v.get("dry_run", False),
        )
        if v.get("dry_run"):
            return ok(res, message="预演完成（未写库）")

        # ★ 服务层返回里**没有 `status`**（它只管生成，不管状态流转），
        #   而前端要显示「这套题是待审核的」。这里补上——
        #   不补的话前端只能猜，而猜错的后果是显示成「已发布」。
        from apps.practice.selectors import PracticeSetSelector

        pid = res.get("practice_set_id")
        ps = PracticeSetSelector.get(org_id, pid) if pid else None
        if ps is not None:
            res = {**res, "status": ps.status, "is_stale": ps.is_stale}
        return ok(res, message=(
            f"已生成 {res.get('item_count', 0)} 道题，待审核"
        ))


class GenerateBatchView(APIView):
    """`POST /api/v1/practice/generate-batch/` 批量生成。

    ★ 逐个学生独立：一个学生数据异常（如没有任何作答记录）
      不该让整批失败——那会让老师重跑 45 次。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = GenerateBatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = PracticeService.generate_batch(
            org_id=org_id,
            source_assignment_id=v["source_assignment_id"],
            student_ids=v.get("student_ids") or [],
            created_by_id=request.user.id,
        )
        return ok(res, message=(
            f"已为 {res.get('generated', 0)} 名学生生成"
            + (f"，{res.get('skipped', 0)} 名跳过"
               if res.get("skipped") else "")
        ))


# ══════════════════════════════════════════════════════════════
#  查询
# ══════════════════════════════════════════════════════════════


class PracticeSetListView(APIView):
    """`GET /api/v1/practice/sets/` 练习集列表。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = PracticeSetSelector.list_sets(
            org_id,
            student_id=int(q["student_id"]) if q.get("student_id") else None,
            source_assignment_id=(
                int(q["source_assignment_id"])
                if q.get("source_assignment_id") else None),
            statuses=_strs(q.get("status")),
            only_stale=q.get("only_stale") in ("1", "true", "True"),
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            PracticeSetSerializer(page, many=True).data)


class PracticeSetDetailView(APIView):
    """`GET /api/v1/practice/sets/{id}/` 详情（含选题理由）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        ps = _must_get(org_id, pk)
        res = PracticeService.detail(org_id=org_id, practice_set_id=pk)
        # ★ 服务层的 `detail()` 已组装好题目与理由；
        #   若它返回的键不全，用序列化器补齐。
        base = PracticeSetDetailSerializer(ps).data
        for k, v in (res or {}).items():
            base.setdefault(k, v)
        return ok(base)


class AssignmentPracticeStatsView(APIView):
    """`GET /api/v1/practice/assignments/{id}/stats/` 某次作业的练习汇总。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        stats = PracticeSetSelector.stats(org_id, pk)
        stats["strategy_distribution"] = (
            PracticeSetSelector.strategy_distribution(org_id, pk))
        return ok(stats)


class AssignmentPendingReviewView(APIView):
    """`GET /api/v1/practice/assignments/{id}/pending/` 待审核队列。

    ★ 只列 `GENERATED`（生成但未审核）。老师要一次看完
      「还有多少人等我看一眼」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        qs = PracticeSetSelector.pending_for_assignment(org_id, pk)
        return ok({
            "source_assignment_id": pk,
            "count": qs.count(),
            "items": PracticeSetSerializer(qs, many=True).data,
        })


# ══════════════════════════════════════════════════════════════
#  审核与失效
# ══════════════════════════════════════════════════════════════


class PracticeApproveView(APIView):
    """`POST /api/v1/practice/sets/{id}/approve/` 审核。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = ApproveSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = PracticeService.approve(
            org_id=org_id, practice_set_id=pk,
            operator_id=request.user.id,
            approve=v.get("approve", True),
        )
        ps = _must_get(org_id, pk)
        return ok(PracticeSetSerializer(ps).data, message=(
            "已通过" if v.get("approve", True) else "已退回"
        ))


class MarkStaleView(APIView):
    """`POST /api/v1/practice/mark-stale/` 标记练习集失效。

    ★ 上游事件（知识标注变更、掌握度重算）触发。
      **失效不清空**：老师要能看到「这套题原来出过什么」，
      而直接删掉会让「为什么这套题是旧的」无从追溯。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = MarkStaleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        n = PracticeService.mark_stale_for_nodes(
            org_id=org_id, student_id=v["student_id"],
            node_ids=v["node_ids"],
        )
        return ok({"marked": n}, message=f"已标记 {n} 套练习集失效")


# ══════════════════════════════════════════════════════════════
#  学生端
# ══════════════════════════════════════════════════════════════


class StudentPracticeView(APIView):
    """`GET /api/v1/practice/sets/{id}/student/` 学生端取题。

    ★★ 不含 `reason`（选题理由）与 `target_node`（哪个知识点弱）。

      理由对老师是决策依据，对学生是标签。设计文档 11.2 明确
      「给能做对的题」是为了**建立信心**——而告诉学生
      「这是你上次做错的」正好抵消这个意图。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        ps = _must_get(org_id, pk)
        # ★ 学生只能取自己的。教师及以上不受限。
        from apps.common.permissions import has_role_at_least

        if not has_role_at_least(request.user, "TEACHER"):
            if ps.student_id != request.user.id:
                raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                              detail="无权查看他人的练习")
        return ok(StudentPracticeSerializer(ps).data)


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None
