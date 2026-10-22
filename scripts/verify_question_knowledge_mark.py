"""知识点补标 · 回归（15 文档 §六）。

★★ 这个功能修的是**一个真实的数据缺口**：

  `题目包.json` 每题只有任务级 `task_code`，没有知识点字段。
  导入器（`scripts/migrate/migrate_p3_load.py` L348）因此把每题挂到
  「该任务的**第一个**知识点」作**占位**（`source=IMPORT`）。

  后果（实测 2026-10）：**44 个知识点里 22 个一道题都没有** ——
  按那些点做学情与个性化练习**永远为空**。

  15 文档 §六 的补救方案是「AI 预打标 + 人工抽检」。
  本套件验的是**那条流程的写入侧**（AI 侧需要 provider）。

★★ 重点验**三条模型约束**是否被正确处理：

  ① 每题有且仅有一个主知识点（`uq_question_primary_node`）
  ② 同一 (题, 知识点) 只能有一条关联（`uq_question_knowledge`）
  ③ 不能把主知识点降成「没有」—— 旧关联要**保留**（可回溯）

运行（不需要后端，走视图直调）：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_question_knowledge_mark.py
"""

from __future__ import annotations

import json
import os
import sys

from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.knowledge.models import KnowledgeNode  # noqa: E402
from apps.questionbank.models import QuestionKnowledge  # noqa: E402
from apps.questionbank.selectors import QuestionSelector  # noqa: E402
from apps.questionbank.services import (  # noqa: E402
    QuestionEditService,
    QuestionService,
)
from apps.questionbank.views import (  # noqa: E402
    QuestionKpCandidatesView,
    QuestionKpQueueView,
    QuestionSetKnowledgeBatchView,
    QuestionSetKnowledgeView,
)

ORG = 1
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


u = User.objects.filter(username="admin").first()
f = APIRequestFactory()


def get_queue(**q):
    req = f.get("/api/v1/questions/kp-queue/", q)
    force_authenticate(req, user=u)
    resp = QuestionKpQueueView.as_view()(req)
    resp.render()
    return resp.status_code, json.loads(resp.content.decode("utf-8"))


def post_mark(qid, body, **q):
    req = f.post(f"/api/v1/questions/{qid}/set-knowledge/", body,
                 format="json")
    force_authenticate(req, user=u)
    if q:
        req.query_params = q
    resp = QuestionSetKnowledgeView.as_view()(req, pk=qid)
    resp.render()
    return resp.status_code, json.loads(resp.content.decode("utf-8"))


def links(qid):
    return list(
        QuestionKnowledge.objects.for_org(ORG).filter(question_id=qid)
        .select_related("knowledge_node").order_by("knowledge_node__code")
    )


def get_candidates(node_id, **q):
    req = f.get("/api/v1/questions/kp-candidates/",
                {"knowledge_node_id": node_id, **q})
    force_authenticate(req, user=u)
    resp = QuestionKpCandidatesView.as_view()(req)
    resp.render()
    return resp.status_code, json.loads(resp.content.decode("utf-8"))


def post_batch(body):
    req = f.post("/api/v1/questions/set-knowledge-batch/", body,
                 format="json")
    force_authenticate(req, user=u)
    resp = QuestionSetKnowledgeBatchView.as_view()(req)
    resp.render()
    return resp.status_code, json.loads(resp.content.decode("utf-8"))


def snapshot(qids):
    """记录这些题当前的关联状态，供批量测试后**精确还原**。

    ★ 不能只「把主点改回 n_from」：批量会新建关联，
      还得把新建的那条删掉 —— 快照/还原比逐条补丁可靠。
    """
    return {
        q: [{"node_id": x.knowledge_node_id, "is_primary": x.is_primary,
             "weight": str(x.weight), "source": x.source} for x in links(q)]
        for q in qids
    }


def restore(snap):
    for qid, rows in snap.items():
        QuestionKnowledge.objects.for_org(ORG).filter(
            question_id=qid).delete()
        for r in rows:
            QuestionKnowledge.objects.for_org(ORG).create(
                org_id=ORG, question_id=qid,
                knowledge_node_id=r["node_id"],
                is_primary=r["is_primary"],
                weight=Decimal(r["weight"]), source=r["source"])


