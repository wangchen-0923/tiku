"""验证「三个不可绕过的保证」中的两个（取题闸门与配图门禁）。

这是**集成测试**，需要数据库。默认用 SQLite 自检库跑：
    set ZF_DB_ENGINE=sqlite
    python scripts/verify_gates.py

★ 为什么单独写这个脚本：这两条约束是设计文档里的「不可绕过的保证」，
  只写代码不验证等于没做。它们一旦失效，后果是**AI 生成的草稿题被组进正式试卷**
  或**未确认的配图随卷印刷**。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(r"D:\tiku\server")
sys.path.insert(0, str(BASE))
sys.path.insert(0, r"D:\tiku\.devdeps")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

import django  # noqa: E402

django.setup()

from apps.accounts.models import Role, User  # noqa: E402
from apps.common.constants import (  # noqa: E402
    FigureReviewStatus,
    QuestionStatus,
    QuestionType,
)
from apps.knowledge.models import Course  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.org.models import Major, Org  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    FigureAttachPoint,
    FigureSrcPath,
    Question,
    QuestionImage,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "[OK]  " if ok else "[FAIL]"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    from django.db import connection

    # 建表
    with connection.cursor() as cur:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='org'")
        if not cur.fetchone():
            print("自检库未建表，请先执行：")
            print("  cd server && python manage.py migrate")
            return 1

    print("=" * 70)
    print("闸门验证（取题门禁 + 配图门禁）")
    print("=" * 70)

    # ── 造数据（★ 先彻底清理上次残留，否则 q_with_fig 会带着已确认的配图）──
    # ★ 注意：模型的 `objects.delete()` 是**软删除**，行还在库里，
    #   会让后续的 org.hard_delete() 撞上 ProtectedError。
    #   清理测试数据必须用 hard_delete()，且按依赖顺序从叶子往根删。
    def purge_org(old) -> None:
        # 1) 叶子：配图（引用 question 与 asset）
        for im in QuestionImage._base_manager.filter(org=old):
            im.hard_delete()
        # 2) 题目（引用 course；被 paper_item 保护，测试库里没有）
        for q in Question._base_manager.filter(org=old):
            q.hard_delete()
        # 3) 资源（被 question_image 保护，但上面已删）
        for a in Asset._base_manager.filter(org=old):
            a.hard_delete()
        # 4) 课程（引用 major）
        for c in Course._base_manager.filter(org=old):
            c.hard_delete()
        # 5) 专业（引用 org）
        for m in Major._base_manager.filter(org=old):
            m.hard_delete()
        # 6) 用户（引用 org，PROTECT）
        #
        # ★★ 必须用 `_base_manager`，不能用 `objects`：
        #   `UserManager.for_org()` 带 `deleted_at__isnull=True` 过滤，
        #   而**软删的用户 org_id 仍在**——用 `objects.filter(org=old)`
        #   查不到它们，`old.hard_delete()` 就会
        #   `ProtectedError: 'User.org'`。
        #
        #   实测踩到过：`gate_teacher` 被软删后仍占着 `org` 外键，
        #   导致本脚本每次重跑都在第 98 行崩掉。
        #
        # ★ `User` 继承 `AbstractBaseUser`，没有自定义 `hard_delete`，
        #   用 `_base_manager.delete()` 物理删。
        for u in User._base_manager.filter(org=old):
            User._base_manager.filter(pk=u.pk).delete()
        # 7) 最后删机构
        old.hard_delete()

    for name in ("闸门测试校", "另一所学校"):
        for old in Org._base_manager.filter(name=name):
            purge_org(old)

    # ★★ 测试体必须包在 try/finally 里。
    #
    #   原来这里没有 try/finally，`purge_org` 直接放在末尾 ——
    #   于是**任何断言失败或中途异常都会跳过清理**，
    #   测试数据（机构/课程/用户/8 道题）就留在库里。
    #
    #   实测踩到过：一次失败的运行留下 8 道 `ORIGINAL_HUMAN` 题
    #   和 2 个机构，之后每次跑基线都对不上（1952 vs 1944），
    #   而原因要翻好几层才看得出来。
    #
    #   ★ 这是本文件自己的「验证脚本必须 try/finally 清理」约定
    #     （见开发进度记录 7.5），它自己却没遵守。
    _org_for_cleanup = None
    try:
        org = Org.objects.create(name="闸门测试校", short_name="测试")
        major = Major.objects.create(org=org, name="机械制造技术", code="JX01")
        course = Course.objects.create(
            org=org, major=major, name="机械基础", code="JXJC",
            style_config={"context_required": True, "question_style": "TASK_DRIVEN"},
        )
        user = User.objects.create_user("gate_teacher", "pw12345678", org=org,
                                        real_name="测试老师", role=Role.TEACHER)
        asset = Asset.objects.create(
            org=org, kind="QUESTION_FIGURE", oss_key="zf/gate/fig1.png",
            mime="image/png", byte_size=1024,
        )

        def mk(stem: str, status: str) -> Question:
            return Question.objects.create(
                org=org, course=course, subject=course.code,
                type=QuestionType.SINGLE_CHOICE,
                stem_html=f"<p>{stem}</p>", stem_text=stem,
                stem_hash=Question.hash_stem(stem) if hasattr(Question, "hash_stem")
                else stem,  # 占位，真实实现见 services
                status=status,
                difficulty_effective=3.0, difficulty_band="MEDIUM",
            )

        q_draft = mk("草稿题：AI 生成的题干", QuestionStatus.DRAFT)
        q_pending = mk("待审题：人工录入待审核", QuestionStatus.PENDING_REVIEW)
        q_approved = mk("已审题：这道题可以组卷", QuestionStatus.APPROVED)
        q_rejected = mk("拒绝题：质量不合格", QuestionStatus.REJECTED)
        q_with_fig = mk("含图题：下图是某机构示意图", QuestionStatus.APPROVED)

        # 给含图题挂一张**未确认**配图
        QuestionImage.objects.create(
            org=org, question=q_with_fig, attach_point=FigureAttachPoint.STEM,
            asset=asset, src_path=FigureSrcPath.VISION_EXTRACT,
            review_status=FigureReviewStatus.PENDING,
        )
        q_with_fig.has_figure = True
        q_with_fig.figure_count = 1
        q_with_fig.save(update_fields=["has_figure", "figure_count"])

        # ── 测试 1：usable_for_paper 只放行 APPROVED 且无未确认配图 ──
        print("\n【测试 1】取题闸门 usable_for_paper()")
        usable_ids = set(
            Question.objects.for_org(org.id).usable_for_paper().values_list("id", flat=True)
        )
        check("DRAFT 题被拦住", q_draft.id not in usable_ids)
        check("PENDING_REVIEW 题被拦住", q_pending.id not in usable_ids)
        check("REJECTED 题被拦住", q_rejected.id not in usable_ids)
        check("APPROVED 且无未确认配图的题被放行", q_approved.id in usable_ids)
        check(
            "APPROVED 但有未确认配图的题被拦住",
            q_with_fig.id not in usable_ids,
            "这是「三个不可绕过的保证」之一",
        )

        # ── 测试 2：配图确认后放行 ──
        print("\n【测试 2】配图确认后应放行")
        img = q_with_fig.images.first()
        img.confirm(method="CLICK", user=user)
        usable_ids2 = set(
            Question.objects.for_org(org.id).usable_for_paper().values_list("id", flat=True)
        )
        check("配图 CONFIRMED 后该题进入可组卷集合", q_with_fig.id in usable_ids2)

        # ── 测试 3：单题属性判据与 QuerySet 判据一致 ──
        print("\n【测试 3】单题判据与 QuerySet 判据一致性")
        q_approved.refresh_from_db()
        q_with_fig.refresh_from_db()
        check("q_approved.is_usable_for_paper 与 QuerySet 一致",
              q_approved.is_usable_for_paper is True)
        check("q_with_fig.is_usable_for_paper 与 QuerySet 一致",
              q_with_fig.is_usable_for_paper is True)

        # ── 测试 4：未确认配图阻断审核通过 ──
        print("\n【测试 4】有未确认配图时不允许审核通过")
        q2 = mk("另一道含图题：如图所示的机构", QuestionStatus.PENDING_REVIEW)
        QuestionImage.objects.create(
            org=org, question=q2, attach_point=FigureAttachPoint.STEM,
            asset=asset, src_path=FigureSrcPath.MANUAL,
            review_status=FigureReviewStatus.PENDING,
        )
        q2.refresh_from_db()
        blocked = False
        try:
            q2.approve(user=user)
        except Exception as exc:  # 期望 FIGURE.PENDING_CONFIRM
            blocked = "FIGURE" in str(getattr(exc, "zf_code", "")) or "配图" in str(exc)
        check("approve() 被配图门禁拦住", blocked,
              "否则配图门禁可被绕过")

        # ── 测试 5：题干提到图但无配图 ──
        print("\n【测试 5】题干提到图但实际无配图应被识别")
        q3 = mk("无图题：下图所示的零件名称是", QuestionStatus.APPROVED)
        q3.refresh_from_db()
        check("referenced_figure_missing 正确识别", q3.referenced_figure_missing is True,
              "该状态会阻断试卷定稿 FIGURE.MISSING_REFERENCED")
        q4 = mk("正常题：销是标准件，主要用于定位", QuestionStatus.APPROVED)
        q4.refresh_from_db()
        check("无图且未提到图的题不误报", q4.referenced_figure_missing is False)

        # ── 测试 6：机构隔离 ──
        print("\n【测试 6】机构隔离")
        org2 = Org.objects.create(name="另一所学校", short_name="他校")
        other = Question.objects.for_org(org2.id).count()
        check("他校查不到本校题目", other == 0)
        check("本校能查到自己的题目",
              Question.objects.for_org(org.id).count() >= 6)
    finally:
        # ★ 无论通过、失败还是抛异常，都要清干净。
        #   按机构名清理（也覆盖上次运行可能留下的残留）。
        for _name in ("闸门测试校", "另一所学校"):
            for _old in Org._base_manager.filter(name=_name):
                purge_org(_old)

    # ── 汇总 ──
    print()
    print("=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 70)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
