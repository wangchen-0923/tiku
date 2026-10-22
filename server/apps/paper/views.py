"""组卷（约束求解、等值、排版导出） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 「组卷 → 定稿」这条链路里，定稿是**不可逆的分界线**：

    定稿前  试卷是草稿，可以随便改题、改分
    定稿后  快照固化；已发布任务的题目不能再变

  所以定稿接口的设计重点是「让阻断项可见」而不是「让老师能强推」。
  `force=True` 只忽略**非阻断告警**；阻断项（B-01…B-06）永远拦。

★ 与题库 API 一致：`org_id` 只从 `request.user` 取。
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from decimal import Decimal, InvalidOperation

from django.http import HttpResponse
import re
from urllib.parse import quote as _url_quote

from typing import Any

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import require_school_admin, IsTeacherAccount
from apps.common.scoping import scope_owned_or_public
from apps.common.responses import branch, created, ok
from apps.paper import composer as C
from apps.paper.selectors import PaperItemSelector, PaperSelector
from apps.paper.serializers import (
    EquivalenceCheckSerializer,
    PaperAddItemsSerializer,
    PaperApplyAllocationSerializer,
    PaperComposeSerializer,
    PaperCreateSerializer,
    PaperDetailSerializer,
    PaperDuplicateSerializer,
    PaperFinalizeSerializer,
    PaperListSerializer,
    PaperPreviewSerializer,
    PaperTargetTotalSerializer,
    PaperUpdateSerializer,
)
from apps.paper.services import EquivalenceService, PaperService


def _org_id(request: Request) -> int:
    """从认证信息取机构 id。★ 绝不受理 query/body 里的 org_id。"""
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                      detail="当前账号未绑定机构，无法访问试卷")
    return int(org_id)


def _must_get(org_id: int, paper_id: int):
    p = PaperSelector.get(org_id, paper_id)
    if p is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="试卷不存在")
    return p


def _finalize_payload(chk) -> dict:
    """把 `FinalizeCheck` 转成前端可直接渲染的结构。

    ★ 阻断项与告警**分开返回**，不要合成一个列表：
      前端要「阻断项红色不可忽略、告警黄色可忽略」，
      合并后就没法区分了。
    """
    # ★ 字段名以 dataclass 定义为准（`code` / `detail` / `blocking` /
    #   `question_id`）。没有 `message` 字段——我第一版凭直觉写了
    #   `i.message`，那会 AttributeError。展示文案由前端按 `code` 映射，
    #   这样多语言与文案调整不用改后端。
    def _one(i) -> dict:
        return {
            "code": i.code,
            "detail": i.detail,
            "question_id": i.question_id,
            "blocking": bool(i.blocking),
        }

    # ★ `can_finalize` / `blocking` / `warnings` 都是 dataclass 上的
    #   **property**，不是方法——只有 `summary()` 是方法。
    #   我第一版全加了括号，`'bool' object is not callable`。
    #   dataclass 上加 `@property` 与加方法的调用形式不同，
    #   这类错误在静态检查里看不出来，只有真跑一次才知道。
    return {
        "can_finalize": chk.can_finalize,
        "summary": chk.summary(),
        "blocking": [_one(i) for i in chk.blocking],
        "warnings": [_one(i) for i in chk.warnings],
    }


# ══════════════════════════════════════════════════════════════
#  试卷
# ══════════════════════════════════════════════════════════════


class PaperListCreateView(APIView):
    """`GET /api/v1/papers/` 列表 · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        status_v = (q.get("status") or "").strip() or None
        purpose = (q.get("purpose") or "").strip() or None
        course_id = q.get("course_id")

        if q.get("only_finalized") in ("1", "true", "True"):
            qs = PaperSelector.finalized(
                org_id,
                course_id=int(course_id) if course_id else None,
            )
        elif course_id:
            qs = PaperSelector.by_course(
                org_id, int(course_id), status=status_v, purpose=purpose)
        else:
            from apps.paper.models import Paper

            qs = Paper.objects.for_org(org_id).select_related("course")
            if status_v:
                qs = qs.filter(status=status_v)
            if purpose:
                qs = qs.filter(purpose=purpose)
            qs = qs.order_by("-id")

        # ★★ 教师看「自己创建的 + 学校公共的」试卷（2026-10-21 规则改版）。
        #   旧版放行**校内全部**（「教师之间借卷」），现在收窄到：
        #     · 自己组的卷                  —— 他的个人产出
        #     · 学校账户建的卷              —— 学校统一印的，本来就该全校用
        #     · 无主的卷（历史/脚本导入）   —— 按公共处理
        #   同事个人组的卷不再可见；要共享请上传公共题库。
        qs = scope_owned_or_public(qs, request.user, owner_field="created_by")

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            PaperListSerializer(page, many=True).data
        )

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = PaperCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        p = PaperService.create_paper(
            org_id=org_id,
            course_id=v["course_id"],
            title=v["title"],
            purpose=v.get("purpose", "QUIZ"),
            compose_mode=v.get("compose_mode", "MANUAL"),
            compose_config=v.get("compose_config") or {},
            difficulty_expect=v.get("difficulty_expect"),
            # ★ 传下去：TC-PAP-107 要求 seed 可被外部钉死
            random_seed=v.get("random_seed"),
            layout_config=v.get("layout_config") or {},
            created_by_id=request.user.id,
        )
        return created(PaperDetailSerializer(
            PaperSelector.get(org_id, p.id)).data)


