"""难度快速打标 · 回归（`P1-100b`）。

★★ 本套件的重点不是「能不能标」，而是**设计文档 20 的四条契约**：

  ① 写 `difficulty_manual`（整数 1–5）
  ② **`difficulty_effective` 与 `difficulty_band` 跟着变**
     —— 这是「人工优先级最高」的落地证明（设计文档 §六 第 2 条）
  ③ `difficulty_source` = **`MANUAL`**（不是 `MANUAL_OVERRIDE`）
  ④ 打标接口**直接返回下一题**（键盘流下每题省一次往返）

★ 还要验一条**设计文档没写、但会决定这页能不能用**的事：
  `APPROVED` 的题**必须能标**（题库里 1945 道题绝大多数是 APPROVED）。

★ 需要后端在 8001 且真 PG。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_question_mark.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

B = "http://127.0.0.1:8001/api/v1"

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


def call(url, *, method="GET", body=None, tk=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if tk:
        req.add_header("Authorization", f"Bearer {tk}")
    try:
        with urllib.request.urlopen(req, data, timeout=90) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


print("=" * 74)
print("难度快速打标 · 回归（P1-100b）")
print("=" * 74)

# ══════════════════════════════════════════════════════════════
section("M0 · 登录与前置")
# ══════════════════════════════════════════════════════════════

st, j = call(f"{B}/auth/login/", method="POST",
             body=admin_login())
tok = (j.get("data") or {}).get("access_token")
check("★ 能登录", bool(tok), f"HTTP {st}")
if not tok:
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════
section("M1 · 队列（`GET /questions/mark-queue/`）")
# ══════════════════════════════════════════════════════════════

st, j = call(f"{B}/questions/mark-queue/?limit=3", tk=tok)
d = j.get("data") or {}
items = d.get("items") or []
check("★ `GET mark-queue` → 200", st == 200, f"HTTP {st}")
check("★ 返回队列 + **进度**（前端要显示「已标 / 总数」）",
      isinstance(d.get("progress"), dict)
      and {"total", "marked", "remaining"} <= set(d["progress"]),
      f"progress={d.get('progress')}")
prog = d.get("progress") or {}
check("★★ 进度自洽：`marked + remaining == total`",
      prog.get("marked", 0) + prog.get("remaining", 0) == prog.get("total"),
      f"{prog.get('marked')} + {prog.get('remaining')} vs {prog.get('total')}")

check("★ 题目含**完整题干**（不是 80 字摘要）",
      all(len(it.get("stem_text") or "") > 0 for it in items)
      and all("stem_preview" not in it for it in items),
      f"{len(items)} 条")
check("★ 题目含**选项与答案**（判断题需要）",
      all("options" in it and "answer" in it for it in items),
      f"首条 options={len(items[0].get('options') or []) if items else '-'}")
check("★ 题目含章节/任务上下文",
      all("task_name" in it and "chapter_name" in it for it in items),
      f"首条 task={items[0].get('task_name')!r} "
      f"chapter={items[0].get('chapter_name')!r}" if items else "")

ids = [it["id"] for it in items]
check("★★ 队列顺序按 `id` **升序**（设计文档 §五：可续）",
      ids == sorted(ids), f"{ids}")

# 默认只列未标注
check("★★ 默认 `only_unmarked` → 列出的题 `difficulty_manual` 都是 null",
      all(it.get("difficulty_manual") is None for it in items),
      f"{[it.get('difficulty_manual') for it in items]}")

# `after_id` 可续
if items:
    last = ids[-1]
    st, j = call(f"{B}/questions/mark-queue/?after_id={last}&limit=3", tk=tok)
    ids2 = [x["id"] for x in ((j.get("data") or {}).get("items") or [])]
    check("★★ `after_id` 从该题**之后**继续（刷新后可续）",
          bool(ids2) and min(ids2) > last,
          f"after={last} → {ids2}")

# 非法参数不炸
st, j = call(f"{B}/questions/mark-queue/?course_id=abc&after_id=xyz&limit=zz",
             tk=tok)
check("★ 非法筛选参数当作「没传」（不让整页 400）", st == 200, f"HTTP {st}")

st, j = call(f"{B}/questions/mark-queue/")
check("★ 未认证 → 401/403", st in (401, 403), f"HTTP {st}")

# ══════════════════════════════════════════════════════════════
section("M2 · ★★★ 打标写入（设计文档 §四 的四条契约）")
# ══════════════════════════════════════════════════════════════

st, j = call(f"{B}/questions/mark-queue/?limit=1", tk=tok)
first = ((j.get("data") or {}).get("items") or [None])[0]
if not first:
    check("★ 有可打标的题", False, "队列为空（全标完了？）")
else:
    qid = first["id"]
    before_eff = first.get("difficulty_effective")
    before_band = first.get("difficulty_band")
    print(f"     目标题 #{qid} 类型={first.get('type')} "
          f"状态={first.get('status')} 原档位={before_band}")

    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={"difficulty": 5})
    r = j.get("data") or {}
    check("★★★ ① `difficulty_manual` 写入为所选整数",
          st == 200 and r.get("difficulty_manual") == 5,
          f"HTTP {st} manual={r.get('difficulty_manual')}")
    check("★★★ ② `difficulty_effective` **跟着变**（人工优先级最高）",
          str(r.get("difficulty_effective")) == "5",
          f"原 {before_eff} → {r.get('difficulty_effective')}")
    check("★★★ ② `difficulty_band` **跟着变**（这是筛选/组卷的依据）",
          r.get("difficulty_band") != before_band
          or before_band == "5",
          f"原 {before_band} → {r.get('difficulty_band')}")
    check("★★★ ③ `difficulty_source` = `MANUAL`（不是 `MANUAL_OVERRIDE`）",
          r.get("difficulty_source") == "MANUAL",
          f"source={r.get('difficulty_source')}")
    check("★★★ ④ 响应**直接带下一题**（键盘流省一次往返）",
          isinstance(r.get("next"), dict)
          and r["next"].get("id") != qid,
          f"next=#{r.get('next', {}).get('id') if r.get('next') else None}")
    check("★★ 响应带更新后的进度",
          isinstance(r.get("progress"), dict)
          and r["progress"].get("marked", 0) >= 1,
          f"progress={r.get('progress')}")

    # ★★ 设计文档没写、但决定这页能不能用的一条
    check("★★★ **APPROVED** 的题也能打标"
          "（题库里绝大多数是 APPROVED；要求先撤回草稿这页就不可用）",
          first.get("status") == "APPROVED",
          f"该题状态={first.get('status')}")

    # 查库核对（不信响应，信数据库）
    st, j = call(f"{B}/questions/{qid}/", tk=tok)
    det = j.get("data") or {}
    check("★★ 重新查详情：`difficulty_manual` 与 `difficulty_band` 都落库了",
          det.get("difficulty_manual") == 5
          and det.get("difficulty_band") == r.get("difficulty_band"),
          f"manual={det.get('difficulty_manual')} "
          f"band={det.get('difficulty_band')}")
    check("★ 详情里 `difficulty_source` 也是 `MANUAL`",
          det.get("difficulty_source") == "MANUAL",
          f"{det.get('difficulty_source')}")

    # 标过之后不再出现在「仅未标注」队列里
    st, j = call(f"{B}/questions/mark-queue/?limit=5", tk=tok)
    ids3 = [x["id"] for x in ((j.get("data") or {}).get("items") or [])]
    check("★★ 标过的题**不再出现**在「仅未标注」队列里",
          qid not in ids3, f"队列={ids3}")

    # 全部 5 个等级都能写
    ok_levels = []
    for lv in (1, 2, 3, 4, 5):
        st2, j2 = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                       tk=tok, body={"difficulty": lv})
        r2 = j2.get("data") or {}
        if st2 == 200 and r2.get("difficulty_manual") == lv:
            ok_levels.append(lv)
    check("★★ 1–5 五个等级都能写", ok_levels == [1, 2, 3, 4, 5],
          f"成功={ok_levels}")

    # 非法值
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={"difficulty": 0})
    check("★★ 难度 0 → 400（档次是 1–5）", st == 400, f"HTTP {st}")
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={"difficulty": 6})
    check("★★ 难度 6 → 400", st == 400, f"HTTP {st}")
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={"difficulty": 2.5})
    check("★★ 难度 2.5 → 400（★ 只接受整数）", st == 400, f"HTTP {st}")
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={})
    check("★ 缺 `difficulty` → 400", st == 400, f"HTTP {st}")
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 tk=tok, body={"difficulty": 3})
    check("★ 不存在的题 → 404",
          call(f"{B}/questions/99999999/mark-difficulty/", method="POST",
               tk=tok, body={"difficulty": 3})[0] == 404, "")
    st, j = call(f"{B}/questions/{qid}/mark-difficulty/", method="POST",
                 body={"difficulty": 3})
    check("★ 未认证 → 401/403", st in (401, 403), f"HTTP {st}")

    # ★ 复原（避免污染题库的难度分布）
    call(f"{B}/questions/{qid}/mark-difficulty/", method="POST", tk=tok,
         body={"difficulty": int(float(before_eff or 3))})
    print(f"     已复原 #{qid} 的难度为 {int(float(before_eff or 3))}")

# ══════════════════════════════════════════════════════════════
section("M3 · 队列与进度的一致性")
# ══════════════════════════════════════════════════════════════

st, j = call(f"{B}/questions/mark-queue/?limit=1&only_unmarked=0", tk=tok)
d2 = j.get("data") or {}
check("★ `only_unmarked=0` 时也能拿到队列（可复核已标过的）",
      st == 200 and bool(d2.get("items")), f"HTTP {st}")
check("★★ 两种筛选下**进度相同**（进度是全库统计，与筛选无关）",
      (d2.get("progress") or {}).get("total") == prog.get("total"),
      f"{d2.get('progress')} vs {prog}")

st, j = call(f"{B}/questions/mark-queue/?limit=1&only_unmarked=1", tk=tok)
p1 = (j.get("data") or {}).get("progress") or {}
check("★★ `only_unmarked=1` 时进度里仍然报**全库**总数"
      "（否则「已标/总数」的分母会随筛选变化）",
      p1.get("total") == prog.get("total"),
      f"{p1.get('total')} vs {prog.get('total')}")

print()
print("=" * 74)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
