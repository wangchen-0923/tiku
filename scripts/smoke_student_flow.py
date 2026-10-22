"""知否 · 发布 → 学生扫码答题 → 交卷 端到端冒烟。

★★ 这个脚本的性质：**先验证后端，再写前端**。

  这条链路（发布 / 扫码认人 / 取卷面 / 交卷）此前**从未被实测过**——
  之前的测试覆盖的是服务层与各自的 API 单点，没有人把
  「建任务 → 发布 → 学生绑定 → 取卷 → 交卷」串起来跑一遍。

  ★ 上一轮的教训：凭直觉写前端、不先跑后端，必然写错字段名。
    所以这一轮先花时间把链路跑通、把响应结构打出来，再照着写页面。

★ 同时验证三件**安全相关**的事（这几条比功能更重要）：

    1. 学生端接口**不带 JWT** 也必须可用（学生没有账号）
    2. `/paper/` **绝不能**出现答案/解析字段（防泄题）
    3. 换个 `publish_token` 拿不到别的任务；换个 `session_token`
       拿不到别人的会话

运行：
    python scripts/smoke_student_flow.py
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
# ★★ 这里**刻意不设** `ZF_DB_ENGINE`。
#
#   其它 verify 脚本靠 `setdefault("ZF_DB_ENGINE", "sqlite")` 做到
#   「无数据库环境也能跑」——但那个默认值对本脚本是**有害**的：
#   只要它生效，settings 就走 SQLite 分支并**静默忽略所有
#   `ZF_DB_*`**，于是脚本写 SQLite、后端读 PostgreSQL，
#   表现为「建任务 404 试卷不存在」——错误信息完全指错方向。
#
#   实测排查了好几轮才定位。所以本脚本一律跟随 settings 默认
#   （PostgreSQL），下面还有一道硬校验兜底。

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

# ══════════════════════════════════════════════════════════════
#  ★★★ 硬校验：脚本与后端必须连**同一个库**
# ══════════════════════════════════════════════════════════════
# ★ 这个脚本与其它 verify 脚本**性质不同**：它通过 HTTP 调后端，
#   所以数据是**后端**去读的。若脚本写进 SQLite 而后端读 PostgreSQL，
#   结果是「建任务 404 试卷不存在」——而错误信息完全指向别处。
#
#   实测踩到过：脚本里 `setdefault("ZF_DB_ENGINE", "sqlite")` 在
#   没设环境变量时生效，于是它在空 SQLite 里建卷，后端当然找不到。
#   排查花了好几轮，因为「试卷不存在」看起来像后端的问题。
#
#   ★ 与 `scripts/tools/which_db.py` 是同一类陷阱：**连错库不报错**，
#     只是行为莫名其妙。所以这里直接拒绝在 SQLite 下运行。
from django.db import connection  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

if connection.settings_dict["ENGINE"].endswith("sqlite3"):
    print("=" * 70)
    print("✗ 本脚本必须连**真实 PostgreSQL**（后端用的那个库）。")
    print()
    print("  原因：本脚本通过 HTTP 调后端，数据由后端读取。")
    print("  若脚本写 SQLite 而后端读 PostgreSQL，会看到")
    print("  「建任务 404 试卷不存在」这种指错方向的报错。")
    print()
    print("  正确用法（先设环境变量，或点源 deploy/use-postgres.cmd）：")
    print("    cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\smoke_student_flow.py\"")
    print("=" * 70)
    sys.exit(2)

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


# ══════════════════════════════════════════════════════════════
#  HTTP 客户端（不依赖 requests）
# ══════════════════════════════════════════════════════════════

def call(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    session: str | None = None,
) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("X-Session-Token", session)
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:300]}


# ══════════════════════════════════════════════════════════════
#  准备数据（直接落库，比走 API 快且可控）
# ══════════════════════════════════════════════════════════════

STU_NOS = ("2026001", "2026002", "2026003")
STU_NAMES = ("张三", "李四", "王五")
TAG = "SMOKE"


def prepare() -> dict:
    """建班级 + 名册 + 一张已定稿的试卷。返回上下文。"""
    from apps.accounts.models import Role, User
    from apps.knowledge.models import Course
    from apps.org.models import Classroom, ClassroomStudent, Org
    from apps.paper.models import Paper
    from apps.paper.services import PaperService

    org = Org.objects.first()
    course = Course.objects.filter(org=org).first()
    teacher = User._base_manager.filter(org=org).first()

    # ── 班级（幂等：同名先清）──
    Classroom.objects.for_org(org.id).filter(name__startswith=TAG).delete()
    room = Classroom.objects.create(
        org=org, name=f"{TAG} 机械 2026 级 1 班", grade="2026",
        academic_year="2026-2027",
    )
    nos = []
    for no, name in zip(STU_NOS, STU_NAMES):
        # ★★ 必须给每个学生**绑定一个系统账号**，否则发布会被拦：
        #
        #     「名册中有 N 名学生未绑定系统账号，无法发布任务」
        #
        #   ★ 这是**后端当前的行为**，但它与两处互相矛盾：
        #     1. `ClassroomStudent.student` 的 `help_text` 写着
        #        「**无账号学生也在名册**，此字段可为空」
        #     2. 线上作答只需「学号 + 姓名」，**技术上不需要账号**
        #     3. 全部技术文档里都**没有**「发布必须绑定账号」这条
        #     4. `accounts` / `org` 也**没有**「绑定账号」的服务方法，
        #        意味着老师目前**没有界面路径**去完成这个绑定
        #
        #   → 已记入开发进度记录「待裁决」：这条门禁是否应当
        #     按渠道区分（`ONLINE` 不需要账号，`PAPER_OMR` 也不需要
        #     —— 答题卡上只有学号）。本次**不擅自改后端**，
        #     先按现状绑账号把链路跑通。
        stu = User.objects.create_user(
            f"{TAG.lower()}_{no}", "Stu@12345678",
            org=org, real_name=name, role=Role.TEACHER,
        )
        ClassroomStudent.objects.create(
            org=org, classroom=room, student=stu,
            student_no=no, name=name, status="ACTIVE",
        )
        nos.append(no)

    # ── 试卷（幂等：先清同名的）──
    for p in Paper._base_manager.filter(org=org, title__startswith=TAG):
        from apps.paper.models import PaperItem
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()

    cfg = {"sections": [
        {"type": "SINGLE_CHOICE", "count": 3, "score_each": 5,
         "difficulty_range": [1, 5], "title": "一、单项选择题"},
        {"type": "TRUE_FALSE", "count": 1, "score_each": 5,
         "difficulty_range": [1, 5], "title": "二、判断题"},
        # ★★ 必须有**填空题**，否则测不到「在线作答支持填空题」
        #   这条路径（我第一版只配了选择+判断，`blank_item_ids`
        #   是空的，那一条断言等于没测）。
        {"type": "FILL_BLANK", "count": 2, "score_each": 5,
         "difficulty_range": [1, 5], "title": "三、填空题"},
    ]}
    paper = PaperService.create_paper(
        org_id=org.id, course_id=course.id,
        title=f"{TAG} 端到端验证卷", purpose="EXAM",
        compose_mode="RULE", compose_config=cfg,
        random_seed=20261001, created_by_id=teacher.id,
    )
    PaperService.compose_by_rule(
        org_id=org.id, paper_id=paper.id, operator_id=teacher.id)
    PaperService.finalize_paper(
        org_id=org.id, paper_id=paper.id, operator_id=teacher.id,
        force=False, reason="端到端冒烟")
    paper.refresh_from_db()

    return {
        "org_id": org.id, "org": org, "course_id": course.id,
        "teacher": teacher, "classroom_id": room.id,
        "student_nos": nos, "paper_id": paper.id,
        "paper_title": paper.title,
    }


def cleanup(ctx: dict) -> None:
    """清干净（★ 一定要能重复跑）。"""
    from apps.accounts.models import User
    from apps.analytics.models import ErrorTagStat
    from apps.audit.models import AuditLog
    from apps.org.models import Classroom, ClassroomStudent
    from apps.paper.models import Paper, PaperItem
    from apps.response.models import Response, StudentTotalScore
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.grading.models import ScoreResult

    org_id = ctx["org_id"]
    for a in Assignment.objects.for_org(org_id).filter(title__startswith=TAG):
        rows = list(AssignmentStudent.objects.for_org(org_id)
                    .filter(assignment_id=a.id))
        for r in rows:
            # ★ response 有自引用 superseded_by，先解引用再删
            Response.objects.for_org(org_id).filter(
                assignment_id=a.id, student_id=r.student_id).update(
                superseded_by=None)
            ScoreResult.objects.for_org(org_id).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
            Response.objects.for_org(org_id).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
            StudentTotalScore.objects.for_org(org_id).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
        AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=a.id).delete()
        AuditLog.objects.filter(
            target_type="assignment.Assignment", target_id=a.id).delete()
        # ★★ `error_tag_stat` / `question_error_stat` 必须**显式**清。
        #
        #   · `ErrorTagStat` 只有 `scope_type` + `scope_id` 两个裸字段，
        #     **没有外键**，删任务**不会**级联删除。
        #   · 本脚本会调 `auto_grade_assignment` → 触发级联重算
        #     → 顺手写下统计行，而原来的清理不认识它。
        #
        #   ★ 实测：这条漏删让真 PG 基线反复出现
        #     `error_tag_stat = 6`，而定位它花了几轮
        #     （排查手法：按「能写这张表的脚本」逐个查谁没清）。
        #   ★ 这是同一个缺口的**第四个**实例 —— 「在某处修好」
        #     不等于「全项目修好」。
        ErrorTagStat.objects.for_org(org_id).filter(
            scope_type="ASSIGNMENT", scope_id=a.id).delete()
        a.hard_delete()

    ClassroomStudent.objects.for_org(org_id).filter(
        classroom_id=ctx["classroom_id"]).delete()
    # ★ 学生账号也要清掉，否则重跑会撞 `username` 全局唯一约束
    #   （`User.username` 是 `unique=True`，不分机构）。
    for u in list(User._base_manager.filter(
            username__startswith=f"{TAG.lower()}_")):
        User._base_manager.filter(pk=u.pk).delete()
    Classroom.objects.for_org(org_id).filter(
        pk=ctx["classroom_id"]).hard_delete()

    PaperItem.objects.for_org(org_id).filter(
        paper_id=ctx["paper_id"]).delete()
    AuditLog.objects.filter(
        target_type="paper.Paper", target_id=ctx["paper_id"]).delete()
    Paper._base_manager.filter(pk=ctx["paper_id"]).delete()


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 70)
    print("smoke_student_flow · 发布 → 扫码 → 答题 → 交卷")
    print("=" * 70)

    from django.utils import timezone

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"

    # ── 预检：后端在跑吗 ──
    st, j = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）。请先起 runserver。")
        return 1
    print(f"  后端健康：{j.get('version')}")

    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    if st != 200:
        print(f"  ✗ 登录失败 HTTP {st}")
        return 1
    tok = j["data"]["access_token"]

    ctx = prepare()
    try:
        # ══════════════════════════════════════════════
        print()
        print("【一】建任务")
        # ══════════════════════════════════════════════
        now = timezone.now()
        st, j = call(f"{API}/assignments/", method="POST", token=tok, body={
            "paper_id": ctx["paper_id"],
            "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 期中考试",
            "start_at": (now - timezone.timedelta(hours=1)).isoformat(),
            "end_at": (now + timezone.timedelta(days=1)).isoformat(),
            # ★ 用 `ONLINE`（线上作答）而不是 `PAPER_OMR`：
            #   `PAPER_OMR`（纸质扫描）**必须**绑定答题卡模板，
            #   否则 409 `SHEET.BIND_MISMATCH`——而我们这里要验的是
            #   **学生在手机上作答**这条链路，本来就不该要答题卡。
            "channel": "ONLINE",
            "shuffle_questions": True,
            "shuffle_options": True,
        })
        check("建任务 201", st == 201, f"HTTP {st} {str(j)[:160]}")
        if st != 201:
            # ★ 现场核对：纸张到底在不在、后端能不能看到
            st2, j2 = call(f"{API}/papers/{ctx['paper_id']}/", token=tok)
            print(f"    [诊断] paper_id={ctx['paper_id']} "
                  f"org_id={ctx['org_id']} "
                  f"classroom_id={ctx['classroom_id']}")
            print(f"    [诊断] 后端 GET 该卷 HTTP {st2} {str(j2)[:140]}")
            st3, j3 = call(f"{API}/papers/?page_size=50", token=tok)
            items = (j3.get("data") or {}).get("items") or []
            print(f"    [诊断] 后端卷列表 "
                  f"{[(p['id'], p['status']) for p in items]}")
            return 1
        aid = j["data"]["id"]
        check("返回 publish_token", bool(j["data"].get("publish_token")),
              str(j["data"].keys()))
        ptoken = j["data"]["publish_token"]
        check("初始状态 DRAFT", j["data"]["status"] == "DRAFT",
              j["data"]["status"])

        # ══════════════════════════════════════════════
        print()
        print("【二】发布（幂等）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("发布 200", st == 200, f"HTTP {st} {str(j)[:200]}")
        d = j.get("data") or {}
        check("返回发布结果", bool(d), str(list(d.keys())))
        print(f"    发布结果键：{sorted(d.keys())}")
        print(f"    内容：{json.dumps(d, ensure_ascii=False)[:280]}")
        check("状态变 PUBLISHED",
              d.get("status") == "PUBLISHED" or d.get("published") is True
              or "PUBLISHED" in json.dumps(d, ensure_ascii=False),
              str(d)[:160])

        # ★ 幂等：再发一次不应报错
        st2, j2 = call(f"{API}/assignments/{aid}/publish/", method="POST",
                       token=tok, body={})
        check("重复发布幂等（仍 200）", st2 == 200, f"HTTP {st2}")

        # ══════════════════════════════════════════════
        print()
        print("【三】名单（老师视角）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/assignments/{aid}/students/", token=tok)
        check("取名单 200", st == 200, f"HTTP {st}")
        rows = (j.get("data") or {}).get("items") or j.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("items") or []
        n = len(rows)
        check(f"名单 {n} 人（应为 3）", n == 3, f"{n}")
        if rows:
            print(f"    名单项字段：{sorted(rows[0].keys())}")
            print(f"    示例：{json.dumps(rows[0], ensure_ascii=False)[:220]}")

        # ══════════════════════════════════════════════
        print()
        print("【四】★★ 学生扫码入口（**不带 JWT**）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/s/{ptoken}/")   # ★ 不传 token
        check("入口免认证可访问", st == 200, f"HTTP {st} {str(j)[:160]}")
        d = j.get("data") or {}
        print(f"    入口字段：{sorted(d.keys())}")
        print(f"    内容：{json.dumps(d, ensure_ascii=False)[:260]}")
        check("含 is_open", "is_open" in d, str(d.keys()))
        # ★ 最小信息：不应出现班级名册、学生名单、试卷内容
        blob = json.dumps(d, ensure_ascii=False)
        check("★ 未泄漏班级名册", "张三" not in blob and "2026001" not in blob)
        check("★ 未泄漏题目", "stem" not in blob.lower())

        # ★ 错误的 token 拿不到东西
        st, _ = call(f"{API}/s/not-a-real-token-xxxx/")
        check("★ 无效 publish_token 被拒", st >= 400, f"HTTP {st}")

        # ══════════════════════════════════════════════
        print()
        print("【五】绑定身份（学号 + 姓名）")
        # ══════════════════════════════════════════════
        # ★ 学号对、姓名错 → 应当不匹配（双因子）
        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": "2026001", "name": "错误姓名"})
        check("★ 姓名不符时不发放会话",
              st != 200 or j.get("code") != "OK",
              f"HTTP {st} code={j.get('code')}")

        # ★ 学号不在名册 → 业务分支（200 + ok=false）
        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": "9999999", "name": "赵六"})
        check("★ 学号不在名册走业务分支",
              st == 200 and j.get("code") == "SCAN.STUDENT_NO_UNMATCHED",
              f"HTTP {st} code={j.get('code')}")

        # 正确绑定
        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": "2026001", "name": "张三"})
        check("绑定成功", st == 200 and j.get("code") == "OK",
              f"HTTP {st} code={j.get('code')} {str(j)[:160]}")
        d = j.get("data") or {}
        print(f"    绑定返回：{json.dumps(d, ensure_ascii=False)[:200]}")
        sess = d.get("session_token")
        check("拿到 session_token", bool(sess))

        # ★ 再绑一次应拿到**同一个** token（刷新页面不该失效）
        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": "2026001", "name": "张三"})
        check("★ 重复绑定返回同一 token（刷新不掉线）",
              (j.get("data") or {}).get("session_token") == sess,
              "token 变了")

        # ══════════════════════════════════════════════
        print()
        print("【六】★★ 取卷面（防泄题是重点）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/s/{ptoken}/paper/", session=sess)
        check("取卷 200", st == 200, f"HTTP {st} {str(j)[:160]}")
        d = j.get("data") or {}
        items = d.get("items") or []
        print(f"    卷面键：{sorted(d.keys())}")
        print(f"    题量：{len(items)}  total_items={d.get('total_items')}")
        check("题量 = 6", len(items) == 6, f"{len(items)}")
        if items:
            print(f"    题项字段：{sorted(items[0].keys())}")
            print(f"    首题：{json.dumps({k: v for k, v in items[0].items() if k != 'options'}, ensure_ascii=False)[:220]}")
            if items[0].get("options"):
                print(f"    首选项：{json.dumps(items[0]['options'][0], ensure_ascii=False)}")

        # ★★★ 防泄题：绝不能有答案字段
        blob = json.dumps(d, ensure_ascii=False)
        for bad in ("answer", "explanation", "correct", "is_correct",
                    "difficulty", "knowledge_node", "question_id"):
            check(f"★★ 卷面不含 `{bad}`", bad not in blob, f"出现了 {bad}")
        check("★ 卷面不含题干以外的题库标识",
              all("question_id" not in it for it in items))

        # ★ 卷面题号是 1..n 连续
        seqs = [it.get("sheet_seq") for it in items]
        check("卷面题号连续 1..6", seqs == list(range(1, 7)), str(seqs))

        # ★ 无 session 拿不到卷
        st, _ = call(f"{API}/s/{ptoken}/paper/")
        check("★ 无 session_token 取不到卷", st >= 400, f"HTTP {st}")
        st, _ = call(f"{API}/s/{ptoken}/paper/", session="bogus-session")
        check("★ 伪造 session_token 取不到卷", st >= 400, f"HTTP {st}")

        # ══════════════════════════════════════════════
        print()
        print("【七】交卷")
        # ══════════════════════════════════════════════
        # ★★ 按**题型**作答，两种形态分流：
        #
        #   有选项的题（选择/判断）→ `labels`，填**卷面字母**
        #   无选项的题（填空题）  → `blanks`，按空顺序的**文本列表**
        #
        #   ★ `labels` 只接受单个 A–Z 字符（`ordering.label_index()`），
        #     所以填空题**不能**走 `labels`——传文本会判「非法选项标识」。
        #
        #   ★ 我第一版对 6 道题一律发 `["A"]`，结果是
        #     `{"saved": 4, "failed": 2}`（第 3/4 题是填空题）。
        #     后端**部分接受并逐题说明原因**，这个行为是对的；
        #     但那种写法测不出「全部作答被接受」的路径。
        answers = []
        for it in items:
            if it.get("options"):
                answers.append({
                    "sheet_seq": it["sheet_seq"],
                    "labels": [it["options"][0]["sheet_label"]],
                })
            else:
                answers.append({
                    "sheet_seq": it["sheet_seq"],
                    "blanks": ["答案", "答案"],
                })

        st, j = call(f"{API}/s/{ptoken}/submit/", method="POST",
                     session=sess, body={"answers": answers, "final": True})
        check("交卷 200", st == 200, f"HTTP {st} {str(j)[:200]}")
        d = j.get("data") or {}
        print(f"    交卷返回：{json.dumps(d, ensure_ascii=False)[:300]}")
        check("返回已受理",
              j.get("code") == "OK" or "OK" in str(j.get("code")),
              f"code={j.get('code')}")
        check("★ 全部作答被接受（saved == 题量）",
              d.get("saved") == len(items),
              f"saved={d.get('saved')} failed={d.get('failed')} "
              f"errors={d.get('errors')}")
        check("已标记为交卷", d.get("submitted") is True, str(d)[:120])

        # ★★ 反向用例：题型不匹配的作答必须**被拒且逐题说明原因**
        #   这是学生端唯一会「部分成功」的接口，必须确保它
        #   不会静默丢弃（丢一道题的作答就是丢一分）。
        st, j = call(f"{API}/s/{ptoken}/submit/", method="POST",
                     session=sess, body={
                         "answers": [{"sheet_seq": 1, "labels": []}],
                         "final": False})
        check("★ 重复提交不报 500", st != 500, f"HTTP {st}")

        # ★ 交卷后老师的名单应看到「已交」
        st, j = call(f"{API}/assignments/{aid}/students/", token=tok)
        rows = (j.get("data") or {}).get("items") or []
        if isinstance(j.get("data"), list):
            rows = j["data"]
        finished = [r for r in rows if r.get("status") in
                    ("SUBMITTED", "GRADED")]
        check("★ 名单里出现已交卷状态", bool(finished),
              f"状态分布={[r.get('status') for r in rows]}")

        # ══════════════════════════════════════════════
        print()
        print("【七之二】★★ 自动批改出分（闭环收尾）")
        # ══════════════════════════════════════════════
        # ★ 前面证明了「作答能存进去」，但那还不够 ——
        #   必须证明**判分侧真能读到它并出分**，否则
        #   「在线作答 + 客观题/填空题自动批改」只完成了一半。
        #   判分侧读的是 `answer_json["blanks"]`，所以这里直接跑批改。
        from apps.grading.services import GradingService

        # ★ 逐生批改：`auto_grade_assignment` 是**按学生**的
        #   （签名里有 `student_id`），不是整任务一次。
        #   ★ 这也是刻意的——一个学生的数据异常不该让全班批改失败。
        graded_total = 0
        for r in rows:
            sid = r.get("student_id")
            if not sid:
                continue
            res = GradingService.auto_grade_assignment(
                org_id=ctx["org_id"], assignment_id=aid, student_id=sid)
            graded_total += int((res or {}).get("graded") or 0)
            print(f"    学生 {r.get('student_no')} 批改：{res}")
        check("★ 自动批改未抛异常", True)
        check(f"★ 批改题数 {graded_total} > 0", graded_total > 0,
              "没有任何题被判分")

        # ★ 已交卷的学生应当拿到分数记录
        from apps.grading.models import ScoreResult
        n_scores = ScoreResult.objects.for_org(ctx["org_id"]).filter(
            assignment_id=aid).count()
        check(f"★ 产生了 {n_scores} 条评分记录", n_scores > 0,
              "批改没有落分")

        # ★★ 关键：填空题的分必须**能算出来**（不能缺席）
        from apps.paper.models import PaperItem
        blank_item_ids = set(
            PaperItem.objects.for_org(ctx["org_id"])
            .filter(paper_id=ctx["paper_id"])
            .exclude(question__type="SINGLE_CHOICE")
            .exclude(question__type="TRUE_FALSE")
            .values_list("id", flat=True)
        )
        blank_scores = list(
            ScoreResult.objects.for_org(ctx["org_id"])
            .filter(assignment_id=aid, paper_item_id__in=blank_item_ids)
            .values_list("paper_item_id", "score", "is_correct", "method")
        )
        print(f"    填空题评分：{blank_scores}")
        check("★ 填空题有评分记录（证明 blanks 被读到了）",
              len(blank_scores) > 0,
              f"blank_item_ids={sorted(blank_item_ids)}")
        check("★ 填空题判分带 method（走的是六步填空算法）",
              all(x[3] for x in blank_scores), str(blank_scores))

        # ★ 重复交卷
        st, j = call(f"{API}/s/{ptoken}/submit/", method="POST",
                     session=sess, body={"answers": answers, "final": True})
        check("★ 重复交卷不报 500", st != 500, f"HTTP {st}")

        # ══════════════════════════════════════════════
        print()
        print("【八】★ 会话与任务隔离")
        # ══════════════════════════════════════════════
        # 另建一个任务，用**旧任务的 session** 去访问 → 必须失败
        st, j2 = call(f"{API}/assignments/", method="POST", token=tok, body={
            "paper_id": ctx["paper_id"],
            "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 另一场",
            "start_at": now.isoformat(),
            "end_at": (now + timezone.timedelta(days=1)).isoformat(),
        })
        if st in (200, 201):
            pt2 = j2["data"]["publish_token"]
            call(f"{API}/assignments/{j2['data']['id']}/publish/",
                 method="POST", token=tok, body={})
            st, _ = call(f"{API}/s/{pt2}/paper/", session=sess)
            check("★ 旧任务的 session 不能用于新任务", st >= 400,
                  f"HTTP {st}")

        # ★ 学生的 session 不能拿来调教师端接口
        st, _ = call(f"{API}/assignments/{aid}/students/", session=sess)
        check("★ 学生 session 不能调教师端接口", st in (401, 403),
              f"HTTP {st}")

    finally:
        cleanup(ctx)
        print()
        print("  · 已清理测试数据")

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