class PaperDetailView(APIView):
    """`GET/PATCH/DELETE /api/v1/papers/{id}/`"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        return ok(PaperDetailSerializer(p).data)

    def delete(self, request: Request, pk: int) -> Response:
        """删除试卷（软删）。

        ★★ 这条**原来缺失**，而类文档字符串却写着支持 DELETE。

          后果很坏：客户端收到的是 DRF 自动生成的 **405**，
          而很多客户端的清理逻辑不看状态码——于是「删了」但数据还在。
          我自己就中招了：测试脚本里连删 9 张卷，一张都没删掉，
          直到核对数据库才发现残留。

          ★ 教训：文档字符串里承诺的 HTTP 方法，要么实现，要么删掉。
            写了却没实现，比不写更糟——它让人以为已经处理了。

        ★ 业务逻辑（软删 vs 物理删的顺序、定稿拦截、审计）
          都在 `PaperService.delete_paper`，视图只做权限与转发。
        """
        org_id = _org_id(request)
        p = PaperService.delete_paper(
            org_id=org_id, paper_id=pk, operator_id=request.user.id)
        return ok({"id": pk, "deleted": True},
                  message=f"已删除试卷「{p.title}」")

    def patch(self, request: Request, pk: int) -> Response:
        """改试卷元信息（标题/用途/期望难度/排版配置）。

        ★ **定稿后不能改标题以外的内容**？不——标题也能改，
          因为改标题不影响卷面题。但 `purpose` 改了会影响
          「哪些任务能用它」，所以定稿后改用途要留审计。
          这里统一：任何字段的修改都写审计（见服务层）。
        """
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        s = PaperUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        # ★★ 保存前先 `normalize_config`：
        #   · 非法枚举 → **400 并给出收到的值**（猜一个排列会静默出错版面）
        #   · 数值夹区间 / 未知键丢弃 / 文本截断
        #   · 定稿卷**允许保存**（只改呈现，不改题、分、顺序）
        layout_config = v.get("layout_config")
        if layout_config is not None:
            from apps.paper import layout as _L

            try:
                layout_config = _L.normalize_config(layout_config)
            except ValueError as e:
                raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                              detail=str(e))

        updated = PaperService.update_paper(
            org_id=org_id, paper_id=pk, operator_id=request.user.id,
            title=v.get("title"), purpose=v.get("purpose"),
            difficulty_expect=v.get("difficulty_expect"),
            layout_config=layout_config,
            # ★ 传下去：漏了会让 compose_config 被静默忽略
            compose_config=v.get("compose_config"),
        )
        return ok(PaperDetailSerializer(updated).data)


class PaperPreviewView(APIView):
    """`GET /api/v1/papers/{id}/preview/` 试卷预览（**不含答案**）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        return ok(PaperPreviewSerializer(p).data)