print("=" * 76)
print("知识点补标 · 回归（15 文档 §六）")
print("=" * 76)

# ══════════════════════════════════════════════════════════════
section("K0 · 前置：缺口确实存在（否则后面的修复没有意义）")
# ══════════════════════════════════════════════════════════════

gap = QuestionSelector.kp_gap(ORG)
print(f"     知识点 {gap['total_nodes']}，其中 0 题 {gap['zero_nodes']}")
check("★★ 存在「0 题知识点」（这是本功能要解决的问题）",
      gap["zero_nodes"] > 0,
      f"{gap['zero_nodes']} / {gap['total_nodes']}"
      + (f"：{gap['zero_list'][:3]}" if gap["zero_nodes"] else ""))

# 找一个「有 ≥2 个知识点」的任务，用于换标
multi = None
for task_id in (KnowledgeNode.objects.for_org(ORG)
                .values_list("task_id", flat=True).distinct()):
    nodes = list(KnowledgeNode.objects.for_org(ORG)
                 .filter(task_id=task_id).order_by("sort_order", "id"))
    if len(nodes) >= 2:
        multi = (task_id, nodes)
        break
check("★ 存在「一个任务 ≥2 个知识点」（换标的必要条件）",
      multi is not None,
      f"task={multi[0]}，知识点 {[n.code for n in multi[1]]}" if multi else "")

if multi is None:
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)

task_id, nodes = multi
n_from, n_to = nodes[0], nodes[1]

# ══════════════════════════════════════════════════════════════
section("K1 · 队列（`GET /questions/kp-queue/`）")
# ══════════════════════════════════════════════════════════════

st, j = get_queue(limit="3")
d = j.get("data") or {}
items = d.get("items") or []
check("★ `GET kp-queue` → 200", st == 200, f"HTTP {st}")
check("★★ 返回队列 + 进度 + **缺口**（老师要知道还差多少）",
      isinstance(d.get("progress"), dict)
      and isinstance(d.get("gap"), dict), f"keys={sorted(d)}")
prog = d.get("progress") or {}
check("★★ 进度自洽：`marked + remaining == total`",
      prog.get("marked", 0) + prog.get("remaining", 0) == prog.get("total"),
      f"{prog}")

first = items[0] if items else {}
check("★★★ 每项带**该任务下的候选知识点** `kp_options`"
      "（否则每题要多一次往返去查候选）",
      isinstance(first.get("kp_options"), list)
      and len(first["kp_options"]) >= 1,
      f"kp_options={first.get('kp_options')}")
check("★★ 每项带**当前主知识点**（老师要看到「原来是哪个」）",
      isinstance(first.get("current_primary"), dict)
      and "code" in first["current_primary"],
      f"current_primary={first.get('current_primary')}")
check("★ 每项带完整题干与选项（补标要看内容才能判断）",
      bool(first.get("stem_text")) and "options" in first,
      f"stem={len(first.get('stem_text') or '')} 字")

# ★ 默认只列未确认的
srcs = {it["current_primary"]["source"] for it in items
        if it.get("current_primary")}
check("★★ 默认只列 `source != TEACHER` 的题"
      "（导入占位 IMPORT / AI 待确认 —— 两者共用这一页）",
      srcs <= {"IMPORT", "AI", "STAT", "DEFAULT"},
      f"来源={srcs}")

ids = [it["id"] for it in items]
check("★ 队列按 `id` 升序（可续）", ids == sorted(ids), f"{ids}")

st, j = get_queue(limit="3", after_id=str(ids[-1]) if ids else "0")
ids2 = [x["id"] for x in ((j.get("data") or {}).get("items") or [])]
check("★ `after_id` 从该题之后继续", bool(ids2) and min(ids2) > ids[-1],
      f"after={ids[-1]} → {ids2}" if ids else "")

# ══════════════════════════════════════════════════════════════
section("K2 · ★★★ 换标写入与三条模型约束")
# ══════════════════════════════════════════════════════════════

# 取一道该任务下、当前主知识点是 n_from 的题
cand = (QuestionKnowledge.objects.for_org(ORG)
        .filter(knowledge_node=n_from, is_primary=True)
        .select_related("question").first())
check("★ 找到一道当前主知识点是「第一个点」的题（导入占位的样子）",
      cand is not None,
      f"题 #{cand.question_id}" if cand else "没找到")

