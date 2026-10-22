"""入库（migrate_p3）。

输入：`docs/教材/知识体系包.json` + `docs/教材/题目包.json`
输出：数据库

★ 按依赖顺序入库：

    机构 → 专业 → 课程 → 章 → 任务 → 知识点 → 知识库条目 → 题目 → 选项/知识点关联

★★ 两条纪律：
  1. **幂等**：可重复执行。用「课程码 + 编码」做自然键更新，不靠主键。
     重复执行不会产生脏数据，便于出错后直接重跑。
  2. **整体事务**：任一步失败全部回滚，不留半截数据。

用法：
    # 默认走 settings 的数据库（PostgreSQL 15433）
    python scripts/migrate/migrate_p3_load.py --dry-run     # 只校验不写库
    python scripts/migrate/migrate_p3_load.py --apply

    # 显式指定 SQLite（仅用于无数据库环境的自检）
    set ZF_DB_ENGINE=sqlite
    python scripts/migrate/migrate_p3_load.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
# ★★ 默认库改为**跟随 settings**（即 PostgreSQL），不再默认 sqlite。
#
#   原来这里是 `setdefault("ZF_DB_ENGINE", "sqlite")`，后果非常隐蔽：
#   在真实 PostgreSQL 上跑本脚本时它**静默连到 SQLite**，
#   把 1944 道题写进 `db.sqlite3.dev-only`，而 PG 一直是空库——
#   而输出里的「入库统计 question 1944」看起来完全正常。
#
#   实测踩到过两次：
#     · 第一次误以为「已在 PG 上入库成功」
#     · 第二次删掉 SQLite 后报「数据库未建表」，而 PG 里明明有 57 张表
#
#   现在：**只有显式设 `ZF_DB_ENGINE=sqlite` 才用 SQLite**；
#   不设就走 settings 的默认（PostgreSQL）。
#   `deploy/use-postgres.cmd` 会显式清空该变量，正好落在 PG 分支。


from common import (  # noqa: E402
    PACK_KNOWLEDGE,
    PACK_QUESTIONS,
    banner,
    read_json,
)

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Chapter,
    Course,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
    Task,
    TaskKnowledge,
)
from apps.media.models import Asset  # noqa: E402
from apps.org.models import Major, Org  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    KnowledgeLinkSource,
    Question,
    QuestionKnowledge,
    QuestionOption,
    QuestionType,
)

ORG_NAME = "测试学校"
ORG_SHORT = "测试"
MAJOR_NAME = "机械制造技术"
MAJOR_CODE = "JX01"


class Loader:
    """幂等入库器。"""

    def __init__(self, org: Org) -> None:
        self.org = org
        self.stats: dict[str, int] = {}

    def _bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    # ── 知识体系 ──
    def load_knowledge(self, kp: dict) -> None:
        course_d = kp["course"]
        course, _ = Course.objects.update_or_create(
            org=self.org, code=course_d["code"],
            defaults={
                "major": Major.objects.filter(org=self.org, code=MAJOR_CODE).first(),
                "name": course_d["name"],
                "grade_band": course_d["grade_band"],
                "level": course_d.get("level", "SECONDARY"),
                "style_config": course_d.get("style_config", {}),
                "status": "ACTIVE",
            },
        )
        self._bump("course")

        node_by_code: dict[str, KnowledgeNode] = {}
        task_by_code: dict[str, Task] = {}

        for ch_d in kp["chapters"]:
            chapter, _ = Chapter.objects.update_or_create(
                course=course, seq=ch_d["seq"],
                defaults={
                    "org": self.org, "name": ch_d["name"], "code": ch_d["code"],
                    "subject": course.code,
                    "grade_band": course.grade_band,
                    "part_seq": ch_d.get("part_seq"),
                    "part_name": ch_d.get("part_name", ""),
                    "status": "ACTIVE",
                },
            )
            self._bump("chapter")

            for t_d in ch_d["tasks"]:
                # ★ 查找键必须含 chapter！
                #   `task.seq` 在**每章内**从 1 重新开始（唯一约束是
                #   `(course, seq)`，但实际数据里任务序号是「章内序号」）。
                #   若只用 `(course, seq)`，不同章的同序号任务会互相覆盖，
                #   实测会把 23 个任务压成 6 个。
                task, _ = Task.objects.update_or_create(
                    course=course, chapter=chapter, seq=t_d["seq"],
                    defaults={
                        "org": self.org,
                        "name": t_d["name"], "code": t_d["code"],
                        "status": "ACTIVE",
                    },
                )
                task_by_code[t_d["code"]] = task
                self._bump("task")

                for k_d in t_d["knowledge"]:
                    node, _ = KnowledgeNode.objects.update_or_create(
                        org=self.org, code=k_d["code"],
                        defaults={
                            "course": course, "task": task,
                            "subject": course.code,
                            "grade_band": course.grade_band,
                            "name": k_d["name"],
                            "seq_in_task": k_d.get("seq"),
                            "sort_order": k_d.get("seq", 0),
                            "level": 4,
                            "status": "ACTIVE",
                        },
                    )
                    node_by_code[k_d["code"]] = node
                    self._bump("knowledge_node")

                    # 任务↔知识点（第一个任务作主归属）
                    TaskKnowledge.objects.update_or_create(
                        task=task, knowledge_node=node,
                        defaults={"org": self.org,
                                  "seq_in_task": k_d.get("seq"),
                                  "is_primary": task is not None},
                    )
                    self._bump("task_knowledge")

                    # 核心内容 → 知识库条目
                    for ei, content in enumerate(k_d.get("entries") or [], 1):
                        entry, created = KnowledgeEntry.objects.update_or_create(
                            org=self.org, chapter=chapter,
                            title=f"{k_d['name']}#{ei}",
                            defaults={
                                "course": course,
                                "content": content,
                                "entry_type": "CONCEPT",
                                "source_type": "TEACHER_INPUT",
                                "source_ref": f"知识点清单/{k_d['code']}",
                                "status": "ACTIVE",
                            },
                        )
                        KnowledgeEntryNode.objects.update_or_create(
                            entry=entry, node=node,
                            defaults={"org": self.org, "weight": 1.0,
                                      "source": "TEACHER"},
                        )
                        self._bump("knowledge_entry")
                        self._bump("knowledge_entry_node")

        # ── 自愈：补齐缺失的条目 ↔ 知识点关联 ──
        #
        # ★★ 实测到的现象：全量导入后 `migrate_validate` 的 C-07 报
        #   「条目 110 / 已挂载 108」，2 条只有条目、没有关联。
        #
        #   根因是**历史残留**：早期版本的加载器还没有写关联那一步，
        #   当时的库只建了条目。之后虽然补上了关联逻辑，但
        #   `update_or_create` 命中已存在条目时会**继续走到**关联那一步，
        #   所以重跑一次就能补齐——只是没人知道要重跑。
        #
        #   （我也一度猜测是「事务中断留下半成品」。实测不支持这个解释：
        #   全量导入是单一事务，中断会整体回滚，不会留半成品。
        #   真正留下半成品的是**改代码之前**的那次导入。）
        #
        #   ★ 显式巡检的价值：它把「需要人工发现并重跑」变成
        #     「每次加载自动对齐」，并且打印补齐条数。
        self._heal_entry_links(kp)

        # 主归属修正：一个知识点只允许一个 is_primary=True
        self._fix_primary_task()

    def _heal_entry_links(self, pack: dict) -> None:
        """补齐「条目在、关联不在」的半成品。

        ★ 判据来自**数据包本身**：每个 `(章, 知识点名, 序号)` 都该有
          一条 `KnowledgeEntry` 与该知识点的 `KnowledgeEntryNode`。
          它是幂等的——已经挂好的不会被重复创建。

        ★ `pack` 从 `load_knowledge(kp)` 传进来，**不是** `self.pack`
          （加载器的 `__init__` 只有 `org` 与 `stats`）。
        """
        healed = 0
        for ch_d in pack.get("chapters", []):
            chapter = Chapter.objects.filter(
                org=self.org, code=ch_d["code"]).first()
            if chapter is None:
                continue
            for t_d in ch_d.get("tasks", []):
                for k_d in t_d.get("knowledge", []):
                    node = KnowledgeNode.objects.filter(
                        org=self.org, code=k_d["code"]).first()
                    if node is None:
                        continue
                    for ei, _content in enumerate(
                            k_d.get("entries") or [], 1):
                        title = f"{k_d['name']}#{ei}"
                        entry = KnowledgeEntry.objects.filter(
                            org=self.org, chapter=chapter,
                            title=title).first()
                        if entry is None:
                            continue
                        _link, created = (
                            KnowledgeEntryNode.objects.update_or_create(
                                entry=entry, node=node,
                                defaults={"org": self.org,
                                          "weight": 1.0,
                                          "source": "TEACHER"},
                            )
                        )
                        if created:
                            healed += 1
                            self._bump("knowledge_entry_node")
        if healed:
            print(f"  [自愈] 补齐 {healed} 条条目↔知识点关联")

    def _fix_primary_task(self) -> None:
        """把每个知识点的 TaskKnowledge 主归属收敛为第一条。"""
        for node in KnowledgeNode.objects.filter(org=self.org):
            links = list(TaskKnowledge.objects.filter(knowledge_node=node)
                         .order_by("task__seq", "task_id"))
            if not links:
                continue
            keep = links[0]
            if not keep.is_primary:
                keep.is_primary = True
                keep.save(update_fields=["is_primary"])
            for other in links[1:]:
                if other.is_primary:
                    other.is_primary = False
                    other.save(update_fields=["is_primary"])

    # ── 题目 ──
    def load_questions(self, qp: dict, course: Course,
                       task_by_code: dict[str, Task]) -> None:
        for q_d in qp["questions"]:
            task = task_by_code.get(q_d["task_code"])
            if task is None:
                raise RuntimeError(
                    f"题 src={q_d['src']} 的任务 {q_d['task_code']} 未入库"
                )

            q, _ = Question.objects.update_or_create(
                org=self.org, stem_hash=q_d["stem_hash"],
                defaults={
                    "course": course,
                    "subject": course.code,
                    "grade_band": course.grade_band,
                    "task": task,
                    "chapter_raw": q_d["chapter_raw"],
                    "type": q_d["type"],
                    "stem_html": q_d["stem_text"],
                    "stem_text": q_d["stem_text"],
                    "answer": q_d["answer"],
                    "answer_text": str(q_d.get("answer_raw", "")),
                    "source_type": q_d.get("source_type", "EXTRACTED"),
                    "status": q_d.get("status", "APPROVED"),
                    "source_ref": q_d["src"],
                    "has_figure": False,
                    "figure_count": 0,
                    "difficulty_effective": 3.00,
                    "difficulty_band": "MEDIUM",
                    # ★★ `difficulty_source` 必须**显式**写成 DEFAULT。
                    #
                    #   模型上这个字段的默认值是 `DifficultySource.MANUAL`
                    #   （「教师标定」），而导入的题**没有任何人工标定**
                    #   ——`difficulty_manual` 是空的。
                    #
                    #   不写这一行会得到一个**自相矛盾的数据**：
                    #     1944 题标着 `source=MANUAL`
                    #     而其中 `difficulty_manual` 非空的只有 4 道
                    #
                    #   后果是界面上把全库都显示成「人工标注」，
                    #   老师无法分辨「标注过的」与「还是默认值的」——
                    #   而这正是解决「题库难度无分层」时最需要看清的信息。
                    "difficulty_source": "DEFAULT",
                },
            )
            self._bump("question")

            # 选项
            if q_d["type"] in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
                correct = q_d["answer"].get("value", "")
                for oi, o in enumerate(q_d.get("options", []), 1):
                    QuestionOption.objects.update_or_create(
                        question=q, label=o["label"],
                        defaults={
                            "org": self.org,
                            "content_html": o["content_text"],
                            "content_text": o["content_text"],
                            "sort_order": oi,
                            "is_correct": o["label"] == correct,
                        },
                    )
                    self._bump("question_option")

            # 知识点关联：★ 存量题库只标到任务级，
            #   因此把该任务下的**第一个知识点**作为主知识点占位，
            #   真实的知识点级补标由 AI 预标注 + 人工抽检完成（见 15 文档第六章）
            first_node = (
                KnowledgeNode.objects.filter(org=self.org, task=task)
                .order_by("sort_order", "id").first()
            )
            if first_node:
                QuestionKnowledge.objects.update_or_create(
                    question=q, knowledge_node=first_node,
                    defaults={"org": self.org, "weight": 1.0,
                              "is_primary": True,
                              "source": KnowledgeLinkSource.IMPORT},
                )
                self._bump("question_knowledge")


def ensure_org_and_major() -> Org:
    """建机构与专业（幂等）。"""
    org, _ = Org.objects.update_or_create(
        name=ORG_NAME, defaults={"short_name": ORG_SHORT, "status": "ACTIVE"},
    )
    Major.objects.update_or_create(
        org=org, code=MAJOR_CODE,
        defaults={"name": MAJOR_NAME, "short_name": "机械", "level": "SECONDARY",
                  "status": "ACTIVE"},
    )
    return org


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        args.dry_run = True
    apply = args.apply

    banner("migrate_p3 · 入库" + ("" if apply else "（dry-run）"))

    if not PACK_KNOWLEDGE.exists() or not PACK_QUESTIONS.exists():
        print("缺少数据包，请先运行 migrate_p1 / migrate_p2")
        return 1

    kp = read_json(PACK_KNOWLEDGE)
    qp = read_json(PACK_QUESTIONS)
    print(f"知识体系包  章 {len(kp['chapters'])}  "
          f"任务 {sum(len(c['tasks']) for c in kp['chapters'])}  "
          f"知识点 {kp['meta']['counts']['knowledge_nodes']}")
    print(f"题目包      题目 {qp['meta']['counts']['total']}")
    print()

    from django.db import connection

    # ★★ 表存在性检查必须用 Django 的 introspection，不能写 raw SQL。
    #
    #   我先后写错过两次：
    #     · `sqlite_master`        → PostgreSQL 上报 relation 不存在
    #     · `information_schema`   → SQLite 上报 no such table
    #       （SQLite **没有** information_schema，那不是标准 SQL）
    #
    #   `connection.introspection.table_names()` 是 Django 为「跨后端」
    #   提供的唯一正确入口。
    if "org" not in connection.introspection.table_names():
        print("数据库未建表，请先：cd server && python manage.py migrate")
        return 1

    org = ensure_org_and_major()
    print(f"机构 {org.name}（id={org.id}）就绪，专业 {MAJOR_CODE} 就绪")

    loader = Loader(org)

    if apply:
        with transaction.atomic():
            loader.load_knowledge(kp)
            course = Course.objects.get(org=org, code=kp["course"]["code"])
            task_by_code = {
                t.code: t
                for t in Task.objects.filter(org=org).select_related("chapter")
            }
            loader.load_questions(qp, course, task_by_code)
        print("已提交（单一事务）")
    else:
        # dry-run：只做映射校验，不写库
        task_codes = {t["code"] for c in kp["chapters"] for t in c["tasks"]}
        missing = {q["task_code"] for q in qp["questions"]} - task_codes
        print("dry-run 映射校验：")
        print(f"  题目引用的任务编码 {len({q['task_code'] for q in qp['questions']})} 个")
        if missing:
            print(f"  ✗ 未在知识体系中的任务：{sorted(missing)[:5]}")
            return 2
        print("  ✓ 全部题目都能映射到任务")
        print()
        print("（--dry-run 未写库；加 --apply 才提交）")

    print()
    print("入库统计：")
    for k in ("course", "chapter", "task", "knowledge_node",
              "task_knowledge", "knowledge_entry",
              "question", "question_option", "question_knowledge"):
        print(f"  {k:<20} {loader.stats.get(k, 0):>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