class PaperScoreAllocationView(APIView):
    """`GET` / `PUT` `/api/v1/papers/{id}/score-allocation/` 配分建议与期望总分。

    · `GET` —— 读**配分建议**（按目标总分反推每空分值），不写库
    · `PUT` —— 设/清「期望总分」（传 `null` 表示清空）

    ★ 「按建议写入配分」在**另一个**视图
      （`PaperApplyAllocationView`，路径 `.../score-allocation/apply/`）。
      ★ 本 docstring **只描述本类实现的方法** ——
        `verify_api_contracts.py` 会扫描 docstring 里提到的方法名，
        并要求它们都在本类实现。写上别人的方法会被判成「承诺未实现」。

    ★★ 为什么「给建议」与「写入」分开两个动作：

      改配分会连带影响**已发布的作业**与**已录的成绩**。
      「先给建议、老师确认后再写」比「直接改了他的卷子」安全得多。
      所以 `GET` 只读，写入走另一个端点且只允许 DRAFT。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        q = request.query_params
        raw = q.get("target_total")
        target = None
        if raw not in (None, ""):
            try:
                target = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                raise ZfError(
                    ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=f"target_total 必须是数字，收到 {raw!r}",
                )
        return ok(PaperService.score_allocation(
            org_id=org_id, paper_id=pk, target_total=target))

    def put(self, request: Request, pk: int) -> Response:
        """设「期望总分」（可传 `null` 清空）。"""
        require_school_admin(request)
        org_id = _org_id(request)
        s = PaperTargetTotalSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        p = PaperService.set_target_total(
            org_id=org_id, paper_id=pk,
            target_total=v.get("target_total_score"),
            operator_id=request.user.id,
        )
        return ok({
            "paper_id": p.id,
            "target_total_score": (str(p.target_total_score)
                                   if p.target_total_score is not None
                                   else None),
            "total_score": str(p.total_score),
        }, message="已保存")


class PaperApplyAllocationView(APIView):
    """`POST /papers/{id}/score-allocation/apply/` 按建议写入配分。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = PaperApplyAllocationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        res = PaperService.apply_allocation(
            org_id=org_id, paper_id=pk,
            scores=s.validated_data["scores"],
            operator_id=request.user.id,
        )
        return ok(res, message=f"已调整 {res['changed']} 道题的分值")


