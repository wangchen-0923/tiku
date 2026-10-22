"""业务异常与错误码映射。

★★ 本文件是 README 第六节「错误码总表」的代码实现。 ★★

设计要点：
  1. **错误码与 HTTP 状态码分离**——同一错误码在不同上下文可用不同状态码；
  2. 标注「非错误」的码（如 `INGEST.SEGMENT_UNCERTAIN`）是**业务分支**，
     返回 HTTP 200，前端按普通响应处理。它们由 `BusinessBranch` 表达；
  3. 所有对外错误必须是 `ZfError` 子类，DRF 异常由 `exception_handler` 统一转换。

禁止：在视图里 `raise Http404` 或直接返回 `Response(status=...)` 表达业务错误。
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


# ══════════════════════════════════════════════════════════════
#  错误码常量（与 README 第六节逐字一致）
# ══════════════════════════════════════════════════════════════
class ErrorCode:
    """全部错误码。命名格式：`<模块>.<语义>`。"""

    # ── 认证与权限 ──
    AUTH_INVALID_CREDENTIALS = "AUTH.INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH.TOKEN_EXPIRED"
    AUTH_FORBIDDEN = "AUTH.FORBIDDEN"
    ORG_MISMATCH = "ORG.MISMATCH"

    # ── 通用 ──
    COMMON_NOT_FOUND = "COMMON.NOT_FOUND"
    COMMON_VALIDATION_ERROR = "COMMON.VALIDATION_ERROR"
    COMMON_RATE_LIMITED = "COMMON.RATE_LIMITED"
    COMMON_IDEMPOTENT_CONFLICT = "COMMON.IDEMPOTENT_CONFLICT"
    COMMON_INTERNAL_ERROR = "COMMON.INTERNAL_ERROR"
    #: ★ 21 文档任务二：状态机禁止的流转（409）。
    #:   消息里必须给出**当前状态与目标状态**，否则老师不知道该怎么办。
    COMMON_INVALID_STATE_TRANSITION = "COMMON.INVALID_STATE_TRANSITION"
    #: ★ 21 文档任务六：组卷体检有阻断项，不许发布（409）。
    PAPER_COVERAGE_BLOCKED = "PAPER.COVERAGE_BLOCKED"

    # ── 题库 ──
    QUESTION_DUPLICATE = "QUESTION.DUPLICATE"
    QUESTION_NOT_APPROVED = "QUESTION.NOT_APPROVED"
    QUESTION_NO_KNOWLEDGE_TAG = "QUESTION.NO_KNOWLEDGE_TAG"

    # ── 题库双轨（v2.0：私有库 / 公共库）──
    #: 题库不存在、不属于本机构、或类型与动作不匹配（400）。
    QUESTION_BANK_INVALID = "QUESTION_BANK.INVALID"
    #: 当前题目状态不允许该动作（提交 / 审核 / 下载 / 下架）（409）。
    #:
    #: ★ 与 `COMMON_INVALID_STATE_TRANSITION` 分开：后者的消息模板是
    #:   「当前状态不允许该操作」，而双轨这里**必须**告诉老师
    #:   「现在是什么状态、要先做什么」（例如「先撤回为草稿再提交」）。
    QUESTION_BANK_INVALID_STATE = "QUESTION_BANK.INVALID_STATE"
    #: 驳回必须填理由（400）。
    QUESTION_BANK_REASON_REQUIRED = "QUESTION_BANK.REASON_REQUIRED"
    #: 下载的来源题已不存在、未审核或被下架（409）。
    QUESTION_BANK_SOURCE_UNAVAILABLE = "QUESTION_BANK.SOURCE_UNAVAILABLE"

    # ── 导入 ──
    INGEST_PARSE_FAILED = "INGEST.PARSE_FAILED"
    INGEST_SEGMENT_UNCERTAIN = "INGEST.SEGMENT_UNCERTAIN"       # ★ 非错误

    # ── 配图（三闸门）──
    FIGURE_PENDING_CONFIRM = "FIGURE.PENDING_CONFIRM"
    FIGURE_BATCH_CONFIRM_REJECTED = "FIGURE.BATCH_CONFIRM_REJECTED"
    FIGURE_MISSING_REFERENCED = "FIGURE.MISSING_REFERENCED"
    FIGURE_SPLIT_BY_PAGE = "FIGURE.SPLIT_BY_PAGE"
    FIGURE_DUPLICATE_ASSET = "FIGURE.DUPLICATE_ASSET"

    # ── 答题卡 ──
    SHEET_BIND_MISMATCH = "SHEET.BIND_MISMATCH"
    SHEET_STUDENT_NO_OVERFLOW = "SHEET.STUDENT_NO_OVERFLOW"
    SHEET_TEMPLATE_VERSION_IMMUTABLE = "SHEET.TEMPLATE_VERSION_IMMUTABLE"
    SHEET_ANCHOR_COORD_MISSING = "SHEET.ANCHOR_COORD_MISSING"    # ★ 见 14 文档 D-45
    #: ★ 作业板块优化需求 3：绑定「校验通过」后即锁定，不允许再换卡（409）。
    #:   换卡会让已打印/已扫描的纸卡与系统对不上，只能重建任务。
    SHEET_BIND_LOCKED = "SHEET.BIND_LOCKED"

    # ── 发布任务 ──
    ASSIGNMENT_WINDOW_CLOSED = "ASSIGNMENT.WINDOW_CLOSED"
    ASSIGNMENT_ALREADY_SUBMITTED = "ASSIGNMENT.ALREADY_SUBMITTED"
    ASSIGNMENT_PAPER_NOT_FINALIZED = "ASSIGNMENT.PAPER_NOT_FINALIZED"

    # ── 扫描 ──
    SCAN_ANCHOR_NOT_FOUND = "SCAN.ANCHOR_NOT_FOUND"
    #: 旧卡没有展示图，且无法从原图重新校正出整页图
    SCAN_DISPLAY_UNAVAILABLE = "SCAN.DISPLAY_UNAVAILABLE"
    SCAN_STUDENT_NO_UNMATCHED = "SCAN.STUDENT_NO_UNMATCHED"      # ★ 非错误
    SCAN_STUDENT_NO_AMBIGUOUS = "SCAN.STUDENT_NO_AMBIGUOUS"

    # ── 批改 ──
    GRADING_SCORE_EXCEED_MAX = "GRADING.SCORE_EXCEED_MAX"
    GRADING_NO_RESPONSE = "GRADING.NO_RESPONSE"
    GRADING_ASSIGNMENT_NOT_GRADED = "GRADING.ASSIGNMENT_NOT_GRADED"

    # ── 组卷 ──
    PAPER_CANDIDATE_INSUFFICIENT = "PAPER.CANDIDATE_INSUFFICIENT"
    PAPER_EQUIVALENCE_FAILED = "PAPER.EQUIVALENCE_FAILED"

    # ── AI ──
    AI_PROVIDER_UNAVAILABLE = "AI.PROVIDER_UNAVAILABLE"
    AI_SCHEMA_INVALID = "AI.SCHEMA_INVALID"
    AI_BUDGET_EXCEEDED = "AI.BUDGET_EXCEEDED"
    AI_KB_INSUFFICIENT = "AI.KB_INSUFFICIENT"                    # ★ 非错误
    AI_CITATION_INVALID = "AI.CITATION_INVALID"

    # ── 个性化练习 ──
    # ★ 业务分支（HTTP 200 + ok=false）：题库不足以组成
    #   符合结构约束的练习。老师要做的是补题，不是重试。
    PRACTICE_BANK_INSUFFICIENT = "PRACTICE.BANK_INSUFFICIENT"

    # ── 导出 ──
    EXPORT_TASK_RUNNING = "EXPORT.TASK_RUNNING"
    EXPORT_LAYOUT_ISSUES = "EXPORT.LAYOUT_ISSUES"

    # ── 知识体系（需求 3 / 决策 Q7）──
    # ★ 「该知识点下已有题目，知识库条目不可改/删」。
    #   口径：**按知识点整体判断**——条目关联的**任一**知识点下有题就拒。
    #   用 409 而不是 400：请求本身没问题，是与**当前数据状态**冲突；
    #   老师要做的动作是「先去处理那几个知识点下的题」，
    #   所以响应还带 `data.blocking`（知识点编码 + 名称 + 题数）指明去哪处理。
    KNOWLEDGE_ENTRY_IN_USE = "KNOWLEDGE.ENTRY_IN_USE"
    #: ★ 2026-10-21（U08/U09）：课程或知识点被引用，不能删。
    #:   与上面那条的区别：上面是「条目改了会让 AI 的依据变」，
    #:   这条是「删了会留下悬空引用」。响应带 `data.references`
    #:   （每项 `{kind,label,count,samples}`），前端直接列给老师看。
    KNOWLEDGE_IN_USE = "KNOWLEDGE.IN_USE"
    #: ★ 课程编码在已有子节点后**不可改**（409）。
    #:   章/任务/知识点的编码都是由 `course.code` 拼出来的，
    #:   改课程编码会让知识点编码与题目引用、纸质教案上的编号全部错位，
    #:   而那些编码已经写进了题目与老师的手写备注里。
    KNOWLEDGE_CODE_LOCKED = "KNOWLEDGE.CODE_LOCKED"


#: 错误码 → 默认 HTTP 状态码。README 中同一码可映射多个状态码时，此处取主用值。
ERROR_CODE_HTTP: dict[str, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.AUTH_TOKEN_EXPIRED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.AUTH_FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.ORG_MISMATCH: status.HTTP_403_FORBIDDEN,
    ErrorCode.COMMON_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.COMMON_VALIDATION_ERROR: status.HTTP_400_BAD_REQUEST,
    ErrorCode.COMMON_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.COMMON_IDEMPOTENT_CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.COMMON_INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.COMMON_INVALID_STATE_TRANSITION: status.HTTP_409_CONFLICT,
    ErrorCode.QUESTION_DUPLICATE: status.HTTP_409_CONFLICT,
    ErrorCode.QUESTION_NOT_APPROVED: status.HTTP_409_CONFLICT,
    ErrorCode.QUESTION_NO_KNOWLEDGE_TAG: status.HTTP_400_BAD_REQUEST,
    ErrorCode.QUESTION_BANK_INVALID: status.HTTP_400_BAD_REQUEST,
    ErrorCode.QUESTION_BANK_INVALID_STATE: status.HTTP_409_CONFLICT,
    ErrorCode.QUESTION_BANK_REASON_REQUIRED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.QUESTION_BANK_SOURCE_UNAVAILABLE: status.HTTP_409_CONFLICT,
    ErrorCode.INGEST_PARSE_FAILED: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCode.INGEST_SEGMENT_UNCERTAIN: status.HTTP_200_OK,
    ErrorCode.FIGURE_PENDING_CONFIRM: status.HTTP_409_CONFLICT,
    ErrorCode.FIGURE_BATCH_CONFIRM_REJECTED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.FIGURE_MISSING_REFERENCED: status.HTTP_409_CONFLICT,
    ErrorCode.FIGURE_SPLIT_BY_PAGE: status.HTTP_409_CONFLICT,
    ErrorCode.FIGURE_DUPLICATE_ASSET: status.HTTP_409_CONFLICT,
    ErrorCode.SHEET_BIND_MISMATCH: status.HTTP_409_CONFLICT,
    ErrorCode.SHEET_STUDENT_NO_OVERFLOW: status.HTTP_400_BAD_REQUEST,
    ErrorCode.SHEET_TEMPLATE_VERSION_IMMUTABLE: status.HTTP_409_CONFLICT,
    ErrorCode.SHEET_ANCHOR_COORD_MISSING: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.SHEET_BIND_LOCKED: status.HTTP_409_CONFLICT,
    ErrorCode.ASSIGNMENT_WINDOW_CLOSED: status.HTTP_409_CONFLICT,
    ErrorCode.ASSIGNMENT_ALREADY_SUBMITTED: status.HTTP_409_CONFLICT,
    ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED: status.HTTP_409_CONFLICT,
    ErrorCode.SCAN_ANCHOR_NOT_FOUND: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCode.SCAN_DISPLAY_UNAVAILABLE: status.HTTP_409_CONFLICT,
    ErrorCode.SCAN_STUDENT_NO_UNMATCHED: status.HTTP_200_OK,
    ErrorCode.SCAN_STUDENT_NO_AMBIGUOUS: status.HTTP_409_CONFLICT,
    ErrorCode.GRADING_SCORE_EXCEED_MAX: status.HTTP_400_BAD_REQUEST,
    ErrorCode.GRADING_NO_RESPONSE: status.HTTP_409_CONFLICT,
    ErrorCode.GRADING_ASSIGNMENT_NOT_GRADED: status.HTTP_409_CONFLICT,
    ErrorCode.PAPER_CANDIDATE_INSUFFICIENT: status.HTTP_409_CONFLICT,
    ErrorCode.PAPER_EQUIVALENCE_FAILED: status.HTTP_409_CONFLICT,
    ErrorCode.PAPER_COVERAGE_BLOCKED: status.HTTP_409_CONFLICT,
    ErrorCode.AI_PROVIDER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.AI_SCHEMA_INVALID: status.HTTP_502_BAD_GATEWAY,
    ErrorCode.AI_BUDGET_EXCEEDED: status.HTTP_402_PAYMENT_REQUIRED,
    ErrorCode.AI_KB_INSUFFICIENT: status.HTTP_200_OK,
    ErrorCode.AI_CITATION_INVALID: status.HTTP_502_BAD_GATEWAY,
    # ★ 业务分支：题库不足不是错误，是「要做的事没做完」
    ErrorCode.PRACTICE_BANK_INSUFFICIENT: status.HTTP_200_OK,
    ErrorCode.EXPORT_TASK_RUNNING: status.HTTP_409_CONFLICT,
    ErrorCode.EXPORT_LAYOUT_ISSUES: status.HTTP_409_CONFLICT,
    # ★ 409：请求合法，但与当前数据状态冲突（该知识点下已有题）
    ErrorCode.KNOWLEDGE_ENTRY_IN_USE: status.HTTP_409_CONFLICT,
    # ★ 409：被引用不能删 / 编码已锁定不能改（U08、U09）
    ErrorCode.KNOWLEDGE_IN_USE: status.HTTP_409_CONFLICT,
    ErrorCode.KNOWLEDGE_CODE_LOCKED: status.HTTP_409_CONFLICT,
}

#: 默认中文提示文案（可在 raise 时覆盖）
ERROR_CODE_MESSAGE: dict[str, str] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: "账号或密码错误",
    ErrorCode.AUTH_TOKEN_EXPIRED: "登录已过期，请重新登录",
    ErrorCode.AUTH_FORBIDDEN: "无操作权限",
    ErrorCode.ORG_MISMATCH: "无权访问其他机构的数据",
    ErrorCode.COMMON_NOT_FOUND: "资源不存在",
    ErrorCode.COMMON_VALIDATION_ERROR: "参数校验失败",
    ErrorCode.COMMON_RATE_LIMITED: "操作过于频繁，请稍后重试",
    ErrorCode.COMMON_IDEMPOTENT_CONFLICT: "相同请求正在处理中",
    ErrorCode.COMMON_INTERNAL_ERROR: "服务内部错误",
    ErrorCode.COMMON_INVALID_STATE_TRANSITION: "当前状态不允许该操作",
    ErrorCode.QUESTION_DUPLICATE: "题目重复",
    ErrorCode.QUESTION_NOT_APPROVED: "题目未审核，不可组卷",
    ErrorCode.QUESTION_NO_KNOWLEDGE_TAG: "缺少知识点标签",
    ErrorCode.QUESTION_BANK_INVALID: "题库不存在或不可用",
    ErrorCode.QUESTION_BANK_INVALID_STATE: "当前状态不允许该操作",
    ErrorCode.QUESTION_BANK_REASON_REQUIRED: "该操作必须填写理由",
    ErrorCode.QUESTION_BANK_SOURCE_UNAVAILABLE: "来源题已不可下载",
    ErrorCode.INGEST_PARSE_FAILED: "文件解析失败",
    ErrorCode.INGEST_SEGMENT_UNCERTAIN: "切题结果需人工校对",
    ErrorCode.FIGURE_PENDING_CONFIRM: "存在未确认的配图",
    ErrorCode.FIGURE_BATCH_CONFIRM_REJECTED: "批量确认条件不满足",
    ErrorCode.FIGURE_MISSING_REFERENCED: "题干提到图但无配图",
    ErrorCode.FIGURE_SPLIT_BY_PAGE: "配图将被分页切断",
    ErrorCode.FIGURE_DUPLICATE_ASSET: "该图与已有配图相似",
    ErrorCode.SHEET_BIND_MISMATCH: "答题卡与试卷结构不匹配",
    ErrorCode.SHEET_STUDENT_NO_OVERFLOW: "学号区超出页面",
    ErrorCode.SHEET_TEMPLATE_VERSION_IMMUTABLE: "模板版本只能新增，不能原地修改",
    ErrorCode.SHEET_ANCHOR_COORD_MISSING: (
        "答题卡模板缺少 anchor_coord 字段，无法确定锚点坐标语义"
    ),
    ErrorCode.SHEET_BIND_LOCKED: "答题卡校验已通过，绑定已锁定，不能再换",
    ErrorCode.ASSIGNMENT_WINDOW_CLOSED: "不在作答时间窗内",
    ErrorCode.ASSIGNMENT_ALREADY_SUBMITTED: "已提交，不允许重复提交",
    ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED: "试卷未定稿，不能发布",
    ErrorCode.SCAN_ANCHOR_NOT_FOUND: "定位点识别失败",
    ErrorCode.SCAN_DISPLAY_UNAVAILABLE: "该卡无法生成校正图，请查看原图",
    ErrorCode.SCAN_STUDENT_NO_UNMATCHED: "学号不在名册中",
    ErrorCode.SCAN_STUDENT_NO_AMBIGUOUS: "学号重复或多涂",
    ErrorCode.GRADING_SCORE_EXCEED_MAX: "分数超过满分",
    ErrorCode.GRADING_NO_RESPONSE: "无作答记录，无法批改",
    ErrorCode.GRADING_ASSIGNMENT_NOT_GRADED: "成绩未确认，不能生成练习",
    ErrorCode.PAPER_CANDIDATE_INSUFFICIENT: "候选题目不足",
    ErrorCode.PAPER_EQUIVALENCE_FAILED: "AB 卷等值不达标",
    ErrorCode.PAPER_COVERAGE_BLOCKED: "试卷体检未通过，不能发布",
    ErrorCode.AI_PROVIDER_UNAVAILABLE: "模型服务暂时不可用",
    ErrorCode.AI_SCHEMA_INVALID: "模型输出格式不符合要求",
    ErrorCode.AI_BUDGET_EXCEEDED: "已超出机构 AI 预算",
    ErrorCode.AI_KB_INSUFFICIENT: "知识库依据不足，已拒绝生成",
    ErrorCode.AI_CITATION_INVALID: "模型引用了不存在的依据",
    ErrorCode.PRACTICE_BANK_INSUFFICIENT:
        "题库不足，无法组成符合结构约束的练习，请先补充题目",
    ErrorCode.EXPORT_TASK_RUNNING: "同类导出任务正在进行中",
    ErrorCode.EXPORT_LAYOUT_ISSUES: "导出前排版检查未通过",
    ErrorCode.KNOWLEDGE_ENTRY_IN_USE:
        "该知识库条目关联的知识点下已有题目，不能修改或删除",
    ErrorCode.KNOWLEDGE_IN_USE: "该对象被其他数据引用，不能删除",
    ErrorCode.KNOWLEDGE_CODE_LOCKED: "编码已被下级节点使用，不能修改",
}


# ══════════════════════════════════════════════════════════════
#  异常基类
# ══════════════════════════════════════════════════════════════
class ZfError(APIException):
    """知否业务异常基类。

    用法::

        raise ZfError(ErrorCode.QUESTION_DUPLICATE, detail="题干与 #1234 重复")

    - `code`：错误码，必须来自 `ErrorCode`
    - `detail`：面向用户的中文提示（不传则用默认文案）
    - `extra`：附加结构化数据，前端可用于跳转或展示（如缺口清单）
    - `http_status`：显式覆盖默认状态码
    """

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.zf_code = code
        self.extra = extra or {}
        message = detail or ERROR_CODE_MESSAGE.get(code, "请求处理失败")
        self.http_status = http_status or ERROR_CODE_HTTP.get(
            code, status.HTTP_400_BAD_REQUEST
        )
        super().__init__(detail=message, code=code)
        # DRF 用 status_code 决定响应状态
        self.status_code = self.http_status


class BusinessBranch(ZfError):
    """业务分支，**不是错误**。

    用于 README 中标注「非错误」的码（如 `INGEST.SEGMENT_UNCERTAIN`）。
    HTTP 返回 200，`code` 仍下发供前端分支处理。
    """

    def __init__(self, code: str, detail: str | None = None, **kw: Any) -> None:
        kw["http_status"] = status.HTTP_200_OK
        super().__init__(code, detail, **kw)


# ══════════════════════════════════════════════════════════════
#  便捷异常（按模块聚合，供业务代码短写）
# ══════════════════════════════════════════════════════════════
class NotFound(ZfError):
    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.COMMON_NOT_FOUND, detail, **kw)


class ValidationFailed(ZfError):
    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.COMMON_VALIDATION_ERROR, detail, **kw)


class Forbidden(ZfError):
    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.AUTH_FORBIDDEN, detail, **kw)


class OrgMismatch(ZfError):
    """跨机构访问。★ 这类请求要记告警日志，可能是攻击行为。"""

    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.ORG_MISMATCH, detail, **kw)


class FigureGateBlocked(ZfError):
    """配图门禁未通过。★ 三闸门之一，未确认配图阻断导入与组卷。"""

    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.FIGURE_PENDING_CONFIRM, detail, **kw)


class QuestionNotUsable(ZfError):
    """题目不可组卷（非 APPROVED，或配图未确认）。"""

    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(ErrorCode.QUESTION_NOT_APPROVED, detail, **kw)


# ══════════════════════════════════════════════════════════════
#  DRF 异常处理器
# ══════════════════════════════════════════════════════════════
def exception_handler(exc, context):
    """统一异常 → `{code, message, data}`。

    在 settings.REST_FRAMEWORK.EXCEPTION_HANDLER 中注册。
    """
    # 业务异常：直接按自带的状态码与错误码返回
    if isinstance(exc, ZfError):
        return _build_response(exc.zf_code, str(exc.detail), exc.extra,
                               exc.http_status)

    # ★★ 领域异常映射：这些不是 `ZfError` 子类（它们在各自的
    #   领域模块里定义，刻意不依赖 `common`），但它们是**业务分支**
    #   而不是内部错误。不映射的话会返回 500「服务内部错误」，
    #   而老师需要看到的是「题库不足，建议先补题」。
    #
    #   我是在实测批量生成时发现这处缺口的：
    #   `InsufficientBankError` 一路冒到顶层变成 500。
    dom = _map_domain_exception(exc)
    if dom is not None:
        code, message, extra, http_status = dom
        return _build_response(code, message, extra, http_status)

    # DRF 标准异常：映射到通用错误码
    response = drf_exception_handler(exc, context)
    if response is not None:
        code, message = _map_drf_exception(exc, response)
        extra = response.data if isinstance(response.data, dict) else {}
        return _build_response(code, message, extra, response.status_code)

    # 未捕获异常：交给 Django 的 500 处理，同时记日志
    import logging

    logging.getLogger("apps.common.exceptions").exception(
        "未捕获异常: %s", exc, exc_info=exc
    )
    return _build_response(
        ErrorCode.COMMON_INTERNAL_ERROR,
        ERROR_CODE_MESSAGE[ErrorCode.COMMON_INTERNAL_ERROR],
        {},
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _map_domain_exception(exc):
    """把**领域异常**映射成业务错误码。

    ★★ 为什么需要这一层：

      `InsufficientBankError` / `StructureConstraintError` 定义在
      `apps.practice.generator`，刻意**不继承** `ZfError`
      （纯算法模块不该依赖 `common` 的异常体系）。

      但它们在业务上是**分支**而不是 bug：

        题库不足   → 可部分成功，应给老师「先补题」的引导
        结构破坏   → 是内部 bug，必须整体失败并告警

      不映射的话两者都变成 500「服务内部错误」——
      而老师看到「服务内部错误」只会反复重试。

    ★ 用**鸭子类型**（按类名匹配）而不是 import：那会让
      `common` 依赖 `practice`，违反分层（下层不能依赖上层）。
      代价是类名改名要同步这里——所以下面用白名单 + 注释说明。
    """
    name = type(exc).__name__

    # 题库不足：业务分支，HTTP 200 + ok=false
    if name == "InsufficientBankError":
        return (
            ErrorCode.PRACTICE_BANK_INSUFFICIENT,
            str(exc) or "题库不足，无法组成符合结构约束的练习",
            {"suggestion": "add_questions"},
            status.HTTP_200_OK,
        )

    # 结构破坏：内部 bug，整体失败
    if name == "StructureConstraintError":
        return (
            ErrorCode.COMMON_INTERNAL_ERROR,
            f"练习结构约束被破坏（内部问题，请反馈）：{exc}",
            {"kind": "STRUCTURE_CONSTRAINT"},
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return None


def _map_drf_exception(exc, response) -> tuple[str, str]:
    """把 DRF 异常映射到我们的错误码。"""
    from rest_framework import exceptions as drf_exc

    if isinstance(exc, drf_exc.ValidationError):
        detail = response.data
        if isinstance(detail, dict):
            first = next(iter(detail.values()), None)
            msg = first[0] if isinstance(first, list) and first else str(first)
        elif isinstance(detail, list) and detail:
            msg = str(detail[0])
        else:
            msg = str(detail)
        return ErrorCode.COMMON_VALIDATION_ERROR, msg

    # ★★ 认异常自带的 `zf_code` / 文案。
    #
    #   背景：认证类（`JWTAuthentication`）会抛 DRF 的
    #   `AuthenticationFailed`，但它携带的是**具体原因**
    #   （「登录已过期，请重新登录」/「凭证类型不正确」）。
    #
    #   我第一版在这里把 `AuthenticationFailed` **一律**映射成
    #   `AUTH_INVALID_CREDENTIALS` + 默认文案「账号或密码错误」，
    #   于是令牌过期时老师看到的是「账号或密码错误」——
    #   他会去反复确认密码，而正确动作是重新登录。
    #
    #   ★ 这条 bug 只在「用一个过期/格式错的令牌请求」时才出现，
    #     正常登录全绿。
    own_code = getattr(exc, "zf_code", None)
    if own_code:
        return own_code, _own_message(exc)

    mapping = {
        drf_exc.NotFound: ErrorCode.COMMON_NOT_FOUND,
        drf_exc.PermissionDenied: ErrorCode.AUTH_FORBIDDEN,
        drf_exc.NotAuthenticated: ErrorCode.AUTH_TOKEN_EXPIRED,
        drf_exc.AuthenticationFailed: ErrorCode.AUTH_INVALID_CREDENTIALS,
        drf_exc.Throttled: ErrorCode.COMMON_RATE_LIMITED,
    }
    for cls, code in mapping.items():
        if isinstance(exc, cls):
            # ★ 认证类抛上来的**具体文案**优先于通用文案：
            #   「登录已过期，请重新登录」比「账号或密码错误」有用得多。
            msg = getattr(exc, "zf_message", None) or _detail_text(exc)
            if msg and cls in (drf_exc.NotAuthenticated,
                               drf_exc.AuthenticationFailed):
                return code, msg
            return code, ERROR_CODE_MESSAGE.get(code, str(exc.detail))
    return ErrorCode.COMMON_VALIDATION_ERROR, str(getattr(exc, "detail", exc))


def _detail_text(exc) -> str:
    """把 DRF 异常的 `detail` 转成一句字符串。

    ★ 认证类抛的 `detail` 可能是 `ErrorDetail`（`str` 子类）或列表，
      直接用会让响应里出现一个 JSON 数组，而契约要求
      `message` 是**面向老师的一句话**。
    """
    d = getattr(exc, "detail", None)
    if d is None:
        return ""
    if isinstance(d, (list, tuple)) and d:
        d = d[0]
    return str(d)


def _own_message(exc) -> str:
    """取异常自带的文案。"""
    msg = getattr(exc, "zf_message", None)
    if msg:
        return str(msg)
    txt = _detail_text(exc)
    if txt:
        return txt
    code = getattr(exc, "zf_code", None)
    return ERROR_CODE_MESSAGE.get(code, "")


def _build_response(code: str, message: str, extra: dict, http_status: int):
    """构造统一错误响应。★ 结构由 responses.ZfJSONRenderer 统一包装。"""
    from rest_framework.response import Response

    return Response(
        {"code": code, "message": message, "data": extra or None},
        status=http_status,
    )
