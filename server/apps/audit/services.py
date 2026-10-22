"""审计日志与数据导出记录 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 本模块被**几乎所有模块**依赖，因此接口要极简且可靠：

    from apps.audit.services import audit_log
    audit_log.record("question.approve", question, operator_id, reason="...")

★★ 设计要点：

  1. **只增不改** —— `AuditLog.save()` 在模型层就拦住了更新。
     审计记录能被改就失去意义。

  2. **禁止解析 request** —— 服务层不碰 HTTP。IP / User-Agent / request_id
     由 `AuditContextMiddleware` 写入 `contextvars`，这里读取
     （见 `apps.audit.context`）。这样任何深度的调用都能带上请求上下文，
     而不需要把 request 一路透传。

  3. **审计失败不能影响主流程** —— 记审计时若出异常，应只记日志不抛出。
     否则「因为写不进去审计日志，所以业务操作失败」是本末倒置。
     但**成绩修改**是例外：那是合规要求，见 `record_strict`。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.db import transaction

from apps.audit.models import AuditLog, DataExportLog
from apps.common.exceptions import ValidationFailed

logger = logging.getLogger(__name__)


class AuditService:
    """审计写入。"""

    # ── 核心入口 ──
    @staticmethod
    def record(
        action: str,
        target: Any,
        operator_id: int | None = None,
        *,
        org_id: int | None = None,
        before: dict | None = None,
        after: dict | None = None,
        reason: str = "",
        strict: bool = False,
    ) -> AuditLog | None:
        """记一条审计。

        参数：
            action: 动作码，用 `AuditAction` 里的值（形如 `question.approve`）
            target: 被操作对象（模型实例）或 `(target_type, target_id)` 元组
            operator_id: 操作人；None 表示系统操作
            org_id: 机构；不传时尝试从 target 推断
            before / after: 变更前后快照（成绩修改必填）
            reason: 操作理由
            strict: True 时审计失败会抛出；默认 False 只记日志

        返回写入的记录；`strict=False` 且失败时返回 None。
        """
        try:
            target_type, target_id = AuditService._resolve_target(target)
            resolved_org = org_id or AuditService._org_of(target)

            ctx = AuditService._context()

            log = AuditLog(
                org_id=resolved_org,
                user_id=operator_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before=before,
                after=after,
                reason=(reason or "")[:200],
                ip=ctx.get("ip"),
                user_agent=(ctx.get("user_agent") or "")[:300],
                request_id=(ctx.get("request_id") or "")[:64],
            )
            # ★ 审计要独立提交：主流程回滚时审计仍应留下痕迹
            #   （例如「尝试改动成绩但事务失败」，这个尝试本身要可追溯）。
            #   但若调用方已在事务里，则跟随其事务，避免出现
            #   「主流程回滚了、审计却commit了」的悬空记录。
            from django.db import connection

            if connection.in_atomic_block:
                log.save()
            else:
                with transaction.atomic():
                    log.save()
            return log
        except Exception:
            if strict:
                raise
            logger.exception(
                "写审计失败 action=%s target=%r operator=%s", action, target, operator_id
            )
            return None

    # ── 便利封装 ──
    @staticmethod
    def record_strict(
        action: str, target: Any, operator_id: int | None = None, **kw: Any
    ) -> AuditLog:
        """严格模式：写入失败会抛出。

        ★ 用于**合规硬要求**的场景：改成绩、删题目、导出数据。
          这些操作的审计缺失是合规问题，不能「静默算了」。
        """
        kw["strict"] = True
        log = AuditService.record(action, target, operator_id, **kw)
        assert log is not None
        return log

    @staticmethod
    def record_diff(
        action: str,
        target: Any,
        operator_id: int | None = None,
        *,
        before: dict,
        after: dict,
        fields: Iterable[str] | None = None,
        reason: str = "",
        strict: bool = True,
    ) -> AuditLog | None:
        """记录**字段级差异**，只保留真正变化的字段。

        ★ 为什么只留变化字段：审计表会被查很久，把整行前后快照都存进去
          会让「到底改了什么」淹没在噪声里。只存变化的字段，
          一眼能看出改动点。

        `fields` 为 None 时比较两个字典的全部键。
        """
        keys = set(fields) if fields is not None else (set(before) | set(after))
        b_diff: dict[str, Any] = {}
        a_diff: dict[str, Any] = {}
        for k in keys:
            bv = before.get(k)
            av = after.get(k)
            if bv != av:
                b_diff[k] = bv
                a_diff[k] = av
        if not b_diff and not a_diff:
            # 没有实际变化就不记，避免审计噪声
            return None
        return AuditService.record(
            action, target, operator_id,
            before=b_diff, after=a_diff, reason=reason, strict=strict,
        )

    # ── 内部工具 ──
    @staticmethod
    def _resolve_target(target: Any) -> tuple[str, int]:
        """把 target 解析成 `(target_type, target_id)`。

        `target_type` 用 `app_label.ModelName` 形式（如 `questionbank.Question`），
        这样查审计时能直接对应到模型，不用维护一张映射表。
        """
        if isinstance(target, tuple) and len(target) == 2:
            return str(target[0]), int(target[1])

        meta = getattr(target, "_meta", None)
        if meta is None:
            raise ValueError(
                f"target 必须是模型实例或 (type, id) 元组，收到 {type(target)!r}"
            )
        pk = getattr(target, "pk", None)
        if pk is None:
            raise ValueError("target 尚未保存（无主键），无法记审计")
        return f"{meta.app_label}.{meta.object_name}", int(pk)

    @staticmethod
    def _org_of(target: Any) -> int | None:
        """从 target 推断机构。"""
        if isinstance(target, tuple):
            return None
        return getattr(target, "org_id", None)

    @staticmethod
    def _context() -> dict[str, Any]:
        """取请求上下文（由中间件写入 contextvars）。

        ★ 取不到时返回空字典，不报错——后台任务、脚本、管理命令
          都没有 HTTP 上下文，但同样需要记审计。
        """
        try:
            from apps.audit.context import get_audit_context

            return get_audit_context()
        except Exception:
            return {}


#: 模块级单例，供 `from apps.audit.services import audit_log` 使用。
#:
#: ★ 为什么用单例而不是一堆散函数：调用点写成
#:   `audit_log.record(...)` 比 `AuditService.record(...)` 短且读起来
#:   像「记一笔账」，在业务代码里出现的频率很高，这个差别会累积。
audit_log = AuditService()


class DataExportService:
    """数据导出记录写入。"""

    @staticmethod
    @transaction.atomic
    def record_export(
        *,
        org_id: int,
        export_type: str,
        operator_id: int,
        scope: dict | None = None,
        row_count: int = 0,
        file_asset_id: int | None = None,
        pii_included: bool = False,
        reason: str = "",
    ) -> DataExportLog:
        """记一次数据导出。

        契约：
          - `operator_id` **必填**：导出必须能追到人。
            库层 `user` 也是非空外键，这里提前给出清晰报错而不是撞库。
          - `pii_included` 决定是否进入「含个人信息导出」清单

        ★ **含 PII 的导出必须留痕**。导出比查询风险高：
          文件一旦落地就无法收回，所以要有「谁、何时、导了哪些范围、
          含不含个人信息」的记录。

        ★ 为什么把 `pii_included` 放进 `scope` JSON 而不是加一列：
          `data_export_log` 的 DDL 已定稿，且 PII 标记天然是
          「本次导出的范围描述」的一部分。放 `scope` 里不破坏表结构，
          查的时候用 `scope__pii_included=True` 即可，索引需求低
          （导出是低频操作）。

        ★ 同时写一条 `data.export` 审计。两条记录用途不同：
          `data_export_log` 面向「导出文件管理」（可追溯文件本身），
          `audit_log` 面向「统一审计查询」。
        """
        if not operator_id:
            raise ValidationFailed(detail="导出必须记录操作人")

        sc = dict(scope or {})
        sc["pii_included"] = bool(pii_included)
        if reason:
            sc["reason"] = reason[:200]

        ctx = AuditService._context()
        if ctx.get("ip"):
            sc["ip"] = ctx["ip"]

        rec = DataExportLog.objects.create(
            org_id=org_id,
            user_id=operator_id,
            export_type=export_type,
            scope=sc,
            row_count=row_count,
            file_asset_id=file_asset_id,
        )
        AuditService.record_strict(
            "data.export",
            ("audit.DataExportLog", rec.id),
            operator_id,
            org_id=org_id,
            after={
                "export_type": export_type,
                "row_count": row_count,
                "pii_included": bool(pii_included),
                "scope": scope or {},
            },
            reason=reason,
        )
        return rec
