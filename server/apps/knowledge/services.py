"""知识体系 · 业务写操作。

★ 分层职责（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response
  - 业务规则在 services，不散落到 models / serializers / views

本模块提供：
  - `KnowledgeCodeService`  编码生成（课程 / 章 / 任务 / 知识点）
  - `KnowledgeTreeService`  树与节点操作（建、改、合并、前置环检测）
  - `KnowledgeEntryService` 知识库条目的录入与采纳计数
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction
from django.db.models import F, Q

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.knowledge.models import (
    Chapter,
    ChapterKnowledge,
    Course,
    CourseStatus,
    KnowledgeEdge,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
    Task,
    TaskKnowledge,
)

# ══════════════════════════════════════════════════════════════
#  ★ 题库章节号的特例映射（课程级配置）
# ══════════════════════════════════════════════════════════════

#: `课程编码 -> {题库章节号: 任务编码}`。
#:
#: ★ 为什么需要它：题库的编号是按**当时**的教材结构编的，教材改版后
#:   编号可能与当前知识体系对不上。硬把题库废弃代价太大，
#:   用显式映射把旧编号接到新位置上更实际。
#:
#: ★ 为什么放在代码常量而不是数据库：这类特例是**一次性数据订正**，
#:   不是持续变化的业务配置；放在代码里可评审、可追溯、可随版本回滚。
#:   将来特例变多（>10 条）再考虑建表。
CHAPTER_MAPPING_OVERRIDES: dict[str, dict[str, str]] = {
    # 机械基础：`4.3` 是 69 道「润滑与密封」题。
    # 编题时该内容还是**附录**，不在章号体系内，故编为 4.3；
    # 现在它是第六章（C06），两个知识点各自成一个任务，
    # 题目统一归到 `T01`（润滑）。
    "JXJC": {
        "4.3": "JXJC-C06-T01",
    },
}


def chapter_overrides_for(course_code: str) -> dict[str, str]:
    """取某课程的章节号特例映射。"""
    return dict(CHAPTER_MAPPING_OVERRIDES.get(course_code, {}))


# ══════════════════════════════════════════════════════════════
#  编码生成
# ══════════════════════════════════════════════════════════════


class KnowledgeCodeService:
    """编码生成。

    ★ 编码规则（README 五之二）：

        <课程码>-C<两位章号>-T<两位任务号>-K<两位知识点序号>

    ★ **补零到两位**是为了字符串排序正确：不补零时 `C10` 会排到 `C2` 前面。
      现在只有 6 章不出问题，章数到两位数就会出错。

    ★ 编码是**内部稳定标识**（改名不变、可跨机构对齐）；
      `name` 是展示名（与纸质教材一致）。两者都存，不允许只存其一。
    """

    CHAPTER_WIDTH = 2
    TASK_WIDTH = 2
    NODE_WIDTH = 2

    @classmethod
    def chapter_code(cls, course_code: str, chapter_seq: int) -> str:
        return f"{course_code}-C{chapter_seq:0{cls.CHAPTER_WIDTH}d}"

    @classmethod
    def task_code(cls, course_code: str, chapter_seq: int, task_seq: int) -> str:
        return (
            f"{cls.chapter_code(course_code, chapter_seq)}"
            f"-T{task_seq:0{cls.TASK_WIDTH}d}"
        )

    @classmethod
    def node_code(
        cls, course_code: str, chapter_seq: int, task_seq: int, node_seq: int
    ) -> str:
        return (
            f"{cls.task_code(course_code, chapter_seq, task_seq)}"
            f"-K{node_seq:0{cls.NODE_WIDTH}d}"
        )

    @staticmethod
    def parse_node_code(code: str) -> dict[str, Any] | None:
        """解析知识点编码。★ 导入与对账时用于反查层级。

        返回 `{course_code, chapter_seq, task_seq, node_seq}`；
        无法解析返回 None（编码可能由人工指定而非本规则生成）。
        """
        m = re.fullmatch(
            r"(?P<course>.+?)-C(?P<ch>\d+)-T(?P<task>\d+)-K(?P<node>\d+)", code or ""
        )
        if not m:
            return None
        return {
            "course_code": m.group("course"),
            "chapter_seq": int(m.group("ch")),
            "task_seq": int(m.group("task")),
            "node_seq": int(m.group("node")),
        }


# ══════════════════════════════════════════════════════════════
#  知识树操作
# ══════════════════════════════════════════════════════════════


class KnowledgeTreeService:
    """知识树写操作。"""

    # ── 课程（U08）──
    #
    # ★★ 三条口径（2026-10-21 定，改之前先读）：
    #
    #   ① **编码一锁到底**：课程已经长出章/题/卷之后，`code` 不可改。
    #      章/任务/知识点的编码都是 `<course.code>-C01-T01-K01` 拼出来的，
    #      改课程编码会让 44 个知识点编码与题目关联、老师教案上的编号
    #      集体错位，而系统里没有任何地方记得「旧编码是什么」。
    #      锁在「有任何下级数据」这一层，比锁在「有章」更保守：
    #      只有一道题挂在课程上（`course` 外键、`subject` 冗余列）时，
    #      改编码同样会造成两份编码不一致。
    #
    #   ② **删除必须是全空的**：`Chapter` / `KnowledgeNode` 对外键是
    #      `CASCADE`，但软删不会触发级联——只把课程打上 `deleted_at`
    #      会把整棵树留成孤儿（章还在、题还挂着、组卷时抽得到）。
    #      所以先数引用，有引用就 409 并**列出是什么**，让老师自己决定
    #      是改名归档还是真的清空。
    #
    #   ③ **归档 ≠ 删除**：`status=ARCHIVED` 是可逆的下线，
    #      删除是软删。两个动作分开给，不混在一个接口里。

    #: 课程被删除前必须为空的引用表（`kind` → 说明）
    COURSE_REFERENCE_KINDS: tuple[tuple[str, str], ...] = (
        ("chapter", "章"),
        ("knowledge_node", "知识点"),
        ("question", "题目"),
        ("paper", "试卷"),
        ("question_group", "题组"),
    )

    @staticmethod
    def course_references(*, org_id: int, course_id: int) -> list[dict[str, Any]]:
        """课程的下级引用清点（只读，删除/改编码前的闸门取数）。

        返回 `[{kind, label, count, samples}, ...]`，只含 count > 0 的项。
        `samples` 给前 5 个名字，让老师知道「是哪几个」而不是只有一个数字。
        """
        from apps.paper.models import Paper
        from apps.questionbank.models import Question, QuestionGroup

        buckets: list[tuple[str, str, Any, str]] = [
            ("chapter", "章",
             Chapter.objects.for_org(org_id).filter(course_id=course_id), "name"),
            ("knowledge_node", "知识点",
             KnowledgeNode.objects.for_org(org_id).filter(course_id=course_id),
             "name"),
            # ★ `for_org()` 已经排除软删行（`OrgScopedManager.for_org` 里
            #   统一加了 `deleted_at__isnull=True`），这里不再重复过滤。
            ("question", "题目",
             Question.objects.for_org(org_id).filter(course_id=course_id),
             "stem_text"),
            ("paper", "试卷",
             Paper.objects.for_org(org_id).filter(course_id=course_id), "title"),
            # ★ `QuestionGroup` **没有** `name` 字段（只有 stem_html /
            #   stem_text），用它的题干前 40 字当样本。
            ("question_group", "题组",
             QuestionGroup.objects.for_org(org_id).filter(course_id=course_id),
             "stem_text"),
        ]

        out: list[dict[str, Any]] = []
        for kind, label, qs, name_field in buckets:
            n = qs.count()
            if not n:
                continue
            samples = [
                str(v)[:40]
                for v in qs.order_by("id").values_list(name_field, flat=True)[:5]
            ]
            out.append({
                "kind": kind,
                "label": label,
                "count": n,
                "samples": samples,
            })
        return out

    @staticmethod
    def _assert_course_code(code: str) -> None:
        """校验课程编码格式（`CODE_VALIDATOR`）。

        ★ 必须**显式调用**：Django 的 `validators` 只在 `full_clean()`
          时生效，而 `objects.create()` 不跑 `full_clean`。
          不校验的后果不是「报错」而是「静默生效」——
          编码里带空格或斜杠，之后所有子节点编码都会带上它，
          而编码是要写进题目、写进教案的东西。
        """
        from apps.org.models import CODE_VALIDATOR

        try:
            CODE_VALIDATOR(code)
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed(detail=str(exc)) from exc

    @staticmethod
    @transaction.atomic
    def create_course(
        *,
        org_id: int,
        name: str,
        code: str,
        major_id: int | None = None,
        grade_band: str | None = None,
        level: str | None = None,
        textbook: dict[str, Any] | None = None,
        total_hours: int | None = None,
        style_config: dict[str, Any] | None = None,
        sort_order: int = 0,
    ) -> Course:
        """新建课程。

        ★ `code` **必须显式给**，不从 `name` 猜拼音首字母：
          编码是全部知识点编码的前缀，猜错了要改就得连带改 44 个节点编码，
          而节点编码已经被题目引用。
        ★ 同机构内 `code` 唯一（库层 `uq_course_org_code`，条件为未软删）。
          这里先查一次是为了给出「已存在」这种可照做的提示，
          而不是让库抛 IntegrityError。
        """
        code = (code or "").strip().upper()
        if not code:
            raise ValidationFailed(detail="课程编码不能为空")
        KnowledgeTreeService._assert_course_code(code)
        if Course.objects.for_org(org_id).filter(code=code).exists():
            raise ValidationFailed(detail=f"课程编码 {code} 已存在")

        major = None
        if major_id:
            from apps.org.models import Major

            major = Major.objects.filter(pk=major_id).first()
            if major is None:
                raise ValidationFailed(detail="所选专业不存在")

        payload: dict[str, Any] = {
            "org_id": org_id,
            "name": name.strip(),
            "code": code,
            "major": major,
            "total_hours": total_hours,
            "textbook": textbook or {},
            "style_config": style_config or {},
            "sort_order": sort_order,
            "status": CourseStatus.ACTIVE,
        }
        if grade_band:
            payload["grade_band"] = grade_band
        if level:
            payload["level"] = level
        return Course.objects.create(**payload)

    #: 允许通过 PATCH 修改的字段（白名单）。
    #: ★ 不在白名单里的字段（`org_id` / `is_template` / `deleted_at`）
    #:   传了就是 400 —— 静默忽略会让调用方以为改成功了。
    COURSE_WRITABLE = frozenset({
        "name", "code", "major_id", "grade_band", "level",
        "textbook", "total_hours", "style_config", "sort_order", "status",
    })

    @staticmethod
    @transaction.atomic
    def update_course(
        *, org_id: int, course_id: int, payload: dict[str, Any]
    ) -> Course:
        """改课程。★ 只接受白名单字段，`code` 有下级数据时锁定。

        错误码：
          - 未知字段 / 编码重复 → 400 `COMMON.VALIDATION_ERROR`
          - 有下级数据时改 `code` → 409 `KNOWLEDGE.CODE_LOCKED`
            （`data.references` 列出是哪些数据卡住了它）
        """
        unknown = set(payload) - KnowledgeTreeService.COURSE_WRITABLE
        if unknown:
            raise ValidationFailed(
                detail=f"不支持修改这些字段：{'、'.join(sorted(unknown))}")

        course = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if course is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")

        new_code = payload.get("code")
        if new_code is not None:
            new_code = str(new_code).strip().upper()
            if not new_code:
                raise ValidationFailed(detail="课程编码不能为空")
            KnowledgeTreeService._assert_course_code(new_code)
            if new_code != course.code:
                refs = KnowledgeTreeService.course_references(
                    org_id=org_id, course_id=course_id)
                if refs:
                    head = "、".join(f"{r['label']} {r['count']}" for r in refs)
                    raise ZfError(
                        ErrorCode.KNOWLEDGE_CODE_LOCKED,
                        detail=(
                            f"课程编码已锁定：{course.code} 下已有 {head}。"
                            f"章 / 任务 / 知识点的编码都以前缀 {course.code} 生成，"
                            f"改编码会让它们全部错位。请改为修改课程名，"
                            f"或新建课程后重新导入。"
                        ),
                        extra={"current_code": course.code,
                               "references": refs},
                    )
                if Course.objects.for_org(org_id).filter(
                    code=new_code
                ).exclude(pk=course_id).exists():
                    raise ValidationFailed(detail=f"课程编码 {new_code} 已存在")
            payload = {**payload, "code": new_code}

        major_id = payload.pop("major_id", None)
        if major_id is not None:
            from apps.org.models import Major

            if major_id == 0 or major_id == "":
                course.major = None
            else:
                major = Major.objects.filter(pk=int(major_id)).first()
                if major is None:
                    raise ValidationFailed(detail="所选专业不存在")
                course.major = major

        status_val = payload.pop("status", None)
        if status_val is not None:
            if status_val not in CourseStatus.values:
                raise ValidationFailed(detail=f"未知状态 {status_val}")
            course.status = status_val

        changed = ["updated_at"]
        for field, value in payload.items():
            if field == "name":
                value = str(value).strip()
                if not value:
                    raise ValidationFailed(detail="课程名不能为空")
            setattr(course, field, value)
            changed.append(field)
        course.save(update_fields=list(dict.fromkeys(changed)))
        return course

    @staticmethod
    @transaction.atomic
    def delete_course(*, org_id: int, course_id: int) -> Course:
        """**软删**课程。★ 有任何下级数据就 409，不做级联软删。

        为什么不做级联：
          `Chapter` / `KnowledgeNode` / `Question` 的软删是逐行打
          `deleted_at`，而题目还被 `QuestionKnowledge` 关联着——
          一次误删课程就会把整棵知识树与挂在树上的题一起隐藏，
          恢复需要按 `deleted_at` 精确回滚多张表，实操上等于丢了。

        想下线但保留数据请用 `PATCH {"status": "ARCHIVED"}`。
        """
        course = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if course is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")

        refs = KnowledgeTreeService.course_references(
            org_id=org_id, course_id=course_id)
        if refs:
            head = "、".join(
                f"{r['label']} {r['count']}（{'、'.join(r['samples'][:3])}…）"
                if r["count"] > 3 else
                f"{r['label']} {r['count']}（{'、'.join(r['samples'])}）"
                for r in refs
            )
            raise ZfError(
                ErrorCode.KNOWLEDGE_IN_USE,
                detail=(
                    f"不能删除课程「{course.name}」：它下面还有 {head}。"
                    f"删除会把这棵树和挂在树上的题一起隐藏。"
                    f"如果只是不想再使用，请改为归档（status=ARCHIVED）。"
                ),
                extra={"references": refs, "course_id": course_id},
            )

        course.delete()   # SoftDeleteModel → 打 deleted_at
        return course

    # ── 章 ──
    @staticmethod
    @transaction.atomic
    def create_chapter(
        *,
        org_id: int,
        course_id: int,
        seq: int,
        name: str,
        part_seq: int | None = None,
        part_name: str = "",
        code: str | None = None,
    ) -> Chapter:
        """新建章。

        契约：
          - `seq`（章号）必填，它是题库章节号 `X.Y` 的 X
          - `code` 不传时按课程码 + 章号自动生成
          - 同课程内章号唯一，重复则抛 `COMMON.VALIDATION_ERROR`
        """
        course = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if course is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")

        if Chapter.objects.for_org(org_id).filter(
            course_id=course_id, seq=seq
        ).exists():
            raise ValidationFailed(detail=f"第 {seq} 章已存在")

        return Chapter.objects.create(
            org_id=org_id,
            course=course,
            subject=course.code,
            grade_band=course.grade_band,
            seq=seq,
            name=name,
            part_seq=part_seq,
            part_name=part_name,
            code=code or KnowledgeCodeService.chapter_code(course.code, seq),
            status="ACTIVE",
        )

    # ── 任务 ──
    @staticmethod
    @transaction.atomic
    def create_task(
        *,
        org_id: int,
        chapter_id: int,
        seq: int,
        name: str,
        is_practical: bool = False,
        objectives: list[str] | None = None,
    ) -> Task:
        """新建任务。

        ★ 唯一键是 **(chapter, seq)**，不是 (course, seq)——
          `task.seq` 在每章内从 1 重排（见 `Task.Meta` 的说明）。
          若按 `(course, seq)` 判断重复，会把不同章的同序号任务误判为冲突。
        """
        chapter = (
            Chapter.objects.for_org(org_id)
            .filter(pk=chapter_id)
            .select_related("course")
            .first()
        )
        if chapter is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="章不存在")

        if Task.objects.for_org(org_id).filter(
            chapter_id=chapter_id, seq=seq
        ).exists():
            raise ValidationFailed(detail=f"本章第 {seq} 个任务已存在")

        return Task.objects.create(
            org_id=org_id,
            course=chapter.course,
            chapter=chapter,
            seq=seq,
            name=name,
            code=KnowledgeCodeService.task_code(
                chapter.course.code, chapter.seq, seq
            ),
            objectives=objectives or [],
            is_practical=is_practical,
            status="ACTIVE",
        )

    # ── 知识点 ──
    @staticmethod
    @transaction.atomic
    def create_node(
        *,
        org_id: int,
        task_id: int,
        name: str,
        seq_in_task: int | None = None,
        exam_weight: float = 1.0,
        code: str | None = None,
    ) -> KnowledgeNode:
        """在任务下新建知识点。

        ★ `seq_in_task` 保留教材原始序号（如「1.」），用于教师对照纸质书；
          `name` 存去掉序号的纯名称（`整体热处理` 而非 `1. 整体热处理`）。
        """
        task = (
            Task.objects.for_org(org_id)
            .filter(pk=task_id)
            .select_related("chapter", "course")
            .first()
        )
        if task is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")

        if code is None:
            # ★★ 不能只用 `count() + 1`：知识点被删除或**移动走**之后，
            #   数量会减少，而剩下的节点编码没变 —— 于是「数量+1」
            #   会算出**已经被占用**的编号，建新节点直接 400「编码已存在」。
            #   实测（2026-10-21，写操作验收）就撞到了：
            #   t1 下原有 K01/K02，K01 被移到 t2 后 count=1，
            #   新建节点算出 K02，与仍在 t1 的 K02 撞车。
            #   → 改为「取该任务下已用的最大序号 + 1」，再兜底避开占用。
            used = [
                c for c in KnowledgeNode.objects.for_org(org_id)
                .filter(task_id=task_id)
                .values_list("code", flat=True)
            ]
            max_seq = 0
            for c in used:
                parsed = KnowledgeCodeService.parse_node_code(c)
                if parsed and parsed.get("node_seq"):
                    max_seq = max(max_seq, int(parsed["node_seq"]))
            n = max(max_seq, len(used)) + 1
            code = KnowledgeCodeService.node_code(
                task.course.code, task.chapter.seq, task.seq, n
            )
            # 编码可能被人工改成不规则形式（不含 `-Kxx`），上面的解析就取
            # 不到序号；这里再兜一层，保证一定拿到一个没被占用的编码。
            guard = 0
            while KnowledgeNode.objects.for_org(org_id).filter(
                code=code
            ).exists() and guard < 200:
                n += 1
                guard += 1
                code = KnowledgeCodeService.node_code(
                    task.course.code, task.chapter.seq, task.seq, n
                )

        if KnowledgeNode.objects.for_org(org_id).filter(code=code).exists():
            raise ValidationFailed(detail=f"知识点编码 {code} 已存在")

        node = KnowledgeNode.objects.create(
            org_id=org_id,
            course=task.course,
            task=task,
            subject=task.course.code,
            grade_band=task.course.grade_band,
            name=name,
            code=code,
            seq_in_task=seq_in_task,
            sort_order=seq_in_task or 1,
            exam_weight=exam_weight,
            level=4,
            status="ACTIVE",
        )
        # 同步任务↔知识点映射（首个任务为主归属）
        KnowledgeTreeService.link_task_node(
            org_id=org_id,
            task_id=task_id,
            node_id=node.id,
            seq_in_task=seq_in_task,
            is_primary=True,
        )
        return node

    @staticmethod
    @transaction.atomic
    def link_task_node(
        *,
        org_id: int,
        task_id: int,
        node_id: int,
        seq_in_task: int | None = None,
        is_primary: bool = False,
    ) -> TaskKnowledge:
        """建立任务↔知识点关联。

        ★ 一个知识点**只能有一个**主归属任务（库层唯一约束）。
          传 `is_primary=True` 时先撤销该知识点已有的主归属，避免撞约束。
        """
        if is_primary:
            TaskKnowledge.objects.for_org(org_id).filter(
                knowledge_node_id=node_id, is_primary=True
            ).update(is_primary=False)

        link, _ = TaskKnowledge.objects.update_or_create(
            task_id=task_id,
            knowledge_node_id=node_id,
            defaults={
                "org_id": org_id,
                "seq_in_task": seq_in_task,
                "is_primary": is_primary,
            },
        )
        return link

    # ── 知识点写操作（U09）──
    #
    # ★★ 三条口径：
    #
    #   ① **`code` 移动后不重算**（计划里写死的一条）。
    #      `JXJC-C01-T01-K01` 这样的编码已经被题目关联、被老师抄进教案。
    #      把知识点从任务 1 挪到任务 2 时，编码里的 `T01` 就与位置不符了——
    #      **这是刻意接受的**：编码是**稳定标识**，不是位置描述。
    #      重算会让「同一道题在两次报表里挂在不同知识点上」，
    #      而 `KnowledgeCodeService.parse_node_code()` 是给导入对账用的，
    #      它解析出来的位置只作参考、不作唯一依据。
    #      ★ 想改编码请显式传 `code`，并接受它与位置不符。
    #
    #   ② **移动必须同时改 4 个冗余列**：`task` / `course` / `subject` /
    #      `grade_band`。只改 `task` 会让「按课程统计知识点」「按课程码
    #      抽题」这两条查询在跨课程移动后返回旧课程的数据——
    #      而界面上看起来一切正常。
    #
    #   ③ **删除前必须清 4 张关联表**：`task_knowledge` /
    #      `chapter_knowledge` / `knowledge_edge` / `knowledge_alias`。
    #      这些都是普通 `models.Model` 或 CASCADE 外键，而软删**不触发级联**，
    #      留着会让「已删除的知识点」继续出现在树的关联查询与前置图里。

    #: 允许通过 PATCH 修改的字段（白名单）。
    NODE_WRITABLE = frozenset({
        "name", "code", "seq_in_task", "sort_order", "exam_weight",
        "status", "task_id", "deprecated",
    })

    @staticmethod
    def node_references(*, org_id: int, node_id: int) -> list[dict[str, Any]]:
        """知识点的引用清点（删除前的闸门取数）。

        返回 `[{kind, label, count, samples, hint}, ...]`，只含 count > 0 的项。

        ★ 为什么把学情也算作「引用」：`KnowledgeMastery` 的外键是 CASCADE，
          真删会连带清掉历史掌握度。知识点删了还能重建，掌握度删了
          没有任何地方能补回来——两件事的不对称性决定了宁可挡住。
        """
        from apps.analytics.models import KnowledgeMastery
        from apps.questionbank.models import QuestionKnowledge

        out: list[dict[str, Any]] = []

        qs = QuestionKnowledge.objects.for_org(org_id).filter(
            knowledge_node_id=node_id, question__deleted_at__isnull=True
        ).select_related("question")
        n = qs.count()
        if n:
            out.append({
                "kind": "question",
                "label": "题目",
                "count": n,
                "samples": [
                    str(getattr(link.question, "stem_text", ""))[:40]
                    for link in qs[:5]
                ],
                "hint": "先在题库里把这些题改挂到别的知识点，或用「合并」把本节点并入保留节点",
            })

        qs = KnowledgeEntryNode.objects.for_org(org_id).filter(
            node_id=node_id, entry__deleted_at__isnull=True
        ).select_related("entry")
        n = qs.count()
        if n:
            out.append({
                "kind": "entry",
                "label": "知识库条目",
                "count": n,
                "samples": [
                    str(getattr(link.entry, "title", "") or "")[:40]
                    for link in qs[:5]
                ],
                # ★ 2026-10-23：这条提示原来写「先解除这些条目与本知识点的
                #   关联」，而当时**页面上根本没有条目管理入口** ——
                #   老师在管理页看到这句话，找不到任何可点的东西。
                #   现在指向真实存在的入口（知识点详情下方的「知识库条目」）。
                "hint": "在「知识库条目」里把条目改挂到别的知识点，或删除"
                        "（条目归创建者本人，学校平台账户可代管）",
            })

        edges = KnowledgeEdge.objects.for_org(org_id).filter(
            Q(from_node_id=node_id) | Q(to_node_id=node_id))
        n = edges.count()
        if n:
            out.append({
                "kind": "edge",
                "label": "前置/相关关系",
                "count": n,
                "samples": [],
                "hint": "删除时会自动清理，不阻止删除",
                "blocking": False,
            })

        n = KnowledgeMastery.objects.for_org(org_id).filter(
            knowledge_node_id=node_id).count()
        if n:
            out.append({
                "kind": "mastery",
                "label": "学生掌握度记录",
                "count": n,
                "samples": [],
                "hint": "历史学情删了补不回来；若只是不想再用这个考点，请改为废弃（deprecated）",
            })

        # ★ 可自动清理的项标记 `blocking=False`：前端只把 blocking 的项
        #   当「拦住删除的原因」列出来，其余只是告知。
        for item in out:
            item.setdefault("blocking", True)

        return out

    @staticmethod
    @transaction.atomic
    def update_node(
        *, org_id: int, node_id: int, payload: dict[str, Any]
    ) -> KnowledgeNode:
        """改知识点 / 移动层级。**`code` 不重算**（见上方 ①）。

        错误码：
          - 未知字段 / 空名 / 非法状态 / 编码重复 → 400
          - 目标位置已有同 `seq_in_task` 的节点 → 400（提示换个序号）
          - `code` 与同机构其它节点重复 → 400
        """
        unknown = set(payload) - KnowledgeTreeService.NODE_WRITABLE
        if unknown:
            raise ValidationFailed(
                detail=f"不支持修改这些字段：{'、'.join(sorted(unknown))}")

        node = KnowledgeNode.objects.for_org(org_id).filter(pk=node_id).first()
        if node is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")

        changed: list[str] = []

        # ── 移动：换任务（可跨章、跨课程）──
        task_id = payload.pop("task_id", None)
        if task_id is not None and int(task_id) != node.task_id:
            task = (
                Task.objects.for_org(org_id)
                .filter(pk=int(task_id))
                .select_related("chapter", "course")
                .first()
            )
            if task is None:
                raise ValidationFailed(detail="目标任务不存在")

            # ① 冗余列必须一起改（见上方 ②）
            node.task = task
            node.course = task.course
            node.subject = task.course.code
            node.grade_band = task.course.grade_band
            changed += ["task", "course", "subject", "grade_band"]

            # ② 主归属关联要跟着搬
            KnowledgeTreeService.link_task_node(
                org_id=org_id, task_id=task.id, node_id=node.id,
                seq_in_task=payload.get("seq_in_task", node.seq_in_task),
                is_primary=True,
            )
            # ③ 章关联：换任务通常也换了章，旧的章关联要撤掉
            ChapterKnowledge.objects.for_org(org_id).filter(
                knowledge_node=node
            ).exclude(chapter_id=task.chapter_id).delete()
            ChapterKnowledge.objects.update_or_create(
                chapter_id=task.chapter_id,
                knowledge_node=node,
                defaults={"org_id": org_id, "is_primary": True},
            )
            # ④ `path` 是物化路径，留着旧路径会让「按 path 前缀查子树」出错
            node.path = f"{task.path or task.code}/{node.code}"[:500]
            changed.append("path")

        # ── 普通字段 ──
        new_code = payload.pop("code", None)
        if new_code is not None:
            new_code = str(new_code).strip()
            if new_code and new_code != node.code:
                if KnowledgeNode.objects.for_org(org_id).filter(
                    code=new_code
                ).exclude(pk=node.id).exists():
                    raise ValidationFailed(detail=f"知识点编码 {new_code} 已存在")
                node.code = new_code
                changed.append("code")

        if "name" in payload:
            name = str(payload.pop("name")).strip()
            if not name:
                raise ValidationFailed(detail="知识点名不能为空")
            node.name = name
            changed.append("name")

        if "status" in payload:
            st = payload.pop("status")
            if st not in ("ACTIVE", "ARCHIVED"):
                raise ValidationFailed(detail=f"未知状态 {st}")
            node.status = st
            changed.append("status")

        # ★ `deprecated` 与 `status` 是两个概念（见模型注释）：
        #   前者是「考纲删了，不再参与组卷」，后者是「已并入别处」。
        if "deprecated" in payload:
            from django.utils import timezone as _tz

            dep = bool(payload.pop("deprecated"))
            node.deprecated_at = _tz.now() if dep else None
            changed.append("deprecated_at")

        for field in ("seq_in_task", "sort_order", "exam_weight"):
            if field in payload:
                setattr(node, field, payload.pop(field))
                changed.append(field)

        # 任务内序号冲突：同一任务下两个节点同序号，
        # 「第 3 个知识点」这种教材口径就没法对齐了。
        if "seq_in_task" in changed and node.seq_in_task is not None:
            dup = KnowledgeNode.objects.for_org(org_id).filter(
                task_id=node.task_id, seq_in_task=node.seq_in_task,
            ).exclude(pk=node.id).exists()
            if dup:
                raise ValidationFailed(
                    detail=(
                        f"该任务下已有教材序号 {node.seq_in_task} 的知识点，"
                        f"请换一个序号（改序号不影响知识点编码）"
                    ))

        if changed:
            node.save(update_fields=list(dict.fromkeys(changed + ["updated_at"])))
        return node

    @staticmethod
    @transaction.atomic
    def delete_node(*, org_id: int, node_id: int) -> KnowledgeNode:
        """**软删**知识点，并清掉它的关联行。

        ★ 有一道题、一条知识库条目、一条学情记录挂在上面就 409。
          这不是「太严」：知识点可以重建（改个名就是新的），
          而挂在上面的题目标注与掌握度没法重建。
        ★ 前置关系（`KnowledgeEdge`）**自动清理**——它不承载数据，
          只是两个节点之间的关系，节点没了关系就该消失。
        """
        node = KnowledgeNode.objects.for_org(org_id).filter(pk=node_id).first()
        if node is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")

        refs = KnowledgeTreeService.node_references(
            org_id=org_id, node_id=node_id)
        blocking = [r for r in refs if r.get("blocking", True)]
        if blocking:
            head = "、".join(f"{r['label']} {r['count']}" for r in blocking)
            raise ZfError(
                ErrorCode.KNOWLEDGE_IN_USE,
                detail=(
                    f"不能删除知识点「{node.name}（{node.code}）」："
                    f"它还有 {head}。"
                    f"若这个考点已经归到别处，请用「合并」把它并入保留的节点；"
                    f"若只是不再考了，请改为废弃（deprecated）。"
                ),
                extra={"references": refs, "node_id": node_id},
            )

        # 关联行清理（软删不级联，必须显式做）
        KnowledgeEdge.objects.for_org(org_id).filter(
            Q(from_node_id=node_id) | Q(to_node_id=node_id)).delete()
        TaskKnowledge.objects.for_org(org_id).filter(
            knowledge_node_id=node_id).delete()
        ChapterKnowledge.objects.for_org(org_id).filter(
            knowledge_node_id=node_id).delete()
        # ★ `KnowledgeAlias` 继承的是 `models.Model`（无 deleted_at），
        #   只能物理删除；节点都删了，别名指向一个不存在的节点更糟。
        from apps.knowledge.models import KnowledgeAlias

        KnowledgeAlias.objects.for_org(org_id).filter(node_id=node_id).delete()

        node.delete()
        return node

    @staticmethod
    @transaction.atomic
    def merge_nodes(
        *, org_id: int, source_id: int, target_id: int
    ) -> dict[str, int]:
        """合并两个知识点。

        使用场景：导入时同名知识点被拆成多条，需要归并。

        **必须迁移全部引用，漏一项就丢数据**：
          1. 题目↔知识点关联（`question_knowledge`）
          2. 条目↔知识点关联（`knowledge_entry_node`）
          3. 任务↔知识点关联（`task_knowledge`）
          4. 前置关系（`knowledge_edge`，双向）
          5. 源节点标记归档（不物理删除，便于回溯）

        返回各表迁移条数。
        """
        if source_id == target_id:
            raise ValidationFailed(detail="源与目标是同一个知识点")

        src = KnowledgeNode.objects.for_org(org_id).filter(pk=source_id).first()
        dst = KnowledgeNode.objects.for_org(org_id).filter(pk=target_id).first()
        if src is None or dst is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")

        from apps.questionbank.models import QuestionKnowledge

        # 1) 题目关联：目标已有则丢弃源关联（避免撞唯一约束）
        existing_q = set(
            QuestionKnowledge.objects.for_org(org_id)
            .filter(knowledge_node=dst)
            .values_list("question_id", flat=True)
        )
        q_moved = 0
        for link in QuestionKnowledge.objects.for_org(org_id).filter(
            knowledge_node=src
        ):
            if link.question_id in existing_q:
                link.delete()
            else:
                link.knowledge_node = dst
                link.save(update_fields=["knowledge_node"])
                q_moved += 1

        # 2) 条目关联
        existing_e = set(
            KnowledgeEntryNode.objects.for_org(org_id)
            .filter(node=dst)
            .values_list("entry_id", flat=True)
        )
        e_moved = 0
        for link in KnowledgeEntryNode.objects.for_org(org_id).filter(node=src):
            if link.entry_id in existing_e:
                link.delete()
            else:
                link.node = dst
                link.save(update_fields=["node"])
                e_moved += 1

        # 3) 任务关联
        existing_t = set(
            TaskKnowledge.objects.for_org(org_id)
            .filter(knowledge_node=dst)
            .values_list("task_id", flat=True)
        )
        t_moved = 0
        for link in TaskKnowledge.objects.for_org(org_id).filter(knowledge_node=src):
            if link.task_id in existing_t:
                link.delete()
            else:
                link.knowledge_node = dst
                link.save(update_fields=["knowledge_node"])
                t_moved += 1

        # 4) 前置关系（双向重接）
        edges = list(
            KnowledgeEdge.objects.for_org(org_id).filter(
                Q(from_node=src) | Q(to_node=src)
            )
        )
        for e in edges:
            e.delete()
        rewired = 0
        for e in edges:
            new_from = dst if e.from_node_id == src.id else e.from_node
            new_to = dst if e.to_node_id == src.id else e.to_node
            if new_from.id == new_to.id:
                continue                      # 合并后成自环，丢弃
            KnowledgeEdge.objects.update_or_create(
                from_node=new_from,
                to_node=new_to,
                relation=e.relation,
                defaults={"org_id": org_id, "weight": e.weight},
            )
            rewired += 1

        # 5) 源节点归档
        src.status = "ARCHIVED"
        src.name = f"{src.name}（已并入 {dst.name}）"
        src.save(update_fields=["status", "name", "updated_at"])

        return {
            "question_links_moved": q_moved,
            "entry_links_moved": e_moved,
            "task_links_moved": t_moved,
            "edges_rewired": rewired,
        }

    # ── 前置关系 ──
    @staticmethod
    @transaction.atomic
    def add_prerequisite(
        *, org_id: int, node_id: int, prerequisite_id: int, weight: float = 1.0
    ) -> KnowledgeEdge:
        """添加前置关系。

        ★ **必须无环**。添加前做可达性检查：若 `prerequisite` 已能沿
          PREREQUISITE 边到达 `node`，则新边会成环，直接拒绝。
          （见 02 文档 4.4 的约束说明。）
        """
        if node_id == prerequisite_id:
            raise ValidationFailed(detail="知识点不能是自己的前置")

        if KnowledgeTreeService._can_reach(
            org_id, start=node_id, target=prerequisite_id
        ):
            raise ValidationFailed(
                detail="该前置关系会成环（前置已能到达本知识点），已拒绝"
            )

        edge, _ = KnowledgeEdge.objects.update_or_create(
            from_node_id=prerequisite_id,
            to_node_id=node_id,
            relation="PREREQUISITE",
            defaults={"org_id": org_id, "weight": weight},
        )
        return edge

    @staticmethod
    def _can_reach(org_id: int, *, start: int, target: int) -> bool:
        """有向可达性（沿 PREREQUISITE 边）。

        用迭代 + 访问集，避免递归深度问题。
        """
        visited: set[int] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            nxt = (
                KnowledgeEdge.objects.for_org(org_id)
                .filter(from_node_id=cur, relation="PREREQUISITE")
                .values_list("to_node_id", flat=True)
            )
            stack.extend(n for n in nxt if n not in visited)
        return False

    @staticmethod
    def find_prerequisite_cycles(org_id: int) -> list[list[int]]:
        """扫描全部前置环。

        ★ 供每日检测任务调用。**发现环只告警，不自动修复**——
          自动修复可能删掉正确的边，应由教研确认后再处理。
        """
        edges = list(
            KnowledgeEdge.objects.for_org(org_id)
            .filter(relation="PREREQUISITE")
            .values_list("from_node_id", "to_node_id")
        )
        graph: dict[int, list[int]] = {}
        for a, b in edges:
            graph.setdefault(a, []).append(b)

        cycles: list[list[int]] = []
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[int, int] = {n: WHITE for n in graph}
        path: list[int] = []

        def dfs(u: int) -> None:
            color[u] = GREY
            path.append(u)
            for v in graph.get(u, []):
                if color.get(v, WHITE) == GREY:
                    i = path.index(v)
                    cycles.append(path[i:] + [v])
                elif color.get(v, WHITE) == WHITE:
                    dfs(v)
            path.pop()
            color[u] = BLACK

        for n in list(graph):
            if color.get(n, WHITE) == WHITE:
                dfs(n)
        return cycles


# ══════════════════════════════════════════════════════════════
#  知识库条目
# ══════════════════════════════════════════════════════════════


class KnowledgeEntryService:
    """知识库条目写操作。

    ★★ 本类新增的改/删走**依赖校验闸门**（需求 3 / 决策 Q7）：

      > **该「知识点」下没有任何题目时，才允许改/删该知识点的条目。**

      三点必须说清楚，否则实现容易走偏：

      1. **按知识点整体判断，不是按条目逐条判断**。
         问的是「这条条目挂着的知识点里，有没有哪个下面有题」，
         不是「这条条目的内容被哪道题引用过」（条目与题目之间
         本来也没有引用关系）。

      2. **一条条目可关联多个知识点**（`KnowledgeEntry.knowledge_node`
         外键**不存在**，走中间表 `entry_links` / `KnowledgeEntryNode`）。
         ★ 只要关联的**任一**知识点下有题就必须拒绝——否则会动到
         「有点在练」的那个知识点的依据，而那正是 AI 生题要召回的东西。

      3. **「有题」的判定是保守的：草稿题也算**（见
         `ENTRY_DEPENDENCY_QUESTION_STATUSES`）。
    """

    #: 允许通过 `update_entry` 修改的字段。
    #:
    #: ★ 其余字段（`org_id` / `course_id` / `chapter_id` / `adopted_count`
    #:   / `embedding` / `created_by` / 时间戳）都是**外键归属与派生统计**，
    #:   不属于「老师改内容」的范畴。传了就报 `COMMON.VALIDATION_ERROR`
    #:   并列出这些字段名，而不是静默忽略。
    EDITABLE_FIELDS: frozenset[str] = frozenset({
        "title", "content", "entry_type", "source_type",
        "source_ref", "status", "node_ids",
    })

    @staticmethod
    def question_dependency(
        *, org_id: int, node_ids: list[int]
    ) -> list[dict[str, Any]]:
        """查这些知识点里**哪些已经有题**（依赖校验的取数，只读）。

        口径（★ 需求方已确认，2026-10-16）：

          - 只看本机构的关联行（`QuestionKnowledge.objects.for_org`）
          - 题目必须**未软删**（`question.deleted_at is null`）
          - ★★ **保守口径：草稿题（DRAFT）也算「有题」**。
            不按 `QUESTION_USABLE_STATUSES`（只认 `APPROVED`）过滤。

        ★★ 为什么草稿题也要算（这条是刻意的保守，不是漏写过滤）：

          `knowledge_entry` 是 AI 生题的**依据来源**，而 L1 门禁只要求
          「该知识点下有 ≥ `KB_MIN_ENTRIES` 条 ACTIVE 条目」，
          **不要求该知识点下已有 APPROVED 的题**。所以「有草稿题」
          恰恰说明有人正在这个知识点上出题、正准备让它变成正式题；
          此时把依据改掉或删掉，等那道草稿题审核通过时，
          它引用的依据已经变了——而审计里没有任何痕迹能解释这件事。

          换句话说：**这里放行的代价是不可逆的（依据丢了），
          这里拦住的代价只是老师多点一下（先把草稿题处理掉）**。
          两种代价不对称，所以宁可挡住。

        返回 `[{node_id, code, name, question_count}, ...]`，空列表表示可以放行。
        """
        from django.db.models import Count

        from apps.questionbank.models import QuestionKnowledge

        ids = [int(i) for i in node_ids if i]
        if not ids:
            return []

        rows = (
            QuestionKnowledge.objects.for_org(org_id)
            .filter(
                knowledge_node_id__in=ids,
                # ★ 未软删的题目。漏掉这个条件会把已删题的关联也算成「有题」，
                #   于是知识点永远解不开锁。
                question__deleted_at__isnull=True,
            )
            .values("knowledge_node_id")
            .annotate(n=Count("id", distinct=True))
        )
        counts = {r["knowledge_node_id"]: r["n"] for r in rows}
        if not counts:
            return []

        nodes = {
            n.id: n
            for n in KnowledgeNode.objects.for_org(org_id).filter(id__in=counts)
        }
        out: list[dict[str, Any]] = []
        for nid in sorted(counts, key=lambda i: (nodes[i].code if i in nodes else "", i)):
            n = nodes.get(nid)
            out.append({
                "node_id": nid,
                "code": (n.code if n else "") or "",
                "name": (n.name if n else "") or "",
                "question_count": counts[nid],
            })
        return out

    @staticmethod
    def assert_entry_writable(entry: KnowledgeEntry, user) -> None:
        """**归属守卫**：谁能改 / 删 / 归档这一条知识库条目。

        口径（2026-10-23 需求方拍板）：**条目归属老师个人，由老师自行建立**。
        与「班级归教师个人」是同一条原则在知识库上的落实。

            - 学校平台账户（L2）：可改**全机构**条目
              （它要为全校内容负责，也是存量导入数据的维护者）
            - 教师（L1）：只能改 `created_by == 自己` 的条目
            - 系统管理员（L3，无机构）：一律拒绝 —— 由 `_org_id()` 先拦，
              这里再兜一层（否则绕过视图直接调服务层就漏了）

        ★★ **`created_by` 为空 = 学校统一导入的公共条目，教师不可改。**
          库里 110 条存量条目全是这种（`created_by IS NULL`）。
          如果按「无主 = 谁都能改」处理，任何教师都能改掉全校共用的
          AI 生题依据，而且审计里只有一个 id —— 这是必须避免的。

        ★ 读取不隔离（与题目/试卷的 `scope_owned_or_public` **不同**）：
          条目是全校 AI 生题的共同依据，`ai_call_log.kb_entry_ids`
          按机构记录。若列表按人收窄，老师会看到「AI 引用了一条
          我打不开的条目」，而 L1 门禁的条目计数也还是全机构的两个数——
          读隔离与检索口径一冲突，就会变成「界面说没依据、AI 说够用」。
          所以：**读全机构共享，写归个人。**
        """
        from apps.common.scoping import Scope, scope_of

        def _deny(detail: str, **extra: Any) -> None:
            """403。★ 自带 `extra` 让前端能区分「公共条目」与「别人的」。"""
            raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail=detail,
                          extra=extra or None)

        scope = scope_of(user)
        if scope == Scope.ORG_ALL.value:
            return
        if scope != Scope.OWN_TEACHING.value:
            _deny("需要教师及以上权限", scope=scope)

        owner_id = entry.created_by_id
        if owner_id is None:
            _deny(
                "这条知识库条目是学校统一导入的公共条目，只有学校平台账户"
                "可以修改。要调整它，请联系学校平台账户；"
                "你自己新建的条目可以自行编辑和删除。",
                owner="PUBLIC",
                entry_id=entry.pk,
            )
        if owner_id != getattr(user, "pk", None):
            _deny(
                "这条知识库条目由其他老师创建，只有创建者本人或学校平台账户"
                "可以修改。你可以新建自己的条目。",
                owner="OTHER",
                entry_id=entry.pk,
                created_by=owner_id,
            )

    @staticmethod
    def assert_entry_modifiable(
        *, org_id: int, node_ids: list[int], action: str
    ) -> None:
        """依赖校验闸门：**任一**关联知识点下有题就拒。

        ★ 错误信息必须**可照做**：不能只说「不允许」，要列出到底是
          哪几个知识点有题（编码 + 名称 + 各有多少题），
          老师才知道先去处理哪里。

        - `code`  = `KNOWLEDGE.ENTRY_IN_USE`（HTTP 409）
        - `data.blocking` = 阻塞清单，供前端直接渲染成跳转链接
        """
        blocking = KnowledgeEntryService.question_dependency(
            org_id=org_id, node_ids=node_ids
        )
        if not blocking:
            return

        head = "、".join(
            f"{b['code']}（{b['name']}，{b['question_count']} 题）"
            for b in blocking[:5]
        )
        more = f" 等 {len(blocking)} 个知识点" if len(blocking) > 5 else ""
        raise ZfError(
            ErrorCode.KNOWLEDGE_ENTRY_IN_USE,
            detail=(
                f"不能{action}：该条目关联的知识点下已有题目——{head}{more}。"
                f"知识库是 AI 生题的依据，有点在练时不允许改动依据；"
                f"请先处理这些知识点下的题目（或先把条目改挂到没有题的知识点上）。"
            ),
            extra={
                # ★ `blocking` 这个名字与 `sheet` / `org` 两处的缺口清单一致
                "blocking": blocking,
                "action": action,
            },
        )

    @staticmethod
    def entry_snapshot(entry: KnowledgeEntry) -> dict[str, Any]:
        """条目的审计快照（`before` / `after` 用）。"""
        return {
            "title": entry.title,
            "content": entry.content,
            "entry_type": entry.entry_type,
            "source_type": entry.source_type,
            "source_ref": entry.source_ref,
            "status": entry.status,
            "node_ids": sorted(
                link.node_id for link in entry.entry_links.all() if link.node_id
            ),
        }

    @staticmethod
    @transaction.atomic
    def create_entry(
        *,
        org_id: int,
        content: str,
        node_ids: list[int],
        title: str = "",
        entry_type: str = "CONCEPT",
        source_type: str = "TEACHER_INPUT",
        source_ref: str = "",
        created_by_id: int | None = None,
    ) -> KnowledgeEntry:
        """录入一条知识库条目并挂到知识点。

        ★ 至少挂一个知识点——不挂的条目**检索不到**，等于没录。

        ★★ `node_ids` 是**列表**（一条条目可挂多个知识点）：
          `KnowledgeEntry` 没有 `knowledge_node` 外键，关联走中间表
          `entry_links`，所以「力的合成同属两个知识点」这类内容
          本来就能一次录进来（P1-18c 的 AI 拆条归类批量建条走这里）。

          - **重复 id 去重**后每个知识点只建 **1 条** `entry_links`
            （`uq_kb_entry_node` 唯一约束下重复插入会 `IntegrityError`，
            而不是「多挂一条」）；
          - id 不存在 / 不属于本机构 → `ValidationFailed`（400），
            ★ **不会**留下悬空关联，也不会留下半条条目
            （整个方法在 `transaction.atomic` 里）。

        ★ **没有 `chapter_id` 参数**（刻意的）：条目的 `course` /
          `chapter` 由**第一个知识点**的 `task.chapter` 反推。
          调用方传 `chapter_id` 会被 `EntryCreateSerializer` **显式 400**，
          而不是静默忽略——静默忽略会让调用方以为章节写进去了。
        """
        # ★ 显式去重（保序）：不要依赖 `filter(id__in=...)` 的返回行数，
        #   那只是 SQL 语义的副作用，读代码的人看不出去重是**承诺**。
        node_ids = list(dict.fromkeys(int(i) for i in node_ids if i))
        if not node_ids:
            raise ValidationFailed(detail="必须至少关联一个知识点")
        if not (content or "").strip():
            raise ValidationFailed(detail="条目内容不能为空")

        nodes = list(KnowledgeNode.objects.for_org(org_id).filter(id__in=node_ids))
        if len(nodes) != len(node_ids):
            # ★ 错误信息里**列出具体是哪个 id**：AI 拆条归类是按批次写的，
            #   只说「存在无效的知识点 ID」会让调用方无法定位是哪一条。
            missing = sorted(set(node_ids) - {n.id for n in nodes})
            raise ValidationFailed(
                detail="存在无效的知识点 ID（不存在或不属于本机构）："
                       + "、".join(str(i) for i in missing)
            )

        # ★★ 按**调用方给的顺序**排（不是 DB 返回顺序）：
        #   条目的 `course` / `chapter` 由**第一个**知识点反推（见
        #   docstring），所以「哪个是第一个」必须是调用方能预判的。
        #   依赖 `IN (...)` 的返回顺序会让同一份 payload 在不同执行
        #   计划下落到不同章节——而这种漂移不会有任何报错。
        by_id = {n.id: n for n in nodes}
        nodes = [by_id[i] for i in node_ids]

        first = nodes[0]
        entry = KnowledgeEntry.objects.create(
            org_id=org_id,
            course=first.course,
            chapter=first.task.chapter if first.task else None,
            title=title or content[:40],
            content=content,
            entry_type=entry_type,
            source_type=source_type,
            source_ref=source_ref,
            status="ACTIVE",
            created_by_id=created_by_id,
        )
        for n in nodes:
            KnowledgeEntryNode.objects.create(
                org_id=org_id, entry=entry, node=n, weight=1.0, source="TEACHER"
            )
        return entry

    @staticmethod
    @transaction.atomic
    def mark_adopted(*, org_id: int, entry_ids: list[int]) -> int:
        """把条目计为「被 AI 采纳」。

        ★ 用 F 表达式原子自增，避免并发丢更新。
          采纳率是知识库健康度的核心指标之一。
        """
        if not entry_ids:
            return 0
        return (
            KnowledgeEntry.objects.for_org(org_id)
            .filter(id__in=entry_ids)
            .update(adopted_count=F("adopted_count") + 1)
        )

    @staticmethod
    @transaction.atomic
    def archive_entry(*, org_id: int, entry_id: int) -> KnowledgeEntry:
        """归档条目。

        ★ 不物理删除：已被 AI 引用过的条目要留痕，
          否则历史 `ai_call_log.kb_entry_ids` 会指向不存在的记录。

        ★★ 与 `delete_entry` 的**语义差别**（两者都「让条目从检索里消失」，
          但性质不同）：

            `archive_entry`（既有，`POST .../entries/{id}/archive/`）
                只把 `status` 改成 `ARCHIVED`。行**没有** `deleted_at`，
                仍在库、仍可被列出（`?status=ARCHIVED` 查得到）、
                仍可被改回 `ACTIVE`。语义是**教学法决定**：
                「这条内容暂时不用」，可逆。

            `delete_entry`（新增，`DELETE .../entries/{id}/`）
                打 `deleted_at`（软删），默认查询**不再返回**它，
                且**写一条 `knowledge_entry.delete` 审计**。
                语义是**老师认为这条录错了**，是数据层面的移除；
                软删只为可追溯与恢复，不表示它是「另一种归档」。

        ★ 归档**不做**依赖校验（Q7）——它不改变条目内容与关联，
          只改状态。要改内容用 `PATCH`，那才过闸门。
        """
        entry = KnowledgeEntry.objects.for_org(org_id).filter(pk=entry_id).first()
        if entry is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="条目不存在")
        entry.status = "ARCHIVED"
        entry.save(update_fields=["status", "updated_at"])
        return entry

    # ── 更新 / 删除（需求 3 · P1-16b）──

    @staticmethod
    @transaction.atomic
    def update_entry(
        *,
        org_id: int,
        entry_id: int,
        payload: dict[str, Any],
        operator_id: int | None = None,
    ) -> KnowledgeEntry:
        """更新知识库条目（`PATCH /api/v1/knowledge/entries/{id}/`）。

        ★ 依赖校验（Q7，见类 docstring）：**只要该条目关联的知识点里
          有任何一个下有题，就拒绝更新**。这里刻意从严：

            改 `node_ids` 时——**新、旧全部**知识点一起过闸门，
            不只是「新增的那几个」。落地就是取**并集**
            `set(旧关联) | set(新关联)`（见实现里的 ③）。

          理由：新增关联的确会让「有点在练」的知识点被写进来，必须拒；
          但**保留**一个已有题的知识点关联同样是「正在动依据」——
          这条接口还能顺手改 `status`（比如先降到 DRAFT 再删），
          如果只在「新增」时校验，就等于留了一条
          先 `PATCH node_ids=[自由点]` 再删的绕道。

        ★ 契约：
          - `payload` 只允许 `EDITABLE_FIELDS` 里的键；其余键报 400 并列出，
            不静默忽略（外键归属与统计字段不许改）
          - `node_ids` 传了就**整体替换**关联；替换后为空 → 400
          - `node_ids` 里的知识点必须属于本机构，否则 400
          - 成功写 `audit_log.record_strict("knowledge_entry.update", ...)`，
            `before` / `after` 都是 `entry_snapshot`
        """
        entry = KnowledgeEntry.objects.for_org(org_id).filter(pk=entry_id).first()
        if entry is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="条目不存在")

        # ① 只接受可编辑字段（显式拒绝，不静默忽略）
        unknown = sorted(set(payload) - KnowledgeEntryService.EDITABLE_FIELDS)
        if unknown:
            raise ValidationFailed(
                detail=(
                    "以下字段不可修改（外键归属与统计字段由系统维护）："
                    + "、".join(unknown)
                ),
                extra={"read_only_fields": unknown},
            )
        if not payload:
            raise ValidationFailed(detail="没有需要更新的字段")

        before = KnowledgeEntryService.entry_snapshot(entry)
        # ★ 旧关联集合（来自快照，不是 payload）：闸门要拿它和**新集合**
        #   一起判，见下面 ③ 的注释。
        old_node_ids = list(before["node_ids"])

        # ② 目标关联 = 传了就整体替换，没传就沿用现有的
        if "node_ids" in payload:
            raw = payload["node_ids"]
            node_ids = list(dict.fromkeys(int(i) for i in (raw or [])))
            if not node_ids:
                raise ValidationFailed(
                    detail="必须至少关联一个知识点（不挂知识点的条目检索不到）"
                )
            found = set(
                KnowledgeNode.objects.for_org(org_id)
                .filter(id__in=node_ids)
                .values_list("id", flat=True)
            )
            missing = [i for i in node_ids if i not in found]
            if missing:
                raise ValidationFailed(
                    detail=f"存在无效的知识点 ID：{missing}",
                    extra={"invalid_node_ids": missing},
                )
        else:
            node_ids = before["node_ids"]

        # ③ 依赖校验闸门：★ **旧关联 ∪ 新关联** 一起过（不是只过新集合）。
        #
        # ★★ 为什么必须是并集（这条以前只判新集合，是**实测复现过的绕过**）：
        #   条目同时挂 [无题点 A, 有题点 B] 时，
        #   `PATCH {"node_ids": [A]}` 的被拒理由是「B 正在被练，不许动它的
        #   依据」；若只判新集合（= {A}），这次 PATCH 会 **200 并摘掉 B 的
        #   关联行** → 条目不再挂 B → 紧接着 `DELETE` 也不再被拦（那时它只
        #   挂 A）——等于用「先改挂、再删除」两步把 B 的 AI 生题依据删掉。
        #   并集把这条绕道的**第一步**就堵死：只要动作前后**任一时刻**该条目
        #   挂着有题的知识点，就拒。
        #
        # ★ 并集**不会**改变既有行为的两种情形（刻意保证）：
        #   - 没传 `node_ids`：新集合 = 旧集合，并集 = 旧集合（与改前一致）；
        #   - 传了但与原集合相同：同理。
        #   所以「正常编辑」（关联不变、知识点无题）仍照旧 200，
        #   只有「把有题的知识点摘掉」这类**以前被漏判**的路径才新增 409。
        gate_ids = sorted(set(old_node_ids) | set(int(i) for i in node_ids))
        KnowledgeEntryService.assert_entry_modifiable(
            org_id=org_id, node_ids=gate_ids, action="修改该知识库条目"
        )

        # ④ 落库
        scalar_fields = [
            k for k in payload
            if k != "node_ids" and k in KnowledgeEntryService.EDITABLE_FIELDS
        ]
        for field in scalar_fields:
            setattr(entry, field, payload[field])
        if scalar_fields:
            entry.save(update_fields=[*scalar_fields, "updated_at"])

        if "node_ids" in payload:
            KnowledgeEntryNode.objects.for_org(org_id).filter(
                entry_id=entry.id
            ).delete()
            for nid in node_ids:
                KnowledgeEntryNode.objects.create(
                    org_id=org_id, entry=entry, node_id=nid,
                    weight=1.0, source="TEACHER",
                )
            # ★ `course` / `chapter` 是**冗余归属**（由知识点反推），
            #   关联变了就要跟着走，否则列表按课程/章筛选会把条目放错地方。
            first = KnowledgeNode.objects.for_org(org_id).filter(
                pk=node_ids[0]
            ).select_related("task__chapter").first()
            if first is not None:
                course_id = first.course_id
                chapter_id = first.task.chapter_id if first.task else None
                if (entry.course_id, entry.chapter_id) != (course_id, chapter_id):
                    entry.course_id = course_id
                    entry.chapter_id = chapter_id
                    entry.save(update_fields=["course", "chapter", "updated_at"])

        entry.refresh_from_db()

        # ⑤ 审计（合规要求：失败要抛出，不能静默算了）
        after = KnowledgeEntryService.entry_snapshot(entry)
        audit_log.record_strict(
            # ★ 动作码沿用既有 `knowledge_entry.*` 命名（与
            #   `AuditAction.KNOWLEDGE_ENTRY_DELETE` 同族）。
            #   注意：`AuditAction` 的 choices 里目前只有 `.delete`，
            #   `.update` 作为常量**不注册**——与既有 `question.update`
            #   / `paper.compose` / `asset.create` 的做法一致（那些
            #   动作码也都只存在于服务层字面量里，不在 choices 内）。
            "knowledge_entry.update",
            entry,
            operator_id,
            org_id=org_id,
            before=before,
            after=after,
            reason=f"PATCH 字段：{'、'.join(sorted(payload))}",
        )
        return entry

    @staticmethod
    @transaction.atomic
    def delete_entry(
        *,
        org_id: int,
        entry_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> KnowledgeEntry:
        """删除知识库条目（`DELETE /api/v1/knowledge/entries/{id}/`）。

        ★ 依赖校验（Q7）：关联的**任一**知识点下有题即拒，
          错误信息里列出有题的知识点编码 + 名称 + 题数。

        ★ **软删**（`KnowledgeEntry` 是 `SoftDeleteModel`）：
          打 `deleted_at`，行仍在库里（`_base_manager` 可见），
          默认查询与检索不再返回它。保留可追溯记录的理由：

            1. 历史 `ai_call_log.kb_entry_ids` 会引用它，硬删会让
               「这次 AI 生题的依据是什么」永久无法回答；
            2. 老师误删后可以恢复（清空 `deleted_at` 即可）；
            3. 审计里的 `before` 快照与这一行互相印证。

        ★ 成功写 `audit_log.record_strict("knowledge_entry.delete",
          entry, operator_id, before=快照, after=None, reason=...)`
          —— `after` 为 `None` 是「已删除」的明确表达。

        ★ **幂等**：已软删的条目再次 `DELETE` 直接返回，**不重复写审计**。
          否则网络重试会灌出一串重复的删除审计，噪音掩盖真实操作；
          而「已删除」这个事实第一次就已经记清了。

        ★★ 与 `archive_entry` 的区别见 `archive_entry` docstring
          ——归档是「暂时不用」（可逆，不写审计），
          删除是「这条录错了」（默认不可见，必须留审计）。
        """
        # ★ 用 `_base_manager` 取：要能看见**已软删**的行才能做到幂等，
        #   否则重复删除会变成 404，让调用方以为「没删成功」而反复重试。
        entry = (
            KnowledgeEntry._base_manager
            .filter(pk=entry_id, org_id=org_id)
            .first()
        )
        if entry is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="条目不存在")

        if entry.deleted_at is None:
            # ① 依赖校验（按知识点整体判断）
            node_ids = [
                link.node_id for link in entry.entry_links.all() if link.node_id
            ]
            KnowledgeEntryService.assert_entry_modifiable(
                org_id=org_id, node_ids=node_ids, action="删除该知识库条目"
            )

            before = KnowledgeEntryService.entry_snapshot(entry)

            # ② 软删（`SoftDeleteModel.delete()` 打 `deleted_at`）
            entry.delete()

            # ③ 审计：before 有、after 明确为 None
            audit_log.record_strict(
                "knowledge_entry.delete",
                entry,
                operator_id,
                org_id=org_id,
                before=before,
                after=None,
                reason=reason or "删除知识库条目（软删）",
            )

        entry.refresh_from_db()
        return entry
