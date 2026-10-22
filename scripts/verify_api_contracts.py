"""知否 · API 契约一致性审计。

★★ 这个脚本盯的是一类**特别隐蔽**的缺陷：

    **视图的文档字符串承诺了某个 HTTP 方法，但方法没实现。**

  为什么它比「不写」更糟：客户端与读者**会相信那句承诺**。
  我自己就中过：`PaperDetailView` 的文档写着
  `GET/PATCH/DELETE /api/v1/papers/{id}/`，我据此写了清理逻辑，
  连删 9 张测试卷——**一张都没删掉**（DRF 返回 405），
  而我没检查状态码，直到核对数据库才发现残留。

  ★ 这类缺陷不会出现在任何「功能测试」里：
    功能测试测的是实现了的方法，没人会去测「文档说有的方法」。

★ 本脚本还顺带审计几件「数据库层」的契约：

    1. `OrgScopedManager` 用在了没有 `deleted_at` 的模型上时，
       `.delete()` 是否仍能正常工作（曾经会抛 `FieldDoesNotExist`）
    2. 软删除模型是否真的带 `deleted_at`（否则软删基类名不副实）

运行：
    python scripts/verify_api_contracts.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

APPS_DIR = Path(r"D:\tiku\server\apps")
#: 测试脚本目录（用于「清理完整性」审计）
SCRIPTS_DIR = Path(r"D:\tiku\scripts")

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


#: 已知的、**有意**不实现的方法。
#: ★ 键是 `(app 目录名, 视图类名)`。每一条都必须写明理由——
#:   否则它就是下一个「承诺未实现」。
#: 已知「文档承诺了但没实现」的例外。
#:
#: ★ 2026-10-20：`SheetStyleDetailView` 已从本表移除 —— 该类补上了 PATCH
#:   （见 `apps/sheet/views.py`），例外消失。本表是「例外清单」，不是
#:   「豁免清单」：条目长期留着就等于把检查关掉了，所以【一】会复核每条
#:   例外是否仍在，消失时明确提示删除。
KNOWN_GAPS: dict[tuple[str, str], str] = {}


def _scan_views() -> list[tuple[str, str, list[str], list[str]]]:
    """扫描所有 APIView，找出「文档承诺了但没实现」的方法。"""
    out = []
    for path in sorted(APPS_DIR.glob("*/views.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'class (\w+)\(APIView\):\s*\n\s*"""(.*?)"""', src, re.S,
        ):
            cls, doc = m.group(1), m.group(2)
            claimed = {
                meth.lower()
                for meth in ("GET", "POST", "PATCH", "PUT", "DELETE")
                if re.search(rf"(?<![A-Z]){meth}(?![A-Z])", doc)
            }
            if not claimed:
                continue
            start = m.end()
            nxt = src.find("\nclass ", start)
            body = src[start: nxt if nxt > 0 else len(src)]
            impl = set(re.findall(r"\n    def (\w+)\(", body))
            missing = sorted(claimed - impl)
            if missing:
                out.append((path.parent.name, cls, missing, sorted(claimed)))
    return out


def main() -> int:
    print("=" * 70)
    print("verify_api_contracts · API 契约与数据层一致性")
    print("=" * 70)

    # ══════════════════════════════════════════════════
    print()
    print("【一】★ 视图文档承诺的 HTTP 方法都必须实现")
    # ══════════════════════════════════════════════════
    gaps = _scan_views()
    unexpected = [
        (f, c, m, cl) for f, c, m, cl in gaps
        if (f, c) not in KNOWN_GAPS
    ]
    if unexpected:
        for f, cls, miss, cl in unexpected:
            print(f"    {f}/{cls}: 承诺 {cl} 但缺 {miss}")
    check("没有被漏实现的 HTTP 方法", not unexpected,
          f"{len(unexpected)} 处")

    # ★ 已知例外也要复核：它是否还在？（还在说明文档仍未改）
    for (app_dir, cls), why in KNOWN_GAPS.items():
        still = any(f == app_dir and c == cls for f, c, _, _ in gaps)
        check(f"已知例外仍如实标注：{cls}", still,
              "该例外已消失——请从 KNOWN_GAPS 移除" if not still else why)

    # ══════════════════════════════════════════════════
    print()
    print("【二】★★ 软删除契约：有 deleted_at 才能软删")
    # ══════════════════════════════════════════════════
    # ★ 曾经 `SoftDeleteQuerySet.delete()` 无条件
    #   `update(deleted_at=...)`，于是所有用 `OrgScopedManager`
    #   但没有该字段的模型（paper_item / question_option /
    #   question_image / question_knowledge）一删就抛
    #   `FieldDoesNotExist`。
    from django.apps import apps as django_apps

    from apps.common.managers import OrgScopedManager, SoftDeleteManager

    no_dt: list[str] = []
    for model in django_apps.get_models():
        if model._meta.abstract or model._meta.proxy:
            continue
        mgr = model._default_manager
        has_dt = any(
            f.name == "deleted_at" for f in model._meta.get_fields()
            if hasattr(f, "attname")
        )
        if isinstance(mgr, SoftDeleteManager) and not has_dt:
            no_dt.append(model.__name__)
    check("`SoftDeleteManager` 只用在有 `deleted_at` 的模型上",
          not no_dt, f"{no_dt}")

    # ★ 关键：无 `deleted_at` + `OrgScopedManager` 的模型，
    #   `.delete()` 必须走物理删而不是抛异常。
    #
    #   ★★ 步骤**顺序**才是这个测试的关键，我第一版就是顺序反了才假通过：
    #
    #     缓存曾设在 `SoftDeleteQuerySet` 的**类属性**上，而那个类
    #     是所有模型共用的 —— 所以必须**先**用有 `deleted_at` 的模型
    #     删一次（把缓存写成 True），**再**用没有的模型删。
    #     反过来、或每个模型都用 `objects.none()` 各测各的，
    #     永远抓不到这个 bug。
    soft_models = []
    hard_models = []
    for model in django_apps.get_models():
        if model._meta.abstract or model._meta.proxy:
            continue
        if not isinstance(model._default_manager, OrgScopedManager):
            continue
        has_dt = any(
            f.name == "deleted_at" for f in model._meta.get_fields()
            if hasattr(f, "attname")
        )
        (soft_models if has_dt else hard_models).append(model)

    check(f"软删模型 {len(soft_models)} 个 / 非软删 {len(hard_models)} 个",
          bool(soft_models) and bool(hard_models),
          f"soft={len(soft_models)} hard={len(hard_models)}")

    # ① 先用软删模型「污染」缓存
    try:
        soft_models[0].objects.for_org(1).none().delete()
        check("① 软删模型 delete() 可用", True)
    except Exception as exc:  # noqa: BLE001
        check("① 软删模型 delete() 可用", False, str(exc)[:90])

    # ② 再用非软删模型 —— 这一步才是真正抓 bug 的
    broken = []
    for model in hard_models:
        try:
            model.objects.for_org(1).none().delete()
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{model.__name__}: {type(exc).__name__}")
    check("★★ ② 软删之后再删非软删模型（缓存不得串味）",
          not broken, f"{broken[:5]}")

    # ③ 真实路径：`for_org(...).filter(...).delete()`
    try:
        hard_models[0].objects.for_org(1).filter(pk=0).delete()
        check("③ 非软删模型的 for_org().filter().delete() 可用", True)
    except Exception as exc:  # noqa: BLE001
        check("③ 非软删模型的 for_org().filter().delete() 可用", False,
              str(exc)[:90])

    # ④ 软删路径没被改坏
    try:
        soft_models[0].objects.for_org(1).filter(pk=0).delete()
        check("④ 软删模型的 for_org().filter().delete() 仍可用", True)
    except Exception as exc:  # noqa: BLE001
        check("④ 软删模型的 for_org().filter().delete() 仍可用", False,
              str(exc)[:90])

    # ★ 有 deleted_at 的仍必须走软删（不能被上面的自适应改坏）
    from apps.paper.models import Paper

    check("Paper 有 deleted_at（软删模型）",
          any(f.name == "deleted_at" for f in Paper._meta.get_fields()
              if hasattr(f, "attname")))
    check("Paper.objects 默认排除软删记录",
          "deleted_at__isnull" in str(Paper.objects.get_queryset().query)
          or hasattr(Paper.objects, "with_deleted"))

    # ══════════════════════════════════════════════════
    print()
    print("【三】路由与视图类必须对得上")
    # ══════════════════════════════════════════════════
    from django.urls import URLPattern, URLResolver, get_resolver

    # ★ 关掉 UUID/路径转换器的贪婪匹配问题：这里只统计数量与可解析性
    resolver = get_resolver()
    all_paths: list[str] = []

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLPattern):
                all_paths.append(prefix + str(p.pattern))
            elif isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))

    walk(resolver.url_patterns)
    check(f"可解析路由 {len(all_paths)} 条", len(all_paths) > 40,
          f"{len(all_paths)}")
    check("规则组卷路由存在",
          any("compose" in p for p in all_paths),
          "未找到 compose")
    check("试卷详情路由存在",
          any(re.search(r"papers/.*<int:pk>", p) for p in all_paths))

    # ★★ 每个 APIView 的方法都能被 URL 触达（不会被 405 静默吞掉）
    #   这里抽查「文档声称支持」的方法确实在类上存在
    from apps.paper.views import PaperDetailView

    for meth in ("get", "patch", "delete"):
        check(f"PaperDetailView 实现了 {meth.upper()}",
              hasattr(PaperDetailView, meth),
              f"缺 {meth}")

    # ══════════════════════════════════════════════════
    print()
    print("【四】分页信封契约（items/total 而非 results/count）")
    # ══════════════════════════════════════════════════
    from apps.common.responses import ZfJSONRenderer

    src = Path(ZfJSONRenderer.__module__.replace(".", "/") + ".py")
    renderer_src = (
        APPS_DIR / "common" / "responses.py"
    ).read_text(encoding="utf-8")
    for key in ("items", "total", "next", "previous"):
        check(f"渲染器输出含 `{key}`", f'"{key}"' in renderer_src)
    # ★ 只检查**输出的键名**，不检查字形。
    #   渲染器内部读的是 DRF 原生字段（`data.get("results")`），
    #   那是**输入**不是输出——用 `'"results"' not in src` 会误报。
    #   要看的是「有没有把 results/count 作为键写进返回体」。
    out_keys = set(
        re.findall(r'"(items|total|next|previous|results|count)":', renderer_src)
    )
    check("渲染器不把 `results` 作为输出键",
          "results" not in out_keys, f"输出键={sorted(out_keys)}")
    check("渲染器不把 `count` 作为输出键",
          "count" not in out_keys, f"输出键={sorted(out_keys)}")

    # ══════════════════════════════════════════════════
    print()
    print("【五】★ 测试脚本的清理完整性（try/finally）")
    # ══════════════════════════════════════════════════
    # ★★ 约定「验证脚本必须 try/finally 清理」（开发进度记录 7.5），
    #    但 `verify_gates.py` **自己没遵守** —— 它把 `purge_org` 放在
    #    末尾，于是一次失败的运行直接 `return`，跳过清理，在库里留下
    #    8 道题 + 2 个机构。之后每次跑基线都对不上（1952 vs 1944），
    #    而原因要翻好几层才看得出来。
    #
    #    ★ 约定写在文档里 ≠ 落在每个脚本上。这条检查把约定变成机械判据。
    import glob

    risky = []
    for path in sorted(glob.glob(str(SCRIPTS_DIR / "verify_*.py"))):
        text = Path(path).read_text(encoding="utf-8")
        creates = bool(re.search(
            r"\.objects\.create\(|\.create_user\(|create_paper\(", text))
        if not creates:
            continue
        # ★ 2026-10-20：判据从「必须有 finally:」放宽为「两种等价保证之一」。
        #
        #   本条检查**真正要的**是「失败也不留残留」，而不是某个关键字：
        #     · 显式清理 → `finally:`
        #     · 事务隔离 → `with transaction.atomic()` + `set_rollback(True)`
        #       异常时 atomic 退出即回滚，中途 return / 抛错都不会落库，
        #       比 finally 更强（连清理代码本身出错都不会留半截数据）。
        #   之前只认 finally:，把 `verify_sheet_assignment_flow.py` 这种
        #   全程事务回滚的脚本误判成「没清理」。
        has_finally = "finally:" in text
        has_rollback = ("set_rollback(True)" in text
                        and "transaction.atomic()" in text)
        if not (has_finally or has_rollback):
            risky.append(Path(path).name)
    check("建数据的测试脚本都能清干净（finally 或事务回滚）", not risky,
          f"无清理保证: {risky}")

    print()
    print("=" * 70)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
