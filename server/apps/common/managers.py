"""模型基础类与 Manager。

本项目所有业务表都有两个共同特征：
  1. **多租户**：带 `org_id`，查询必须限定在本机构内；
  2. **软删除**：带 `deleted_at`，删除是标记而非物理删除。

★ 隔离的实现方式是「查询集默认过滤」而不是数据库 RLS：
  应用层忘记传 org_id 时会被 Manager 拦住（抛 `OrgScopeRequired`），
  而不是默默返回别的机构的数据。**宁可报错，不可串数据。**
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import QuerySet

T = TypeVar("T", bound="BaseModel")


class OrgScopeRequired(RuntimeError):
    """未指定机构就做了机构隔离查询。

    ★ 这是**编程错误**而非业务异常，因此直接抛 RuntimeError 让测试发现，
      不转成 HTTP 响应。生产环境出现该异常说明有代码漏传 org。
    """


# ══════════════════════════════════════════════════════════════
#  Manager
# ══════════════════════════════════════════════════════════════


class SoftDeleteQuerySet(models.QuerySet):
    """默认只返回未删除记录；`with_deleted()` 可绕过。

    ★★ `delete()` 必须**探测字段**，不能无条件 `update(deleted_at=...)`。

      我原来这里是无条件的，而 `OrgScopedQuerySet` 继承了它。
      于是**任何用 `OrgScopedManager` 但没有 `deleted_at` 的模型**
      （`paper_item`、`question_option`、`question_image`、
      `question_knowledge` …）一旦 `.delete()` 就炸：

          FieldDoesNotExist: PaperItem has no field named 'deleted_at'

      ★ 这个缺陷的形态很坏：`get_queryset()` 早就做了字段探测
        （见 `OrgScopedManager.get_queryset` 的说明），
        所以**读**一直是对的、只有**删**会炸——而在测试里
        往往死在 `finally` 的清理代码上，看起来像「测试写得不对」，
        不像「框架有 bug」。实测就被这个误导了一轮。

      ★ 探测之后：有 `deleted_at` 的走软删（保留历史），
        没有的走物理删（纯关联表/子表，解除关系就是删行）。
    """

    # ★★ 缓存必须**按模型**，不能设在类属性上。
    #
    #   我第一版写的是 `type(self)._has_deleted_at = True/False` ——
    #   而 `SoftDeleteQuerySet` 是**全部模型共用**的一个类，
    #   所以第一次探测到「有 deleted_at」之后，`True` 就被缓存
    #   给了**所有**模型，随后 `paper_item.delete()` 照样去写
    #   `deleted_at` 而炸 —— 修复等于没修。
    #
    #   ★ 而且它**假通过**了我当时的验证：我用
    #     `Model.objects.none().delete()` 测，那是每次新建的
    #     queryset，缓存还没被污染。要触发这个 bug 必须
    #     先用一个软删模型删一次，再用无 `deleted_at` 的模型删。
    _has_deleted_at_cache: dict[type, bool] = {}

    def _model_has_deleted_at(self) -> bool:
        model = self.model
        cached = SoftDeleteQuerySet._has_deleted_at_cache.get(model)
        if cached is None:
            try:
                model._meta.get_field("deleted_at")
                cached = True
            except Exception:
                cached = False
            SoftDeleteQuerySet._has_deleted_at_cache[model] = cached
        return cached

    def delete(self):  # type: ignore[override]
        """批量删除。有 `deleted_at` → 软删；没有 → 物理删。"""
        if not self._model_has_deleted_at():
            return super().delete()
        return super().update(deleted_at=timezone.now())

    def hard_delete(self):
        """物理删除。★ 仅用于清理任务与测试，业务代码禁止调用。"""
        return super().delete()

    def alive(self) -> SoftDeleteQuerySet:
        if not self._model_has_deleted_at():
            return self
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> SoftDeleteQuerySet:
        return self

    def dead(self) -> SoftDeleteQuerySet:
        if not self._model_has_deleted_at():
            return self.none()
        return self.filter(deleted_at__isnull=False)


class OrgScopedQuerySet(SoftDeleteQuerySet):
    """带机构隔离的查询集。"""

    def for_org(self, org_id: int) -> OrgScopedQuerySet:
        return self.filter(org_id=org_id)


class OrgScopedManager(models.Manager.from_queryset(OrgScopedQuerySet)):  # type: ignore[misc]
    """机构隔离 Manager。

    用法::

        Question.objects.for_org(org_id).filter(status=APPROVED)

    ★ 直接调 `.all()` / `.filter()` **不会**自动加机构过滤——
      因为 Manager 拿不到 request 上下文。强制隔离靠两点：
        1. `require_org()`：显式声明必须带机构
        2. `OrgScopeMiddleware` + `selectors` 层的约定（见 01 文档分层职责）
    """

    def get_queryset(self) -> OrgScopedQuerySet:
        """默认排除软删除记录。

        ★ 自适应：**部分关联表没有 `deleted_at` 字段**（如 `task_knowledge`、
          `chapter_knowledge`、`knowledge_entry_node`）。这些是纯关联表，
          解除关联就是物理删行，不需要保留历史。
          这里做字段探测，避免为它们单独定义 Manager，
          也避免误加 `deleted_at__isnull` 导致 `FieldError`。
        """
        qs = super().get_queryset()
        if self._model_has_field("deleted_at"):
            qs = qs.filter(deleted_at__isnull=True)
        return qs

    def _model_has_field(self, name: str) -> bool:
        try:
            self.model._meta.get_field(name)
            return True
        except Exception:
            return False

    def for_org(self, org_id: int) -> OrgScopedQuerySet:
        """★ 唯一的正常取数入口。"""
        if not org_id:
            raise OrgScopeRequired(
                f"{self.model.__name__}.objects.for_org() 需要有效的 org_id，"
                f"收到 {org_id!r}"
            )
        return self.get_queryset().filter(org_id=org_id)

    def with_deleted(self) -> OrgScopedQuerySet:
        """含已删除记录。仅审计与恢复流程可用。"""
        return self.model._base_manager.get_queryset()  # type: ignore[attr-defined]

    def all_orgs(self) -> OrgScopedQuerySet:
        """跨机构查询。★ 仅平台管理员与统计任务可用，业务代码禁用。"""
        return self.get_queryset()


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):  # type: ignore[misc]
    """无机构维度的软删除 Manager（如 `org` 表自身、平台级表）。"""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return super().get_queryset().filter(deleted_at__isnull=True)

    def with_deleted(self) -> SoftDeleteQuerySet:
        return self.model._base_manager.get_queryset()  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════
#  抽象基类
# ══════════════════════════════════════════════════════════════


class TimeStampedModel(models.Model):
    """时间戳基类。★ 全部用 timestamptz（USE_TZ=True），存 UTC。"""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteModel(TimeStampedModel):
    """软删除基类。"""

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = SoftDeleteManager()

    class Meta:
        abstract = True

    def delete(self, using: Any = None, keep_parents: bool = False):  # type: ignore[override]
        """软删除单条。"""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])

    def hard_delete(self, using: Any = None, keep_parents: bool = False):
        """物理删除。★ 仅测试与清理任务可用。"""
        return super().delete(using=using, keep_parents=keep_parents)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class BaseModel(SoftDeleteModel):
    """★ 业务表统一基类（带机构 + 软删除）。

    所有业务表都继承它。表名规则见 README 4.1：小写 + 下划线 + 单数。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.PROTECT,          # ★ 机构不可被误删
        related_name="+",
        db_index=True,
    )

    objects = OrgScopedManager()

    #: 绕过机构过滤的底层 manager（迁移与测试用）
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.org_id:
            raise OrgScopeRequired(
                f"{type(self).__name__} 必须设置 org（机构隔离字段不可为空）"
            )
        super().save(*args, **kwargs)


class PlatformModel(SoftDeleteModel):
    """平台级表（无机构维度，如 `org` 自身、系统提示词模板）。"""

    class Meta:
        abstract = True


# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════


def get_or_none(queryset: QuerySet, **filters: Any):
    """取单条，不存在返回 None（不抛异常）。"""
    return queryset.filter(**filters).first()


def require_org_id(org_id: int | None) -> int:
    """校验 org_id 有效，否则抛错。供 service 层入口使用。"""
    if not org_id:
        raise OrgScopeRequired("该操作需要机构上下文，但未提供 org_id")
    return org_id
