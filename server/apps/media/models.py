"""统一资源表（asset）与对象存储 · 数据模型。

★ 铁律：删除 `asset` 前必须 `ref_count = 0`。
  同一张图可能被多道题、多份试卷快照、多个练习引用，
  提前清理会让已发布试卷的配图变成死链。
"""

from __future__ import annotations

from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import AssetKind
from apps.common.managers import OrgScopedManager, SoftDeleteModel


class AssetQuerySet(models.QuerySet):
    """资源查询集。"""

    def cleanable(self, now=None):
        """可清理的资源：无引用 + 已过保留期。"""
        now = now or timezone.now()
        return self.filter(
            ref_count=0,
        ).filter(
            models.Q(retain_until__isnull=True) | models.Q(retain_until__lte=now)
        )

    def by_kind(self, kind: str):
        return self.filter(kind=kind)

    def by_phash(self, phash: int, tolerance: int = 0):
        """按感知哈希查相似图。★ tolerance > 0 时用汉明距离近似。"""
        if tolerance == 0:
            return self.filter(phash=phash)
        # 汉明距离在数据库侧无法直接算整数按位差，交由应用层在候选集上过滤
        return self.filter(phash__isnull=False)


class AssetManager(OrgScopedManager.from_queryset(AssetQuerySet)):  # type: ignore[misc]
    """资源 Manager。★ 取数一律走 for_org()。"""


class Asset(SoftDeleteModel):
    """统一资源表。

    所有二进制资源（题目配图、扫描原图、生成的 PDF、导出包）都在这里登记。
    实际字节存对象存储，本表只存元数据与引用计数。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="assets",
        verbose_name=_("机构"),
    )
    kind = models.CharField(_("资源类型"), max_length=30, choices=AssetKind.choices)

    # ── 对象存储 ──
    oss_key = models.CharField(
        _("对象存储 key"), max_length=500, unique=True,
        help_text=_("格式 zf/{org_id}/{kind}/..."),
    )
    mime = models.CharField(_("MIME 类型"), max_length=100)
    byte_size = models.BigIntegerField(_("字节数"))

    # ── 图片/文档属性 ──
    width = models.IntegerField(_("宽(px)"), null=True, blank=True)
    height = models.IntegerField(_("高(px)"), null=True, blank=True)
    dpi = models.SmallIntegerField(
        _("DPI"), null=True, blank=True, help_text=_("印刷用图需要")
    )
    page_count = models.SmallIntegerField(_("页数"), null=True, blank=True)

    thumb_keys = models.JSONField(
        _("缩略图"), default=dict, blank=True,
        help_text=_('形如 {"sm": "...", "md": "...", "lg": "..."}'),
    )

    # ── 去重与生命周期 ──
    phash = models.BigIntegerField(
        _("感知哈希"), null=True, blank=True, db_index=True,
        help_text=_("图片去重用；64 位整数"),
    )
    #: ★ 内容 SHA-256。扫描导入按它去重（同一页重复上传不重复识别）。
    sha256 = models.CharField(
        _("内容 SHA-256"), max_length=64, blank=True, default="", db_index=True,
    )
    ref_count = models.IntegerField(
        _("引用计数"), default=0,
        help_text=_("★ 归零前不可删除"),
    )
    retain_until = models.DateTimeField(
        _("保留至"), null=True, blank=True,
        help_text=_("到期且无引用才可清理"),
    )

    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。
    objects = AssetManager()

    class Meta:
        db_table = "asset"
        verbose_name = _("资源")
        verbose_name_plural = _("资源")
        indexes = [
            models.Index(
                fields=["org", "kind"], name="idx_asset_org_kind",
            ),
            models.Index(
                fields=["kind", "retain_until"], name="idx_asset_cleanup",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(
                fields=["phash"], name="idx_asset_phash",
                condition=models.Q(phash__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.oss_key}"

    # ── 引用计数（★ 唯一的增减入口，禁止直接改字段）──
    @transaction.atomic
    def add_ref(self, count: int = 1) -> None:
        """增加引用。"""
        if count <= 0:
            raise ValueError("引用增量必须为正")
        type(self).objects.filter(pk=self.pk).update(
            ref_count=models.F("ref_count") + count
        )
        self.refresh_from_db(fields=["ref_count"])

    @transaction.atomic
    def release_ref(self, count: int = 1) -> None:
        """释放引用。"""
        if count <= 0:
            raise ValueError("引用减量必须为正")
        type(self).objects.filter(pk=self.pk).update(
            ref_count=models.functions.Greatest(
                models.F("ref_count") - count, models.Value(0)
            )
        )
        self.refresh_from_db(fields=["ref_count"])

    @property
    def is_cleanable(self) -> bool:
        """是否可清理（无引用且已过保留期）。"""
        if self.ref_count > 0:
            return False
        if self.retain_until is None:
            return True
        return self.retain_until <= timezone.now()

    @property
    def size_display(self) -> str:
        """人类可读体积。"""
        n = float(self.byte_size)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"
