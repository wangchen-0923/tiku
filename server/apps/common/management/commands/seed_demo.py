"""演示数据：一份能走完「命题 → 组卷 → 发布 → 答题 → 批改 → 学情 →
下一次组卷」闭环的最小数据集。

    python manage.py seed_demo --with-paper --with-assignment --with-scan
    python manage.py seed_demo --reset

产出（全部以 `DEMO` / `demo_` 为标识，可一键清理）：

    · 1 个班级 + 10 名学生（学号 20260001–20260010）
    · 1 份 10 题试卷：6 单选 + 2 多选 + 2 主观（简答/论述），总分 100
    · 4 个知识点，10 道题全部已标主知识点（CONFIRMED）
    · 1 个已发布任务（默认纯线上 + 逐生乱序）
    · `--with-scan` 时再加 1 张答题卡（并尽量绑定到任务）

★ 为什么默认「纯线上」而不是「绑卡」：
  绑定答题卡要先 `publish()` 渲染 PDF/预览图（reportlab → Pillow）。
  在装不上二进制轮子的环境里这一步必失败，会把整条演示数据准备
  一起带下去。所以 `--with-scan` 是**可选加强**，失败则**明确告知并降级**
  为纯线上任务，其余数据照常就绪 —— 让「缺 Pillow」只损失演示的一小块，
  而不是损失整份演示数据。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.accounts.models import User
from apps.assignment.models import Assignment, AssignmentStudent
from apps.assignment.services import AssignmentService
from apps.common.exceptions import ZfError
from apps.grading.models import ScoreResult
from apps.knowledge.models import Course, KnowledgeNode
from apps.org.models import Classroom, ClassroomStudent, Org
from apps.org.services import ClassroomService, RosterService
from apps.paper.models import Paper, PaperItem
from apps.paper.services import PaperService
from apps.questionbank.models import Question, QuestionKnowledge, QuestionOption
from apps.questionbank.services import QuestionService
from apps.response.models import Response, StudentTotalScore

PREFIX = "DEMO"
USER_PREFIX = "demo_stu_"
STUDENT_NOS = [f"2026{i:04d}" for i in range(1, 11)]

#: 10 道题：6 单选(6 分) + 2 多选(7 分) + 2 主观(25 分) = 100
SPEC = [
    ("SINGLE_CHOICE", 6, "平面机构自由度计算公式中，n 表示的是？"),
    ("SINGLE_CHOICE", 6, "四杆机构中，最短杆与最长杆长度之和小于等于其余两杆"
                        "长度之和时，该机构可能是？"),
    ("SINGLE_CHOICE", 6, "普通平键连接传递扭矩时，主要失效形式是？"),
    ("SINGLE_CHOICE", 6, "一对渐开线标准直齿圆柱齿轮正确啮合的条件是？"),
    ("SINGLE_CHOICE", 6, "轴的材料常选用 45 钢并进行调质处理，主要目的是？"),
    ("SINGLE_CHOICE", 6, "滚动轴承代号 6208 中，内径为多少毫米？"),
    ("MULTIPLE_CHOICE", 7, "下列属于间歇运动机构的有？"),
    ("MULTIPLE_CHOICE", 7, "带传动的主要优点包括？"),
    ("SHORT_ANSWER", 25, "简述铰链四杆机构曲柄存在的条件，并说明如何判断"
                         "机构类型。"),
    ("ESSAY", 25, "结合实例说明为什么机械设计中要进行强度校核，并写出"
                  "提高构件疲劳强度的三条措施。"),
]

OPTIONS_4 = [
    {"label": "A", "content_text": "构件数"},
    {"label": "B", "content_text": "低副数"},
    {"label": "C", "content_text": "高副数"},
    {"label": "D", "content_text": "约束数"},
]


class Command(BaseCommand):
    help = "生成/清理演示数据（班级、试卷、任务、答题卡）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reset", action="store_true",
                            help="只清理演示数据，不新建")
        parser.add_argument("--with-paper", action="store_true",
                            help="建知识点、10 道题、试卷并定稿")
        parser.add_argument("--with-assignment", action="store_true",
                            help="建班级、10 名学生并发布任务")
        parser.add_argument("--with-scan", action="store_true",
                            help="另建一张答题卡并尝试绑定（需要 Pillow）")
        parser.add_argument("--org-id", type=int, default=None,
                            help="指定机构；默认复用库里第一个机构")
        parser.add_argument("--force", action="store_true",
                            help="试卷体检不通过时强制定稿（演示用途）")

    # ══════════════════════════════════════════════════════
    def handle(self, *args, **opts) -> None:
        org = self._resolve_org(opts["org_id"])
        if opts["reset"]:
            self._cleanup(org.id)
            self.stdout.write(self.style.SUCCESS(
                f"已清理机构 #{org.id}「{org.name}」下的全部 DEMO 数据"))
            return

        do_paper = opts["with_paper"] or opts["with_assignment"] or \
            opts["with_scan"]
        if not do_paper:
            self.stdout.write(self.style.WARNING(
                "什么都没建：请至少给一个 --with-paper / --with-assignment "
                "/ --with-scan（或用 --reset 清理）"))
            return

        # 先清一遍：保证可重复执行（幂等），不会撞唯一约束
        self._cleanup(org.id)

        course = self._course(org)
        nodes = self._nodes(org, course)
        paper, items = self._paper(org, course, nodes, force=opts["force"])

        room, students = None, []
        if opts["with_assignment"] or opts["with_scan"]:
            room, students = self._classroom(org)

        tpl = None
        if opts["with_scan"]:
            tpl = self._sheet(org, paper, items)

        assignment = None
        if opts["with_assignment"] or opts["with_scan"]:
            assignment = self._assignment(org, paper, room, tpl)

        # ── 汇总 ──
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("演示数据已就绪"))
        self.stdout.write(f"  机构      #{org.id} {org.name}")
        self.stdout.write(f"  课程      #{course.id} {course.name}"
                          f"（{course.code}）")
        self.stdout.write(f"  知识点    {len(nodes)} 个：" +
                          "、".join(n.name for n in nodes))
        self.stdout.write(f"  试卷      #{paper.id}「{paper.title}」"
                          f" {len(items)} 题 / 总分 {paper.total_score}")
        if room:
            self.stdout.write(f"  班级      #{room.id}「{room.name}」"
                              f" {len(students)} 人")
        if assignment:
            self.stdout.write(
                f"  任务      #{assignment.id}「{assignment.title}」"
                f" {assignment.status}"
                + ("（已绑答题卡）" if assignment.sheet_template_id
                   else "（纯线上 + 逐生乱序）"))
        self.stdout.write("")
        self.stdout.write("下一步：")
        if assignment:
            self.stdout.write(
                f"  批改工作台   /assignments/{assignment.id}/grading")
            self.stdout.write(
                f"  主观题阅卷   /assignments/{assignment.id}/review")
            self.stdout.write(
                f"  学情 / 题目分析  /assignments/{assignment.id}/analytics")
        self.stdout.write(f"  组卷 / 发布前体检  /papers/{paper.id}/compose")
        self.stdout.write("  清理         python manage.py seed_demo --reset")

    # ══════════════════════════════════════════════════════
    #  清理
    # ══════════════════════════════════════════════════════
    def _cleanup(self, org_id: int) -> None:
        """★ 一律走 `_base_manager`（含软删行）且**先子后父**：
        否则软删残留的 `Assignment` 会以 PROTECT 顶住 `Paper.hard_delete()`。
        """
        from apps.analytics.models import (
            ErrorTagStat,
            ItemStat,
            QuestionErrorStat,
            QuestionStat,
        )
        from apps.media.models import Asset
        from apps.omr.models import ScanBatch, ScanCard, SubjectiveCrop
        from apps.practice.models import PracticeSet
        from apps.sheet.models import (
            PaperSheetBinding,
            SheetStyle,
            SheetTemplate,
        )

        for a in Assignment._base_manager.filter(
                org_id=org_id, title__startswith=PREFIX):
            SubjectiveCrop._base_manager.filter(assignment=a).delete()
            ScanCard._base_manager.filter(assignment=a).delete()
            ScanBatch._base_manager.filter(assignment=a).delete()
            ScoreResult._base_manager.filter(assignment=a).delete()
            Response._base_manager.filter(assignment=a).delete()
            StudentTotalScore._base_manager.filter(assignment=a).delete()
            PracticeSet._base_manager.filter(assignment=a).delete()
            AssignmentStudent._base_manager.filter(assignment=a).delete()
            a.hard_delete()

        ErrorTagStat._base_manager.filter(org_id=org_id).delete()
        QuestionErrorStat._base_manager.filter(org_id=org_id).delete()
        ItemStat._base_manager.filter(org_id=org_id).delete()
        QuestionStat._base_manager.filter(org_id=org_id).delete()

        for p in Paper._base_manager.filter(org_id=org_id,
                                            title__startswith=PREFIX):
            PaperSheetBinding._base_manager.filter(paper=p).delete()
            PaperItem._base_manager.filter(paper=p).delete()
            p.hard_delete()

        for t in SheetTemplate._base_manager.filter(
                org_id=org_id, name__startswith=PREFIX):
            t.hard_delete()
        for st in SheetStyle._base_manager.filter(
                org_id=org_id, name__startswith=PREFIX):
            st.hard_delete()

        for q in Question._base_manager.filter(org_id=org_id,
                                               stem_text__startswith=PREFIX):
            QuestionKnowledge._base_manager.filter(question=q).delete()
            QuestionOption._base_manager.filter(question=q).delete()
            q.hard_delete()

        for n in KnowledgeNode._base_manager.filter(
                org_id=org_id, name__startswith=PREFIX):
            QuestionKnowledge._base_manager.filter(knowledge_node=n).delete()
            n.hard_delete()

        for c in Classroom._base_manager.filter(
                org_id=org_id, name__startswith=PREFIX):
            ClassroomStudent._base_manager.filter(classroom=c).delete()
            c.hard_delete()

        for u in User._base_manager.filter(username__startswith=USER_PREFIX):
            User._base_manager.filter(pk=u.pk).delete()

        for asset in Asset._base_manager.filter(
                org_id=org_id, oss_key__startswith="sheets/"):
            asset.hard_delete()

    # ══════════════════════════════════════════════════════
    #  各段数据
    # ══════════════════════════════════════════════════════
    def _resolve_org(self, org_id: int | None) -> Org:
        if org_id:
            org = Org._base_manager.filter(pk=org_id).first()
            if org is None:
                raise SystemExit(f"机构 #{org_id} 不存在")
            return org
        org = Org._base_manager.order_by("id").first()
        if org is None:
            raise SystemExit(
                "库里没有任何机构 —— 请先创建机构，或用 --org-id 指定")
        return org

    def _course(self, org: Org) -> Course:
        course = Course.objects.for_org(org.id).filter(code="JXJC").first()
        if course is None:
            course = Course.objects.for_org(org.id).order_by("id").first()
        if course is None:
            raise SystemExit("库里没有课程 —— 请先建课程，或导入课程模板")
        return course

    def _nodes(self, org: Org, course: Course) -> list[KnowledgeNode]:
        names = [
            f"{PREFIX} 平面机构自由度",
            f"{PREFIX} 四杆机构",
            f"{PREFIX} 键连接与轴承",
            f"{PREFIX} 齿轮传动",
        ]
        out = []
        for i, name in enumerate(names, start=1):
            node, _ = KnowledgeNode.objects.get_or_create(
                org_id=org.id, course=course, name=name,
                defaults={
                    "subject": course.code,
                    "code": f"{course.code}-DEMO{i:02d}",
                    "path": f"/DEMO{i:02d}/",
                    "level": 1,
                    "sort_order": i,
                    "exam_weight": Decimal("1.00"),
                },
            )
            out.append(node)
        return out

    def _paper(self, org: Org, course: Course,
               nodes: list[KnowledgeNode], *, force: bool) -> tuple:
        questions: list[Question] = []
        for i, (qtype, _score, stem) in enumerate(SPEC, start=1):
            text = f"{PREFIX} 第{i}题 {stem}"
            options = [] if qtype in ("SHORT_ANSWER", "ESSAY") else list(
                OPTIONS_4)
            if qtype == "MULTIPLE_CHOICE":
                answer = {"value": ["A", "B"]}
            elif qtype == "SINGLE_CHOICE":
                answer = {"value": "A"}
            else:
                answer = {}
            q = QuestionService.create_question(
                org_id=org.id, course_id=course.id, question_type=qtype,
                stem_text=text, options=options, answer=answer,
                source_type="TEST",
            )
            QuestionService.approve_question(org_id=org.id, question_id=q.id)
            questions.append(Question.objects.for_org(org.id).get(pk=q.id))

            # 每题一个主知识点（CONFIRMED）—— 覆盖率与体检才有东西可算
            QuestionKnowledge.objects.create(
                org_id=org.id, question=q, knowledge_node=nodes[(i - 1) % len(nodes)],
                weight=Decimal("1.00"), is_primary=True, review_state="CONFIRMED",
            )

        paper = PaperService.create_paper(
            org_id=org.id, course_id=course.id,
            title=f"{PREFIX} 机械基础阶段测（10 题）")
        PaperService.add_questions(
            org_id=org.id, paper_id=paper.id,
            items=[{"question_id": q.id, "score": score}
                   for q, (_t, score, _s) in zip(questions, SPEC)],
            section_title="一、综合",
        )
        try:
            PaperService.finalize_paper(org_id=org.id, paper_id=paper.id)
        except ZfError as exc:
            if not force:
                raise SystemExit(
                    f"定稿被拦：{exc.detail}\n"
                    "演示数据不追求体检满分；加 --force 可强制定稿") from exc
            PaperService.finalize_paper(
                org_id=org.id, paper_id=paper.id, force=True,
                reason="演示数据：跳过组卷体检")

        paper = Paper.objects.for_org(org.id).get(pk=paper.id)
        items = list(PaperItem.objects.for_org(org.id).filter(
            paper=paper).order_by("seq"))
        return paper, items

    def _classroom(self, org: Org) -> tuple:
        room = ClassroomService.create_classroom(
            org_id=org.id, name=f"{PREFIX}班（机械基础）", grade="2026级",
            academic_year="2026-2027")
        rows = [{"student_no": no, "name": f"演示学生{i:02d}"}
                for i, no in enumerate(STUDENT_NOS, start=1)]
        RosterService.import_roster(
            org_id=org.id, classroom_id=room.id, rows=rows)

        students = []
        for i, (no, row) in enumerate(zip(STUDENT_NOS, rows), start=1):
            u = User.objects.create(
                username=f"{USER_PREFIX}{i:02d}", real_name=row["name"],
                org_id=org.id, role="STUDENT")
            cs = ClassroomStudent.objects.for_org(org.id).get(
                classroom=room, student_no=no)
            cs.student = u
            cs.save(update_fields=["student", "updated_at"])
            students.append(u)
        return room, students

    def _sheet(self, org: Org, paper: Paper, items: list) -> object | None:
        """建答题卡并发布。发布失败（缺 Pillow/reportlab）时降级返回 None。"""
        from apps.sheet.services import SheetStyleService, TemplateService

        style = SheetStyleService.create_style(
            org_id=org.id,
            data={"name": f"{PREFIX} 答题卡样式", "page_size": "A4"},
        )
        tpl = TemplateService.create_draft(
            org_id=org.id, name=f"{PREFIX} 答题卡", style_id=style.id,
            sections=[
                {"order": 1, "type": "SINGLE_CHOICE", "title": "一、单选题",
                 "count": 6, "options": 4, "on_sheet": True, "score": 6},
                {"order": 2, "type": "MULTIPLE_CHOICE", "title": "二、多选题",
                 "count": 2, "options": 4, "on_sheet": True, "score": 7},
                {"order": 3, "type": "SHORT_ANSWER", "title": "三、简答题",
                 "count": 1, "on_sheet": True, "score": 25},
                {"order": 4, "type": "ESSAY", "title": "四、论述题",
                 "count": 1, "on_sheet": True, "score": 25},
            ],
        )
        try:
            tpl = TemplateService.publish(org_id=org.id, template_id=tpl.id)
        except Exception as exc:      # noqa: BLE001
            self.stdout.write(self.style.WARNING(
                f"答题卡已建为草稿（#{tpl.id}），但发布失败："
                f"{type(exc).__name__}: {exc}"))
            self.stdout.write(self.style.WARNING(
                "  → 发布要渲染 PDF/预览图（reportlab → Pillow）。"
                "装不上二进制轮子时这是预期结果；"
                "任务将降级为「纯线上 + 逐生乱序」，其余演示不受影响。"))
            return None
        return tpl

    def _assignment(self, org: Org, paper: Paper, room, tpl) -> Assignment:
        now = timezone_now()
        a = AssignmentService.create_assignment(
            org_id=org.id, paper_id=paper.id, classroom_id=room.id,
            title=f"{PREFIX} 机械基础阶段测",
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(days=7),
            # 绑了卡就不能开乱序（纸质无法按人印不同题序）
            sheet_template_id=tpl.id if tpl else None,
            shuffle_questions=tpl is None,
        )
        AssignmentService.publish(org_id=org.id, assignment_id=a.id)
        return Assignment.objects.for_org(org.id).get(pk=a.id)


def timezone_now():
    from django.utils import timezone

    return timezone.now()
