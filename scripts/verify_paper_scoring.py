"""配分建议与总分校验 · 回归（`P1-58b`）。

★ 纯函数为主，**在 SQLite 内存库里跑**（不依赖真 PG）。

★★ 重点验证「凑不到目标分」的情况**如实报告**、不假装凑上：
  若这里断言「shortfall 永远是 0」，就会把「算法硬凑」当成正确行为 ——
  而老师拿到一份 101 分的卷子时，没人知道那 1 分是哪来的。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_paper_scoring.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_DB_ENGINE"] = "sqlite"
os.environ["ZF_CACHE"] = "locmem"
for _k in ("ZF_DB_HOST", "ZF_DB_PORT", "ZF_DB_NAME", "ZF_DB_USER",
           "ZF_DB_PASSWORD"):
    os.environ.pop(_k, None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402
from django.conf import settings  # noqa: E402

django.setup()
settings.DATABASES["default"]["NAME"] = ":memory:"
from django.db import connection  # noqa: E402

connection.settings_dict["NAME"] = ":memory:"

from django.core.management import call_command  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

from apps.paper.scoring import (  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库
    check_total,
    suggest_allocation,
)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


D = Decimal


def secs(*pairs, current=None):
    """`secs(("SINGLE_CHOICE", 20), ...)` → sections 列表。"""
    out = []
    for i, (t, n) in enumerate(pairs):
        d = {"type": t, "count": n}
        if current is not None:
            d["current_score"] = current[i]
        out.append(d)
    return out


print("=" * 72)
print("配分建议与总分校验 · 回归")
print("=" * 72)

# ══════════════════════════════════════════════════════════════
section("S1 · 总分校验（`check_total`）")
# ══════════════════════════════════════════════════════════════

r = check_total(target_total=None, actual_total=92)
check("★★ 未设期望总分 → **不校验**（`consistent=True`）",
      r.consistent and not r.checked,
      f"{r.detail}")
check("★ 未设定时差额为 0、且理由说明清楚",
      r.diff == 0 and "未设定" in r.detail, r.detail)

r = check_total(target_total=100, actual_total=100)
check("相等 → consistent", r.consistent and r.diff == 0, r.detail)

r = check_total(target_total=100, actual_total=92)
check("★★ 不足时差额为**负**，且文案说「少」",
      not r.consistent and r.diff == D("-8") and "少" in r.detail,
      f"diff={r.diff} · {r.detail}")

r = check_total(target_total=100, actual_total=108)
check("★★ 超出时差额为**正**，且文案说「多」",
      not r.consistent and r.diff == D("8") and "多" in r.detail,
      f"diff={r.diff} · {r.detail}")

r = check_total(target_total=0, actual_total=0)
check("★ 目标 0 与实算 0 → 一致（0 是合法目标，不是「未设定」）",
      r.checked and r.consistent, r.detail)

r = check_total(target_total=D("99.99"), actual_total=D("100"))
check("★ 小数差额能算对（99.99 vs 100 → +0.01）",
      r.diff == D("0.01"), f"diff={r.diff}")

# ══════════════════════════════════════════════════════════════
section("S2 · 配分建议：能凑到目标分的情况")
# ══════════════════════════════════════════════════════════════

# 典型卷：20 选择 + 10 判断 + 4 解答 = 100 分
plan = suggest_allocation(
    sections=secs(("SINGLE_CHOICE", 20), ("TRUE_FALSE", 10),
                  ("SHORT_ANSWER", 4)),
    target_total=100,
)
for a in plan.allocations:
    print(f"     {a.type:<16} ×{a.count:<3} 权重{a.weight:<5} "
          f"建议 {a.suggested_score}/题 = {a.subtotal}")
print(f"     合计 {plan.suggested_total} / 目标 {plan.target_total} "
      f"差额 {plan.shortfall}")

check("★ 各题型都分到了分（没有 0 分题型）",
      all(a.suggested_score > 0 for a in plan.allocations),
      str([str(a.suggested_score) for a in plan.allocations]))
check("★★ 解答题单位分值 > 选择题（权重体现工作量差异）",
      next(a for a in plan.allocations if a.type == "SHORT_ANSWER"
           ).suggested_score
      > next(a for a in plan.allocations if a.type == "SINGLE_CHOICE"
             ).suggested_score,
      str({a.type: str(a.suggested_score) for a in plan.allocations}))
check("★ 分值都落在粒度上（0.5 的整数倍）",
      all((a.suggested_score % D("0.5")) == 0 for a in plan.allocations),
      str([str(a.suggested_score) for a in plan.allocations]))
check("★★ 合计与目标分**一致**（或如实报告差额）",
      plan.suggested_total + plan.shortfall == plan.target_total,
      f"建议 {plan.suggested_total} + 差额 {plan.shortfall} = "
      f"{plan.suggested_total + plan.shortfall} vs 目标 {plan.target_total}")
check("★ 有面向老师的说明文案", bool(plan.messages), str(plan.messages[:1]))

# ══════════════════════════════════════════════════════════════
section("S3 · ★★ 凑不到目标分时：如实报告，不硬凑")
# ══════════════════════════════════════════════════════════════

# 极端：目标 100 分、只有 7 道题、粒度 0.5 → 100/7 ≈ 14.29，凑不到整数
plan7 = suggest_allocation(
    sections=secs(("SHORT_ANSWER", 7)),
    target_total=100,
)
check("★★ `建议合计 + shortfall == 目标分` **恒成立**"
      "（差额必须如实报，不能凭空消失）",
      plan7.suggested_total + plan7.shortfall == plan7.target_total,
      f"{plan7.suggested_total} + {plan7.shortfall} = "
      f"{plan7.suggested_total + plan7.shortfall} vs 100")
check("★ 有差额时给出**可照做的建议**（调题数/改粒度/手工微调）",
      plan7.shortfall == 0
      or any("题数" in m or "粒度" in m or "微调" in m
             for m in plan7.messages),
      str(plan7.messages))

# 另一极端：目标 1 分、30 道题 → 单题分值小于粒度
tiny = suggest_allocation(
    sections=secs(("SINGLE_CHOICE", 30)), target_total=1,
)
check("★★ 目标分**远小于**题数时，单题分值不小于粒度（不出现 0 分题）",
      all(a.suggested_score >= tiny.granularity for a in tiny.allocations),
      f"粒度 {tiny.granularity}，建议 "
      f"{[str(a.suggested_score) for a in tiny.allocations]}")
check("★ 这种情况会**超出**目标分（如实报告 shortfall 为负）",
      tiny.shortfall <= 0, f"shortfall={tiny.shortfall}")

# ══════════════════════════════════════════════════════════════
section("S4 · 边界与异常输入")
# ══════════════════════════════════════════════════════════════

empty = suggest_allocation(sections=[], target_total=100)
check("★ 无题 → 空方案 + 明确说明（不崩）",
      not empty.allocations and "没有题目" in " ".join(empty.messages),
      str(empty.messages))

zero = suggest_allocation(
    sections=[{"type": "SINGLE_CHOICE", "count": 0}], target_total=100)
check("★ 题数为 0 的题型被过滤（不产生 0 分题型）",
      not zero.allocations, str(len(zero.allocations)))

zero_t = suggest_allocation(
    sections=secs(("SINGLE_CHOICE", 10)), target_total=0)
check("★★ 目标 0 分：不产生负分值、也不崩",
      all(a.suggested_score > 0 for a in zero_t.allocations),
      f"建议 {[str(a.suggested_score) for a in zero_t.allocations]} "
      f"shortfall={zero_t.shortfall}")

# 未知题型必须分到分（权重兜底，不能是 0）
unknown = suggest_allocation(
    sections=secs(("WEIRD_TYPE_XYZ", 5)), target_total=50)
check("★★ 未知题型也分到分（权重兜底，不是 0）",
      unknown.allocations
      and unknown.allocations[0].suggested_score > 0,
      f"{[str(a.suggested_score) for a in unknown.allocations]}")

# 自定义权重能生效
w1 = suggest_allocation(
    sections=secs(("SINGLE_CHOICE", 10), ("SHORT_ANSWER", 10)),
    target_total=100)
w2 = suggest_allocation(
    sections=secs(("SINGLE_CHOICE", 10), ("SHORT_ANSWER", 10)),
    target_total=100, weights={"SHORT_ANSWER": 1.0})
sc1 = next(a for a in w1.allocations if a.type == "SINGLE_CHOICE")
sc2 = next(a for a in w2.allocations if a.type == "SINGLE_CHOICE")
check("★★ 老师可自定义权重（降低解答题权重后选择题分到更多）",
      sc2.suggested_score >= sc1.suggested_score,
      f"默认 {sc1.suggested_score} → 自定义 {sc2.suggested_score}")

# 粒度
fine = suggest_allocation(
    sections=secs(("SHORT_ANSWER", 7)), target_total=100,
    granularity=D("0.25"))
check("★ 粒度可换成 0.25（更细的凑分能力）",
      fine.granularity == D("0.25"),
      f"建议 {fine.suggested_total} 差额 {fine.shortfall}")

# ══════════════════════════════════════════════════════════════
section("S5 · 不变量（无论输入如何都必须成立）")
# ══════════════════════════════════════════════════════════════

cases = [
    (secs(("SINGLE_CHOICE", 20)), 100),
    (secs(("SINGLE_CHOICE", 20), ("SHORT_ANSWER", 4)), 100),
    (secs(("TRUE_FALSE", 10)), 20),
    (secs(("SHORT_ANSWER", 3)), 50),
    (secs(("SINGLE_CHOICE", 1)), 1),
    (secs(("SINGLE_CHOICE", 100)), 100),
    (secs(("FILL_BLANK", 15), ("CALCULATION", 5)), 120),
]
inv_ok = True
inv_detail = []
for s, tgt in cases:
    pl = suggest_allocation(sections=s, target_total=tgt)
    # ① 建议合计 + 差额 == 目标（差额如实）
    if pl.suggested_total + pl.shortfall != pl.target_total:
        inv_ok = False
        inv_detail.append(f"目标 {tgt}: {pl.suggested_total}+{pl.shortfall}≠{tgt}")
    # ② 单题分值 > 0
    if any(a.suggested_score <= 0 for a in pl.allocations):
        inv_ok = False
        inv_detail.append(f"目标 {tgt}: 有 0 分题型")
    # ③ 合计 = 各题型小计之和
    if sum((a.subtotal for a in pl.allocations), D("0")) != pl.suggested_total:
        inv_ok = False
        inv_detail.append(f"目标 {tgt}: 小计之和不等于合计")

check("★★★ 不变量：`小计之和 == 合计` 且 `合计 + 差额 == 目标` 且 `无 0 分题型`",
      inv_ok, "; ".join(inv_detail[:3]) if inv_detail else
      f"{len(cases)} 个用例全部满足")

# ══════════════════════════════════════════════════════════════
section("S6 · HTTP 端到端（需要后端在 8001 且真 PG 有试卷）")
# ══════════════════════════════════════════════════════════════

import json as _json  # noqa: E402
import urllib.error as _ue  # noqa: E402
import urllib.request as _ur  # noqa: E402

B = "http://127.0.0.1:8001/api/v1"


def _call(url, *, method="GET", body=None, token=None):
    req = _ur.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = _json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _ur.urlopen(req, data, timeout=60) as r:
            raw = r.read().decode("utf-8")
            return r.status, (_json.loads(raw) if raw else {})
    except _ue.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, _json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


st, j = _call(f"{B}/auth/login/", method="POST",
              body=admin_login())
tok = (j.get("data") or {}).get("access_token")
if not tok:
    check("★ 后端可达且能登录（跳过 HTTP 段则此项失败）", False,
          f"HTTP {st} · {str(j)[:120]}")
else:
    # 找一份有题的试卷
    st, j = _call(f"{B}/papers/?page_size=50", token=tok)
    ups = [p for p in ((j.get("data") or {}).get("items") or [])
           if (p.get("item_count") or 0) > 0]
    if not ups:
        check("★ 有可用的试卷", False, "库里没有带题的试卷")
    else:
        # ★★ 分两类试卷：**只读**测试用哪份都行；
        #    **写入**测试必须用 **DRAFT** —— 已定稿的卷子
        #    后端会（正确地）拒绝改配分与期望总分。
        #
        #    ★ 我第一版只用「题最多」那一份，结果是 FINALIZED，
        #      于是两条写入断言失败 —— **而那是正确行为**。
        #      判据要按「这份卷子允许做什么」来选，不是按题数。
        pid_any = max(ups, key=lambda p: p["item_count"])["id"]
        drafts = [p for p in ups if p.get("status") == "DRAFT"]
        pid = drafts[0]["id"] if drafts else None
        finals = [p for p in ups if p.get("status") == "FINALIZED"]
        orig_target = ups[0].get("target_total_score")

        st, j = _call(f"{B}/papers/{pid_any}/score-allocation/?target_total=100",
                      token=tok)
        d = j.get("data") or {}
        check("★ `GET ?target_total=100` → 200 且给出建议",
              st == 200 and bool(d.get("allocations")),
              f"HTTP {st} · 建议 {len(d.get('allocations') or [])} 个题型")
        check("★★ 响应含**逐题展开** `expanded`（前端据此一键写入）",
              isinstance(d.get("expanded"), dict) and d["expanded"],
              f"{len(d.get('expanded') or {})} 项")
        check("★★ `expanded` 的合计 == `suggested_total`（两处口径一致）",
              str(d.get("suggested_total_from_expanded"))
              == str(d.get("suggested_total")),
              f"{d.get('suggested_total_from_expanded')} vs "
              f"{d.get('suggested_total')}")

        st, j = _call(f"{B}/papers/{pid_any}/score-allocation/",
                      method="GET", token=tok)
        d0 = j.get("data") or {}
        check("★ 不带 `target_total` 时也能用（回退到试卷存的期望分）",
              st == 200, f"HTTP {st} · needs_target={d0.get('needs_target')}")

        # ★★ 已定稿的卷子：写入必须被拒（这是**正确**行为，要断言它）
        if finals:
            fid = finals[0]["id"]
            st, j = _call(f"{B}/papers/{fid}/score-allocation/", method="PUT",
                          token=tok, body={"target_total_score": "100"})
            check("★★ 已定稿的卷子**不许改期望总分**（历史成绩的依据）",
                  st == 400 and "定稿" in str(j.get("message")),
                  f"HTTP {st} · {str(j.get('message'))[:60]}")
            st, j = _call(f"{B}/papers/{fid}/score-allocation/apply/",
                          method="POST", token=tok,
                          body={"scores": {"1": "3.5"}})
            check("★★ 已定稿的卷子**不许改配分**",
                  st == 400 and "草稿" in str(j.get("message")),
                  f"HTTP {st} · {str(j.get('message'))[:60]}")

        if pid is None:
            check("★ 库里有 DRAFT 试卷用于写入测试", False,
                  f"{len(ups)} 份带题试卷里没有草稿")
        else:
            # 设期望总分
            st, j = _call(f"{B}/papers/{pid}/score-allocation/", method="PUT",
                          token=tok, body={"target_total_score": "100"})
            check("★★ 草稿卷 `PUT` 设期望总分 → 200", st == 200,
                  f"HTTP {st} · {str(j)[:90]}")

            # 非法值
            st, j = _call(f"{B}/papers/{pid}/score-allocation/", method="PUT",
                          token=tok, body={"target_total_score": "9999"})
            check("★★ 期望总分 > 1000 被拒（防误输入）",
                  st == 400, f"HTTP {st} · {str(j.get('message'))[:60]}")

            st, j = _call(f"{B}/papers/{pid}/score-allocation/", method="PUT",
                          token=tok, body={"target_total_score": "abc"})
            check("★ 非数字被拒", st == 400,
                  f"HTTP {st} · {str(j.get('message'))[:60]}")

            # 清空（null）
            st, j = _call(f"{B}/papers/{pid}/score-allocation/", method="PUT",
                          token=tok, body={"target_total_score": None})
            check("★★ `null` 表示**清空**（不是设为 0）",
                  st == 200
                  and (j.get("data") or {}).get("target_total_score") is None,
                  f"HTTP {st} · {str(j.get('data'))[:80]}")

            # apply 端点
            st, j = _call(f"{B}/papers/{pid}/score-allocation/apply/",
                          method="POST", token=tok,
                          body={"scores": {"1": "3.5"}})
            check("★ `POST .../apply/` 能按题号写分值",
                  st == 200, f"HTTP {st} · {str(j)[:100]}")

            st, j = _call(f"{B}/papers/{pid}/score-allocation/apply/",
                          method="POST", token=tok,
                          body={"scores": {"1": "0"}})
            check("★★ 分值 0 被拒（0 分题会让得分率除零）",
                  st == 400, f"HTTP {st} · {str(j.get('message'))[:70]}")

            st, j = _call(f"{B}/papers/{pid}/score-allocation/apply/",
                          method="POST", token=tok,
                          body={"scores": {"99999": "5"}})
            check("★ 不存在的题号被拒", st == 400,
                  f"HTTP {st} · {str(j.get('message'))[:70]}")

            st, j = _call(f"{B}/papers/{pid}/score-allocation/apply/",
                          method="POST", token=tok,
                          body={"scores": {"abc": "5"}})
            check("★★ `scores` 的键必须是数字（键也要校验，不只是值）",
                  st == 400, f"HTTP {st} · {str(j.get('message'))[:70]}")

        st, j = _call(f"{B}/papers/999999/score-allocation/", token=tok)
        check("★ 不存在的试卷 → 404", st == 404, f"HTTP {st}")

        st, j = _call(f"{B}/papers/{pid_any}/score-allocation/")
        check("★ 未认证 → 401/403", st in (401, 403), f"HTTP {st}")

        # 恢复原期望总分（避免污染）
        if pid is not None:
            _call(f"{B}/papers/{pid}/score-allocation/", method="PUT",
                  token=tok, body={"target_total_score": None})

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)