if cand is None:
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)

qid = cand.question_id
before = links(qid)
before_to_count = QuestionKnowledge.objects.for_org(ORG).filter(
    knowledge_node=n_to).count()
print(f"     目标题 #{qid}；改前 {[(x.knowledge_node.code, x.is_primary, x.source) for x in before]}")
print(f"     「{n_to.code}」当前挂题数 {before_to_count}")

st, j = post_mark(qid, {"knowledge_node_id": n_to.id})
res = j.get("data") or {}
check("★★★ `POST set-knowledge` → 200", st == 200,
      f"HTTP {st} · {str(j.get('message'))[:60]}")

after = links(qid)
prim = [x for x in after if x.is_primary]
others = [x for x in after if not x.is_primary]

check("★★★ 约束① 改后**有且仅有一个**主知识点",
      len(prim) == 1 and prim[0].knowledge_node_id == n_to.id,
      f"主={[x.knowledge_node.code for x in prim]}")
check("★★★ 约束③ 旧的**被保留**（不是删掉）—— 可回溯",
      any(x.knowledge_node_id == n_from.id for x in after),
      f"关联={[(x.knowledge_node.code, x.is_primary) for x in after]}")
check("★★ 旧关联被降为**非主**且权重降到次要档（0.30）",
      all(str(x.weight) == "0.30" for x in others),
      f"{[(x.knowledge_node.code, str(x.weight)) for x in others]}")
check("★★★ 主知识点的 `source` 变成 `TEACHER`（人工确认过）",
      prim[0].source == "TEACHER", prim[0].source)
check("★★★ **「0 题知识点」少了 1 个**"
      "（这就是本功能修复缺口的机制）",
      QuestionKnowledge.objects.for_org(ORG).filter(
          knowledge_node=n_to).count() == before_to_count + 1,
      f"{before_to_count} → "
      f"{QuestionKnowledge.objects.for_org(ORG).filter(knowledge_node=n_to).count()}")

check("★★ 响应带回 `before`（审计/回溯用）",
      isinstance(res.get("before"), list) and res["before"],
      f"before={len(res.get('before') or [])} 条")
check("★★★ 响应**直接带下一题**（键盘流省一次往返）",
      isinstance(res.get("next"), dict) and res["next"].get("id") != qid,
      f"next=#{res.get('next', {}).get('id')}")
check("★ 响应带更新后的进度与缺口",
      isinstance(res.get("progress"), dict)
      and isinstance(res.get("gap"), dict),
      f"gap.zero={res.get('gap', {}).get('zero_nodes')}")

# 该题不再出现在「未确认」队列里
st, j = get_queue(limit="50", after_id="0")
queue_ids = [x["id"] for x in ((j.get("data") or {}).get("items") or [])]
check("★★ 改标后的题**不再出现**在未确认队列里",
      qid not in queue_ids, f"# {qid} in queue = {qid in queue_ids}")

# ══════════════════════════════════════════════════════════════
section("K3 · 幂等与约束②（已存在的关联要升，不能重复建）")
# ══════════════════════════════════════════════════════════════

n_links_before = len(links(qid))
st, j = post_mark(qid, {"knowledge_node_id": n_to.id})
res2 = j.get("data") or {}
check("★★★ 重复标同一知识点**幂等**（`changed=False`，不写库）",
      st == 200 and res2.get("changed") is False,
      f"changed={res2.get('changed')} · {str(res2.get('reason'))[:40]}")
check("★★ 关联条数不变（没有重复建 —— 撞 `uq_question_knowledge`）",
      len(links(qid)) == n_links_before,
      f"{n_links_before} → {len(links(qid))}")

# 换回来（n_to → n_from，此时 n_from 是**非主**关联 → 走「升」的分支）
st, j = post_mark(qid, {"knowledge_node_id": n_from.id})
check("★★★ 换回一个**已存在的非主**知识点 → 走「升为主」而不是新建",
      st == 200
      and len([x for x in links(qid) if x.is_primary]) == 1
      and [x for x in links(qid) if x.is_primary][0].knowledge_node_id
      == n_from.id,
      f"主={[x.knowledge_node.code for x in links(qid) if x.is_primary]}")

