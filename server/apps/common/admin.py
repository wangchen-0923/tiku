"""Django 后台（admin）的共享注册辅助。

★★ 为什么要有这个模块：

  16 个 app 的 `admin.py` 原来**全是空壳**——后台能打开，但里面
  一个模型都没有。48 个业务模型只能靠 shell 看，这在验证环境、
  排查数据问题时非常低效。

  手写 48 个 `ModelAdmin` 类有两个问题：
    1. **必然分叉**：`list_display` / `search_fields` 的写法各人不同
    2. **漏改**：新增字段时没人会回头维护 admin

  所以这里提供 `register_model()`：**按模型字段自动生成**合理的
  `list_display` / `search_fields` / `list_filter`，各 app 的 admin.py
  只需一行调用。

★ 设计边界（刻意不做的事）：

  · **不注册跨机构数据给机构管理员看**。Django admin 是**平台级**
    工具，而本项目的机构隔离靠 `OrgScopedManager`（应用层）。
    admin 走 `_base_manager` 时**会绕过隔离**——所以 admin 仅限
    `is_superuser`（平台管理员）使用，这已在 `admin.site` 的
    默认权限模型里（`is_staff` + 模型权限）。

  · **不用 `admin.site.register` 的自动发现**。Django 默认会
    自动注册所有模型（若 `django.contrib.admin` 的 autodiscover
    生效且 app 的 admin.py 不注册则不会），显式注册更可控。

  · **软删除的模型要能看出「已删」**。列表里加 `deleted_at` 列，
    否则老师/运维会以为数据真的没了。
"""

from __future__ import annotations

import logging
from typing import Any

from django.apps import apps
from django.contrib import admin

logger = logging.getLogger(__name__)

#: 这些字段名不进 `search_fields`
#: ★ 都是「搜了没意义或会拖慢」的：时间、JSON、布尔、二进制
_NO_SEARCH_SUFFIX = ("_at", "_json", "_ref", "hash", "simhash", "embedding")
_NO_SEARCH_TYPES = frozenset({
    "DateTimeField", "DateField", "TimeField", "DurationField",
    "BooleanField", "JSONField", "BinaryField", "DecimalField",
    "FloatField", "IntegerField", "BigIntegerField", "SmallIntegerField",
    "PositiveIntegerField", "PositiveSmallIntegerField", "UUIDField",
    "FileField", "ImageField", "TextField",
})

#: 优先放到 `list_display` 前面的字段（一眼要看到的）
_PRIORITY = (
    "id", "name", "title", "code", "username", "real_name", "seq",
    "status", "type", "question_type",
)


def _searchable(model: Any) -> list[str]:
    """挑出适合 `search_fields` 的字符字段。

    ★ 只放 `CharField`（含 `choices`）：`icontains` 在长文本上会全表扫，
      而 `TextField`（题干、解析）**恰好是最想搜的**——所以对
      `TextField` 单独处理：只在显式指定时才加（见 `register_model`
      的 `extra_search`），避免默认就把 48 张表的文本列全接上。
    """
    out: list[str] = []
    for f in model._meta.get_fields():
        if not hasattr(f, "attname") or f.auto_created:
            continue
        if f.get_internal_type() != "CharField":
            continue
        if f.name is None:
            continue
        if any(f.name.endswith(s) for s in _NO_SEARCH_SUFFIX):
            continue
        out.append(f.name)
    return out[:8]


def _list_display(model: Any) -> list[str]:
    """挑出列表页要显示的列（最多 9 列）。"""
    visible = [
        f.name for f in model._meta.get_fields()
        if hasattr(f, "attname") and not f.auto_created
        and f.get_internal_type() not in ("BinaryField",)
    ]
    picked: list[str] = []
    for p in _PRIORITY:
        if p in visible and p not in picked:
            picked.append(p)
    for n in visible:
        if len(picked) >= 9:
            break
        if n in picked:
            continue
        # 跳过又长又没信息量的
        if n in ("created_at", "updated_at", "deleted_at"):
            continue
        picked.append(n)
    return picked[:9]


def _list_filter(model: Any) -> list[str]:
    """挑出右侧筛选器可用字段。

    ★ 只放 `choices` 字段与布尔：放自由文本会让筛选器变成
      「几百个选项的下拉」，反而不可用。
    """
    out: list[str] = []
    for f in model._meta.get_fields():
        if not hasattr(f, "attname") or f.auto_created:
            continue
        if f.get_internal_type() == "BooleanField" and f.name in (
                "is_active", "is_preset", "is_primary", "is_correct",
                "is_stale", "is_template"):
            out.append(f.name)
        elif getattr(f, "choices", None):
            out.append(f.name)
    return out[:6]


def register_model(
    model: Any,
    *,
    extra_search: list[str] | None = None,
    extra_display: list[str] | None = None,
    extra_filter: list[str] | None = None,
    readonly: list[str] | None = None,
    inlines: list[Any] | None = None,
    ordering: tuple[str, ...] | None = None,
) -> Any:
    """按字段自动生成 `ModelAdmin` 并注册。

    参数全部可选——不传就用自动推断的结果。
    """
    name = model.__name__
    if admin.site.is_registered(model):
        # ★ 幂等：重复注册会抛 `AlreadyRegistered`。
        #   本模块被 16 个 app 的 admin.py 调用，而 Django 的
        #   autodiscover 在某些配置下可能触发两次，所以这里兜住。
        return admin.site._registry[model]

    display = list(_list_display(model))
    for f in (extra_display or []):
        if f not in display:
            display.append(f)
    search = list(_searchable(model))
    for f in (extra_search or []):
        if f not in search:
            search.append(f)
    filters = list(_list_filter(model))
    for f in (extra_filter or []):
        if f not in filters:
            filters.append(f)

    # ★ 软删除的模型：列表里显示 `deleted_at`，
    #   否则运维看不出「这条其实已经删了」。
    has_soft_delete = any(
        f.name == "deleted_at" for f in model._meta.get_fields()
        if hasattr(f, "attname")
    )
    if has_soft_delete and "deleted_at" not in display:
        display.append("deleted_at")

    attrs: dict[str, Any] = {
        "list_display": tuple(display),
        "search_fields": tuple(search),
        "list_filter": tuple(filters),
        "list_per_page": 50,
        # ★ 外键用下拉会为「几万条题目」渲染几万个 option，
        #   页面直接卡死。改用 `raw_id_fields` 的搜索框形式。
        "raw_id_fields": tuple(
            f.name for f in model._meta.get_fields()
            if hasattr(f, "attname") and not f.auto_created
            and f.get_internal_type() == "ForeignKey"
        ),
        "readonly_fields": tuple(readonly or []),
        "show_full_result_count": False,
    }
    if ordering:
        attrs["ordering"] = ordering
    if inlines:
        attrs["inlines"] = tuple(inlines)

    meta = type(f"{name}Admin", (admin.ModelAdmin,), attrs)
    admin.site.register(model, meta)
    logger.debug("admin 已注册 %s", name)
    return admin.site._registry[model]


def register_app(app_label: str, *, exclude: frozenset[str] = frozenset()):
    """把一个 app 下的**所有**模型批量注册。

    ★ 用 `apps.get_app_config(app_label).get_models()`：它只返回
      该 app 自己定义的模型（不含 Django 内建的），正是我们要的。
    """
    cfg = apps.get_app_config(app_label)
    out = []
    for model in cfg.get_models():
        if model.__name__ in exclude:
            continue
        out.append(register_model(model))
    return out