class PaperExportView(APIView):
    """`GET` / `POST /api/v1/papers/{id}/export/` 导出试卷（PDF / Word / 排版 JSON）。

    **`POST`（推荐，前端页面用）** body：

        {"format": "layout" | "pdf" | "docx",
         "layout_config": {...}}      # 契约 v3 全量参数

      · `layout` → 返回排版 JSON（与 PDF / Word 共用同一份）
      · `pdf` / `docx` → 返回文件流

    **`GET`（保留，脚本依赖）** 查询参数：

        `format`   `pdf`（默认）| `docx`
        `layout`   `1` → 不导出文件，改返回**排版 JSON**
        `margin_mm` / `line_spacing` / `base_font_pt` / `page_size`
        / `orientation` / `show_score` / `title_font_pt`
                   → 覆盖 `paper.layout_config`（只允许标量键）

      ★ v3 已删除 `answers` / `choice_layout` / `opt_style` /
        `show_answer_space` / `answer_space_mm`；读到一律忽略。
        结构化键（`option_cols_by_seq` / `blank_blocks` / 文本类）只走 POST。

    ★★ 两条路**共用** `resolve_config()` + `build_render_data()` +
      `build_layout()`，不得另写一份参数解析。

    ★★ 返回 `HttpResponse` 而**不是** DRF `Response`：
      二进制走统一响应包装会被转成 JSON。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    GET_OVERRIDE_KEYS = (
        "page_size", "orientation", "margin_mm", "line_spacing",
        "base_font_pt", "title_font_pt", "show_score",
    )
    GET_BOOL_KEYS = ("show_score",)
    GET_NUM_KEYS = ("margin_mm", "line_spacing", "base_font_pt",
                    "title_font_pt")

    def get(self, request: Request, pk: int):
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        q = request.query_params

        override: dict[str, Any] = {}
        for k in self.GET_OVERRIDE_KEYS:
            if k in q:
                override[k] = q.get(k)
        for k in self.GET_BOOL_KEYS:
            if k in override:
                override[k] = str(override[k]).lower() in ("1", "true", "yes")
        for k in self.GET_NUM_KEYS:
            if k in override:
                try:
                    override[k] = float(override[k])
                except (TypeError, ValueError):
                    raise ZfError(
                        ErrorCode.COMMON_VALIDATION_ERROR,
                        detail=f"参数 {k} 必须是数字，收到 {q.get(k)!r}",
                    )

        fmt = str(q.get("format", "pdf")).lower()
        if str(q.get("layout", "")).lower() in ("1", "true", "yes"):
            fmt = "layout"
        elif fmt not in ("pdf", "docx"):
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"format 只支持 pdf / docx，收到 {fmt!r}",
            )
        return self._respond(p, fmt=fmt, override=override or None)

    def post(self, request: Request, pk: int):
        """结构化参数入口：body 里给全量 `layout_config`。"""
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        body = request.data or {}
        if not isinstance(body, dict):
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="请求体必须是 JSON 对象")
        fmt = str(body.get("format") or "layout").lower()
        if fmt not in ("layout", "pdf", "docx"):
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"format 只支持 layout / pdf / docx，收到 {fmt!r}",
            )
        override = body.get("layout_config")
        if override is not None and not isinstance(override, dict):
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="layout_config 必须是对象")
        return self._respond(p, fmt=fmt, override=override or None)

    def _respond(self, paper, *, fmt: str,
                 override: dict[str, Any] | None):
        """GET / POST 的公共出口（校验 → JSON 或文件流）。"""
        from apps.paper.export import (
            export_docx,
            export_pdf,
            preview_layout_json,
            resolve_config,
        )

        try:
            cfg = resolve_config(paper, override)
        except ValueError as e:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR, detail=str(e))

        if fmt == "layout":
            return ok(preview_layout_json(paper, layout_config=cfg))

        if fmt == "pdf":
            data, issues = export_pdf(paper, layout_config=cfg)
            ctype = "application/pdf"
            ext = "pdf"
        else:
            data, issues = export_docx(paper, layout_config=cfg)
            ctype = ("application/vnd.openxmlformats-officedocument"
                     ".wordprocessingml.document")
            ext = "docx"
        return _paper_file_response(
            paper, data=data, issues=issues, ctype=ctype, ext=ext)


class PaperAnswerExportView(APIView):
    """`POST /api/v1/papers/{id}/export/answer/` 导出**独立答案 PDF**。

    body：`{"layout_config": {...}}`（契约 v3 全量参数）。

    ★ 只印答案：标题「《卷面标题》参考答案」、按大题分组、组内逐题 `12. A`；
      不含题干、不含解析。**不是**试卷 PDF 的「带答案版」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int):
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        body = request.data or {}
        if not isinstance(body, dict):
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="请求体必须是 JSON 对象")
        override = body.get("layout_config")
        if override is not None and not isinstance(override, dict):
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="layout_config 必须是对象")

        from apps.paper.export import export_answer_pdf, resolve_config

        try:
            cfg = resolve_config(p, override or None)
        except ValueError as e:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR, detail=str(e))

        data, issues = export_answer_pdf(p, layout_config=cfg)
        return _paper_file_response(
            p, data=data, issues=issues, ctype="application/pdf",
            ext="pdf", suffix="-答案")


