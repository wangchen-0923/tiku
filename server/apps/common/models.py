"""跨模块公共模型 · 异步任务运行记录。

★★ 为什么需要一张**独立的**任务表，而不是复用各业务表的状态字段：

  异步化之后，用户点完按钮就走了。他回来要问三件事：
  「跑完了吗」「跑到哪了」「为什么没成」。
  这三件事在业务表里**答不出来**：
    · 批改：`score_result` 只反映「哪些题已判」，答不出「总共要批多少人」
      —— 后者取决于点按钮那一刻的已交卷名单，事后查不到；
    · AI 生题：`ai_call_log` 只记「某次调用」，答不出「我这次请求的结果在哪」。

  所以需要一条**受理凭据**：点下去就落一行，跑完更新这一行。
  它同时也是「前端轮询的唯一入口」。

★ 不做的两件事（避免过度设计）：

  ① **不做任务结果文件存储**。`result` 只放摘要与业务主键，
     明细一律回业务表查 —— 否则同一份数据两处存，必然不一致。
  ② **不做任务取消的强制中断**。`CANCELLED` 只是「标记不再继续」，
     已经跑完的那部分成果保留（批改是有价值的，不该因为点了取消就回滚）。

★ 与 `TaskKind` 的关系：**只登记真有异步需求的类型**。
  空壳枚举会诱使后来者「先建个类型以后再填」，最终造出无业务需求的任务
  （U28b 之前 14 个空壳 tasks 就是这么来的）。加类型时同步加消费者。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.managers import BaseModel


class TaskKind(models.TextChoices):
    """任务类型。

    ★ 每加一个值，必须同时有**投递方**（dispatch 函数）和**消费者**（task），
      否则 `GET /tasks/` 里会永远出现 `PENDING` 的幽灵行。
    """

    AUTO_GRADE = "AUTO_GRADE", "整班自动批改"
    AI_GENERATE = "AI_GENERATE", "AI 生题"


class TaskRunStatus(models.TextChoices):
    """任务状态。

    ★★ 六个状态里最容易搞错的是 `PARTIAL`，所以单独说明：

      `SUCCESS` 要求**每一项都成功**；`PARTIAL` 是「跑完了，但有失败项」。
      把部分失败报成 `SUCCESS` 的后果是：老师看到绿色的「已完成」，
      却找不到任何一个学生的分数 —— 而错误信息只写在日志里。
      反之报成 `FAILED` 又会让「90 个人批好、2 个人失败」看起来全废。

      `PARTIAL` 必须同时带 `failed > 0` 与 `result.failed_items`。
    """

    PENDING = "PENDING", "排队中"
    RUNNING = "RUNNING", "执行中"
    SUCCESS = "SUCCESS", "全部成功"
    PARTIAL = "PARTIAL", "部分成功"
    FAILED = "FAILED", "失败"
    CANCELLED = "CANCELLED", "已取消"


#: 终态集合。★ 只有这几个状态允许写 `finished_at`。
TERMINAL_STATUSES: frozenset[str] = frozenset({
    TaskRunStatus.SUCCESS,
    TaskRunStatus.PARTIAL,
    TaskRunStatus.FAILED,
    TaskRunStatus.CANCELLED,
})


class TaskRun(BaseModel):
    """一次异步任务的运行记录。

    生命周期::

        PENDING ──投递成功──→ RUNNING ──全部成功──→ SUCCESS
           │                    │
           │                    ├──有失败项──→ PARTIAL
           │                    └──整体失败──→ FAILED
           └──投递失败（回退同步，见 dispatch_*）──→ FAILED

    ★ `PENDING` **不表示一定在队列里**：投递失败时会立刻转 `FAILED`
      并把异常写进 `error`。所以「一直是 PENDING」只可能是
      worker 没启动或任务名路由错 —— 这两种都在
      `scripts/verify_celery_wiring.py` 的死信检测里。
    """

    kind = models.CharField(
        _("任务类型"), max_length=20, choices=TaskKind.choices, db_index=True,
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=TaskRunStatus.choices,
        default=TaskRunStatus.PENDING, db_index=True,
    )

    # ── 业务定位 ──
    #
    # ★ `biz_type` + `biz_id` 是**弱引用**（不加外键）：
    #   任务记录不该因为业务行被清理而级联消失 —— 它是「这次操作发生过」
    #   的证据。反过来，业务行删了以后任务详情里查不到明细，属可接受降级。
    biz_type = models.CharField(
        _("业务类型"), max_length=40, blank=True, default="",
        help_text=_("assignment / paper / knowledge_entry …"),
    )
    biz_id = models.BigIntegerField(
        _("业务主键"), null=True, blank=True,
    )

    # ── 进度 ──
    #
    # ★★ 三个计数都用 `F()` 原子更新（见 `task_runs.bump`），
    #    **不要** `run.done += 1; run.save()`：批改是并发跑的，
    #    后写的会覆盖先写的，最终 done 恒等于 1。
    total = models.IntegerField(_("总数"), default=0)
    done = models.IntegerField(_("已完成"), default=0)
    failed = models.IntegerField(_("失败数"), default=0)

    # ── 执行痕迹 ──
    celery_task_id = models.CharField(
        _("Celery 任务 id"), max_length=64, blank=True, default="",
        help_text=_("★ 与 broker 对账用；投递失败时为空串"),
    )
    params = models.JSONField(
        _("入参快照"), default=dict, blank=True,
        help_text=_("★ 可 JSON 序列化的那份，便于原样重投"),
    )
    result = models.JSONField(
        _("结果摘要"), default=dict, blank=True,
        help_text=_("只放摘要与业务主键，明细回业务表查"),
    )
    error = models.TextField(
        _("错误信息"), blank=True, default="",
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("发起人"),
        help_text=_("★ 教师端「我的任务」按此收窄；系统级任务为空"),
    )

    started_at = models.DateTimeField(_("开始时间"), null=True, blank=True)
    finished_at = models.DateTimeField(_("结束时间"), null=True, blank=True)

    class Meta:
        db_table = "task_run"
        verbose_name = _("异步任务")
        verbose_name_plural = _("异步任务")
        indexes = [
            # 列表页默认排序：机构 + 倒序时间
            models.Index(fields=["org", "-created_at"], name="idx_task_run_org"),
            # 「我的任务」与按类型筛
            models.Index(
                fields=["org", "created_by", "-created_at"],
                name="idx_task_run_owner",
            ),
            models.Index(
                fields=["org", "kind", "status"], name="idx_task_run_kind",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"任务{self.pk}({self.kind}/{self.status})"

    # ── 派生属性（只读，供序列化用）──
    #
    # ★ 不落库：进度百分比是 `done/total` 的纯函数，
    #   存一份就会有「存的值与计数不一致」的可能，而没人会去核对它。

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def percent(self) -> int:
        """完成百分比（0–100）。

        ★ `total == 0` 时返回 **0 而不是 100**：总数未知不等于「全做完了」。
          AI 生题在受理时 `total=count`，批改受理时 `total=len(student_ids)`，
          都应该是已知的；真出现 0 说明投递时就漏了，要看 `params`。
        """
        if not self.total:
            return 100 if self.is_terminal and self.status == TaskRunStatus.SUCCESS else 0
        return min(100, int(self.done * 100 / self.total))

    def mark_running(self, *, celery_task_id: str = "") -> None:
        """PENDING → RUNNING。★ 幂等：已是 RUNNING 时不重置 `started_at`。"""
        fields = ["status", "updated_at"]
        if self.status == TaskRunStatus.PENDING:
            self.status = TaskRunStatus.RUNNING
            if self.started_at is None:
                self.started_at = timezone.now()
                fields.append("started_at")
        if celery_task_id and not self.celery_task_id:
            self.celery_task_id = celery_task_id
            fields.append("celery_task_id")
        self.save(update_fields=fields)