# ══════════════════════════════════════════════════════════════
section("K4 · 拒绝路径（放开 ≠ 不查）")
# ══════════════════════════════════════════════════════════════

# 跨任务的知识点必须被拒
other = (KnowledgeNode.objects.for_org(ORG)
         .exclude(task_id=task_id).first())
if other is not None:
    st, j = post_mark(qid, {"knowledge_node_id": other.id})
    check("★★★ **跨任务**的知识点被拒"
          "（组卷按任务+知识点取数，跨挂会抽错章）",
          st == 400, f"HTTP {st} · {str(j.get('message'))[:70]}")
else:
    check("★（跳过）没有别的任务可构造跨任务用例", True)

st, j = post_mark(qid, {"knowledge_node_id": 99999999})
check("★ 不存在的知识点 → 404/400", st in (400, 404), f"HTTP {st}")

st, j = post_mark(qid, {})
check("★ 缺 `knowledge_node_id` → 400", st == 400, f"HTTP {st}")

st, j = post_mark(qid, {"knowledge_node_id": n_to.id, "source": "HACK"})
check("★ 非法 `source` → 400", st == 400, f"HTTP {st}")

st, j = post_mark(qid, {"knowledge_node_id": n_to.id, "confidence": 1.5})
check("★ `confidence` 超范围 → 400", st == 400, f"HTTP {st}")

# ══════════════════════════════════════════════════════════════
section("K5 · ★★ 批量补标与候选池（2026-10-16 需求方口径）")
# ══════════════════════════════════════════════════════════════

# ── 候选池：必须是**该任务下**的题，且带「现在挂的是哪个点」 ──
st, j = get_candidates(n_to.id, limit="5")
cd = j.get("data") or {}
citems = cd.get("items") or []
check("★ `GET kp-candidates` → 200", st == 200, f"HTTP {st}")
check("★★★ 候选池只含**该知识点所属任务**的题"
      "（跨任务挂点会被拒，候选池不限任务等于让老师白勾）",
      all(it.get("task_id") == task_id for it in citems),
      f"task_id 集合={sorted({it.get('task_id') for it in citems})}")
check("★★ 每题带 `current_primary`（这一步是**覆盖**，要看清覆盖什么）",
      all(isinstance(it.get("current_primary"), dict) for it in citems
          if it.get("current_primary") is not None) and bool(citems),
      f"样例={citems[0].get('current_primary') if citems else None}")
check("★ 响应带知识点自身信息（页面标题/面包屑要显示）",
      (cd.get("node") or {}).get("code") == n_to.code,
      f"node={cd.get('node')}")
check("★ 默认**不限** only_unmarked（候选池要能看全部题）",
      cd.get("only_unmarked") is False, f"{cd.get('only_unmarked')}")

# search 要生效
if citems:
    key = (citems[0].get("stem_text") or "")[:6]
    st, j = get_candidates(n_to.id, limit="5", search=key)
    hit = ((j.get("data") or {}).get("items") or [])
    check("★ `search` 能在候选池里搜题干", bool(hit) and all(
        key in (x.get("stem_text") or "") for x in hit),
        f"关键词={key!r} 命中 {len(hit)} 条")

st, j = get_candidates(99999999)
check("★ 不存在的知识点 → 404/400", st in (400, 404), f"HTTP {st}")

# ── 准备批量用例：同任务下两道「占位」题 + 另一任务的一道题 ──
same_task = list(
    QuestionKnowledge.objects.for_org(ORG)
    .filter(knowledge_node=n_from, is_primary=True,
            question__task_id=task_id)
    .values_list("question_id", flat=True)[:2]
)
cross = (QuestionKnowledge.objects.for_org(ORG)
         .filter(is_primary=True)
         .exclude(question__task_id=task_id)
         .values_list("question_id", flat=True).first())

check("★ 找到 2 道同任务的占位题（批量用例）", len(same_task) == 2,
      f"{same_task}")
check("★ 找到 1 道**别的任务**的题（用来验「不整批回滚」）",
      cross is not None, f"#{cross}")