def _safe_export_title(title: str) -> str:
    """导出文件名里的试卷名称清洗。

    `\\ / : * ? " < > |` 与控制字符 → `_`；连续空白压一个空格；
    去首尾空格；空标题回退「试卷」；最多保留 120 个字符。
    """
    s = str(title or "").strip()
    s = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "试卷"
    return s[:120].strip() or "试卷"


def _paper_file_response(paper, *, data: bytes, issues: list,
                         ctype: str, ext: str,
                         suffix: str = "") -> HttpResponse:
    """把导出字节包成下载响应；阻断项不出文件。

    文件名规则（v3.1）：
      · 试卷：`<paper.title>-试卷.<ext>`
      · 答案：`<paper.title>-答案.pdf`
      · 一律取 `paper.title`（不是 `title_text`，不加《》）
      · ASCII 回退：`paper-{id}-paper.<ext>` / `paper-{id}-answer.<ext>`
      · `filename*` 对整个文件名做 `quote(name, safe="")`
    """
    blocking = [i for i in issues if i.get("blocking")]
    if blocking:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail=blocking[0].get("message", "导出失败"),
            extra={"issues": issues},
        )
    if not data:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail="导出内容为空（试卷里可能还没有题）",
            extra={"issues": issues},
        )
    is_answer = suffix == "-答案"
    label = "答案" if is_answer else "试卷"
    safe_title = _safe_export_title(paper.title)
    filename = f"{safe_title}-{label}.{ext}"
    ascii_name = f"paper-{paper.id}-{'answer' if is_answer else 'paper'}.{ext}"
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{_url_quote(filename, safe='')}"
    )
    if issues:
        resp["X-Zf-Issues"] = _url_quote(
            ";".join(str(i.get("code", "")) for i in issues)[:400])
    return resp


class PaperAnswerKeyView(APIView):
    """`GET /api/v1/papers/{id}/answer-key/` 答案与解析。

    ★ 单独一个接口、单独的权限点：答案不该跟着试卷结构一起返回。
      合并的话，任何一次「把试卷详情响应发给学生」的疏忽都是泄题，
      而这类疏忽在前后端联调时很容易发生（同一个对象复用）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        out = []
        for it in p.items.all().select_related("question").order_by("seq"):
            snap = it.snapshot_json or {}
            q = it.question
            out.append({
                "seq": it.seq,
                "section_title": it.section_title,
                "score": str(it.score),
                "question_id": it.question_id,
                "type": snap.get("type") or q.type,
                # 快照优先：题库事后纠正答案不该改变已考过卷子的答案
                "answer": snap.get("answer") or q.answer,
                "answer_text": snap.get("answer_text") or q.answer_text,
                "explanation_text": (
                    snap.get("explanation_text") or q.explanation_text
                ),
                "explanation_html": (
                    snap.get("explanation_html") or q.explanation_html
                ),
            })
        return ok({
            "paper_id": p.id,
            "title": p.title,
            "status": p.status,
            "items": out,
        })


class PaperItemsView(APIView):
    """`POST /api/v1/papers/{id}/items/` 批量加题 · `DELETE` 删题。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = PaperAddItemsSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        items = PaperService.add_questions(
            org_id=org_id, paper_id=pk,
            items=[
                {"question_id": x["question_id"], "score": x["score"],
                 "section_title": x.get("section_title") or
                 v.get("section_title", ""),
                 "group_key": x.get("group_key", "")}
                for x in v["items"]
            ],
            section_title=v.get("section_title", ""),
            operator_id=request.user.id,
            allow_unapproved=v.get("allow_unapproved", False),
        )
        p = _must_get(org_id, pk)
        return ok(PaperDetailSerializer(p).data,
                  message=f"已添加 {len(items)} 道题")

    def delete(self, request: Request, pk: int) -> Response:
        """删一个卷面题。

        ★ 要传 `item_id`（卷面题 id），不是 `question_id`——
          同一道题理论上不该在一份卷里出现两次，
          用 `question_id` 看似等价，但一旦出现重复就删错。
        """
        org_id = _org_id(request)
        _must_get(org_id, pk)
        item_id = request.query_params.get("item_id")
        if not item_id:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="需要传 item_id（卷面题 id）")
        PaperService.remove_item(
            org_id=org_id, paper_id=pk, item_id=int(item_id))
        return ok({"item_id": int(item_id)}, message="已移除")


