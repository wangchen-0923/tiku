"""状态机最小收口 · 唯一状态入口。

★★ 为什么要有这个模块（21 文档任务二）：

  在这之前，`obj.status = "..."` 散落在服务层各处。后果不是「代码难看」，
  而是三类真实事故：

    1. **非法流转无人拦**：`SCANNED` 被改回 `SUBMITTED`（扫描覆盖线上提交），
       于是「已扫描」的学生在名单里退回「已提交」，反复批改、反复算分。
    2. **终态被复活**：`VOID`（作废）的评分被后续批改重新覆盖，
       作废动作形同虚设。
    3. **改状态没有痕迹**：没人知道「这张卡什么时候从 NEEDS_REVIEW
       变成 REVIEWED 的」——出问题时无法回溯。

  所以这里收口**四个实体**（不追求全量框架）：

       ScanBatch · ScanCard · AssignmentStudent · ScoreResult

★★ 用法（唯一正确姿势）：

      from apps.common.state import transition
      transition(card, "REVIEWED", actor_id=user.id, reason="人工复核")

  ★ `transition()` 负责：校验 → 赋值 → 保存 → 审计。
  ★ 需要事务时由**调用方**开（服务层已有 `@transaction.atomic`）。
  ★ 禁止 `obj.status = ...` 直写；`scripts/check_state_writes.py`
    在 CI 里做静态检查（排除迁移与测试）。

★★ 灰度开关（21 文档第九节「旧代码路径保留一个版本」）：

  `strict=False`（默认）时，非法流转**只告警不抛错**，行为与改造前一致；
  `strict=True` 时抛 `COMMON.INVALID_STATE_TRANSITION`（HTTP 409）。

  新接口一律 `strict=True`；存量链路先用默认值跑一轮，验收后再收紧。
  这样「状态机改造」不会在某天早上把整条扫描链路拦死。
"""

from __future__ import annotations

from typing import Any

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ZfError

#: 审计动作
A_STATE_TRANSITION = "state.transition"


def _model_key(obj: Any) -> str:
    """`ScanCard` → `"omr.ScanCard"`。

    ★ 用 `_meta.label`（`app_label.ModelName`）而不是类名：
      四个实体分属四个 app，类名恰好不重名只是运气。
    """
    meta = getattr(obj, "_meta", None)
    if meta is None:
        return type(obj).__name__
    return f"{meta.app_label}.{meta.object_name}"


#: ★ 转移矩阵：`{模型: {from: {to, ...}}}`。
#:
#: ★★ 规则（21 文档 3.2）：
#:   · `VOID` 是终态，除审计外不可再流转
#:   · `SCANNED` 不得退回 `SUBMITTED`
#:   · `AssignmentStudent` 不回到 `NOT_STARTED`
#:   · **同态流转视为幂等**（重复调用不报错、不重复审计）
TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "omr.ScanBatch": {
        "UPLOADING": frozenset({"PROCESSING", "FAILED"}),
        "PROCESSING": frozenset({"COMPLETED", "WAITING_REVIEW", "FAILED"}),
        # ★ 失败可重试：FAILED → PROCESSING 是唯一出口
        "FAILED": frozenset({"PROCESSING"}),
        # 完成/待仲裁是「已处理完」，允许重扫后回到 PROCESSING
        "COMPLETED": frozenset({"PROCESSING"}),
        "WAITING_REVIEW": frozenset({"PROCESSING", "COMPLETED"}),
    },
    "omr.ScanCard": {
        "PENDING": frozenset({"AUTO_OK", "NEEDS_REVIEW", "FAILED", "VOID"}),
        "AUTO_OK": frozenset({"REVIEWED", "VOID"}),
        "NEEDS_REVIEW": frozenset({"REVIEWED", "VOID"}),
        # ★ 重扫：FAILED → PENDING。
        #   `REVIEWED` 也放行：识别失败但老师能人工指定涂点时，
        #   复核动作本身是合法的（21 文档边界表「切图缺失不阻断评分」
        #   同一思路：识别失败不等于这份作答不能用）。
        "FAILED": frozenset({"PENDING", "REVIEWED", "NEEDS_REVIEW", "VOID"}),
        "REVIEWED": frozenset({"VOID"}),
        # 终态
        "VOID": frozenset(),
    },
    "assignment.AssignmentStudent": {
        "NOT_STARTED": frozenset({"IN_PROGRESS", "SUBMITTED", "SCANNED",
                                  "ABSENT"}),
        "IN_PROGRESS": frozenset({"SUBMITTED", "SCANNED", "ABSENT"}),
        # ★ 扫描覆盖线上提交：SUBMITTED → SCANNED 允许，反向禁止
        "SUBMITTED": frozenset({"SCANNED", "ABSENT"}),
        "SCANNED": frozenset({"ABSENT"}),
        "PARTIAL": frozenset({"SUBMITTED", "SCANNED", "ABSENT"}),
        "ABSENT": frozenset(),
    },
    "grading.ScoreResult": {
        "PENDING": frozenset({"AUTO_GRADED", "NEEDS_REVIEW",
                              "MANUAL_ENTERED", "VOID"}),
        "AUTO_GRADED": frozenset({"TEACHER_MODIFIED", "TEACHER_CONFIRMED",
                                  "NEEDS_REVIEW", "VOID"}),
        "NEEDS_REVIEW": frozenset({"TEACHER_CONFIRMED", "TEACHER_MODIFIED",
                                   "MANUAL_ENTERED", "VOID"}),
        # ★ 人工录分可反复修改（幂等覆盖）
        "MANUAL_ENTERED": frozenset({"MANUAL_ENTERED", "TEACHER_MODIFIED",
                                     "VOID"}),
        "TEACHER_CONFIRMED": frozenset({"TEACHER_MODIFIED", "VOID"}),
        "TEACHER_MODIFIED": frozenset({"TEACHER_MODIFIED", "VOID"}),
        "VOID": frozenset(),
    },
}

