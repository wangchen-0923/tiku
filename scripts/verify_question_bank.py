"""验收：题库双轨（计划 U11 模型与回填 / U12 可见性收口）。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg \
    <py3.12> scripts/verify_question_bank.py

★★ 这个脚本要拦住的是**两个方向的错**，而它们看起来完全不同：

    ① 漏放开  —— 公共题在别的机构看不见。老师抱怨「公共库是空的」，
                 而数据库里明明有 200 道已审核的题。
                 成因几乎总是：某处取数没走 `for_org()`。

    ② 漏收紧  —— 公共题在**不该看见**的地方被看见 / 被改。
                 后果是跨机构泄露未审内容，或一校老师改了另一校提交、
                 平台已审核的题。成因几乎总是：某处写路径复用了读路径的取数。

  所以下面每一组都是「同一道题，从两个机构的视角各看一遍」。
  只测一个机构是测不出这两个方向的——那正是 R1 风险点。

★★ 全部在 `transaction.atomic()` 里跑并 `set_rollback(True)`：
   造出来的机构、用户、题目一行都不落库。**不要在清理里删数据**——
   删不干净会留下「机构 2 有 0 道题但有一个默认题库」这种脏状态，
   第二次跑就会看到不一样的数字。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from django.db import IntegrityError, connection, transaction  # noqa: E402
from django.db.models import Count, F  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.common.constants import QuestionStatus  # noqa: E402
from apps.org.models import Org, OrgType  # noqa: E402
from apps.org.services import OrgService  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionBank,
    QuestionBankKind,
)
from apps.questionbank.selectors import QuestionSelector  # noqa: E402
from apps.questionbank.services import (  # noqa: E402
    QuestionBankService,
    QuestionService,
)

PASS = 0
FAIL = 0
ORG_ID = 1
TAG = "qbv_"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def data(resp):
    d = getattr(resp, "data", None)
    if isinstance(d, dict) and "data" in d:
        return d["data"]
    return d


def raw(resp):
    """错误响应要读**未拆包装**的 `resp.data`。

    ★ 这一处踩过：成功响应的 `resp.data` 就是业务体，
      而错误响应是 `{code, message, data:{detail}}` —— 用 `data()` 拆一层之后
      `code` 就丢了，于是「拒绝码对不对」这类断言永远拿到空字符串、
      永远失败，看起来像业务没写对。**两种形状必须分开取。**
    """
    return getattr(resp, "data", None)


def code_of(resp) -> str:
    d = raw(resp)
    if isinstance(d, dict):
        return str(d.get("code") or "")
    return ""


def detail_of(resp) -> str:
    d = raw(resp)
    if isinstance(d, dict):
        extra = d.get("data")
        det = extra.get("detail") if isinstance(extra, dict) else None
        return str(det or d.get("message") or "")
    return ""


def rows_of(resp) -> list:
    d = data(resp)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("items", "results"):
            if isinstance(d.get(key), list):
                return d[key]
    return []


def total_of(resp) -> int | None:
    d = data(resp)
    if isinstance(d, dict):
        if "count" in d:
            return int(d["count"])
        if "total" in d:
            return int(d["total"])
    return None


def new_school(name: str) -> Org:
    return Org.objects.create(
        name=name, short_name=name[:6], type=OrgType.SCHOOL, status="ACTIVE",
        ai_config={}, grading_config={"student_no_default_digits": 8},
        quota_config={},
    )


def main() -> int:
    print("=" * 74)
    print("  题库双轨验收（U11 模型与回填 · U12 可见性收口）")
    print("=" * 74)

    if connection.settings_dict["ENGINE"].endswith("sqlite3"):
        print("  [FAIL] 必须在 PostgreSQL 上跑（sqlite 是空壳副本，数据对不上）")
        return 2

    school = User.objects.filter(
        role=Role.ORG_ADMIN, org_id=ORG_ID, deleted_at__isnull=True).first()
    if school is None:
        print("  [FAIL] 缺少学校平台账户（admin / ORG_ADMIN）")
        return 2
    course = (
        Question.objects.filter(org_id=ORG_ID, course__isnull=False)
        .values_list("course_id", flat=True).first()
    )
    if course is None:
        print("  [FAIL] org 1 没有带课程的题目，无法造验收数据")
        return 2

    with transaction.atomic():
        # ══════════════════════════════════════════════════
        print("\n【U11-1】结构与存量回填")
        # ══════════════════════════════════════════════════
        platform_orgs = list(Org.objects.filter(type=OrgType.PLATFORM))
        check("★ 平台机构行存在", len(platform_orgs) == 1, f"{len(platform_orgs)} 行")
        pf = platform_orgs[0] if platform_orgs else None
        check("★ 平台机构 type=PLATFORM", pf is not None and pf.type == "PLATFORM")

        pub = QuestionBankService.public_bank()
        check("★ 公共库存在", pub is not None)
        check("★★ 公共库挂在平台机构行上",
              pub is not None and pf is not None and pub.org_id == pf.id,
              f"bank.org={getattr(pub, 'org_id', None)} platform={pf and pf.id}")
        check("★ 公共库 kind=PUBLIC",
              pub is not None and pub.kind == QuestionBankKind.PUBLIC)

        total_all = Question._base_manager.count()
        null_bank = Question._base_manager.filter(bank__isnull=True).count()
        check(f"★★ 1946 道题全有 bank_id（含软删，共 {total_all} 道）",
              null_bank == 0, f"{null_bank} 道没有题库")

        # 私有题的 org 必须与库的 org 一致（不变式 1）
        mismatched = (
            Question._base_manager
            .filter(bank__kind=QuestionBankKind.PRIVATE)
            .exclude(org_id=F("bank__org_id"))
            .count()
        )
        check("★★ 私有题 org_id 与所属库 org_id 一致", mismatched == 0,
              f"{mismatched} 道不一致")

        # 每机构恰一个默认私有库
        dup_default = list(
            QuestionBank.objects.filter(kind=QuestionBankKind.PRIVATE, is_default=True)
            .values("org_id").annotate(n=Count("id")).filter(n__gt=1)
        )
        check("★★ 每机构只有一个默认私有库", not dup_default, str(dup_default))

        # ══════════════════════════════════════════════════
        print("\n【U11-2】建题落库规则")
        # ══════════════════════════════════════════════════
        b1 = QuestionBankService.ensure_default_private(ORG_ID)
        b2 = QuestionBankService.ensure_default_private(ORG_ID)
        check("★★ ensure_default_private 幂等（两次同一行）", b1.id == b2.id,
              f"{b1.id} vs {b2.id}")

        q_default = QuestionService.create_question(
            org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
            stem_text=f"{TAG}建题未指定题库应落默认私有库", answer={"value": True},
            check_duplicate=False,
        )
        check("★★ 建题未指定题库 → 落本机构默认私有库",
              q_default.bank_id == b1.id,
              f"bank_id={q_default.bank_id} 期望 {b1.id}")

        q_custom = QuestionService.create_question(
            org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
            stem_text=f"{TAG}建题指定本机构空私有库", answer={"value": True},
            check_duplicate=False, bank_id=b1.id,
        )
        check("★ 显式指定本机构私有库 → 落该库", q_custom.bank_id == b1.id)

        # 造一个「别人的私有库」：另一个机构
        other = new_school(f"{TAG}第二学校")
        other_teacher = User.objects.create_user(
            username=f"{TAG}t2", password="Tmp@12345678", real_name="验收他校教师",
            role=Role.TEACHER, org_id=other.id,
        )
        other_bank = QuestionBankService.ensure_default_private(other.id)
        check("★ 他校默认库与本机构默认库不是同一行", other_bank.id != b1.id)

        r_bad = None
        try:
            QuestionService.create_question(
                org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
                stem_text=f"{TAG}传别人的 bank_id", answer={"value": True},
                check_duplicate=False, bank_id=other_bank.id,
            )
        except Exception as exc:  # noqa: BLE001
            r_bad = exc
        check("★★ 传他校的 bank_id 被拒（不能把题挂到别人库）",
              r_bad is not None, "没有抛错，题被挂到别的机构库上了")

        r_pub_bank = None
        try:
            QuestionService.create_question(
                org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
                stem_text=f"{TAG}传公共库 id", answer={"value": True},
                check_duplicate=False, bank_id=pub.id,
            )
        except Exception as exc:  # noqa: BLE001
            r_pub_bank = exc
        check("★★ 直接建进公共库被拒（必须先下载/提交流程）",
              r_pub_bank is not None, "没有抛错")

        # DB 级：第二个默认库
        db_guard = False
        try:
            with transaction.atomic():
                QuestionBank.objects.create(
                    org_id=ORG_ID, kind=QuestionBankKind.PRIVATE,
                    name=f"{TAG}第二个默认库", is_default=True,
                )
        except IntegrityError:
            db_guard = True
        check("★★ DB 层拦住第二个默认私有库（部分唯一索引生效）", db_guard)

        # ══════════════════════════════════════════════════
        print("\n【U12-1】造双轨数据：1 道公共已审 + 1 道公共待审 + 1 道公共草稿")
        # ══════════════════════════════════════════════════
        def make_public(stem: str, status: str) -> Question:
            q = QuestionService.create_question(
                org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
                stem_text=stem, answer={"value": True}, check_duplicate=False,
                created_by_id=other_teacher.id,
            )
            q.org_id = pf.id
            q.bank_id = pub.id
            q.status = status
            q.save(update_fields=["org", "bank", "status", "updated_at"])
            return q

        q_pub_ok = make_public(f"{TAG}公共库已审核题", QuestionStatus.APPROVED)
        q_pub_pending = make_public(f"{TAG}公共库待审题", QuestionStatus.PENDING_REVIEW)
        q_pub_draft = make_public(f"{TAG}公共库草稿题", QuestionStatus.DRAFT)
        q_priv = QuestionService.create_question(
            org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
            stem_text=f"{TAG}本机构私有题", answer={"value": True},
            check_duplicate=False,
        )
        check("★ 测试数据就位（公共题 org 指向平台行）",
              q_pub_ok.org_id == pf.id and q_pub_ok.bank_id == pub.id)

        # 目标机构：另一个学校（模拟「外校老师」）
        partner = new_school(f"{TAG}外校")
        partner_admin = User.objects.create_user(
            username=f"{TAG}pa", password="Tmp@12345678", real_name="外校管理员",
            role=Role.ORG_ADMIN, org_id=partner.id,
        )
        c_partner = APIClient()
        c_partner.force_authenticate(user=partner_admin)
        c_school = APIClient()
        c_school.force_authenticate(user=school)

        # ══════════════════════════════════════════════════
        print("\n【U12-2】for_org 收口：两个方向各看一遍")
        # ══════════════════════════════════════════════════
        def visible(org_id: int, qid: int) -> bool:
            return Question.objects.for_org(org_id).filter(pk=qid).exists()

        check("★★ 公共已审题：本院可见", visible(ORG_ID, q_pub_ok.id))
        check("★★ 公共已审题：外校可见（双轨的核心）", visible(partner.id, q_pub_ok.id))
        check("★★ 公共待审题：本院也不可见（本院不是它所属机构）",
              not visible(ORG_ID, q_pub_pending.id))
        check("★★ 公共待审题：外校不可见（未审核不放行）",
              not visible(partner.id, q_pub_pending.id))
        check("★★ 公共草稿题：外校不可见", not visible(partner.id, q_pub_draft.id))
        check("★★ 本机构私有题：本院可见", visible(ORG_ID, q_priv.id))
        check("★★ 本机构私有题：外校不可见（不能泄露）",
              not visible(partner.id, q_priv.id))

        check("★★ usable_for_paper 不含公共已审题（决策 3：先下载）",
              not Question.objects.for_org(ORG_ID).usable_for_paper()
              .filter(pk=q_pub_ok.id).exists())
        check("★ private_only 排除公共已审题",
              not Question.objects.for_org(ORG_ID).private_only()
              .filter(pk=q_pub_ok.id).exists())
        check("★ private_only 含本机构私有题",
              Question.objects.for_org(ORG_ID).private_only()
              .filter(pk=q_priv.id).exists())

        # ══════════════════════════════════════════════════
        print("\n【U12-3】接口层：list 的 bank 参数")
        # ══════════════════════════════════════════════════
        r_priv = c_partner.get(
            "/api/v1/questions/?keyword=" + TAG + "&page_size=50")
        r_pub = c_partner.get("/api/v1/questions/?bank=public&page_size=1")
        r_all = c_partner.get(
            "/api/v1/questions/?bank=public&keyword=" + TAG + "&page_size=50")
        priv_ids = {int(x["id"]) for x in rows_of(r_priv)}
        check("★★ 外校默认列表（private）不含公共题",
              q_pub_ok.id not in priv_ids,
              f"默认列表里出现了公共题 #{q_pub_ok.id}：{sorted(priv_ids)[:5]}")
        check("★★ 外校 bank=public 能搜到公共已审题",
              any(int(x["id"]) == q_pub_ok.id for x in rows_of(r_all)),
              f"返回 {len(rows_of(r_all))} 行：{[x.get('id') for x in rows_of(r_all)][:5]}")
        ids_pub = {int(x["id"]) for x in rows_of(r_all)}
        check("★★ 公共 Tab 不含待审/草稿",
              q_pub_pending.id not in ids_pub and q_pub_draft.id not in ids_pub)

        r_us = c_partner.get(
            "/api/v1/questions/?bank=public&only_usable=1&page_size=1")
        check("★★ 公共 + only_usable 明确报 400（不是返回空列表）",
              r_us.status_code == 400, f"HTTP {r_us.status_code}")
        r_badparam = c_partner.get("/api/v1/questions/?bank=xxx&page_size=1")
        check("★ bank 参数非法 → 400", r_badparam.status_code == 400,
              f"HTTP {r_badparam.status_code}")

        # ══════════════════════════════════════════════════
        print("\n【U12-4】写路径不能命中公共题")
        # ══════════════════════════════════════════════════
        r_get = c_partner.get(f"/api/v1/questions/{q_pub_ok.id}/")
        check("★ 外校能查看公共已审题详情（只读）",
              r_get.status_code == 200, f"HTTP {r_get.status_code}")
        r_get_pending = c_partner.get(f"/api/v1/questions/{q_pub_pending.id}/")
        check("★★ 外校看不到待审题详情（404）",
              r_get_pending.status_code == 404, f"HTTP {r_get_pending.status_code}")

        r_patch = c_partner.patch(
            f"/api/v1/questions/{q_pub_ok.id}/",
            {"stem_text": f"{TAG}外校试图改公共题"}, format="json")
        check("★★ 外校 PATCH 公共题被拒（409）",
              r_patch.status_code == 409,
              f"HTTP {r_patch.status_code} {code_of(r_patch)}")
        check("★★ 拒绝码是 QUESTION_BANK.INVALID_STATE",
              code_of(r_patch) == "QUESTION_BANK.INVALID_STATE", code_of(r_patch))
        check("★★ 拒绝消息提示「下载到本校私有库」",
              "下载" in detail_of(r_patch), detail_of(r_patch))
        r_after = c_school.get(f"/api/v1/questions/{q_pub_ok.id}/")
        check("★★ 被拒之后题干没被改动",
              (data(r_after) or {}).get("stem_text") == q_pub_ok.stem_text,
              str((data(r_after) or {}).get("stem_text"))[:40])

        r_del = c_partner.delete(
            f"/api/v1/questions/{q_pub_ok.id}/", {}, format="json")
        check("★★ 外校 DELETE 公共题被拒（409）",
              r_del.status_code == 409,
              f"HTTP {r_del.status_code} {code_of(r_del)}")

        r_mark = c_partner.post(
            f"/api/v1/questions/{q_pub_ok.id}/mark-difficulty/",
            {"difficulty": 4}, format="json")
        check("★★ 外校给公共题打难度标被拒",
              r_mark.status_code in (400, 404, 409),
              f"HTTP {r_mark.status_code}")

        # ══════════════════════════════════════════════════
        print("\n【U12-5】静态检查：除 questionbank 外无人直查 Question.org_id")
        # ══════════════════════════════════════════════════
        #: 这条断言是给「半年后的人」看的：他很可能为了赶进度，
        #: 在某个新模块里写 `Question.objects.filter(org_id=...)`，
        #: 于是那个模块永远看不到公共题（漏放开），或者绕过了 `private_only()`
        #: （漏收紧）。两种错都不会报错。
        #:
        #: ★ 用 AST 而不是正则扫全文：正则会把**注释与文档字符串**里的示例
        #:   当成真代码（`paper/selectors.py` 的模块注释里就写着
        #:   「业务代码里出现 `Question.objects.filter(...)` 一律视为缺陷」，
        #:   正则版本会把这句话本身报成违规）。
        import ast

        def _root_name(node) -> str | None:
            while isinstance(node, ast.Attribute):
                node = node.value
            return node.id if isinstance(node, ast.Name) else None

        offenders: list[str] = []
        apps_dir = ROOT / "server" / "apps"
        for path in apps_dir.rglob("*.py"):
            rel = path.relative_to(ROOT / "server").as_posix()
            if rel.startswith("apps/questionbank/") or "/migrations/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                # Question.objects.<attr>，其中 <attr> 绕过 for_org
                if node.attr not in {"filter", "all_orgs", "get_queryset",
                                     "exclude", "update", "create"}:
                    continue
                inner = node.value
                if (isinstance(inner, ast.Attribute) and inner.attr == "objects"
                        and _root_name(inner) == "Question"):
                    offenders.append(f"{rel}:{node.lineno}  Question.objects.{node.attr}")
        check("★★ 无模块绕过 for_org 直查/直改 Question", not offenders,
              "\n           " + "\n           ".join(offenders[:8]))

        # ══════════════════════════════════════════════════
        print("\n【U13】教师提交到公共库")
        # ══════════════════════════════════════════════════
        from apps.knowledge.models import Course, KnowledgeNode
        from apps.questionbank.models import QuestionKnowledge

        course_obj = Course.objects.for_org(ORG_ID).filter(pk=course).first()
        node = KnowledgeNode.objects.for_org(ORG_ID).filter(
            course_id=course, task__isnull=False).order_by("code").first() \
            or KnowledgeNode.objects.for_org(ORG_ID).filter(
                course_id=course).order_by("code").first()
        check("★ 验收环境具备课程与知识点", course_obj is not None and node is not None)
        if course_obj is None or node is None:
            print("  [FAIL] 缺少课程/知识点，U13-U15 无法继续")
            transaction.set_rollback(True)
            return 2

        submitter = User.objects.create_user(
            username=f"{TAG}sub", password="Tmp@12345678", real_name="提交教师",
            role=Role.TEACHER, org_id=ORG_ID,
        )
        c_submitter = APIClient()
        c_submitter.force_authenticate(user=submitter)

        q_src = QuestionService.create_question(
            org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
            stem_text=f"{TAG}待提交到公共库的题", answer={"value": True},
            check_duplicate=False, created_by_id=submitter.id,
            task_id=node.task_id if node else None,
        )
        QuestionKnowledge.objects.create(
            org_id=ORG_ID, question=q_src, knowledge_node=node,
            is_primary=True, source="TEACHER",
        )

        r_sub = c_submitter.post(
            f"/api/v1/questions/{q_src.id}/submit-to-public/",
            {"note": "这道题覆盖销的定位作用"}, format="json")
        check("★★ 教师提交自己的题 → 201",
              r_sub.status_code == 201, f"HTTP {r_sub.status_code} {code_of(r_sub)}")
        sub_body = data(r_sub) or {}
        pub2_id = sub_body.get("public_question_id")
        check("★★ 返回公共副本 id 与状态", bool(pub2_id)
              and sub_body.get("status") == QuestionStatus.PENDING_REVIEW,
              str(sub_body)[:120])

        pub2 = Question._base_manager.filter(pk=pub2_id).first() if pub2_id else None
        check("★★ 公共副本挂在平台机构 + 公共库",
              pub2 is not None and pub2.org_id == pf.id and pub2.bank_id == pub.id,
              f"org={getattr(pub2, 'org_id', None)}")
        check("★★ 私有原件**仍在原处**（复制而非移动）",
              Question.objects.for_org(ORG_ID).private_only()
              .filter(pk=q_src.id).exists())
        check("★ 原件状态未变", 
              Question._base_manager.get(pk=q_src.id).status == QuestionStatus.DRAFT)
        check("★★ 选项被一起复制（判断题补的规范选项）",
              pub2 is not None and pub2.options.count() == q_src.options.count(),
              f"{pub2 and pub2.options.count()} vs {q_src.options.count()}")
        check("★★ 知识点**不复制**，但 code 记进 source_ref",
              pub2 is not None and pub2.knowledge_links.count() == 0
              and node.code in ((pub2.source_ref or {}).get("source_node_codes") or []),
              str((pub2.source_ref or {}).get("source_node_codes") if pub2 else None))
        check("★ 作答统计被清零（不带本校实测数据进公共库）",
              pub2 is not None and pub2.answer_count == 0)

        r_dup_submit = c_submitter.post(
            f"/api/v1/questions/{q_src.id}/submit-to-public/", {}, format="json")
        check("★★ 重复提交同一道题 → 409（审核队列不该出现重复题）",
              r_dup_submit.status_code == 409,
              f"HTTP {r_dup_submit.status_code} {code_of(r_dup_submit)}")

        r_sub_other = c_submitter.post(
            f"/api/v1/questions/{q_priv.id}/submit-to-public/", {}, format="json")
        check("★★ 提交别人创建的题 → 403",
              r_sub_other.status_code == 403,
              f"HTTP {r_sub_other.status_code} {code_of(r_sub_other)}")

        r_sub_by_admin = c_school.post(
            f"/api/v1/questions/{q_src.id}/submit-to-public/", {}, format="json")
        check("★★ 机构管理员可以代提交（已提交过 → 409，说明权限已放行）",
              r_sub_by_admin.status_code == 409,
              f"HTTP {r_sub_by_admin.status_code} {code_of(r_sub_by_admin)}")

        r_sub_public = c_submitter.post(
            f"/api/v1/questions/{q_pub_ok.id}/submit-to-public/", {}, format="json")
        check("★ 把公共题再提交一次 → 拒绝（写路径只认私有题）",
              r_sub_public.status_code in (400, 403, 409),
              f"HTTP {r_sub_public.status_code}")

        # ══════════════════════════════════════════════════
        print("\n【U14】平台管理员审核队列")
        # ══════════════════════════════════════════════════
        sysadmin = User.objects.filter(
            role=Role.PLATFORM_ADMIN, is_active=True).order_by("id").first()
        check("★ 存在平台管理员账号", sysadmin is not None)
        c_admin = APIClient()
        c_admin.force_authenticate(user=sysadmin)

        r_q_denied = c_school.get("/api/v1/questions/public/review-queue/")
        check("★★ 机构管理员看审核队列 → 403（不该看到同行提交了什么）",
              r_q_denied.status_code == 403, f"HTTP {r_q_denied.status_code}")

        r_q = c_admin.get("/api/v1/questions/public/review-queue/?limit=200")
        check("★★ 平台管理员能取审核队列", r_q.status_code == 200,
              f"HTTP {r_q.status_code}")
        qb = data(r_q) or {}
        items = qb.get("items") or []
        stats = qb.get("stats") or {}
        check("★★ 队列里有刚提交的题",
              any(int(i["id"]) == pub2_id for i in items),
              f"共 {len(items)} 条")
        check("★★ 队列项带来源机构名（审核员要知道谁提交的）",
              all("submitted_from_org_name" in i for i in items))
        check("★★ 队列项带等待时长（R7 堆积可见）",
              all(i.get("waiting_hours") is not None for i in items))
        check("★ stats 四态齐全",
              set(stats) >= {"pending", "approved", "rejected", "archived", "total"},
              str(stats))
        check("★★ stats.pending 与队列 total 一致",
              stats.get("pending") == qb.get("total"),
              f"{stats.get('pending')} vs {qb.get('total')}")

        r_rej_no_reason = c_admin.post(
            f"/api/v1/questions/public/{pub2_id}/review/",
            {"action": "REJECT"}, format="json")
        check("★★ 驳回不填理由 → 400 且码为 REASON_REQUIRED",
              r_rej_no_reason.status_code == 400
              and code_of(r_rej_no_reason) == "QUESTION_BANK.REASON_REQUIRED",
              f"HTTP {r_rej_no_reason.status_code} {code_of(r_rej_no_reason)}")

        r_appr = c_admin.post(
            f"/api/v1/questions/public/{pub2_id}/review/",
            {"action": "APPROVE", "reason": "内容可用"}, format="json")
        check("★★ 通过审核 → 200", r_appr.status_code == 200,
              f"HTTP {r_appr.status_code} {code_of(r_appr)}")
        check("★★ 状态变为 APPROVED",
              Question._base_manager.get(pk=pub2_id).status == QuestionStatus.APPROVED)
        check("★★ 通过后外校立刻可见（for_org 只放行 APPROVED）",
              Question.objects.for_org(partner.id).filter(pk=pub2_id).exists())

        r_appr2 = c_admin.post(
            f"/api/v1/questions/public/{pub2_id}/review/",
            {"action": "APPROVE"}, format="json")
        check("★★ 重复审核同一道 → 409（状态机挡住）",
              r_appr2.status_code == 409,
              f"HTTP {r_appr2.status_code} {code_of(r_appr2)}")

        # 驳回路径：另造一道待审（同一道题不能提交两次，见上面的幂等断言）
        q_src2 = QuestionService.create_question(
            org_id=ORG_ID, course_id=course, question_type="TRUE_FALSE",
            stem_text=f"{TAG}将被驳回的公共题", answer={"value": True},
            check_duplicate=False, created_by_id=submitter.id,
        )
        r_sub2 = c_submitter.post(
            f"/api/v1/questions/{q_src2.id}/submit-to-public/", {}, format="json")
        pub3_id = (data(r_sub2) or {}).get("public_question_id")
        check("★ 第二道待审题已提交", bool(pub3_id), str(data(r_sub2))[:100])

        r_rej = c_admin.post(
            f"/api/v1/questions/public/{pub3_id}/review/",
            {"action": "REJECT", "reason": "题干缺少条件，请补"}, format="json")
        check("★★ 驳回成功 → 200", r_rej.status_code == 200,
              f"HTTP {r_rej.status_code} {code_of(r_rej)}")
        rej_q = Question._base_manager.get(pk=pub3_id)
        check("★★ 状态为 REJECTED", rej_q.status == QuestionStatus.REJECTED)
        check("★★ 驳回理由落库（老师收到的唯一反馈）",
              ((rej_q.source_ref or {}).get("reject_reason") or "") == "题干缺少条件，请补",
              str((rej_q.source_ref or {}).get("reject_reason")))
        check("★★ 被驳回的题外校不可见",
              not Question.objects.for_org(partner.id).filter(pk=pub3_id).exists())

        # 批量：混入一道已通过的
        r_batch = c_admin.post(
            "/api/v1/questions/public/batch-review/",
            {"question_ids": [pub2_id, pub3_id], "action": "APPROVE"},
            format="json")
        bb = data(r_batch) or {}
        check("★★ 批量审核：已审的进 skipped，不是整批失败",
              r_batch.status_code == 200
              and pub2_id not in (bb.get("done") or [])
              and any(int(s["id"]) == pub2_id for s in (bb.get("skipped") or [])),
              f"HTTP {r_batch.status_code} {str(bb)[:120]}")

        r_batch_big = c_admin.post(
            "/api/v1/questions/public/batch-review/",
            {"question_ids": list(range(1, 60)), "action": "APPROVE"},
            format="json")
        check("★ 批量超过 50 道 → 400（减速带）",
              r_batch_big.status_code == 400, f"HTTP {r_batch_big.status_code}")

        r_arch_bad = c_admin.post(
            f"/api/v1/questions/public/{pub3_id}/archive/", {}, format="json")
        check("★ 未通过的题不能下架 → 409",
              r_arch_bad.status_code == 409, f"HTTP {r_arch_bad.status_code}")

        # ══════════════════════════════════════════════════
        print("\n【U15】下载到私有库")
        # ══════════════════════════════════════════════════
        r_dl_bad = c_school.post(
            f"/api/v1/questions/public/{pub2_id}/download/",
            {"course_id": 99999999}, format="json")
        check("★★ 下载必须指定**本机构**的课程（否则 400）",
              r_dl_bad.status_code == 400,
              f"HTTP {r_dl_bad.status_code} {code_of(r_dl_bad)}")

        other_course = Course.objects.for_org(partner.id).first()
        if other_course is not None:
            r_dl_other = c_partner.post(
                f"/api/v1/questions/public/{pub2_id}/download/",
                {"course_id": other_course.id}, format="json")
            check("★ 外校用自己的课程可以下载（双轨闭环）",
                  r_dl_other.status_code in (201, 400, 409),
                  f"HTTP {r_dl_other.status_code}")

        r_dl = c_school.post(
            f"/api/v1/questions/public/{pub2_id}/download/",
            {"course_id": course, "task_id": node.task_id if node else None},
            format="json")
        check("★★ 下载成功 → 201", r_dl.status_code == 201,
              f"HTTP {r_dl.status_code} {code_of(r_dl)} {str(data(r_dl))[:100]}")
        dl = data(r_dl) or {}
        copy_id = dl.get("question_id")
        copy_q = Question._base_manager.filter(pk=copy_id).first() if copy_id else None
        check("★★ 副本是 DRAFT（过了平台审 ≠ 过了本校审）",
              copy_q is not None and copy_q.status == QuestionStatus.DRAFT,
              getattr(copy_q, "status", None))
        check("★★ 副本记了 source_question_id（可追溯）",
              copy_q is not None and copy_q.source_question_id == pub2_id)
        check("★★ 副本记了 source_version（漂移判据）",
              copy_q is not None and bool(copy_q.source_version))
        check("★★ 副本落在本机构课程上（不是来源机构的课程）",
              copy_q is not None and copy_q.course_id == course
              and copy_q.org_id == ORG_ID)
        check("★★ 知识点按 code 在本机构树上重新映射",
              copy_q is not None and copy_q.knowledge_links.filter(
                  knowledge_node=node).exists(),
              f"mapped={dl.get('mapped_nodes')} unmapped={dl.get('unmapped_node_codes')}")
        check("★ 映射来的知识点标为 IMPORT（待人工确认）",
              copy_q is not None and copy_q.knowledge_links.filter(
                  source="IMPORT").exists())
        check("★★ 下载后仍**不**自动进组卷池（要先本校审）",
              not Question.objects.for_org(ORG_ID).usable_for_paper()
              .filter(pk=copy_id).exists())

        r_dl_dup = c_school.post(
            f"/api/v1/questions/public/{pub2_id}/download/",
            {"course_id": course}, format="json")
        check("★★ 重复下载 → 409（附已有副本 id）",
              r_dl_dup.status_code == 409
              and (raw(r_dl_dup).get("data") or {}).get("existing_question_id") == copy_id,
              f"HTTP {r_dl_dup.status_code} {str(raw(r_dl_dup))[:120]}")

        # 审核通过后可组卷
        QuestionService.approve_question(
            org_id=ORG_ID, question_id=copy_id,
            operator_id=school.id, reason="本校确认适用",
        )
        check("★★ 副本审核通过后才进组卷池",
              Question.objects.for_org(ORG_ID).usable_for_paper()
              .filter(pk=copy_id).exists())

        # 漂移：改公共题 → 副本的 source_drift 应为 True
        r_ss0 = c_school.get(f"/api/v1/questions/{copy_id}/source-status/")
        ss0 = data(r_ss0) or {}
        check("★ source-status 初始不漂移", ss0.get("drifted") is False, str(ss0)[:120])
        pub2.stem_text = pub2.stem_text + "（平台修订）"
        pub2.save(update_fields=["stem_text", "updated_at"])
        r_ss1 = c_school.get(f"/api/v1/questions/{copy_id}/source-status/")
        ss1 = data(r_ss1) or {}
        check("★★ 公共题被修订后 → drifted=True（角标数据源）",
              ss1.get("drifted") is True, str(ss1)[:140])
        r_list = c_school.get(
            f"/api/v1/questions/?keyword={TAG}待提交&page_size=50")
        check("★ 列表里也带漂移标记（不用逐条查来源）",
              any(x.get("source_drift") is True for x in rows_of(r_list)),
              str([(x.get("id"), x.get("source_drift")) for x in rows_of(r_list)][:5]))

        # 下架后不能再下载
        r_arch = c_admin.post(
            f"/api/v1/questions/public/{pub2_id}/archive/",
            {"reason": "内容需重写"}, format="json")
        check("★★ 下架已通过的公共题 → 200", r_arch.status_code == 200,
              f"HTTP {r_arch.status_code} {code_of(r_arch)}")
        check("★★ 下架后外校立刻不可见（ARCHIVED 不在放行集合里）",
              not Question.objects.for_org(partner.id).filter(pk=pub2_id).exists())
        check("★★ 下架**不影响**已下载副本（复制语义的代价）",
              Question.objects.for_org(ORG_ID).private_only()
              .filter(pk=copy_id).exists())

        submitter2 = User.objects.create_user(
            username=f"{TAG}sub2", password="Tmp@12345678", real_name="再下载教师",
            role=Role.TEACHER, org_id=ORG_ID,
        )
        c_sub2 = APIClient()
        c_sub2.force_authenticate(user=submitter2)
        r_dl_arch = c_sub2.post(
            f"/api/v1/questions/public/{pub2_id}/download/",
            {"course_id": course}, format="json")
        check("★★ 下架后新用户无法再下载 → 409",
              r_dl_arch.status_code == 409
              and code_of(r_dl_arch) == "QUESTION_BANK.SOURCE_UNAVAILABLE",
              f"HTTP {r_dl_arch.status_code} {code_of(r_dl_arch)}")

        transaction.set_rollback(True)

    print("\n" + "=" * 74)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
