"""灌入 11 个场景的提示词基线。

用法::

    python manage.py seed_prompt_templates                # 平台默认（org 为空）
    python manage.py seed_prompt_templates --org 1        # 给某个机构灌一份
    python manage.py seed_prompt_templates --dry-run      # 只看会做什么
    python manage.py seed_prompt_templates --bump         # 内容变了，升版本

★★ 幂等的语义（这三点必须分清，否则会把"重跑"当成"改动"）：

    1. **已存在且内容一致** → 跳过。重跑一百次也只有 1 条。
    2. **已存在但内容不同** → 默认**报冲突不动手**。
       原因：`AICallLog` 记了 `prompt_version`，原地改模板会让历史日志
       无法复现当时的效果。要改就显式加 `--bump` 升一个版本。
    3. **不存在** → 建 version=1，状态直接 `ACTIVE`。

★ 同一 (org, key) 下只能有一个 `ACTIVE`（DB 唯一约束）。所以升版本时
  必须**先归档旧的，再建新的**，顺序颠倒会撞约束。
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.ai.prompt_baseline import BASELINES
from apps.ai.prompts import PROMPT_SCENES, invalidate, scene_key

#: 参与「内容是否一致」比较的字段。
#: ★ 不含 status / metrics / version —— 它们是状态，不是内容。
_CONTENT_FIELDS = (
    "system_prompt", "user_prompt", "output_schema",
    "glossary", "few_shot", "kb_injection", "subject", "grade_band",
)


class Command(BaseCommand):
    help = "灌入 11 个提示词场景的基线模板（幂等）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--org", type=int, default=None,
            help="机构 id。不给则灌平台默认模板（org 为空）",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只打印计划，不写库",
        )
        parser.add_argument(
            "--bump", action="store_true",
            help="内容与库中不一致时，归档旧版本并新建 version+1",
        )

    def handle(self, *args, **opts):
        org_id: int | None = opts["org"]
        dry_run: bool = opts["dry_run"]
        bump: bool = opts["bump"]

        self._assert_coverage()

        if org_id is not None:
            self._assert_org_exists(org_id)

        created = skipped = bumped = conflicts = 0
        conflict_keys: list[str] = []

        for item in BASELINES:
            key = item["key"]
            with transaction.atomic():
                action, detail = self._apply(
                    item, org_id=org_id, dry_run=dry_run, bump=bump
                )
            if action == "create":
                created += 1
                self.stdout.write(f"  + {key} v{detail} 新建")
            elif action == "skip":
                skipped += 1
                self.stdout.write(f"  = {key} v{detail} 已存在且一致")
            elif action == "bump":
                bumped += 1
                self.stdout.write(f"  ^ {key} → v{detail} 升版本")
            else:
                conflicts += 1
                conflict_keys.append(key)
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! {key} 内容不一致（库中 v{detail}）"
                        f"——加 --bump 才会升版本"
                    )
                )

        if not dry_run:
            invalidate(org_id=org_id)

        self.stdout.write("")
        scope = f"机构 {org_id}" if org_id else "平台默认"
        self.stdout.write(
            f"{scope}：新建 {created} / 跳过 {skipped} / "
            f"升版本 {bumped} / 冲突 {conflicts}"
            + ("（dry-run，未写库）" if dry_run else "")
        )

        if conflicts:
            raise CommandError(
                f"{len(conflict_keys)} 条模板内容与库中不一致且未加 --bump："
                f"{', '.join(conflict_keys)}"
            )

    # ── 内部 ──────────────────────────────────────────────

    def _assert_coverage(self) -> None:
        """11 个场景一个都不能少。

        ★ 缺一条的后果不是报错，而是那条链路上悄悄用回
          「通用最小提示词」——输出质量下降，但没有任何异常可查。
        """
        want = {scene_key(s) for s in PROMPT_SCENES}
        have = {b["key"] for b in BASELINES}
        missing = want - have
        extra = have - want
        if missing:
            raise CommandError(f"基线缺少场景：{sorted(missing)}")
        if extra:
            raise CommandError(f"基线含未知场景：{sorted(extra)}")

    def _assert_org_exists(self, org_id: int) -> None:
        from apps.org.models import Org

        if not Org.objects.filter(pk=org_id).exists():
            raise CommandError(f"机构 {org_id} 不存在")

    def _apply(
        self, item: dict[str, Any], *, org_id: int | None,
        dry_run: bool, bump: bool,
    ) -> tuple[str, int]:
        """处理一条基线。返回 (动作, 版本号)。"""
        from apps.ai.models import AIPromptTemplate, PromptTemplateStatus

        latest = (
            AIPromptTemplate.objects
            .filter(org_id=org_id, key=item["key"])
            .order_by("-version")
            .first()
        )

        if latest is None:
            if not dry_run:
                AIPromptTemplate.objects.create(
                    org_id=org_id,
                    key=item["key"],
                    task=item["task"],
                    subject=item.get("subject", ""),
                    grade_band=item.get("grade_band", "SECONDARY_VOCATIONAL"),
                    system_prompt=item["system_prompt"],
                    user_prompt=item["user_prompt"],
                    output_schema=item.get("output_schema") or {},
                    glossary=item.get("glossary") or {},
                    few_shot=item.get("few_shot") or [],
                    kb_injection=item.get("kb_injection") or {},
                    version=1,
                    status=PromptTemplateStatus.ACTIVE,
                )
            return "create", 1

        if self._same_content(latest, item):
            return "skip", latest.version

        if not bump:
            return "conflict", latest.version

        new_version = int(latest.version) + 1
        if not dry_run:
            # ★ 顺序不能反：同 (org,key) 只允许一个 ACTIVE
            AIPromptTemplate.objects.filter(pk=latest.pk).update(
                status=PromptTemplateStatus.ARCHIVED
            )
            AIPromptTemplate.objects.create(
                org_id=org_id,
                key=item["key"],
                task=item["task"],
                subject=item.get("subject", ""),
                grade_band=item.get("grade_band", "SECONDARY_VOCATIONAL"),
                system_prompt=item["system_prompt"],
                user_prompt=item["user_prompt"],
                output_schema=item.get("output_schema") or {},
                glossary=item.get("glossary") or {},
                few_shot=item.get("few_shot") or [],
                kb_injection=item.get("kb_injection") or {},
                version=new_version,
                status=PromptTemplateStatus.ACTIVE,
            )
        return "bump", new_version

    @staticmethod
    def _same_content(row, item: dict[str, Any]) -> bool:
        return all(getattr(row, f, None) == item.get(f) for f in _CONTENT_FIELDS)
