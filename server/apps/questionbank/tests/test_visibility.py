"""题库双轨可见性（`for_org`）· 单测。

★ 为什么这一组最值得先写：`QuestionQuerySet.for_org()` 是全项目**风险最高**
  的一处判定（见其文档）。业务代码里有 39 处 `Question.objects.for_org(...)`，
  写错一处的结果是**跨机构泄露**或**公共题集体不可见**，而且两种都不报错。
  端到端脚本 `verify_question_bank.py` 能守住调用方式，守不住判据本身。

★★ 本文件钉死三件事：

  1. 本机构可见范围 = 本机构全部题（含草稿/待审）∪ 公共库**已审核**题；
  2. 公共库的 DRAFT / PENDING_REVIEW / REJECTED **不得**被别的机构看见；
  3. `Question.objects.for_org(x)`（走 Manager）与
     `Question.objects.all().for_org(x)`（走 QuerySet）**结果必须一致**。

  第 3 条是 2026-10-22 踩过的坑：`OrgScopedManager` 自带一个
  `for_org`（只做 `filter(org_id=...)`），它会**静默屏蔽** QuerySet 上的
  同名覆写 —— 没有报错、没有警告，唯一可观察到的差异就是「公共题看不见」，
  而它看起来像「公共库里没题」。这条断言就是那个坑的哨兵。
"""

from __future__ import annotations

import pytest

from apps.common.constants import QuestionStatus, QuestionType
from apps.org.models import Org, OrgType
from apps.questionbank.models import (
    Question,
    QuestionBank,
    QuestionBankKind,
    QuestionBankStatus,
)

pytestmark = pytest.mark.django_db


def _org(name: str, *, kind: str = OrgType.SCHOOL) -> Org:
    return Org.objects.create(name=name, type=kind)


def _bank(org: Org, name: str, *, kind: str = QuestionBankKind.PRIVATE) -> QuestionBank:
    return QuestionBank.objects.create(
        org=org,
        name=name,
        kind=kind,
        status=QuestionBankStatus.ACTIVE,
    )


def _question(org: Org, bank: QuestionBank, stem: str, *, status: str) -> Question:
    return Question.objects.create(
        org=org,
        bank=bank,
        subject="JXJC",
        type=QuestionType.SINGLE_CHOICE,
        stem_html=f"<p>{stem}</p>",
        stem_text=stem,
        stem_hash=f"hash-{stem}",
        status=status,
    )


@pytest.fixture()
def world():
    """三所学校 + 一个平台公共库，题目状态覆盖全部枚举。"""
    org_a = _org("甲校")
    org_b = _org("乙校")
    platform = _org("知否平台", kind=OrgType.PLATFORM)

    bank_a = _bank(org_a, "甲校私有库")
    bank_b = _bank(org_b, "乙校私有库")
    bank_pub = _bank(platform, "平台公共库", kind=QuestionBankKind.PUBLIC)

    q_a_draft = _question(org_a, bank_a, "甲校草稿", status=QuestionStatus.DRAFT)
    q_a_ok = _question(org_a, bank_a, "甲校已审", status=QuestionStatus.APPROVED)
    q_b_ok = _question(org_b, bank_b, "乙校已审", status=QuestionStatus.APPROVED)

    q_pub_ok = _question(platform, bank_pub, "公共已审", status=QuestionStatus.APPROVED)
    q_pub_draft = _question(platform, bank_pub, "公共草稿", status=QuestionStatus.DRAFT)
    q_pub_pending = _question(
        platform, bank_pub, "公共待审", status=QuestionStatus.PENDING_REVIEW
    )
    q_pub_rejected = _question(
        platform, bank_pub, "公共已拒", status=QuestionStatus.REJECTED
    )

    return {
        "org_a": org_a,
        "org_b": org_b,
        "platform": platform,
        "q_a_draft": q_a_draft,
        "q_a_ok": q_a_ok,
        "q_b_ok": q_b_ok,
        "q_pub_ok": q_pub_ok,
        "q_pub_draft": q_pub_draft,
        "q_pub_pending": q_pub_pending,
        "q_pub_rejected": q_pub_rejected,
    }


def test_own_org_questions_all_visible_including_drafts(world):
    """本机构的题**全部**可见 —— 题库管理页要能看到自己的草稿与待审。"""
    ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    assert world["q_a_draft"].id in ids
    assert world["q_a_ok"].id in ids


def test_other_org_questions_invisible(world):
    """跨机构私有题**绝对**不可见。"""
    ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    assert world["q_b_ok"].id not in ids


def test_public_approved_visible(world):
    """公共库已审核题对本机构可见（这是「老师能看到公共题」的唯一来源）。"""
    ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    assert world["q_pub_ok"].id in ids


@pytest.mark.parametrize(
    "key",
    ["q_pub_draft", "q_pub_pending", "q_pub_rejected"],
)
def test_public_unapproved_invisible(world, key):
    """★ 公共库未审核的题属于「提交者与审核人之间」，别的机构看到就是泄漏。

    用 `status` 判而不是判「审核人非空」：`approve()` 是唯一能置 APPROVED
    的入口且同时写 `reviewed_by`，两者等价，但 `status` 有索引。
    """
    ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    assert world[key].id not in ids


def test_manager_and_queryset_agree(world):
    """★★ 哨兵：Manager 上的 `for_org` 与 QuerySet 上的 `for_org` 结果必须一致。

    `OrgScopedManager` 自带同名方法会静默屏蔽 QuerySet 覆写。一旦有人
    调错（或基类被改动），本断言立刻红；否则只能靠「老师抱怨公共题看不见」
    这种事后现象发现。
    """
    via_manager = set(
        Question.objects.for_org(world["org_a"].id).values_list("id", flat=True)
    )
    via_queryset = set(
        Question.objects.all().for_org(world["org_a"].id).values_list("id", flat=True)
    )
    assert via_manager == via_queryset


def test_two_orgs_do_not_leak_into_each_other(world):
    """双向隔离：甲看不见乙的、乙看不见甲的，且各自都看得见公共已审题。"""
    a_ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    b_ids = set(Question.objects.for_org(world["org_b"].id).values_list("id", flat=True))

    assert world["q_b_ok"].id not in a_ids
    assert world["q_a_ok"].id not in b_ids
    assert world["q_pub_ok"].id in a_ids
    assert world["q_pub_ok"].id in b_ids


def test_soft_deleted_question_not_visible(world):
    """软删的题不出现在任何取数里（`for_org` 之上还叠着软删过滤）。"""
    victim = world["q_a_ok"]
    victim.delete()

    ids = set(Question.objects.for_org(world["org_a"].id).values_list("id", flat=True))
    assert victim.id not in ids
