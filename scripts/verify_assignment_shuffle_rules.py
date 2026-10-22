"""线下扫描乱序规则 / 线上逐生乱序回归（规格四）。

全程事务回滚。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_PORT", "15433")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.assignment.models import AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ValidationFailed  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

ORG_ID = 1
PAPER_ID = 569
CLASSROOM_ID = 257


def _pick_template(paper_id: int) -> int:
    """动态选一张与试卷匹配的 **ACTIVE** 答题卡。

    ★★ 不要硬编码模板 id（原来是 `TEMPLATE_ID = 213`）。
      答题卡每次调整几何（行距、字号、涂点尺寸）都会重发升版，
      旧版随即置 DEPRECATED，而绑定要求 ACTIVE ——
      硬编码会让整个脚本在"模板换版"后失效，报「答题卡尚未发布」。

      本次实测就是这样：213 → 225 后脚本直接崩，
      而错误信息指向"尚未发布"，看起来像是发布逻辑坏了。

    ★ 判据用 `BindingService.validate` 而不是"题数相等"：
      那也是绑定时的真实判据，脚本不该自创一套。
    """
    from apps.sheet.models import SheetTemplate
    from apps.sheet.services import BindingService

    for t in (
        SheetTemplate.objects.for_org(ORG_ID)
        .filter(status="ACTIVE").order_by("-version", "-id")
    ):
        try:
            if BindingService.validate(
                org_id=ORG_ID, paper_id=paper_id, template_id=t.id
            ).get("ok"):
                return t.id
        except Exception:      # noqa: BLE001 —— 结构不匹配就换下一张
            continue
    raise RuntimeError(
        f"找不到与试卷 {paper_id} 匹配的 ACTIVE 答题卡，"
        f"请先为该卷重发一张答题卡"
    )


TEMPLATE_ID = _pick_template(PAPER_ID)
MSG = "线下扫描作业不支持逐生乱序"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _new(title: str, **kw):
    now = timezone.now()
    return AssignmentService.create_assignment(
        org_id=ORG_ID, paper_id=PAPER_ID, classroom_id=CLASSROOM_ID,
        title=title, start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), **kw,
    )


def main() -> int:
    with transaction.atomic():
        print("\n【R1】线下扫描 + 乱序：创建/绑定即阻断")
        msg = ""
        try:
            _new("TEST 线下乱序", sheet_template_id=TEMPLATE_ID,
                 shuffle_questions=True, shuffle_options=True)
        except ValidationFailed as exc:
            msg = str(exc)
        check("★ 绑卡 + 乱序直接阻断", MSG in msg, msg)

        a1 = _new("TEST 线下先建后绑", sheet_template_id=TEMPLATE_ID)
        msg2 = ""
        try:
            AssignmentService.bind_sheet(
                org_id=ORG_ID, assignment_id=a1.id,
                template_id=TEMPLATE_ID)
        except ValidationFailed as exc:
            msg2 = str(exc)
        check("★ 未开乱序时可绑定", msg2 == "", msg2)

        msg3 = ""
        try:
            AssignmentService.update_settings(
                org_id=ORG_ID, assignment_id=a1.id,
                changes={"shuffle_questions": True})
        except ValidationFailed as exc:
            msg3 = str(exc)
        check("★ 已绑卡后改乱序被阻断", bool(msg3), msg3)

        print("\n【R2】线下扫描 + 关闭乱序：可发布")
        a2 = _new("TEST 线下正常", sheet_template_id=TEMPLATE_ID)
        res2 = AssignmentService.publish(
            org_id=ORG_ID, assignment_id=a2.id)
        check("线下无乱序发布成功", res2["status"] == "PUBLISHED",
              res2["status"])
        check("返回 online_only=False", res2.get("online_only") is False,
              str(res2.get("online_only")))

        print("\n【R3】线上作业 + 乱序：发布不受影响，逐生冻结")
        a3 = _new("TEST 线上乱序", shuffle_questions=True,
                  shuffle_options=True)
        res3 = AssignmentService.publish(
            org_id=ORG_ID, assignment_id=a3.id)
        check("★ 未绑卡 + 乱序 → 纯线上发布成功",
              res3["status"] == "PUBLISHED", res3["status"])
        check("online_only=True", res3.get("online_only") is True,
              str(res3.get("online_only")))
        rows = list(AssignmentStudent.objects.filter(
            assignment=a3).exclude(student_id__isnull=True)[:5])
        check("已为每个学生冻结 question_order",
              bool(rows) and all(r.question_order for r in rows),
              str([bool(r.question_order) for r in rows]))
        # 逐生顺序不同：取前 3 人比较
        orders = [tuple(r.question_order[:8]) for r in rows[:3]]
        check("★ 不同学生的题目顺序不同（逐生乱序）",
              len(set(orders)) == len(orders), str(orders))
        opt_orders = [tuple(sorted(r.option_order.items()))[:2]
                      for r in rows[:3]]
        check("★ 选项顺序也按学生冻结",
              all(r.option_order for r in rows), str(opt_orders))

        print("\n【R4】未绑卡 + 未开乱序：发布阻断并给绑定入口")
        a4 = _new("TEST 未绑无乱序")
        msg4 = ""
        try:
            AssignmentService.publish(org_id=ORG_ID, assignment_id=a4.id)
        except Exception as exc:      # noqa: BLE001
            msg4 = str(exc)
        check("★ 提示先绑定答题卡", "请先在作业详情绑定答题卡" in msg4, msg4)

        transaction.set_rollback(True)

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