#: 状态字段名。四个实体**恰好**都叫 `status`（`ScoreResult` 也叫 `state`，见下）
_STATUS_FIELD: dict[str, str] = {
    "grading.ScoreResult": "state",
}


def state_field(obj: Any) -> str:
    """该实体的状态字段名。"""
    return _STATUS_FIELD.get(_model_key(obj), "status")


def can_transition(obj: Any, to_state: str) -> bool:
    """是否允许流转（不写库、不抛错）。供序列化器/前端做按钮置灰。"""
    key = _model_key(obj)
    matrix = TRANSITIONS.get(key)
    if matrix is None:
        return True                     # 未收口的实体不限制
    cur = str(getattr(obj, state_field(obj)) or "")
    if cur == to_state:
        return True                     # 幂等
    return to_state in matrix.get(cur, frozenset())


def transition(
    obj: Any,
    to_state: str,
    *,
    actor_id: int | None = None,
    reason: str = "",
    extra: dict[str, Any] | None = None,
    strict: bool = False,
    audit: bool = True,
    save: bool = True,
    extra_fields: list[str] | None = None,
) -> Any:
    """唯一状态入口：校验、赋值、保存、审计。

    参数：
        obj:         四个受管实体之一
        to_state:    目标状态
        actor_id:    操作人（None = 系统）
        reason:      变更理由（进审计）
        extra:       附加审计信息（如 `{"responses_written": 12}`）
        strict:      True 时非法流转抛 409；False 时只告警（灰度）
        audit:       False 时跳过审计（调用方已记更完整的审计时用）
        save:        False 时只赋值不落库（调用方自行批量保存）
        extra_fields: 额外要写进 `update_fields` 的字段名

    返回 `obj`，便于链式调用。

    ★★ 同态流转（`cur == to_state`）**直接返回**：
      不保存、不审计。否则每次「重复复核同一张卡」都会写一条审计，
      审计表会被无意义的重复记录淹没。
    """
    field = state_field(obj)
    cur = str(getattr(obj, field) or "")
    key = _model_key(obj)

    if cur == to_state:
        return obj

    matrix = TRANSITIONS.get(key)
    allowed = True if matrix is None else to_state in matrix.get(cur, frozenset())
    if not allowed:
        message = (
            f"{key} 不允许从 {cur or '（空）'} 流转到 {to_state}"
        )
        if strict:
            raise ZfError(
                ErrorCode.COMMON_INVALID_STATE_TRANSITION,
                detail=message,
                extra={"entity": key, "from": cur, "to": to_state},
            )
        # ★ 灰度模式：记 WARNING 继续，行为与改造前一致。
        #   用 logger 而不是 print —— 生产上这条日志就是「收紧前的证据」。
        import logging

        logging.getLogger(__name__).warning("非法状态流转（灰度放行）：%s", message)

    before = {field: cur}
    setattr(obj, field, to_state)

    if save:
        fields = [field]
        if extra_fields:
            for f in extra_fields:
                if f not in fields:
                    fields.append(f)
        # ★ `updated_at` 只有部分模型有；用 `auto_now` 的模型必须显式带上，
        #   否则 `update_fields` 会把它漏掉。
        if any(f.name == "updated_at" for f in obj._meta.fields):
            fields.append("updated_at")
        obj.save(update_fields=fields)

    if audit:
        audit_log.record(
            A_STATE_TRANSITION, obj, actor_id,
            before=before,
            after={field: to_state, **(extra or {})},
            reason=reason,
        )
    return obj


def assert_transition(obj: Any, to_state: str) -> None:
    """只校验不执行（`strict` 版本的 `can_transition`）。"""
    if not can_transition(obj, to_state):
        raise ZfError(
            ErrorCode.COMMON_INVALID_STATE_TRANSITION,
            detail=(f"{_model_key(obj)} 不允许从 "
                    f"{getattr(obj, state_field(obj))} 流转到 {to_state}"),
            extra={
                "entity": _model_key(obj),
                "from": str(getattr(obj, state_field(obj)) or ""),
                "to": to_state,
            },
        )


def allowed_next(obj: Any) -> list[str]:
    """当前状态允许的下一状态列表（前端置灰按钮/后端提示用）。"""
    matrix = TRANSITIONS.get(_model_key(obj))
    if matrix is None:
        return []
    cur = str(getattr(obj, state_field(obj)) or "")
    return sorted(matrix.get(cur, frozenset()))