if len(same_task) == 2:
    q1, q2 = same_task
    ids_all = [q1, q2] + ([cross] if cross else [])
    snap = snapshot(ids_all)
    audit_before = AuditLog.objects.filter(
        action="question.knowledge_mark").count()

    st, j = post_batch({"knowledge_node_id": n_to.id, "question_ids": ids_all})
    bd = j.get("data") or {}
    check("★★★ `POST set-knowledge-batch` → 200", st == 200,
          f"HTTP {st} · {str(j.get('message'))[:70]}")
    check("★★★ 同任务的 2 道**被改标**", bd.get("updated") == 2,
          f"updated={bd.get('updated')} 详情={bd.get('updated_items')}")
    check("★★★ 跨任务那 1 道**被拒且带原因**"
          "（不是整批回滚：另外 2 道照常写入）",
          bd.get("failed") == 1
          and "任务" in str((bd.get("failed_items") or [{}])[0].get("detail")),
          f"failed={bd.get('failed_items')}")
    check("★★ 被拒的那道**没被改动**（原主点还在）",
          [x.knowledge_node_id for x in links(cross)
           if x.is_primary] == [x["node_id"] for x in snap[cross]
                                if x["is_primary"]] if cross else True,
          f"{[(x.knowledge_node.code, x.is_primary) for x in links(cross)]}")
    check("★★★ **逐题写审计**（批量 = N 次操作，不是 1 次）",
          AuditLog.objects.filter(action="question.knowledge_mark").count()
          == audit_before + 2,
          f"{audit_before} → "
          f"{AuditLog.objects.filter(action='question.knowledge_mark').count()}")
    check("★★ 「0 题知识点」缺口随批量收敛",
          QuestionKnowledge.objects.for_org(ORG).filter(
              knowledge_node=n_to).count() >= 3,
          f"「{n_to.code}」现有 "
          f"{QuestionKnowledge.objects.for_org(ORG).filter(knowledge_node=n_to).count()} 条关联")

    # ── 幂等：再提交同一批 → 全部 skipped，不再写库 ──
    audit_mid = AuditLog.objects.filter(
        action="question.knowledge_mark").count()
    st, j = post_batch({"knowledge_node_id": n_to.id,
                        "question_ids": [q1, q2]})
    bd2 = j.get("data") or {}
    check("★★★ 重复批量**幂等**：`updated=0 / skipped=2`",
          bd2.get("updated") == 0 and bd2.get("skipped") == 2,
          f"updated={bd2.get('updated')} skipped={bd2.get('skipped')}")
    check("★★ 幂等时**不重复写审计**（避免噪声）",
          AuditLog.objects.filter(action="question.knowledge_mark").count()
          == audit_mid,
          f"审计条数未变：{audit_mid}")

    # ── 上限与入参校验 ──
    st, j = post_batch({"knowledge_node_id": n_to.id,
                        "question_ids": list(range(1, 202))})
    check("★★ 超过 200 道 → 400（**拒绝而不是静默截断**）",
          st == 400, f"HTTP {st} · {str(j.get('message'))[:60]}")
    st, j = post_batch({"knowledge_node_id": n_to.id, "question_ids": []})
    check("★ 空 `question_ids` → 400", st == 400, f"HTTP {st}")
    st, j = post_batch({"knowledge_node_id": n_to.id,
                        "question_ids": [q1, q1, q2]})
    check("★ 重复 id 会去重（`requested=2`）",
          (j.get("data") or {}).get("requested") == 2,
          f"requested={(j.get('data') or {}).get('requested')}")

    # ── 收尾：还原这几道题 ──
    try:
        restore(snap)
        print(f"\n  已还原题 {ids_all} 的知识点关联")
    except Exception as e:  # noqa: BLE001
        print(f"\n  ★ 还原失败（不影响断言）：{type(e).__name__}: {e}")

# ── 收尾：把该题恢复成导入时的样子（K01 为主，source=IMPORT）──
try:
    q = QuestionService._must_get(ORG, qid)
    QuestionKnowledge.objects.for_org(ORG).filter(
        question=q, knowledge_node=n_to).delete()
    QuestionKnowledge.objects.for_org(ORG).filter(
        question=q, knowledge_node=n_from).update(
        is_primary=True, weight=1.0, source="IMPORT")
    print(f"\n  已把题 #{qid} 恢复为导入占位状态")
except Exception as e:  # noqa: BLE001
    print(f"\n  ★ 恢复失败（不影响断言）：{type(e).__name__}: {e}")

print()
print("=" * 76)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 76)
sys.exit(1 if FAIL else 0)
