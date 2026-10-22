"""答题卡列表分页/纸张筛选回归（`page_size` ≠ `paper_size`）。

运行：
    python scripts/verify_sheet_list_filter.py

★ 用例（用户指定）：
    GET ?page_size=200              → 3 条（分页参数不得进筛选）
    GET ?page_size=2                → 2 条、total=3（分页生效）
    GET ?paper_size=A5              → 1 条（A5）
    GET ?paper_size=A5&page_size=200→ 1 条

★ 整个用例包在事务里并回滚：不修改、不删除任何既有业务数据。
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, r"D:\tiku\server")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.sheet.models import SheetTemplate  # noqa: E402
from apps.sheet.services import SheetStyleService, TemplateService  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _make(org_id: int, name: str, paper: str) -> SheetTemplate:
    style = SheetStyleService.create_style(
        org_id=org_id,
        data={"name": f"{name}-style", "page_size": paper,
              "title_text": name},
    )
    return TemplateService.create_draft(
        org_id=org_id,
        name=name,
        sections=[{
            "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
            "count": 5, "per_row": 5, "options": 4, "score": 2,
        }],
        style_id=style.id,
    )


def main() -> int:
    org = Org.objects.first()
    user = User._base_manager.filter(
        org=org,
        role__in=["TEACHER", "ORG_ADMIN", "PLATFORM_ADMIN"],
    ).first() or User._base_manager.filter(org=org).first()
    if org is None or user is None:
        print("缺少 org / 用户，无法验证")
        return 2

    tag = uuid.uuid4().hex[:6]
    try:
        with transaction.atomic():
            # 事务内隐藏既有卡；回滚后原样恢复。绝不删数据。
            SheetTemplate._base_manager.filter(
                org_id=org.id, deleted_at__isnull=True,
            ).update(deleted_at=timezone.now())

            _make(org.id, f"TEST-LIST-A4-1-{tag}", "A4")
            _make(org.id, f"TEST-LIST-A4-2-{tag}", "A4")
            _make(org.id, f"TEST-LIST-A5-{tag}", "A5")

            c = APIClient()
            c.force_authenticate(user=user)

            def get(qs: str) -> tuple[int, int]:
                r = c.get(f"/api/v1/sheet/templates/{qs}")
                if r.status_code != 200:
                    return r.status_code, -1
                data = r.json().get("data") or {}
                return len(data.get("items") or []), int(
                    data.get("total") or 0)

            n, total = get("?page_size=200")
            check("?page_size=200 → 3 条（分页参数不影响筛选）",
                  n == 3 and total == 3, f"items={n} total={total}")

            n, total = get("?page_size=2")
            check("?page_size=2 → 2 条、total=3（分页生效）",
                  n == 2 and total == 3, f"items={n} total={total}")

            n, total = get("?paper_size=A5")
            check("?paper_size=A5 → 只返回 A5 那张",
                  n == 1 and total == 1, f"items={n} total={total}")

            n, total = get("?paper_size=A5&page_size=200")
            check("?paper_size=A5&page_size=200 → 1 条",
                  n == 1 and total == 1, f"items={n} total={total}")

            r = c.get(
                "/api/v1/sheet/templates/?paper_size=A5&page_size=200")
            items = (r.json().get("data") or {}).get("items") or []
            check("筛选结果确实是 A5 卡",
                  bool(items) and items[0].get("page_size") == "A5",
                  str([i.get("page_size") for i in items]))

            transaction.set_rollback(True)
    except Exception as exc:      # noqa: BLE001
        print(f"\n运行异常：{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 2

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过（用例数据已随事务回滚）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