class PaperFinalizeCheckView(APIView):
    """`GET /api/v1/papers/{id}/finalize-check/` 定稿前检查。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        chk = PaperService.check_finalize(org_id, pk)
        return ok(_finalize_payload(chk))


class PaperFinalizeView(APIView):
    """`POST /api/v1/papers/{id}/finalize/` 定稿。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = PaperFinalizeSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        # ★ 定稿前把检查结果一并返回：万一被拦，前端能直接显示原因，
        #   不必再调一次 check 接口（而多一次往返就多一次状态可能变化）。
        p = PaperService.finalize_paper(
            org_id=org_id, paper_id=pk, operator_id=request.user.id,
            force=v.get("force", False), reason=v.get("reason", ""),
        )
        return ok(PaperDetailSerializer(p).data, message="已定稿")


class PaperDuplicateView(APIView):
    """`POST /api/v1/papers/{id}/duplicate/` 派生 AB 卷 / 补考卷。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = PaperDuplicateSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        new = PaperService.duplicate_paper(
            org_id=org_id, paper_id=pk,
            variant_label=v.get("variant_label", ""),
            title=v.get("title", ""),
            share_group=v.get("share_group", True),
            created_by_id=request.user.id,
        )
        return created(PaperDetailSerializer(
            PaperSelector.get(org_id, new.id)).data, message="已派生")


class PaperRefreshSnapshotsView(APIView):
    """`POST /api/v1/papers/{id}/refresh-snapshots/` 刷新快照。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        p = _must_get(org_id, pk)
        n = PaperService.refresh_snapshots(org_id=org_id, paper_id=pk)
        return ok({"paper_id": pk, "refreshed": n},
                  message=f"已刷新 {n} 道题的快照")


# ══════════════════════════════════════════════════════════════
#  统计 / 候选 / 等值
# ══════════════════════════════════════════════════════════════


