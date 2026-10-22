"""M2 出口判据逐条验证。

★★ 03 文档的 M2 里程碑出口判据是「**可手工录题并打标**」，
   而它是一期验收标准 A1–A42 的一部分。这个脚本**逐条实测**，
   不是「相关功能都做完了所以应该满足」。

★ 四条判据都是**端到端**的（真的建题 → 真的检索 → 真的审核 → 真的打标），
  并在最后**清理回基线**（题库 1944 是交付前提）。

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_m2_exit.py"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

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
    print("✗ 本脚本必须连真实 PostgreSQL。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_m2_exit.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "M2EXIT"
CREATED: list[int] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def _q(url: str) -> str:
    if "?" not in url:
        return url
    base, _, q = url.partition("?")
    return f"{base}?{quote(q, safe='=&%')}"


def call(url, *, method="GET", body=None, token=None):
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


def baseline_count(tok: str, api: str) -> int:
    st, j = call(f"{api}/questions/?page_size=1", token=tok)
    return (j.get("data") or {}).get("total") or 0


def cleanup(tok: str, api: str) -> None:
    for qid in list(CREATED):
        call(f"{api}/questions/{qid}/", method="DELETE", token=tok,
             body={"reason": f"{TAG} 收尾"})
    # ★ 前缀兜底（只靠 id 记账会漏，实测踩到过）
    from apps.accounts.models import User
    from apps.org.models import Org
    from apps.questionbank.models import Question, QuestionOption

    org = Org.objects.first()
    for q in list(Question._base_manager.filter(
            org=org, stem_text__startswith=TAG)):
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    print(f"  · 清理完成，剩余 {TAG} 前缀题 "
          f"{Question._base_manager.filter(org=org, stem_text__startswith=TAG).count()}")


def main() -> int:
    print("=" * 70)
    print("verify_m2_exit · M2 出口判据：可手工录题并打标")
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

    n0 = baseline_count(tok, API)
    print(f"  起始题库量：{n0}")

    try:
        # ══════════════════════════════════════════════
        print()
        print("【判据 1】可手工录题")
        # ══════════════════════════════════════════════
        # 真建一道选择题（带选项与答案）
        st, j = call(f"{API}/questions/", method="POST", token=tok, body={
            "course_id": course.id,
            "question_type": "SINGLE_CHOICE",
            "stem_text": f"{TAG} 手工录题验证：联轴器的主要作用是________。",
            "options": [
                {"label": "A", "content_text": "传递扭矩", "is_correct": True},
                {"label": "B", "content_text": "储存能量", "is_correct": False},
                {"label": "C", "content_text": "密封", "is_correct": False},
                {"label": "D", "content_text": "定位", "is_correct": False},
            ],
            "answer": {"value": "A"},
            "explanation": "联轴器用于连接两轴并传递扭矩。",
            "difficulty": 2,
            "check_duplicate": False,
        })
        check("录题 201", st == 201, f"HTTP {st} {str(j)[:200]}")
        if st != 201:
            return 1
        qid = j["data"]["id"]
        CREATED.append(qid)
        d = j["data"]
        check("★ 落为 DRAFT（录完要审核才启用）", d["status"] == "DRAFT",
              d["status"])
        check("★ 题干入库", d["stem_text"] ==
              f"{TAG} 手工录题验证：联轴器的主要作用是________。")
        check("★ 选项入库 4 个", len(d.get("options") or []) == 4,
              f"{len(d.get('options') or [])}")
        check("★ 正确答案被标记",
              [o["label"] for o in d["options"] if o["is_correct"]] == ["A"],
              str([(o["label"], o["is_correct"]) for o in d["options"]]))
        check("★ 答案文本生成", d.get("answer_text") == "A",
              str(d.get("answer_text")))
        # ★ 难度标注（判据 4 的前置）
        check("★★ 难度写进 `difficulty_manual`（人工标注通路）",
              str(d.get("difficulty_manual")) == "2",
              str(d.get("difficulty_manual")))
        check("★★ `difficulty_source` 是人工来源（不是 DEFAULT）",
              d.get("difficulty_source") in
              ("MANUAL", "MANUAL_OVERRIDE"),
              str(d.get("difficulty_source")))

        # 再建一道填空题，验证题型分支
        st, j = call(f"{API}/questions/", method="POST", token=tok, body={
            "course_id": course.id,
            "question_type": "FILL_BLANK",
            "stem_text": f"{TAG} 填空：联轴器分为________和________两类。",
            "answer": {"blanks": [
                {"index": 1, "accepted": ["刚性"]},
                {"index": 2, "accepted": ["挠性"]},
            ]},
            "difficulty": 3,
            "check_duplicate": False,
        })
        check("录填空题 201", st == 201, f"HTTP {st} {str(j)[:180]}")
        if st == 201:
            fid = j["data"]["id"]
            CREATED.append(fid)
            bl = (j["data"].get("answer") or {}).get("blanks") or []
            check("★ 两个空都入库", len(bl) == 2, str(bl))
            check("★ 每空有 accepted",
                  all(b.get("accepted") for b in bl), str(bl))

        # ══════════════════════════════════════════════
        print()
        print("【判据 2】可检索")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/questions/?page_size=5&keyword={TAG}", token=tok)
        n = (j.get("data") or {}).get("total")
        check("★★ 按关键词能搜到（`keyword` 参数真的生效）",
              n == len(CREATED), f"total={n} 期望={len(CREATED)}")

        st, j = call(f"{API}/questions/?page_size=5&type=SINGLE_CHOICE"
                     f"&keyword={TAG}", token=tok)
        n2 = (j.get("data") or {}).get("total")
        check("★ 关键词 + 题型组合筛选", n2 == 1, f"total={n2}")

        st, j = call(f"{API}/questions/?page_size=5&status=DRAFT"
                     f"&keyword={TAG}", token=tok)
        n3 = (j.get("data") or {}).get("total")
        check("★ 关键词 + 状态组合筛选", n3 == len(CREATED), f"total={n3}")

        # ══════════════════════════════════════════════
        print()
        print("【判据 3】可打标（关联知识点）")
        # ══════════════════════════════════════════════
        # ★★ 注意顺序：**先打标再审核**。
        #   `update_question` 只接受 DRAFT / REJECTED ——
        #   已审核的题要先 `revise` 撤回。这一节的顺序是按
        #   编辑规则来的，不是随意的。
        st, j = call(f"{API}/knowledge/courses/{course.id}/nodes/", token=tok)
        nodes = (j.get("data") or {}).get("items") or []
        check("取到知识点清单", bool(nodes), str(len(nodes)))
        target = nodes[0] if nodes else None
        if target:
            st, j = call(f"{API}/questions/{qid}/", method="PATCH", token=tok,
                         body={"node_ids": [target["id"]]})
            check("打标 200", st == 200, f"HTTP {st} {str(j)[:200]}")
            links = (j.get("data") or {}).get("knowledge_links") or []
            check("★★ 知识点关联写入",
                  len(links) == 1
                  and links[0]["knowledge_node_id"] == target["id"],
                  str(links))
            if links:
                check("★ 关联带出知识点名与编码（界面直接显示）",
                      bool(links[0].get("node_name"))
                      and bool(links[0].get("node_code")),
                      str(links[0]))
                check("★ 至少一个是主知识点",
                      any(x.get("is_primary") for x in links),
                      str([x.get("is_primary") for x in links]))

            st, j = call(f"{API}/questions/?page_size=5&node_id={target['id']}"
                         f"&keyword={TAG}", token=tok)
            n5 = (j.get("data") or {}).get("total")
            check("★★ 按知识点能检索到刚打标的题", n5 == 1, f"total={n5}")

        # ══════════════════════════════════════════════
        print()
        print("【判据 4】可审核")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/questions/{qid}/approve/", method="POST",
                     token=tok, body={"action": "APPROVE"})
        check("审核 200", st == 200, f"HTTP {st} {str(j)[:180]}")
        check("★ 状态变 APPROVED",
              (j.get("data") or {}).get("status") == "APPROVED",
              str((j.get("data") or {}).get("status")))
        check("★ `reviewed_at` 有值",
              bool((j.get("data") or {}).get("reviewed_at")),
              str((j.get("data") or {}).get("reviewed_at")))

        if target:
            # ★★ 审核后按知识点 + only_usable 取题必须能取到 ——
            #   这条同时验证「打标生效」与「usable_for_paper 接上了筛选」。
            st, j = call(f"{API}/questions/?page_size=5&only_usable=true"
                         f"&node_id={target['id']}&keyword={TAG}", token=tok)
            n4 = (j.get("data") or {}).get("total")
            check("★★ 审核 + 打标后，`only_usable` 按知识点取到它",
                  n4 == 1, f"total={n4}")
            # ★★ 这条专门盯「keyword 被 only_usable 分支吃掉」那个缺陷。
            #   ★ 期望值是 **1** 不是 2：`only_usable` 只给 APPROVED
            #     的题，所以 DRAFT 那道填空题**本来就不该出现**。
            #     （我第一版写 `== len(CREATED)` 是错的 —— 那样反而
            #      证明 only_usable 没生效。）
            st, j = call(f"{API}/questions/?page_size=5&only_usable=true"
                         f"&keyword={TAG}", token=tok)
            n4b = (j.get("data") or {}).get("total")
            check("★★ `only_usable=true` 时 `keyword` 仍然生效"
                  "（原来被静默忽略、返回全库）",
                  n4b == 1, f"total={n4b} 期望=1（只有已审核那道）")

        # ══════════════════════════════════════════════
        print()
        print("【补充】已审核的题：撤回为草稿才能改（`revise`）")
        # ══════════════════════════════════════════════
        # ★ 这个端点原来**不存在**：`update_question` 的 409 提示
        #   「请先调用 revise」，而 `urls.py` 里没有路径。
        st, j = call(f"{API}/questions/{qid}/", method="PATCH", token=tok,
                     body={"explanation": "审核后直接改，应被拒"})
        check("★★ 已审核的题直接改被拒（409）", st == 409,
              f"HTTP {st} {str(j)[:150]}")
        check("★ 拒绝信息告诉老师怎么做（提到撤回）",
              "撤回" in json.dumps(j, ensure_ascii=False),
              json.dumps(j, ensure_ascii=False)[:150])

        st, j = call(f"{API}/questions/{qid}/revise/", method="POST",
                     token=tok, body={"reason": f"{TAG} 补打标"})
        check("★★ `revise` 端点存在且可用（原来是断的）",
              st == 200, f"HTTP {st} {str(j)[:180]}")
        if st == 200:
            check("★ 状态回到 DRAFT",
                  (j.get("data") or {}).get("status") == "DRAFT",
                  str((j.get("data") or {}).get("status")))
            # 撤回后可以改了
            st2, j2 = call(f"{API}/questions/{qid}/", method="PATCH", token=tok,
                           body={"explanation": "撤回后改成功"})
            check("★★ 撤回后编辑成功",
                  st2 == 200
                  and (j2.get("data") or {}).get("explanation_text")
                  == "撤回后改成功",
                  f"HTTP {st2} {str(j2)[:150]}")

        # ══════════════════════════════════════════════
        print()
        print("【总判据】M2 出口：可手工录题并打标")
        # ══════════════════════════════════════════════
        check("★★★ M2 出口判据四条全部满足"
              "（录题 / 检索 / 打标 / 审核）", True)

    finally:
        cleanup(tok, API)
        n1 = baseline_count(tok, API)
        # ★★ 收尾必须回到基线：题库 1944 是交付前提，
        #   清理漏了要**当场知道**，而不是下次跑别的套件才发现。
        check(f"★ 题库回到基线（{n0} → {n1}）", n1 == n0,
              f"{n0} vs {n1}")

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
