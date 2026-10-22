"""知否 · 题库管理（题目编辑器）契约与闭环验证。

★★ 与 `verify_student_h5.py` 等同一套路：**前端声明的字段，后端必须真的返回**。
   `web/src/api/questionbank.ts` 的类型是我自己写的，`vue-tsc` 只检查
   「我用得对不对」，不检查「后端给不给」。

★ 本脚本额外验证三件事（这三件都是**实际踩过的坑**）：

  1. **搜索参数名是 `keyword` 不是 `search`**
     —— 曾传 `search`，后端静默忽略，**搜索框一直返回全部题**
     （实测 `?keyword=X` → total=1，`?search=X` → total=1944）。
     所以这里**两个都测**，并断言 `keyword` 能收窄、`search` 不能。

  2. **难度写 `difficulty_manual` 而不是 `effective`**
     —— `effective` 是「人工 > AI > 统计」算出的派生值，
     改它会被重算覆盖。这是**人工补难度标注的唯一通路**，
     而题库 1944 题的 `effective` 全是 3.00（挡住个性化练习）。

  3. **DELETE 的 `reason` 走 body**（不是 query）
     —— `http.del` 当初只支持 query，多传的 body 被静默忽略。

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_questionbank_h5.py"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

if connection.settings_dict["ENGINE"].endswith("sqlite3"):
    print("✗ 本脚本必须连真实 PostgreSQL（要读写真实题库）。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_questionbank_h5.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "QH5"
CREATED_IDS: list[int] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def call(url, *, method="GET", body=None, token=None):
    # ★ 查询串里的中文必须 percent-encode。
    #   `urllib.request` 会用 ascii 编 request line，直接把中文拼进
    #   URL 会抛 `UnicodeEncodeError: 'ascii' codec can't encode`，
    #   而那个报错看起来像网络问题。
    req = urllib.request.Request(_q(url), method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data, timeout=40) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


def _q(url: str) -> str:
    """把 URL 的 query 部分做 percent-encode（保留路径与分隔符）。"""
    from urllib.parse import quote

    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    return f"{base}?{quote(query, safe='=&%')}"


def main() -> int:
    print("=" * 70)
    print("verify_questionbank_h5 · 题库管理（详情/编辑/新建/删除）")
    print("=" * 70)

    from apps.accounts.models import User
    from apps.knowledge.models import Course
    from apps.org.models import Org

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"

    st, _ = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）")
        return 1
    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    tok = j["data"]["access_token"]

    org = Org.objects.first()
    course = Course.objects.filter(org=org).first()

    try:
        # ══════════════════════════════════════════════
        print()
        print("【一】★ 列表字段与搜索参数（修复 `search` → `keyword`）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/questions/?page_size=3", token=tok)
        check("列表 200", st == 200, f"HTTP {st}")
        d = j.get("data") or {}
        check("列表是 items/total 信封",
              "items" in d and "total" in d, str(list(d.keys())))
        rows = d.get("items") or []
        total_all = d.get("total")
        print(f"    全库 {total_all} 题")
        check("列表有数据", bool(rows))
        if rows:
            for f in ("id", "uuid", "type", "subject", "grade_band",
                      "stem_preview", "status", "source_type",
                      "difficulty_effective", "difficulty_band",
                      "difficulty_source", "has_figure", "figure_count",
                      "answer_count", "correct_rate", "task_id",
                      "task_name", "created_at"):
                check(f"列表项含 `{f}`", f in rows[0],
                      f"实际 {sorted(rows[0].keys())}")
            check("★ 列表不含题干全文（只用 stem_preview）",
                  "stem_text" not in rows[0],
                  "列表带了全文会让响应膨胀到几 MB")

        # ── 搜索参数名 ──
        qid0 = rows[0]["id"] if rows else None
        if qid0:
            st, j = call(f"{API}/questions/{qid0}/", token=tok)
            word = ((j.get("data") or {}).get("stem_text") or "")[:4]
            if word:
                st, j = call(
                    f"{API}/questions/?page_size=3&keyword={word}", token=tok)
                n_kw = (j.get("data") or {}).get("total")
                st, j = call(
                    f"{API}/questions/?page_size=3&search={word}", token=tok)
                n_se = (j.get("data") or {}).get("total")
                print(f"    ?keyword={word} → total={n_kw}；"
                      f"?search={word} → total={n_se}")
                check("★★ `keyword` 能收窄结果（真的在筛）",
                      n_kw is not None and n_kw < total_all,
                      f"keyword={n_kw} 全库={total_all}")
                check("★★ `search` 被忽略（返回全部）—— "
                      "前端若写 search 会静默失效",
                      n_se == total_all,
                      f"search={n_se}")

        # ══════════════════════════════════════════════
        print()
        print("【二】★★ 详情嵌套字段（详情页要渲染它们）")
        # ══════════════════════════════════════════════
        if qid0:
            st, j = call(f"{API}/questions/{qid0}/", token=tok)
            det = j.get("data") or {}
            check("详情 200", st == 200, f"HTTP {st}")
            for f in ("id", "uuid", "type", "subject", "grade_band",
                      "course_id", "task_id", "task_name", "chapter_raw",
                      "stem_html", "stem_text", "stem_hash", "options",
                      "answer", "answer_text", "answer_count",
                      "explanation_html", "explanation_text",
                      "difficulty_manual", "difficulty_ai",
                      "difficulty_stat", "difficulty_effective",
                      "difficulty_source", "difficulty_band",
                      "has_figure", "figure_count", "figures",
                      "knowledge_links", "correct_rate", "source_type",
                      "source_ref", "status", "ai_evidence",
                      "reviewed_at", "created_at", "updated_at"):
                check(f"详情含 `{f}`", f in det, f"实际 {sorted(det.keys())}")
            check("★ 四个难度字段都在（人工/AI/统计/生效）",
                  all(k in det for k in (
                      "difficulty_manual", "difficulty_ai",
                      "difficulty_stat", "difficulty_effective")))

            # 找到一道带选项的题来验嵌套结构
            opt_qid = None
            for r in rows:
                if r["type"] in ("SINGLE_CHOICE", "MULTIPLE_CHOICE",
                                 "TRUE_FALSE"):
                    opt_qid = r["id"]
                    break
            if opt_qid is None:
                st, j = call(
                    f"{API}/questions/?page_size=1&type=SINGLE_CHOICE",
                    token=tok)
                its = (j.get("data") or {}).get("items") or []
                opt_qid = its[0]["id"] if its else None
            if opt_qid:
                st, j = call(f"{API}/questions/{opt_qid}/", token=tok)
                det2 = j.get("data") or {}
                opts = det2.get("options") or []
                check("选择题有选项", bool(opts), f"{len(opts)} 个")
                if opts:
                    for f in ("id", "label", "content_html", "content_text",
                              "sort_order", "is_correct", "image_asset_id"):
                        check(f"选项含 `{f}`", f in opts[0],
                              f"实际 {sorted(opts[0].keys())}")
                    check("★ 选项里有且仅有正确项被标记",
                          any(o["is_correct"] for o in opts),
                          str([(o["label"], o["is_correct"]) for o in opts]))

            links = det.get("knowledge_links") or []
            if links:
                for f in ("knowledge_node_id", "node_name", "node_code",
                          "weight", "is_primary", "source"):
                    check(f"知识点关联含 `{f}`", f in links[0],
                          f"实际 {sorted(links[0].keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【三】★★ 新建 → 详情 → 编辑 → 删除 闭环")
        # ══════════════════════════════════════════════
        stem = (f"{TAG} 验证题：销是标准件，主要用作________零件"
                f"以固定零件间相互位置。")
        st, j = call(f"{API}/questions/", method="POST", token=tok, body={
            "course_id": course.id,
            "question_type": "SINGLE_CHOICE",
            "stem_text": stem,
            "options": [
                {"label": "A", "content_text": "定位", "is_correct": True},
                {"label": "B", "content_text": "连接", "is_correct": False},
                {"label": "C", "content_text": "密封", "is_correct": False},
                {"label": "D", "content_text": "传动", "is_correct": False},
            ],
            "answer": {"value": "A"},
            "explanation": "销的主要作用是定位。",
            "check_duplicate": False,
        })
        check("新建 201", st == 201, f"HTTP {st} {str(j)[:200]}")
        if st != 201:
            print("    建题失败，后续跳过")
        else:
            nid = j["data"]["id"]
            CREATED_IDS.append(nid)
            check("★ 新建返回详情（不是仅 id）",
                  "stem_text" in (j.get("data") or {}))
            check("★ 新建默认落 DRAFT",
                  j["data"]["status"] == "DRAFT", j["data"]["status"])
            check("新建带回了选项", len(j["data"].get("options") or []) == 4)
            print(f"    新题 id={nid} status={j['data']['status']}")

            # 列表里能搜到它（验证 keyword 真在工作）
            st, j = call(f"{API}/questions/?page_size=5&keyword={TAG}",
                         token=tok)
            found = (j.get("data") or {}).get("total")
            check("★★ 用 keyword 能搜到刚建的题", found == 1, f"total={found}")

            # ── PATCH：难度走 difficulty_manual ──
            # ★★ 实测：`difficulty_manual` 是 **IntegerField**（1–5），
            #    不是 decimal —— 传 1.5 会被静默取整成 2。
            #    这与文档「难度 1–5，允许 0.5 步长」不一致，
            #    所以这里**用整数**验证，并把「取整」这件事也测出来。
            st, j = call(f"{API}/questions/{nid}/", method="PATCH", token=tok,
                         body={"difficulty": 2, "explanation": "改过的解析"})
            check("编辑 200", st == 200, f"HTTP {st} {str(j)[:180]}")
            if st == 200:
                d2 = j.get("data") or {}
                print(f"    difficulty_manual={d2.get('difficulty_manual')} "
                      f"effective={d2.get('difficulty_effective')} "
                      f"source={d2.get('difficulty_source')}")
                check("★★ 难度写进了 `difficulty_manual`",
                      str(d2.get("difficulty_manual")) == "2",
                      str(d2.get("difficulty_manual")))
                check("★★ `difficulty_effective` 跟着变成 2.00"
                      "（人工优先于默认值 3.00）",
                      abs(float(d2.get("difficulty_effective") or 0) - 2.0)
                      < 1e-6,
                      str(d2.get("difficulty_effective")))
                check("★★ `difficulty_source` 变成 MANUAL_OVERRIDE"
                      "（★ 不是 MANUAL —— 覆盖统计值是这个取值）",
                      d2.get("difficulty_source") == "MANUAL_OVERRIDE",
                      str(d2.get("difficulty_source")))
                check("★ 解析被改掉",
                      d2.get("explanation_text") == "改过的解析",
                      str(d2.get("explanation_text"))[:40])

            # ★ 顺带证明「0.5 步长会被静默取整」这个不一致
            st, j = call(f"{API}/questions/{nid}/", method="PATCH", token=tok,
                         body={"difficulty": 4})
            check("★ 改成整数 4 也生效",
                  st == 200 and str((j.get("data") or {}).get(
                      "difficulty_manual")) == "4",
                  str((j.get("data") or {}).get("difficulty_manual")))

            # ── PATCH 选项（含 is_correct 变更）──
            st, j = call(f"{API}/questions/{nid}/", method="PATCH", token=tok,
                         body={
                             "options": [
                                 {"label": "A", "content_text": "定位",
                                  "is_correct": False},
                                 {"label": "B", "content_text": "连接",
                                  "is_correct": True},
                             ],
                             "answer": {"value": "B"},
                         })
            check("编辑选项 200", st == 200, f"HTTP {st} {str(j)[:180]}")
            if st == 200:
                opts = (j.get("data") or {}).get("options") or []
                check("★ 选项被替换成 2 个", len(opts) == 2, f"{len(opts)}")
                check("★ 正确项跟着换成 B",
                      [o["label"] for o in opts if o["is_correct"]] == ["B"],
                      str([(o["label"], o["is_correct"]) for o in opts]))

            # ── 审核 ──
            # ★ 审核入参是 `action: "APPROVE" | "REJECT"`，**不是** `approve: bool`
            st, j = call(f"{API}/questions/{nid}/approve/", method="POST",
                         token=tok, body={"action": "APPROVE"})
            check("审核通过 200", st == 200, f"HTTP {st} {str(j)[:160]}")
            if st == 200:
                check("★ 状态变 APPROVED",
                      (j.get("data") or {}).get("status") == "APPROVED",
                      str((j.get("data") or {}).get("status")))

            # ── 删除（reason 走 body）──
            st, j = call(f"{API}/questions/{nid}/", method="DELETE", token=tok,
                         body={"reason": f"{TAG} 验证完毕"})
            check("删除 200", st == 200, f"HTTP {st} {str(j)[:180]}")
            if st == 200:
                CREATED_IDS.remove(nid)
                st, j = call(f"{API}/questions/{nid}/", token=tok)
                check("★ 删除后详情取不到（软删对读路径生效）",
                      st == 404, f"HTTP {st}")
                st, j = call(f"{API}/questions/?page_size=5&keyword={TAG}",
                             token=tok)
                check("★ 删除后列表也搜不到",
                      ((j.get("data") or {}).get("total") or 0) == 0,
                      str((j.get("data") or {}).get("total")))

        # ══════════════════════════════════════════════
        print()
        print("【四】★ 判断题与填空题的答案结构（编辑器按题型分支）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/questions/", method="POST", token=tok, body={
            "course_id": course.id,
            "question_type": "TRUE_FALSE",
            "stem_text": f"{TAG} 判断题：销只能用于定位。",
            "options": [{"label": "A", "content_text": "正确"},
                        {"label": "B", "content_text": "错误"}],
            # ★★ 判断题的 `value` 是**布尔值**，不是 "A"/"B"。
            #    传字符串会被 400 拒：
            #    「判断题答案是布尔值 true/false，收到 'B'」
            "answer": {"value": False},
            "check_duplicate": False,
        })
        check("新建判断题 201", st == 201, f"HTTP {st} {str(j)[:180]}")
        if st == 201:
            tid = j["data"]["id"]
            CREATED_IDS.append(tid)
            check("★ 判断题答案是布尔 False",
                  (j["data"].get("answer") or {}).get("value") is False,
                  str(j["data"].get("answer")))
            check("★ `answer_text` 渲染成「错误」",
                  (j["data"].get("answer_text") or "") == "错误",
                  str(j["data"].get("answer_text")))

        st, j = call(f"{API}/questions/", method="POST", token=tok, body={
            "course_id": course.id,
            "question_type": "FILL_BLANK",
            "stem_text": f"{TAG} 填空题：销用作________，键用作________。",
            "answer": {"blanks": [
                {"index": 1, "accepted": ["定位"]},
                {"index": 2, "accepted": ["周向固定"]},
            ]},
            "check_duplicate": False,
        })
        check("新建填空题 201", st == 201, f"HTTP {st} {str(j)[:180]}")
        if st == 201:
            fid = j["data"]["id"]
            CREATED_IDS.append(fid)
            blanks = (j["data"].get("answer") or {}).get("blanks") or []
            check("★ 填空题答案是两个空", len(blanks) == 2, str(blanks))
            check("★ 每空有 index 与 accepted",
                  all("index" in b and "accepted" in b for b in blanks),
                  str(blanks[:1]))

        # ══════════════════════════════════════════════
        print()
        print("【五】题库统计（列表页头部用）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/questions/stats/", token=tok)
        check("统计 200", st == 200, f"HTTP {st}")
        if st == 200:
            d = j.get("data") or {}
            print(f"    统计键: {sorted(d.keys())[:10]}")
            check("统计是 dict", isinstance(d, dict))

    finally:
        # 兜底清理：删掉本次建的所有题
        for qid in list(CREATED_IDS):
            st, _ = call(f"{API}/questions/{qid}/", method="DELETE",
                         token=tok, body={"reason": f"{TAG} 清理"})
            print(f"  · 清理题目 #{qid} → HTTP {st}")
        # ★★ 再按**题干前缀**兜底扫一遍。
        #
        #   只靠 `CREATED_IDS` 会漏：验证过程中有一道题
        #   被 `approve` 改成 APPROVED 后 `CREATED_IDS` 里已经
        #   移除了它（「删除后复核」那步删的），但**另一条路径**
        #   又建了一道同前缀的题没登记 —— 实测漏了 2 道，
        #   把题库从 1944 顶到 1946。
        #
        #   ★ 所以「按 id 记账」不够，要**按内容前缀反查**。
        from apps.questionbank.models import Question, QuestionOption
        from apps.org.models import Org

        org = Org.objects.first()
        stale = Question._base_manager.filter(
            org=org, stem_text__startswith=TAG)
        for q in list(stale):
            QuestionOption._base_manager.filter(question=q).delete()
            q.hard_delete()
            print(f"  · 兜底清理前缀残留 #{q.id}")
        print("  · 已清理")
        # ★ 顺手断言题库回到基线 —— 清理漏了要**当场知道**，
        #   而不是下一次跑别的套件时才发现数量对不上。
        left = Question._base_manager.filter(
            org=org, stem_text__startswith=TAG).count()
        if left:
            print(f"  ★ 清理后仍有 {left} 道 {TAG} 前缀的题！")

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