class PaperAnalysisView(APIView):
    """`GET /api/v1/papers/{id}/analysis/` 试卷分析（难度/题型/知识点覆盖）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        cov = PaperSelector.coverage(org_id, pk)
        return ok({
            "paper_id": pk,
            "total_score": PaperItemSelector.total_score_of(org_id, pk),
            "difficulty_distribution": PaperSelector.difficulty_distribution(
                org_id, pk),
            "type_distribution": PaperSelector.type_distribution(org_id, pk),
            # ★ 同样是 property：`coverage` / `uncovered_count` 不加括号。
            "coverage": {
                "total_nodes": cov.total_nodes,
                "covered_nodes": cov.covered_nodes,
                "coverage": cov.coverage,
                "uncovered_count": cov.uncovered_count,
            },
        })


class PaperCandidatesView(APIView):
    """`GET /api/v1/papers/candidates/` 组卷候选池。

    ★ 走 `PaperSelector.usable_questions()`——它内部调
      `usable_for_paper()`，是**组卷取数唯一入口**。
      老师在这个界面上看到的题，就是能被选进卷子的题。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = PaperSelector.usable_questions(
            org_id,
            paper_id=int(q["paper_id"]) if q.get("paper_id") else None,
            course_id=int(q["course_id"]) if q.get("course_id") else None,
            task_ids=_ints(q.get("task_id")),
            node_ids=_ints(q.get("node_id")),
            question_types=_strs(q.get("type")),
            difficulty_bands=_strs(q.get("difficulty_band")),
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        from apps.questionbank.serializers import QuestionListSerializer

        data = QuestionListSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


class PaperEquivalenceView(APIView):
    """`POST /api/v1/papers/equivalence/` AB 卷等值检查。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = EquivalenceCheckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        eq = EquivalenceService.check_and_record(
            org_id=org_id,
            paper_a_id=v["paper_a_id"], paper_b_id=v["paper_b_id"],
            operator_id=request.user.id,
        )
        # ★ 字段名以模型为准：`expect_rate_a/b`（期望得分率）、
        #   `diff`（差值）、`knowledge_jaccard`（知识点重叠）。
        #   没有 `score_rate_*` / `knowledge_overlap` / `checked_at`。
        return ok({
            "id": eq.id,
            "paper_group_id": eq.paper_group_id,
            "passed": eq.passed,
            "expect_rate_a": str(eq.expect_rate_a),
            "expect_rate_b": str(eq.expect_rate_b),
            "diff": str(eq.diff),
            "knowledge_jaccard": str(eq.knowledge_jaccard),
            "detail": eq.detail,
            "created_at": eq.created_at,
        })


class PaperComposeView(APIView):
    """`POST /api/v1/papers/{id}/compose/` 按规则自动组卷。

    ★★ 这是一条**可重入**的接口：重复调用会**重建**整个卷面，
      而不是往后面累加。理由是「按规则组卷」的语义是
      「把这份卷子组出来」，不是「再组一份加进去」。
      累加的话老师点两次就会得到题量翻倍的卷子，而且看不出为什么。

    ★★ 候选不足时返回 **200 + `ok=false`（业务分支）**，不是 409。

      12 文档 TC-PAP-102 写的是「`PAPER.CANDIDATE_INSUFFICIENT` 409
      + 缺口清单」。这里用 200 的理由：**缺口是可预期的业务结果，
      不是异常**。老师收到的应该是「这个知识点只有 3 道题，你要
      少要 2 道还是换个知识点」，而不是一个红色错误框。

      ★ 这个偏差**必须说明**：后端仍用 409 语义的错误码，
        我把状态码放宽到 200 并保留同一个 `code`，
        前端按 `code` 分支即可（见 `docs/开发进度记录.md` 的偏差记录）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = PaperComposeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        paper = _must_get(org_id, pk)
        if paper.compose_mode != "RULE":
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"该试卷的组卷方式是 {paper.compose_mode}，"
                       f"不能按规则组卷。请改为 RULE 或走手动加题接口",
            )
        if not (paper.compose_config or {}).get("sections"):
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail="该试卷没有 compose_config.sections，无法按规则组卷",
            )

        try:
            result = PaperService.compose_by_rule(
                org_id=org_id, paper_id=pk,
                candidate_limit=v["candidate_limit"],
                operator_id=request.user.id,
                allow_partial=v["allow_partial"],
            )
        except C.ComposeConfigError as exc:
            # 配置本身写错 → 这是**真的用错了**，走错误而不是分支
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"compose_config 不合法：{exc}",
            ) from exc
        except C.CandidateInsufficientError as exc:
            # ★ 候选不足 → 业务分支（200），带上缺口清单
            return branch(
                ErrorCode.PAPER_CANDIDATE_INSUFFICIENT,
                {
                    "gaps": exc.gaps,
                    "message": str(exc),
                },
                message=f"题量不足，无法组卷：{exc}",
            )

        result["paper"] = PaperDetailSerializer(
            PaperSelector.get(org_id, pk)).data
        return ok(result)


def _ints(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    out = []
    for p in str(raw).replace("，", ",").split(","):
        p = p.strip()
        if p:
            try:
                out.append(int(p))
            except ValueError:
                continue
    return out or None


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None


class PaperCoverageView(APIView):
    """`GET /api/v1/papers/{id}/coverage/` 发布前覆盖体检（21 文档任务六）。

    ★ 只读：不阻断、不写库。**定稿**时才会真正拿它拦人
      （`finalize_paper` 内有同一份体检）。
      分开的理由：组卷页要反复看「还缺什么」，
      而每次看都触发一次拦截会很烦。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.paper.coverage import check_paper_coverage

        org_id = _org_id(request)
        try:
            return ok(check_paper_coverage(org_id, pk))
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc
