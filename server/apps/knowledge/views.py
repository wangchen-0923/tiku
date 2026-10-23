"""课程、章、任务、知识点树、知识库 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 树的接口分两个粒度，这一步是刻意的：

    GET /knowledge/courses/{id}/tree/      完整树（章→任务→知识点）
    GET /knowledge/courses/{id}/nodes/     扁平知识点列表（带题数与 KB 数）

  树用于「浏览与拖动」，扁平列表用于「一张表看全班知识点的
  题量分布与知识库缺口」。合成一个接口会让其中一方拿到
  自己不需要的大量结构——而老师要的「哪些知识点没题」这个视图，
  正是靠扁平列表 + `question_count` 才一眼看出来的。
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import require_school_admin, IsSchoolAdmin, IsTeacherAccount
from apps.common.responses import created, ok
from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.extract import (
    DEFAULT_MAX_BYTES,
    SCAN_PDF_WARNING,
    extract_document,
    supported_extensions,
)
from apps.knowledge.selectors import (
    ChapterSelector,
    CourseSelector,
    KnowledgeEntrySelector,
    KnowledgeNodeSelector,
    TaskSelector,
)
from apps.knowledge.serializers import (
    ChapterCreateSerializer,
    ChapterSerializer,
    CourseCreateSerializer,
    CourseSerializer,
    CourseUpdateSerializer,
    EntryCreateSerializer,
    EntryUpdateSerializer,
    KnowledgeEntrySerializer,
    KnowledgeImportCommitSerializer,
    KnowledgeImportExtractSerializer,
    KnowledgeImportParseSerializer,
    KnowledgeNodeSerializer,
    NodeCreateSerializer,
    NodeUpdateSerializer,
    PrerequisiteSerializer,
    TaskCreateSerializer,
    TaskSerializer,
)
from apps.knowledge.services import (
    KnowledgeEntryService,
    KnowledgeTreeService,
)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _entry_context(request: Request, entries) -> dict:
    """知识库条目序列化的上下文：**一次算出整页的可编辑性**。

    ★★ 为什么要有这个函数：`editable` 依赖「关联知识点下有没有题」，
      如果不批量算，序列化 100 行就是 100+ 次 `COUNT`。
      这里先把整页涉及的 `node_id` 收齐，再用
      `KnowledgeEntrySelector.blocking_by_node()` 一次 `GROUP BY` 查完。

    ★ `can_manage_all` 用 `scope_of()` 判，与写接口的
      `assert_entry_writable()` 同一个判据来源（都走 `scoping`），
      不在这里另写 `role == "ORG_ADMIN"`。
    """
    from apps.common.scoping import Scope, scope_of

    node_ids: set[int] = set()
    for e in entries:
        for link in e.entry_links.all():
            if link.node_id:
                node_ids.add(link.node_id)

    org_id = getattr(request.user, "org_id", None)
    return {
        "request": request,
        "user": request.user,
        "can_manage_all": scope_of(request.user) == Scope.ORG_ALL.value,
        "blocking_by_node": (
            KnowledgeEntrySelector.blocking_by_node(org_id, list(node_ids))
            if org_id else {}
        ),
    }


# ══════════════════════════════════════════════════════════════
#  课程
# ══════════════════════════════════════════════════════════════


class CourseListView(APIView):
    """`/api/v1/knowledge/courses/` 课程列表与新建。

    ★ 读要教师及以上（老师组卷要选课程），写只给学校平台账户：
      课程是**全校共用的教学资产**，教师在它下面自由增删等于让
      44 个知识点的编号由某一位老师随手改掉。
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), IsSchoolAdmin()]
        return [IsAuthenticated(), IsTeacherAccount()]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = CourseSelector.list_active(
            org_id,
            major_id=int(q["major_id"]) if q.get("major_id") else None,
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            CourseSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        """`POST` 新建课程。★ 201。

        重复编码返回 400 并说明「已存在」，不是 500：
        这是最常发生的一种输入错误，不该表现成服务端故障。
        """
        org_id = _org_id(request)
        s = CourseCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = dict(s.validated_data)
        c = KnowledgeTreeService.create_course(
            org_id=org_id,
            name=v["name"],
            code=v["code"],
            major_id=v.get("major_id"),
            grade_band=v.get("grade_band") or None,
            level=v.get("level") or None,
            textbook=v.get("textbook") or {},
            total_hours=v.get("total_hours"),
            style_config=v.get("style_config") or {},
            sort_order=v.get("sort_order", 0),
        )
        return created(CourseSerializer(c).data, message="课程已创建")


class CourseDetailView(APIView):
    """`/api/v1/knowledge/courses/{id}/` 课程详情 / 改 / 删。

    ★★ 删除是**软删**且要求全空（见 `delete_course` 的说明）：
      有章、有题、有卷就 409 并带 `data.references`。
      想下线但保留数据请 `PATCH {"status": "ARCHIVED"}`。
    """

    def get_permissions(self):
        if self.request.method in ("PATCH", "DELETE"):
            return [IsAuthenticated(), IsSchoolAdmin()]
        return [IsAuthenticated(), IsTeacherAccount()]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = CourseSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")
        data = CourseSerializer(c).data
        # ★ 详情页要能提前知道「这门课能不能删 / 编码能不能改」，
        #   而不是等点了删除才拿到 409。
        data["references"] = KnowledgeTreeService.course_references(
            org_id=org_id, course_id=pk)
        return ok(data)

    def patch(self, request: Request, pk: int) -> Response:
        """`PATCH` 改课程。★ `code` 在已有下级数据时 409。"""
        org_id = _org_id(request)
        s = CourseUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        c = KnowledgeTreeService.update_course(
            org_id=org_id, course_id=pk, payload=dict(s.validated_data))
        return ok(CourseSerializer(c).data, message="课程已更新")

    def delete(self, request: Request, pk: int) -> Response:
        """`DELETE` 软删课程（要求无任何下级引用）。"""
        org_id = _org_id(request)
        c = KnowledgeTreeService.delete_course(org_id=org_id, course_id=pk)
        data = CourseSerializer(c).data
        data["deleted_at"] = c.deleted_at
        return ok(data, message="课程已删除")


class CourseTreeView(APIView):
    """`GET /api/v1/knowledge/courses/{id}/tree/` 完整知识树。

    ★★ 形状以 `KnowledgeNodeSelector.tree()` 为准：

        `CourseTree.chapters[]`
            `.tasks[]`
                `.nodes[]`

      ——是 `tasks` / `nodes`，**不是** `children`；
      而且是**纯 dict**，不是模型实例。

      我连续两版都猜错了结构：第一版以为是 `chapter_count()`
      这类方法，第二版以为是带 `.children` 的对象。
      两次都只在真跑一次时才暴露。

    ★ 只查 3 次库（章 / 任务 / 知识点各一次），不逐层查。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = CourseSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")

        tree = KnowledgeNodeSelector.tree(org_id, pk)

        node_ids = [
            n["id"] for ch in tree.chapters
            for t in ch["tasks"] for n in t["nodes"]
        ]
        entry_counts = _entry_counts(org_id, node_ids)

        chapters = []
        for ch in tree.chapters:
            tasks = []
            for t in ch["tasks"]:
                nodes = [
                    {
                        "id": n["id"], "name": n["name"], "code": n["code"],
                        "level": "NODE",
                        "seq_in_task": n["seq_in_task"],
                        "exam_weight": n["exam_weight"],
                        # ★ L1 闸门的可见面：这个知识点有几条知识库内容
                        "entry_count": entry_counts.get(n["id"], 0),
                        "children": [],
                    }
                    for n in t["nodes"]
                ]
                tasks.append({
                    "id": t["id"], "name": t["name"], "code": t["code"],
                    "level": "TASK", "seq": t["seq"],
                    "is_practical": t["is_practical"],
                    "children": nodes,
                })
            chapters.append({
                "id": ch["id"], "name": ch["name"], "code": ch["code"],
                "level": "CHAPTER", "seq": ch["seq"],
                "part_seq": ch["part_seq"], "part_name": ch["part_name"],
                "children": tasks,
            })

        # 按篇分组（篇是「属性」不是层级，分组只在展示层做）
        parts: dict[str, list[dict]] = {}
        for ch in chapters:
            parts.setdefault(ch["part_name"] or "", []).append(ch)

        return ok({
            "course": {"id": c.id, "name": c.name, "code": c.code},
            "chapter_count": len(chapters),
            "task_count": sum(len(ch["children"]) for ch in chapters),
            "node_count": sum(
                len(t["children"]) for ch in chapters
                for t in ch["children"]
            ),
            "parts": [
                {
                    "part": k,
                    "part_seq": (v[0]["part_seq"] if v else None),
                    "chapters": v,
                }
                for k, v in sorted(
                    parts.items(),
                    key=lambda kv: (kv[1][0]["part_seq"] or 0)
                    if kv[1] else 0,
                )
            ],
        })


def _entry_counts(org_id: int, node_ids: list[int]) -> dict[int, int]:
    """`{node_id: 生效知识库条数}`（**一次查询**）。

    ★ 逐节点调 `count_active_for_node()` 会是 44 次查询——
      而知识树是最常打开的页面之一。
    """
    if not node_ids:
        return {}
    from django.db.models import Count

    from apps.knowledge.models import KnowledgeEntry as _KE

    # ★★ `KnowledgeEntry` **没有** `knowledge_node` 外键；
    #   知识点关联走中间表 `entry_links`（多对多）。
    #   用 `knowledge_node_id__in` 会 `FieldError: Cannot resolve
    #   keyword`——我第一版就是那么写的。
    #
    #   一条条目可以链多个知识点，所以按 `entry_links__node_id`
    #   分组计数：`values("entry_links__node_id")`。
    rows = (
        _KE.objects.for_org(org_id)
        .filter(entry_links__node_id__in=node_ids, status="ACTIVE")
        .values("entry_links__node_id")
        .annotate(n=Count("id", distinct=True))
    )
    return {
        r["entry_links__node_id"]: r["n"] for r in rows
        if r["entry_links__node_id"] is not None
    }


class CourseNodesView(APIView):
    """`GET /api/v1/knowledge/courses/{id}/nodes/` 扁平知识点列表。

    ★ 带 `question_count` 与 `entry_count`——这是
      「哪些知识点没题」「哪些知识点没法 AI 生题」两个视图的
      唯一来源。

    ★★ `NodeBrief` 是**扁平** dataclass：

        `id` / `code` / `name` / `task_id` / `task_name`
        `chapter_seq` / `chapter_name` / `seq_in_task`
        `exam_weight` / `question_count` / `entry_count`

      我第一版按 `b.node.id` 取（以为它包着模型实例），
      直接 `AttributeError`。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        c = CourseSelector.get(org_id, pk)
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")
        briefs = KnowledgeNodeSelector.briefs(org_id, pk)

        if request.query_params.get("lacking_kb") in ("1", "true", "True"):
            briefs = [b for b in briefs if not b.kb_sufficient]

        return ok({
            "course_id": pk,
            "count": len(briefs),
            "items": [
                {
                    "id": b.id,
                    "name": b.name,
                    "code": b.code,
                    "task_id": b.task_id,
                    "task_name": b.task_name,
                    "chapter_seq": b.chapter_seq,
                    "chapter_name": b.chapter_name,
                    "seq_in_task": b.seq_in_task,
                    "exam_weight": float(b.exam_weight or 0),
                    "question_count": b.question_count,
                    "entry_count": b.entry_count,
                    "kb_sufficient": b.kb_sufficient,
                }
                for b in briefs
            ],
        })


class CourseHealthView(APIView):
    """`GET /api/v1/knowledge/health/` 知识库健康度。

    ★ 这是「AI 精准命题」能不能跑起来的**前置体检**：
      `sufficient_for_generation` 为假时生题会被 L1 闸门拒，
      而老师需要先知道是哪个知识点缺内容。

    ★★ `coverage` / `utilisation` / `sufficient_for_generation`
      都是 `KbHealth` 上的 **property**，**不加括号**。
      同一个 dataclass 上混用两种调用形式是这类错误的温床：
      我第一版给前两个加了括号（`'float' object is not callable`），
      第三个因为拼写不同而静默取不到（返回 `None`）。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        course_id = request.query_params.get("course_id")
        h = KnowledgeEntrySelector.health(
            org_id, int(course_id) if course_id else None)
        return ok({
            "course_id": int(course_id) if course_id else None,
            "total_nodes": h.total_nodes,
            "covered_nodes": h.covered_nodes,
            "total_entries": h.total_entries,
            "adopted_entries": h.adopted_entries,
            "coverage": h.coverage,
            "utilisation": h.utilisation,
            "sufficient_for_generation": h.is_sufficient_for_generation,
        })


class ChapterListCreateView(APIView):
    """`GET /api/v1/knowledge/chapters/?course_id=` · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        course_id = request.query_params.get("course_id")
        if not course_id:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 course_id")
        qs = ChapterSelector.by_course(org_id, int(course_id))
        return ok(ChapterSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        s = ChapterCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ 真实签名：`create_chapter(*, org_id, course_id, seq, name,
        #   part_seq=None, part_name="", code=None)`——**没有** `part`
        #   这个参数（那是 `part_seq` + `part_name` 两个字段）。
        ch = KnowledgeTreeService.create_chapter(
            org_id=org_id, course_id=v["course_id"], name=v["name"],
            seq=v["seq"], part_seq=v.get("part_seq"),
            part_name=v.get("part_name", ""),
            code=v.get("code") or None,
        )
        return created(ChapterSerializer(ch).data, message="章已创建")


class TaskListCreateView(APIView):
    """`GET /api/v1/knowledge/tasks/?chapter_id=` · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        if q.get("chapter_id"):
            qs = TaskSelector.by_chapter(org_id, int(q["chapter_id"]))
        elif q.get("course_id"):
            qs = TaskSelector.by_course(org_id, int(q["course_id"]))
        else:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 chapter_id 或 course_id")
        return ok(TaskSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        s = TaskCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        t = KnowledgeTreeService.create_task(
            org_id=org_id, chapter_id=v["chapter_id"], name=v["name"],
            seq=v["seq"], is_practical=v.get("is_practical", False),
            objectives=v.get("objectives") or [],
        )
        return created(TaskSerializer(t).data, message="任务已创建")


# ══════════════════════════════════════════════════════════════
#  知识点
# ══════════════════════════════════════════════════════════════


class NodeListCreateView(APIView):
    """`GET /api/v1/knowledge/nodes/?task_id=` · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        if q.get("task_id"):
            qs = KnowledgeNodeSelector.by_task(org_id, int(q["task_id"]))
        elif q.get("chapter_id"):
            qs = KnowledgeNodeSelector.by_chapter(
                org_id, int(q["chapter_id"]))
        elif q.get("course_id"):
            qs = KnowledgeNodeSelector.by_course(
                org_id, int(q["course_id"]))
        else:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 task_id / chapter_id / course_id")
        # ★★ `question_count` 必须**显式算出来**。
        #
        #   `KnowledgeNodeSerializer.question_count` 是
        #   `read_only=True, default=0` —— queryset 上没有这个注解时
        #   DRF **静默回落成 0**。实测（2026-10-16）：这个端点对 44 个
        #   知识点全部返回「0 题」，而真实情况是 23 个有题、21 个没有 ——
        #   拿它当数据源会把「缺口 21 个」误算成「44 个全缺」。
        #   ★ 口径与 `courses/{id}/nodes/` 对齐：数关联条数。
        from django.db.models import Count

        from apps.questionbank.models import QuestionKnowledge

        nodes = list(qs)
        q_counts = {
            row["knowledge_node_id"]: row["n"]
            for row in QuestionKnowledge.objects.for_org(org_id)
            .filter(knowledge_node_id__in=[n.id for n in nodes])
            .values("knowledge_node_id")
            .annotate(n=Count("id"))
        }
        for n in nodes:
            n.entry_count = KnowledgeEntrySelector.count_active_for_node(
                org_id, n.id)
            n.question_count = q_counts.get(n.id, 0)
        return ok(KnowledgeNodeSerializer(nodes, many=True).data)

    def post(self, request: Request) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        s = NodeCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        n = KnowledgeTreeService.create_node(
            org_id=org_id, task_id=v["task_id"], name=v["name"],
            seq_in_task=v["seq_in_task"],
            exam_weight=v.get("exam_weight", 1.0),
        )
        return created(KnowledgeNodeSerializer(n).data, message="知识点已创建")


class NodeDetailView(APIView):
    """`/api/v1/knowledge/nodes/{id}/` 知识点详情（含前置链）/ 改 / 删。

    ★★ 「移动」就是 `PATCH {"task_id": 目标任务}`：

        - 可以跨任务、跨章、跨课程；
        - `course` / `subject` / `grade_band` / `path` 四个冗余列一起改；
        - **`code` 不变**（它是稳定标识，不是位置描述，见服务层说明）；
        - 主归属关联（`task_knowledge.is_primary`）与章关联一起搬。

    ★★ `DELETE` 前先数引用：有题 / 有知识库条目 / 有学情记录就 409，
      响应带 `data.references` 说明是什么卡住了它。
      知识点能重建，题目标注与掌握度不能——所以挡住。
    """

    def get_permissions(self):
        if self.request.method in ("PATCH", "DELETE"):
            return [IsAuthenticated(), IsSchoolAdmin()]
        return [IsAuthenticated(), IsTeacherAccount()]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        n = KnowledgeNodeSelector.get(org_id, pk)
        if n is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")
        n.entry_count = KnowledgeEntrySelector.count_active_for_node(
            org_id, pk)
        data = KnowledgeNodeSerializer(n).data
        data["prerequisites"] = [
            {"id": p.id, "name": p.name, "code": p.code}
            for p in KnowledgeNodeSelector.prerequisites(org_id, pk)
        ]
        # ★ 详情页要能提前显示「为什么这颗删除按钮是灰的」。
        data["references"] = KnowledgeTreeService.node_references(
            org_id=org_id, node_id=pk)
        return ok(data)

    def patch(self, request: Request, pk: int) -> Response:
        """`PATCH` 改知识点 / 移动层级。"""
        org_id = _org_id(request)
        s = NodeUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        n = KnowledgeTreeService.update_node(
            org_id=org_id, node_id=pk, payload=dict(s.validated_data))
        n.entry_count = KnowledgeEntrySelector.count_active_for_node(
            org_id, pk)
        return ok(KnowledgeNodeSerializer(n).data, message="知识点已更新")

    def delete(self, request: Request, pk: int) -> Response:
        """`DELETE` 软删知识点（无引用时）+ 清理关联行。"""
        org_id = _org_id(request)
        n = KnowledgeTreeService.delete_node(org_id=org_id, node_id=pk)
        data = KnowledgeNodeSerializer(n).data
        data["deleted_at"] = n.deleted_at
        return ok(data, message="知识点已删除")


class NodePrerequisiteView(APIView):
    """`POST /api/v1/knowledge/nodes/prerequisite/` 加前置关系。

    ★ 成环会被服务层拦（`_can_reach` 判可达性）。
      前置关系成环会让「先修顺序」失去意义，
      而学生端的「先补前置」推荐会陷入死循环。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = PrerequisiteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        KnowledgeTreeService.add_prerequisite(
            org_id=org_id, node_id=v["node_id"],
            prerequisite_id=v["prerequisite_id"],
            weight=v.get("weight", 1.0),
        )
        return ok(None, message="前置关系已建立")


class NodeCycleCheckView(APIView):
    """`GET /api/v1/knowledge/nodes/cycles/` 前置关系成环检查。

    ★ 运维巡检接口：成环是「当前置关系多了之后」才出现的，
      而它一旦出现，学生端的先修补练就废了。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        cycles = KnowledgeTreeService.find_prerequisite_cycles(org_id)
        return ok({
            "count": len(cycles),
            "cycles": [
                {"node_ids": c,
                 "codes": [
                     getattr(KnowledgeNodeSelector.get(org_id, nid), "code", "")
                     for nid in c
                 ]}
                for c in cycles
            ],
        })


class NodeLackingKbView(APIView):
    """`GET /api/v1/knowledge/nodes/lacking-kb/?course_id=` 缺知识库的知识点。

    ★ 这是 L1 闸门「拒绝生题」的**运营端**：老师生不出题时
      打开这个列表就知道该补哪个知识点的内容。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        course_id = request.query_params.get("course_id")
        if not course_id:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 course_id")
        rows = KnowledgeNodeSelector.nodes_lacking_kb(
            org_id, int(course_id))
        return ok({
            "course_id": int(course_id),
            "count": len(rows),
            "items": [
                {"id": b.id, "name": b.name, "code": b.code,
                 "entry_count": b.entry_count,
                 "question_count": b.question_count}
                for b in rows
            ],
        })


# ══════════════════════════════════════════════════════════════
#  知识库条目
# ══════════════════════════════════════════════════════════════


class EntryListCreateView(APIView):
    """`GET /api/v1/knowledge/entries/?node_id=` · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        if q.get("node_id"):
            qs = KnowledgeEntrySelector.by_node(
                org_id, int(q["node_id"]),
                status=q.get("status") or None)
        elif q.get("chapter_id"):
            qs = KnowledgeEntrySelector.by_chapter(
                org_id, int(q["chapter_id"]))
        else:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 node_id 或 chapter_id")
        # ★ 预取关联与归属人：否则序列化时每行都要查 `entry_links`
        #   和 `created_by`（一页 100 行 ≈ 300 次 SQL）。
        qs = qs.select_related("created_by").prefetch_related("entry_links")
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            KnowledgeEntrySerializer(
                page, many=True, context=_entry_context(request, page)
            ).data
        )

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = EntryCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★★ 真实签名：`create_entry(*, org_id, content, node_ids,
        #   title="", entry_type, source_type, source_ref, created_by_id)`。
        #
        #   三点与我最初猜的不同：
        #     1. 是 **`node_ids`（列表）** 不是 `node_id`——一条条目
        #        可以链多个知识点（如「力的合成」同属两个知识点）
        #     2. **没有 `chapter_id`**；章节归属由知识点反推
        #     3. **服务层**里 `title` 可选（`title or content[:40]`），
        #        `content` 才是必填；★ 但 `EntryCreateSerializer` 把
        #        `title` 声明成了**必填** —— 接口比服务层更严，
        #        AI 拆条归类必须自己造标题（本次未改，只记录）。
        #
        # ★★ 这行以前读 `v.get("node_ids")`，而**序列化器当时没声明
        #   `node_ids`** —— DRF 对未声明字段静默忽略，于是它永远是
        #   `None`，多知识点建条**在接口上做不了**（模型与服务层都支持）。
        #   现在 `EntryCreateSerializer.validate()` 显式声明并把
        #   `node_id`（向后兼容写法）并入 `node_ids`、去重、两者都空时
        #   直接 400，所以 `v["node_ids"]` 一定非空。
        #   ★ 仍保留 `node_id` 兜底表达式：将来若序列化器被改回
        #     「不保证 node_ids」，这里会退化成单数写法而**不会**
        #     静默建成无关联条目（服务层还会再拦一次空列表）。
        node_ids = [int(x) for x in (
            v.get("node_ids")
            or ([v["node_id"]] if v.get("node_id") else [])
        )]
        e = KnowledgeEntryService.create_entry(
            org_id=org_id,
            node_ids=node_ids,
            title=v["title"], content=v["content"],
            entry_type=v.get("entry_type", "CONCEPT"),
            source_type=v.get("source_type", "TEACHER_INPUT"),
            # ★ `source_ref` 至今**没被 `EntryCreateSerializer` 声明**：
            #   DRF 会静默丢掉调用方传的值，于是这里永远是 `""`
            #   （实测：传 `source_ref="AI 拆条 批次#12"` → 201 但库里为空）。
            #   与本次修的 `node_ids` 是同一个坑，本次未改，只记录。
            source_ref=v.get("source_ref", ""),
            created_by_id=request.user.id,
        )
        return created(
            KnowledgeEntrySerializer(
                e, context=_entry_context(request, [e])
            ).data,
            message="知识库条目已创建")


class EntryAdoptView(APIView):
    """`POST /api/v1/knowledge/entries/adopt/` 批量采纳条目。

    ★ 采纳是「把这条内容认定为可作依据」——它直接影响
      L1 闸门的判据（只有 `ACTIVE` 且已采纳的条目才计入）。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        ids = (request.data or {}).get("entry_ids") or []
        if not ids:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 entry_ids（非空数组）")
        n = KnowledgeEntryService.mark_adopted(
            org_id=org_id, entry_ids=[int(x) for x in ids])
        return ok({"adopted": n}, message=f"已采纳 {n} 条")


class EntryArchiveView(APIView):
    """`POST /api/v1/knowledge/entries/{id}/archive/` 归档条目。

    ★ 权限 2026-10-23 起从「学校平台账户」放宽到「教师 + 归属守卫」：
      条目归属老师个人（需求方拍板），归档只是把自己的条目下架，
      不该比编辑更严。**采纳（adopt）仍只给学校账户** ——
      那是对全校 AI 生题门禁的表态，不是个人动作。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        # ★ 真实签名只有 `(org_id, entry_id)`——没有 `operator_id`。
        existing = KnowledgeEntrySelector.get(org_id, pk)
        if existing is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="条目不存在")
        KnowledgeEntryService.assert_entry_writable(existing, request.user)
        e = KnowledgeEntryService.archive_entry(
            org_id=org_id, entry_id=pk)
        return ok(
            KnowledgeEntrySerializer(
                e, context=_entry_context(request, [e])
            ).data,
            message="已归档")


class EntryDetailView(APIView):
    """`/api/v1/knowledge/entries/{id}/` 单条条目的查 / 改 / 删。

    ★ 三个方法一次给出，而不是「详情一个视图、改删各一个」：
      它们共用同一把**依赖校验闸门**（Q7：关联知识点下有题就不许改删），
      拆成三个视图会让那把闸门的调用点散落三处，漏一处就是绕过。

    ★★ 权限（2026-10-23 需求方拍板「条目归属老师个人，由老师自行建立」）：

        GET     教师及以上 —— **读全机构共享**
        PATCH   教师及以上 + **归属守卫**
        DELETE  教师及以上 + **归属守卫**

      归属守卫在 `KnowledgeEntryService.assert_entry_writable()`：
      学校平台账户可改全机构；教师只能改自己创建的；
      `created_by IS NULL`（学校统一导入的公共条目）只有学校账户能动。

      ★ 为什么**读不隔离**（与题目/试卷的 `scope_owned_or_public` 不同）：
        条目是全校 AI 生题的共同依据，`ai_call_log.kb_entry_ids` 按机构记。
        若列表按人收窄，老师会看到「AI 引用了一条我打不开的条目」，
        而 L1 门禁的条目计数仍是全机构的 —— 界面说没依据、AI 说够用。
        所以：**读共享，写归个人。**
      ★ 但「能不能改」仍是一道闸门：`KnowledgeEntry` 是 AI 生题的**依据**，
        改它等于改 AI 的输入，所以即便本人创建，Q7（关联知识点下有题）
        依然会拦。

    ★ 为什么这里**不写**归档接口的完整路径：`verify_api_contracts.py` 会扫
      docstring 里的 HTTP 方法名，扫到那个端点的动词就会认为「本视图承诺了
      它却没实现」→ **假红**。★ 假红会训练人忽略告警，所以宁可换个说法；
      归档接口的完整路径见 `urls.py`。

    ★ 路由顺序：`entries/adopt/` 必须排在 `entries/<int:pk>/` 之前
      （见 `urls.py` 的说明），本视图注册在后面。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def _guard(self, request: Request, pk: int) -> None:
        """归属守卫。★ 用 `_base_manager` 取，**已软删的也要拦** ——
        否则教师可以对别人已删除的条目调 DELETE 拿到 200（幂等分支），
        等于多出一条「能操作别人条目」的证据。
        """
        org_id = _org_id(request)
        entry = (
            KnowledgeEntry._base_manager
            .filter(pk=pk, org_id=org_id)
            .first()
        )
        if entry is not None:
            KnowledgeEntryService.assert_entry_writable(entry, request.user)

    def get(self, request: Request, pk: int) -> Response:
        """`GET` 单条详情（含 `node_ids`）。"""
        org_id = _org_id(request)
        e = KnowledgeEntrySelector.get(org_id, pk)
        if e is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="条目不存在")
        return ok(
            KnowledgeEntrySerializer(
                e, context=_entry_context(request, [e])
            ).data
        )

    def patch(self, request: Request, pk: int) -> Response:
        """`PATCH` 更新条目。

        ★ 只接受可编辑字段（见 `EntryUpdateSerializer` 的说明）：
          外键归属（`course_id` / `chapter_id`）与统计字段传了就是 400。
        ★ 依赖校验在服务层：关联的任一知识点下有题 → 409
          `KNOWLEDGE.ENTRY_IN_USE`，`data.blocking` 列出是哪几个知识点。
        """
        org_id = _org_id(request)
        self._guard(request, pk)
        s = EntryUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        v = dict(s.validated_data)
        if "node_ids" in v:
            v["node_ids"] = [int(x) for x in v["node_ids"]]
        e = KnowledgeEntryService.update_entry(
            org_id=org_id, entry_id=pk, payload=v,
            operator_id=getattr(request.user, "id", None),
        )
        return ok(
            KnowledgeEntrySerializer(
                e, context=_entry_context(request, [e])
            ).data,
            message="知识库条目已更新")

    def delete(self, request: Request, pk: int) -> Response:
        """`DELETE` **软删**条目（不是归档，也不是物理删除）。

        ★ 与 `POST .../archive/` 的区别写在
          `KnowledgeEntryService.delete_entry` 的 docstring 里：
          归档只改 `status`，行不软删、可逆、不写审计；
          删除打 `deleted_at`、默认查询不可见、必须留审计。

        ★ 返回体里带 `deleted_at` 与 `node_ids`，前端据此把行从列表移除，
          不需要再发一次 `GET`（那时已经 404 了）。
        """
        org_id = _org_id(request)
        self._guard(request, pk)
        e = KnowledgeEntryService.delete_entry(
            org_id=org_id, entry_id=pk,
            operator_id=getattr(request.user, "id", None),
            reason=str((request.data or {}).get("reason") or "")[:200],
        )
        data = KnowledgeEntrySerializer(
            e, context=_entry_context(request, [e])
        ).data
        data["deleted_at"] = e.deleted_at
        return ok(data, message="知识库条目已删除（软删，可在审计中追溯）")


# ══════════════════════════════════════════════════════════════
#  文档导入 · 文本抽取（需求 3 / `P1-18b` 的最后一步：接线）
# ══════════════════════════════════════════════════════════════

#: 抽取上限的 settings 覆盖键（部署级**收紧**上限用；默认不设）。
IMPORT_MAX_BYTES_SETTING = "ZF_KNOWLEDGE_IMPORT_MAX_BYTES"


def _import_max_bytes() -> int:
    """本次抽取的大小上限（字节）。

    ★ 默认值**就是** `extract.DEFAULT_MAX_BYTES`（20MB，与
      `media.storage.KIND_MAX_BYTES["KNOWLEDGE_ATTACH"]` 对齐）——
      不在这里另写一个数字：两处各写一份，阈值迟早各自漂移，
      而「存储层收下了、抽取层又拒了」正是 `extract.py` 里写明的要避免的事。

    ★ `settings.ZF_KNOWLEDGE_IMPORT_MAX_BYTES` 只用来**调小**：
      它让回归能用 1KB 触发「超限」分支（而不是真造一个 20MB 文件），
      也留给将来「某类机构只许传 2MB」这类部署级收紧。

      ★ 它**不是**请求参数：上限若由客户端在请求里指定，那就没有上限。
    """
    raw = getattr(settings, IMPORT_MAX_BYTES_SETTING, None)
    if raw is None or raw == "":
        return DEFAULT_MAX_BYTES
    return int(raw)


class KnowledgeImportExtractView(APIView):
    """`POST /api/v1/knowledge/import/extract/` 上传文档并抽取出文本。

    职责**只有一件**：`文件字节 → 文本`（含结构化 `blocks`）。
    **不入库、不建条目、不调 AI** —— 拆条与自动归类是 `P1-18c`。

    权限：**机构管理员**（`IsSchoolAdmin`）。★ 它会把**整份文档内容**
      读出来放进响应，是「把外部资料批量读进系统」的入口，
      与条目改动同一档（条目的更新/删除同样是机构管理员）。
      ★ 抽取层本身与角色无关：若将来要让教师直接用，
        只需改这一行权限，不必动 `extract.py`。

    请求：`multipart/form-data`，字段名 `file`（**一次一个**）。

    响应 `data`：`extract.ExtractedDoc.as_dict()` 的**全部字段**
      （`filename` / `source_type` / `text` / `char_count` /
      `non_whitespace_count` / `page_count` / `page_char_counts` /
      `paragraph_count` / `encoding` / `byte_size` / `image_count` /
      `heading_count` / `is_empty` / `warnings` / `blocks`）
      ＋ 三个**只为前端分支**的字段：

        `needs_ocr`              疑似扫描件（`SCAN_PDF_WARNING in warnings`）
        `supported_extensions`   当前支持的后缀（`.docx` / `.pdf` / `.txt`）
        `max_bytes`              本次生效的上限（默认 20MB）

      ★ 字段名与 `as_dict()` **逐一对应**，不另起一套（前端只认一套）。

    ★★ 「扫描件 / 空文档」**不是错误**（HTTP 200 + `warnings` 提示）：
      本期不做的只是 OCR，老师仍能拿到（可能为空的）文本自己贴；
      抛 4xx 会让他以为上传失败而反复重试。这与项目里
      `INGEST.SEGMENT_UNCERTAIN` / `PRACTICE.BANK_INSUFFICIENT`
      「非错误码走 200」的口径一致。

    失败：
      400 `COMMON.VALIDATION_ERROR` —— 缺 `file` 字段、0 字节、
          后缀不在白名单（`.doc` 提示「另存为 .docx」）、
          扩展名与内容（魔数）不符、超过 `max_bytes`；
      422 `INGEST.PARSE_FAILED` —— 内容侧读不出来（zip 坏 / PDF 结构坏 /
          PDF 已加密）。
      ★ 两者的判定与文案全部在 `apps.knowledge.extract`（**唯一真源**），
        本视图不重复实现、也不改写它的 `message`。

    ★ 本视图**不读任何业务数据**：没有 ORM 查询、不用 `org_id`。
      （回归里用 `CaptureQueriesContext` 盯着这次抽取调用，断言 0 条 SQL。）
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        """上传 → 抽取 → 原样返回（**不落盘、不入库**）。"""
        s = KnowledgeImportExtractSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        upload = s.validated_data["file"]

        limit = _import_max_bytes()
        content = upload.read()
        doc = extract_document(upload.name or "", content, max_bytes=limit)

        # ★ `as_dict()` 之后**只加不换**：三个附加字段都是新键，
        #   不动它已有的任何一个名字。
        data = doc.as_dict()
        data["needs_ocr"] = SCAN_PDF_WARNING in doc.warnings
        data["supported_extensions"] = list(supported_extensions())
        data["max_bytes"] = limit

        if doc.is_empty:
            message = (
                "文件已读取，但没有取到文本（原因见 warnings）；"
                "本端点只做抽取，未写入任何数据"
            )
        else:
            message = f"已抽取文本 {doc.char_count} 字符（{doc.source_type}）"
            if doc.warnings:
                message += f"；另有 {len(doc.warnings)} 条提示，见 warnings"
        return ok(data, message=message)


# ══════════════════════════════════════════════════════════════
#  文档导入 · AI 拆条归类（需求 3 / `P1-18c`）
# ══════════════════════════════════════════════════════════════


class KnowledgeImportParseView(APIView):
    """`POST /api/v1/knowledge/import/parse/` 把文本交给 AI 拆条 + 自动归类。

    职责**只有一件**：`文本 → 候选条目`（含建议知识点关联）。
    ★★ **不落库** —— 入库是 `/import/commit/`，且必须由老师确认后调用。

    权限：**机构管理员**（`IsSchoolAdmin`）。★ 它与 `/import/extract/` 同一档：
      本端点会**整篇文档**发往 AI 厂商（脱敏后），
      并产生**真金白银**的调用费用，所以与「把外部资料批量读进系统」
      的抽取端点、以及条目改/删保持同一权限档（S-5 / S-2 / S-3）。
      ★ 教师（`TEACHER`）→ `403 AUTH.FORBIDDEN`。

    请求（JSON）：`{text, filename?, node_hints?}`

      `text`        必填。**已抽取的文本**（`/import/extract/` 的响应 `data.text`）。
                    ★★ 本端点**不收文件**：收文件会让「选错文件 → 立刻花掉
                    几十次模型调用」发生，而老师还没看过一个字。
                    先 extract（免费）→ 老师看文本 → 再 parse（花钱）。
                    `.docx` 的标题行带 `#`/`##` 前缀，那就是标题层级，
                    `blocks[].level` 是它的结构版——**不必**再传 blocks。
      `filename`    可选。进 `source_ref` 与提示词（只作溯源/回显）。
      `node_hints`  可选。知识点 id 数组（≤ 50）。服务端会解析出
                    `node_code` / `node_name` 一起送给模型，并**过滤掉**
                    不存在 / 跨机构的 id（不给模型编造 id 的机会）。

    响应 `data`：

      `candidates[]`  每条形如
                      `{index, title, content, entry_type, source_ref,
                        suggested_node_ids, confidence, warnings,
                        section_no, heading_path}`
      `dropped[]`     被丢掉的结构性坏候选（`{section_no, reason, title}`）
      `errors[]`      某一块的 AI 失败（`{section_no, code, detail, call_log_id}`）
      `blocks[]`      切块结果（`{section_no, heading_path, chars}`）
      `node_hints`    `{used, dropped, requested}`
      `ai`            `{calls[], status_counts, cost_yuan_total, all_ok}`
                      —— 每次调用一条，含 `call_log_id` / `status` /
                      `input_tokens` / `output_tokens` / `cost_yuan` / `latency_ms`
      `prompt_template_key` / `prompt_source`（`DB` 或 `BUILTIN`）
      `persisted`     恒为 `false`（**契约**：本端点零副作用）

    ★★ **模型返回坏 JSON 不是 500**：编排层会追加纠正提示重试一次，
      仍不合格则记为 `SCHEMA_INVALID` 进 `errors`，HTTP 仍 **200**，
      `candidates` 为空（或只含其它块的结果）。
      口径与「扫描件走 200」一致：AI 没答好是**业务分支**，
      不是「服务器坏了」——老师要的是「这一块没解析出来，其余可用」，不是 500。

    ★★ **逐块独立**：40 块的长文档里第 7 块超时，只影响第 7 块。

    失败：
      400 `COMMON.VALIDATION_ERROR` —— 缺 `text` / `text` 全空白 /
          切出的块数超过单次上限（文案含实测块数与上限，可照做）。

    ★ **零副作用**：本方法（`import_service.parse_document`）**绝不写**
      `knowledge_entry` / `knowledge_entry_node`；只写 `ai_call_log`
      （调用台账，必须留痕）。回归里同时用「行数不变」与「SQL 里没有
      针对这两张表的写语句」两种方式独立验证。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        """解析（**预览**）：返回候选条目，不落库。"""
        from apps.knowledge.import_service import parse_document

        s = KnowledgeImportParseSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        data = parse_document(
            org_id=_org_id(request),
            text=v["text"],
            filename=v.get("filename") or "",
            node_hints=v.get("node_hints") or [],
            operator_id=getattr(request.user, "id", None),
            batch_ref=v.get("batch_ref") or "",
        )

        n = len(data["candidates"])
        if n:
            message = (
                f"AI 拆出 {n} 条候选条目（{data['blocks_total']} 块，"
                f"已调用 {data['blocks_called']} 次）；"
                f"**未入库**，请老师确认后调用 import/commit/"
            )
            if data["errors"]:
                message += f"；另有 {len(data['errors'])} 块未解析成功，见 errors"
        else:
            message = (
                "AI 没有返回可用候选条目（原因见 errors / dropped）；"
                "本端点未写入任何数据"
            )
        return ok(data, message=message)


class KnowledgeImportCommitView(APIView):
    """`POST /api/v1/knowledge/import/commit/` 把老师确认的候选逐条入库。

    ★★ 权限、错误码与「逐条独立」三件事与 S-2（条目更新）同档：
      **机构管理员**（`IsSchoolAdmin`）、依赖闸门（Q7）用
      `KNOWLEDGE.ENTRY_IN_USE`、一条失败不影响其余。

    请求（JSON）：`{candidates: [...], batch_ref}`

      `candidates`  parse 返回的候选数组（老师可在前端删/改/改挂知识点后提交）。
                    ★ **收得很松**：字段级校验在服务层逐条做，
                    坏的那条进 `failed` 桶（而不是整请求 400）。
      `batch_ref`   批次标识，必填。写进每条审计的 `after.batch_ref`，
                    用于「这一批是谁在什么时候导进来的」对账。

    响应 `data`（★ 三桶形态与 `set_primary_knowledge_batch` 一致）：

      `requested` / `created` / `skipped` / `failed`
      `created_items[]` / `skipped_items[]` / `failed_items[]`
      `created_entry_ids[]`   ★ 精确 id 列表（调用方按它清理/跳转，不必猜）

    ★ `skipped` 的口径：同一次请求里**完全重复**的候选
      （正文 + 知识点集合都相同）只建第一条，其余进 `skipped`
      （`reason = DUPLICATE_IN_BATCH`）；模型确实会在不同块里重复输出同一条内容。

    ★ `failed` 的原因码（HTTP 整体仍 **200**，因为这是批量结果而不是单条失败）：

      | `code` | 触发条件 |
      | --- | --- |
      | `KNOWLEDGE.ENTRY_IN_USE` | 该候选要挂的知识点**下已有题目**（含草稿题）——Q7 闸门，与条目改/删同一个码（HTTP 409 的那个） |
      | `COMMON.VALIDATION_ERROR` | 本体非法：`content` 空 / 没有 `suggested_node_ids` / `entry_type` 不在 8 种类型内 / 知识点 id 不存在或跨机构 |
      | `UNEXPECTED` | 意外异常（已写日志）——**也记账**，否则前端会显示「失败 0」而实际少了 1 条 |

    ★ 成功入库的条目：`source_type = UPLOADED_DOC`、`status = ACTIVE`、
      `source_ref` 沿用候选里的值（空则回落 `{batch_ref}#{n}`），
      并**逐条**写一条 `knowledge_entry.import` 审计（`after` = 条目快照 + batch_ref）。
      ★ 为什么不是 `knowledge_entry.update`：审计要能区分
      「老师手工录入/编辑」与「文档拆条入库」——事后追溯时两者是两件事。

    失败（整请求级）：
      400 `COMMON.VALIDATION_ERROR` —— 缺 `candidates` / `candidates` 为空 /
          缺 `batch_ref` / 超过单次 200 条上限。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        """入库：逐条独立，返回 created / skipped / failed 三桶。"""
        from apps.knowledge.import_service import commit_candidates

        s = KnowledgeImportCommitSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        data = commit_candidates(
            org_id=_org_id(request),
            candidates=v["candidates"],
            batch_ref=v["batch_ref"],
            operator_id=getattr(request.user, "id", None),
        )
        message = (
            f"入库完成：新增 {data['created']} 条，跳过 {data['skipped']} 条（批内重复），"
            f"失败 {data['failed']} 条"
        )
        if data["failed"]:
            message += "；失败原因见 failed_items"
        return ok(data, message=message)


# ══════════════════════════════════════════════════════════════
#  ★ 知识点标签升级（21 文档任务五）
# ══════════════════════════════════════════════════════════════


def _org(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


class AliasListCreateView(APIView):
    """`GET/POST /api/v1/knowledge/aliases/`

    GET  `?node_id=` 查某节点的别名（不传则列全机构）。
    POST `{node_id, alias}` 新增别名。

    ★ 新增时若该别名已指向另一个节点，返回 400 并给出冲突节点：
      「带传动」同时指向两个节点，解析就永远有歧义——
      正确动作是先合并，而不是再加一条。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeAliasService

        node_id = request.query_params.get("node_id")
        return ok({"items": KnowledgeAliasService.list_aliases(
            _org(request), node_id=int(node_id) if node_id else None)})

    def post(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeAliasService

        node_id = request.data.get("node_id")
        alias = request.data.get("alias")
        if not node_id or not alias:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="node_id 与 alias 都必填")
        obj = KnowledgeAliasService.add_alias(
            org_id=_org(request), node_id=int(node_id), alias=str(alias),
            operator_id=request.user.id,
        )
        return created({"id": obj.id, "node_id": obj.node_id,
                        "alias": obj.alias}, message="已添加别名")


class AliasResolveView(APIView):
    """`GET /api/v1/knowledge/aliases/resolve/?text=带传动&course_id=`

    把一段文本解析成知识点（用于导入/批量标注）。
    ★ 命中多个节点时返回 `matched=false` + 候选，**不猜**。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeAliasService

        text = request.query_params.get("text") or ""
        course_id = request.query_params.get("course_id")
        return ok(KnowledgeAliasService.resolve(
            _org(request), text,
            course_id=int(course_id) if course_id else None))


class NodeMergeView(APIView):
    """`POST /api/v1/knowledge/nodes/{id}/merge/`

    `{source_id, target_id, reason}` —— 把 source 并进 target，写可回滚日志。

    ★ 路径里的 `{id}` 必须等于 `target_id`：
      两个 id 都给是为了让 URL 可读（「把这个并进 {id}」），
      而服务层仍按显式 id 执行，避免读错 URL 造成反向合并。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request, pk: int) -> Response:
        from apps.knowledge.tagging import KnowledgeMergeService

        source_id = request.data.get("source_id")
        target_id = request.data.get("target_id") or pk
        if not source_id:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="source_id 必填")
        if int(target_id) != int(pk):
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="target_id 必须与路径中的知识点一致")
        log = KnowledgeMergeService.merge(
            org_id=_org(request), source_id=int(source_id),
            target_id=int(pk), operator_id=request.user.id,
            reason=str(request.data.get("reason") or ""),
        )
        return ok({"log_id": log.id, "counts": log.counts},
                  message="已合并，可按日志回滚")


class NodeMergeRollbackView(APIView):
    """`POST /api/v1/knowledge/merges/{id}/rollback/` 按日志反向恢复。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request, pk: int) -> Response:
        from apps.knowledge.tagging import KnowledgeMergeService

        return ok(KnowledgeMergeService.rollback(
            org_id=_org(request), log_id=pk, operator_id=request.user.id),
            message="已回滚")


class MergeHistoryView(APIView):
    """`GET /api/v1/knowledge/merges/?node_id=` 合并历史。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeMergeService

        node_id = request.query_params.get("node_id")
        return ok({"items": KnowledgeMergeService.history(
            _org(request), node_id=int(node_id) if node_id else None)})


class CoverageView(APIView):
    """`GET /api/v1/knowledge/coverage/?course_id=` 标注覆盖率报表。

    ★ 只有 `CONFIRMED` 计入（AI 的 `PENDING` 不算）：
      否则 AI 一跑覆盖率就「达标」，而没人看过那些标注。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeCoverageService

        course_id = request.query_params.get("course_id")
        return ok(KnowledgeCoverageService.coverage_report(
            _org(request), course_id=int(course_id) if course_id else None))


class TagReviewQueueView(APIView):
    """`GET /api/v1/knowledge/tag-review/` 待确认标注队列。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.tagging import KnowledgeCoverageService

        state = request.query_params.get("state") or "PENDING"
        limit = int(request.query_params.get("limit") or 100)
        return ok(KnowledgeCoverageService.review_queue(
            _org(request), review_state=state, limit=limit))


class TagReviewView(APIView):
    """`POST /api/v1/knowledge/tag-review/{link_id}/` 确认/驳回标注。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        from apps.knowledge.tagging import KnowledgeReviewService

        state = str(request.data.get("state") or "CONFIRMED").upper()
        link = KnowledgeReviewService.review(
            org_id=_org(request), link_id=pk, state=state,
            operator_id=request.user.id,
            reason=str(request.data.get("reason") or ""),
        )
        return ok({"link_id": link.id, "review_state": link.review_state},
                  message="已更新确认状态")
