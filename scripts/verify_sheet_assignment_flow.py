"""作业绑定 + 扫描写回 + 数据合并 · 集成验证。

运行：
    python scripts/verify_sheet_assignment_flow.py

覆盖验收 5：
  · 未绑卡不能发布
  · 扫描写回后客观题分数正确
  · 同题线上 + 线下并存时**线下（扫描）生效**，且保留历史记录

★ 使用真实 DB 对象，但整个流程包在事务里，结束时回滚——
  不污染开发库（对象存储里的测试文件会留下，可忽略）。
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, r"D:\tiku\server")
# 本机 Django 只在 .devdeps 里；追加到末尾，避免覆盖系统 numpy/cv2（cp314）。
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402
from datetime import timedelta  # noqa: E402
from django.db import transaction  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.response.models import Response  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402
from apps.sheet.services import BindingService, SheetStyleService, TemplateService  # noqa: E402
from apps.omr.scan_service import import_uploads, recognize_batch  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SCALE = 300 / 25.4


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _pick_questions(org_id: int):
    sc = list(
        Question.objects.for_org(org_id)
        .filter(type="SINGLE_CHOICE", status="APPROVED")
        .annotate(n=Count("options")).filter(n=4)[:2]
    )
    tf = Question.objects.for_org(org_id).filter(
        type="TRUE_FALSE", status="APPROVED").first()
    fill = None
    for q in Question.objects.for_org(org_id).filter(
        type="FILL_BLANK", status="APPROVED")[:80]:
        ans = q.answer or {}
        if isinstance(ans.get("blanks"), list) and len(ans["blanks"]) == 2:
            fill = q
            break
    return sc, tf, fill


def _make_paper(org_id: int, course_id: int, teacher_id: int,
                questions) -> tuple[Paper, list[PaperItem]]:
    sc, tf, fill = questions
    paper = PaperService.create_paper(
        org_id=org_id, course_id=course_id,
        title=f"答题卡验收卷-{uuid.uuid4().hex[:6]}",
        purpose="QUIZ", created_by_id=teacher_id,
    )
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[
            {"question_id": sc[0].id, "score": 2},
            {"question_id": sc[1].id, "score": 2},
            {"question_id": tf.id, "score": 2},
            {"question_id": fill.id, "score": 4},
        ],
        operator_id=teacher_id, allow_unapproved=False,
    )
    PaperService.finalize_paper(
        org_id=org_id, paper_id=paper.id, operator_id=teacher_id, force=True)
    paper.refresh_from_db()
    items = list(PaperItem.objects.for_org(org_id)
                 .filter(paper=paper).order_by("seq"))
    return paper, items


def _make_sections() -> list[dict]:
    return [
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 2, "per_row": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 1, "per_row": 5, "options": 2, "score": 2},
        {"order": 3, "type": "FILL_BLANK", "title": "三、填空题",
         "count": 1, "score": 4,
         "items": [{"blanks": 2, "chars": 6, "per_row": 2,
                    "blank_scores": [2, 2]}]},
    ]


def _make_template(org_id: int, name: str, sections: list[dict]):
    style = SheetStyleService.create_style(
        org_id=org_id, data={
            "name": f"{name}-页面", "page_size": "A4",
            "title_text": name,
        })
    tpl = TemplateService.create_draft(
        org_id=org_id, name=name, sections=sections, style_id=style.id)
    return TemplateService.publish(org_id=org_id, template_id=tpl.id)


def _raster_fill(layout: dict, answers: dict[int, str],
                 student_no: str) -> bytes:
    from apps.sheet.render import render_sheet_pdf

    pdf = render_sheet_pdf(layout)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=300)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    doc.close()
    img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    page = layout["pages"][0]
    for q in page.get("objectives") or []:
        label = answers.get(int(q["seq"]))
        if not label:
            continue
        o = next((x for x in q["options"] if x["label"] == label), None)
        if o is None:
            continue
        cv2.rectangle(
            img,
            (int((o["x_mm"] - o["w_mm"] * 0.42) * SCALE),
             int((o["y_mm"] - o["h_mm"] * 0.42) * SCALE)),
            (int((o["x_mm"] + o["w_mm"] * 0.42) * SCALE),
             int((o["y_mm"] + o["h_mm"] * 0.42) * SCALE)),
            (0, 0, 0), -1)
    for pos, d in zip((layout.get("student_no") or {}).get("positions") or [],
                      student_no):
        b = next((x for x in pos["bubbles"] if x["value"] == d), None)
        if b is None:
            continue
        cv2.rectangle(
            img,
            (int((b["x_mm"] - b["w_mm"] * 0.4) * SCALE),
             int((b["y_mm"] - b["h_mm"] * 0.4) * SCALE)),
            (int((b["x_mm"] + b["w_mm"] * 0.4) * SCALE),
             int((b["y_mm"] + b["h_mm"] * 0.4) * SCALE)),
            (0, 0, 0), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def main() -> int:
    org = Org.objects.first()
    teacher = User._base_manager.filter(
        org=org, role__in=["TEACHER", "ORG_ADMIN", "PLATFORM_ADMIN"]).first() \
        or User._base_manager.filter(org=org).first()
    course = Course.objects.for_org(org.id).first()
    if not org or not teacher or not course:
        print("缺少基础数据（org / teacher / course）")
        return 1

    sc, tf, fill = _pick_questions(org.id)
    if not (sc and tf and fill):
        print("题库缺少可用题目（需 2 道 4 选项单选 + 判断 + 2 空填空）")
        return 1

    code = ZfError
    tag = uuid.uuid4().hex[:6]
    try:
        with transaction.atomic():
            print("\n【F1】建卷 / 建卡 / 绑定")
            paper, items = _make_paper(
                org.id, course.id, teacher.id, (sc, tf, fill))
            check("试卷已定稿且 4 题", paper.status == "FINALIZED"
                  and len(items) == 4, f"{paper.status}/{len(items)}")
            tpl = _make_template(org.id, f"验收卡-{tag}", _make_sections())
            check("答题卡发布为 ACTIVE",
                  tpl.status == "ACTIVE" and tpl.page_count >= 1,
                  f"{tpl.status}/{tpl.page_count}")
            unbound_tpl = _make_template(
                org.id, f"验收卡-未绑-{tag}", _make_sections())
            binding = BindingService.bind(
                org_id=org.id, paper_id=paper.id, template_id=tpl.id,
                operator_id=teacher.id)
            check("绑定成功且校验通过",
                  binding.validation_result.get("ok") is True,
                  str(binding.validation_result.get("blocking"))[:100])

            print("\n【F2】未绑卡不能发布")
            room = Classroom.objects.create(
                org=org, name=f"验收班-{tag}", grade="2026",
                academic_year="2026-2027")
            stu = User._base_manager.filter(
                org=org, role="STUDENT").first()
            ClassroomStudent.objects.create(
                org=org, classroom=room, student=stu,
                student_no="20265001", name="验收学生")
            from apps.sheet.models import PaperSheetBinding
            draft_unbound = AssignmentService.create_assignment(
                org_id=org.id, paper_id=paper.id, classroom_id=room.id,
                title=f"未绑卡建作业-{tag}",
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=2),
                sheet_template_id=unbound_tpl.id,
                created_by_id=teacher.id,
            )
            check("新建作业选未绑卡 → 直接绑定成功",
                  draft_unbound.sheet_template_id == unbound_tpl.id
                  and PaperSheetBinding.objects.for_org(org.id).filter(
                      paper_id=paper.id,
                      sheet_template_id=unbound_tpl.id,
                      is_primary=True).exists())
            draft = AssignmentService.create_assignment(
                org_id=org.id, paper_id=paper.id, classroom_id=room.id,
                title=f"验收作业-{tag}",
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=2),
                created_by_id=teacher.id,
            )
            check("★ 建作业不再自动带出答题卡（绑定只在作业详情）",
                  draft.sheet_template_id is None,
                  str(draft.sheet_template_id))
            # 人为清空绑定后再发布
            draft.sheet_template = None
            draft.save(update_fields=["sheet_template", "updated_at"])
            BindingService.unbind(
                org_id=org.id, paper_id=paper.id, unbind_all=True)
            blocked = False
            try:
                AssignmentService.publish(
                    org_id=org.id, assignment_id=draft.id,
                    operator_id=teacher.id)
            except ZfError as exc:
                blocked = exc.zf_code == "SHEET.BIND_MISMATCH"
            check("未绑卡发布被阻断（SHEET.BIND_MISMATCH）", blocked)
            # ★ 作业页唯一入口：即便这个卡从没绑过这份试卷，也能一次写两处。
            AssignmentService.bind_sheet(
                org_id=org.id, assignment_id=draft.id,
                template_id=unbound_tpl.id, operator_id=teacher.id)
            draft.refresh_from_db()
            from apps.sheet.models import PaperSheetBinding
            check("未绑过的卡可直接在作业页绑定（写作业字段）",
                  draft.sheet_template_id == unbound_tpl.id,
                  str(draft.sheet_template_id))
            check("同一服务同时 upsert 了试卷绑定",
                  PaperSheetBinding.objects.for_org(org.id).filter(
                      paper_id=paper.id,
                      sheet_template_id=unbound_tpl.id,
                      is_primary=True).exists())
            # ★ 需求 3：校验通过即锁定 —— 想换**另一张**卡必须 409。
            #   判据是现算的（有绑定 + 重跑校验仍 ok），没有新增字段。
            locked_code = ""
            try:
                AssignmentService.bind_sheet(
                    org_id=org.id, assignment_id=draft.id,
                    template_id=tpl.id, operator_id=teacher.id)
            except ZfError as exc:
                locked_code = exc.zf_code
            check("★ 已校验通过的绑定不能再换卡（SHEET.BIND_LOCKED）",
                  locked_code == "SHEET.BIND_LOCKED", locked_code)
            # 不匹配的卡：必须阻断并返回差异清单
            # ★ 用**未绑卡**的新作业来验：已锁定的作业会先被锁拦住，
            #   那样就验不到「差异清单」这条了。
            bad_tpl = _make_template(org.id, f"验收卡-不匹配-{tag}", [{
                "order": 1, "type": "SINGLE_CHOICE", "title": "少题",
                "count": 1, "options": 4, "score": 2,
            }])
            draft_bad = AssignmentService.create_assignment(
                org_id=org.id, paper_id=paper.id, classroom_id=room.id,
                title=f"验收作业-不匹配卡-{tag}",
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=2),
                created_by_id=teacher.id,
            )
            mismatch = None
            try:
                AssignmentService.bind_sheet(
                    org_id=org.id, assignment_id=draft_bad.id,
                    template_id=bad_tpl.id, operator_id=teacher.id)
            except ZfError as exc:
                mismatch = exc.extra.get("validation") if exc.extra else None
            check("不匹配卡绑定被阻断且带差异清单",
                  bool(mismatch and mismatch.get("blocking")),
                  str((mismatch or {}).get("blocking", [])[:1]))
            # ★ 考号位数必须与班级名册一致
            from copy import deepcopy
            from apps.common.exceptions import ValidationFailed
            style6 = SheetStyleService.create_style(
                org_id=org.id, data={
                    "name": f"6位考号-{tag}", "page_size": "A4",
                    "info_config": {
                        **deepcopy(unbound_tpl.style.info_config),
                        "student_no": {
                            **deepcopy(unbound_tpl.style.info_config[
                                "student_no"]), "digits": 6, "per_row": 6,
                        },
                    },
                })
            tpl6 = TemplateService.create_draft(
                org_id=org.id, name=f"6位卡-{tag}",
                sections=_make_sections(), style_id=style6.id)
            tpl6 = TemplateService.publish(
                org_id=org.id, template_id=tpl6.id)
            # ★ 同样用未绑卡的新作业：已锁定的作业换卡会先 409
            draft_6 = AssignmentService.create_assignment(
                org_id=org.id, paper_id=paper.id, classroom_id=room.id,
                title=f"验收作业-6位卡-{tag}",
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=2),
                created_by_id=teacher.id,
            )
            msg6 = ""
            try:
                AssignmentService.bind_sheet(
                    org_id=org.id, assignment_id=draft_6.id,
                    template_id=tpl6.id, operator_id=teacher.id)
            except ValidationFailed as exc:
                msg6 = str(exc)
            check("考号位数不一致 → 阻断并提示位数",
                  "6 位" in msg6 and "8 位" in msg6, msg6)
            # 名册混合长度 → 阻断并列出异常学号
            stu2 = User._base_manager.filter(
                org=org, role="STUDENT").exclude(pk=stu.pk).first()
            extra = ClassroomStudent.objects.create(
                org=org, classroom=room, student=stu2,
                student_no="123", name="异常学号")
            msg_mix = ""
            try:
                AssignmentService.bind_sheet(
                    org_id=org.id, assignment_id=draft.id,
                    template_id=unbound_tpl.id, operator_id=teacher.id)
            except ValidationFailed as exc:
                msg_mix = str(exc)
            check("名册学号混合长度 → 阻断并列出异常",
                  "长度不一致" in msg_mix
                  and ("123" in msg_mix or "20265001" in msg_mix),
                  msg_mix)
            extra.delete()
            # 恢复到匹配卡再发布
            AssignmentService.bind_sheet(
                org_id=org.id, assignment_id=draft.id,
                template_id=unbound_tpl.id, operator_id=teacher.id)
            AssignmentService.publish(
                org_id=org.id, assignment_id=draft.id,
                operator_id=teacher.id)
            draft.refresh_from_db()
            check("绑定后发布成功", draft.status == "PUBLISHED", draft.status)

            print("\n【F3】扫描识别 → 写回 Response → 自动批改")
            layout = tpl.layout_json
            # ★ 故意做成「两题对、一题错」，这样能验证写回**内容**正确，
            #   而不只是"产生了 3 条评分"。
            correct: dict[int, str] = {}
            for it in items:
                q = it.question
                if q.type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
                    opts = list(q.options.all())
                    hit = next((o.label for o in opts if o.is_correct), "")
                    if hit:
                        correct[int(it.seq)] = hit
                elif q.type == "TRUE_FALSE":
                    ans = q.answer or {}
                    correct[int(it.seq)] = "A" if ans.get("value") is True \
                        else "B"
            answers: dict[int, str] = {}
            for q in layout["pages"][0].get("objectives") or []:
                seq = int(q["seq"])
                labels = [o["label"] for o in q["options"]]
                if seq == 3:
                    # 判断题故意涂错：正确为 A 就涂 B，反之涂 A
                    answers[seq] = "B" if correct.get(seq) == "A" else "A"
                elif seq in correct and correct[seq] in labels:
                    answers[seq] = correct[seq]
                else:
                    answers[seq] = labels[0]
            png = _raster_fill(layout, answers, "20265001")
            imp = import_uploads(
                org_id=org.id, assignment_id=draft.id,
                files=[(f"scan-{tag}.png", png)],
                operator_id=teacher.id)
            check("上传 1 页并建档", imp["pages"] == 1, str(imp["pages"]))
            res = recognize_batch(
                org_id=org.id, batch_id=imp["batch_id"])
            check("识别完成（auto_ok 1）", res["auto_ok"] == 1, str(res))

            from apps.omr.models import ScanCard

            card = ScanCard.objects.for_org(org.id).filter(
                scan_batch_id=imp["batch_id"]).first()
            check("卡片自动通过且归属学生",
                  card and card.status in ("AUTO_OK", "REVIEWED")
                  and card.student_id == stu.id,
                  f"{getattr(card, 'status', None)}/{getattr(card, 'student_id', None)}")
            check("页码识别为 1（页定位块）",
                  getattr(card, "page_no", None) == 1,
                  str(getattr(card, "page_no", None)))
            check("★ 识别成功后生成整页展示图 display_asset",
                  card.display_asset_id is not None,
                  str(card.display_asset_id))
            if card.display_asset_id:
                check("展示图尺寸为 300dpi A4 整页",
                      (card.display_asset.width, card.display_asset.height)
                      == (2480, 3508),
                      f"{card.display_asset.width}×"
                      f"{card.display_asset.height}")
                check("原图 raw_asset 仍保留",
                      card.raw_asset_id is not None, str(card.raw_asset_id))

            active = list(
                Response.objects.for_org(org.id).filter(
                    assignment_id=draft.id, student_id=stu.id,
                    superseded_by__isnull=True,
                )
            )
            check("扫描写回 3 道客观题",
                  len(active) == 3
                  and all(r.source == "SCAN_UPLOAD" for r in active),
                  f"{len(active)} 条：{[r.source for r in active]}")

            scores = list(ScoreResult.objects.for_org(org.id).filter(
                assignment_id=draft.id, student_id=stu.id))
            check("客观题已自动批改（3 条评分）",
                  len(scores) == 3, str(len(scores)))
            by_seq = {
                int(s.paper_item.seq): float(s.score) for s in scores
                if s.paper_item_id
            }
            check("客观题分数按答案正确计算（2 / 2 / 0）",
                  by_seq == {1: 2.0, 2: 2.0, 3: 0.0},
                  str(by_seq))
            check("客观题总分 = 4",
                  abs(sum(by_seq.values()) - 4.0) < 1e-9,
                  str(sum(by_seq.values())))

            print("\n【F4】线上 + 线下同题：线下生效、保留历史")
            r = next(
                (x for x in active
                 if int((x.answer_json or {}).get("sheet_seq") or 0) == 1),
                active[0],
            )
            before = Response.objects.for_org(org.id).filter(
                assignment_id=draft.id, student_id=stu.id,
                paper_item_id=r.paper_item_id).count()
            ResponseService.record_online(
                org_id=org.id, assignment_id=draft.id, student_id=stu.id,
                sheet_seq=1,
                marked_labels=["B"],
            )
            rows = list(Response.objects.for_org(org.id).filter(
                assignment_id=draft.id, student_id=stu.id,
                paper_item_id=r.paper_item_id))
            winner = next((x for x in rows if x.superseded_by_id is None), None)
            check("线上记录到达后，扫描仍是生效记录",
                  winner is not None and winner.source == "SCAN_UPLOAD",
                  str(getattr(winner, "source", None)))
            check("被覆盖的线上记录保留（历史 +1）",
                  len(rows) == before + 1,
                  f"{before} → {len(rows)}")
            check("存在 superseded 历史记录",
                  any(x.superseded_by_id is not None for x in rows))

            print("\n【F5】扫描兜底：作业字段为空 → 试卷主绑定并回写")
            legacy = AssignmentService.create_assignment(
                org_id=org.id, paper_id=paper.id, classroom_id=room.id,
                title=f"验收作业-旧字段-{tag}",
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=2),
                sheet_template_id=unbound_tpl.id,
                created_by_id=teacher.id,
            )
            legacy.sheet_template = None
            legacy.save(update_fields=["sheet_template", "updated_at"])
            png2 = _raster_fill(tpl.layout_json, {}, "20265001")
            imp2 = import_uploads(
                org_id=org.id, assignment_id=legacy.id,
                files=[(f"legacy-{tag}.png", png2)],
                operator_id=teacher.id)
            legacy.refresh_from_db()
            check("作业字段为空时按试卷主绑定兜底",
                  legacy.sheet_template_id == unbound_tpl.id,
                  str(legacy.sheet_template_id))
            check("兜底后回写 assignment.sheet_template",
                  imp2["pages"] == 1, str(imp2.get("pages")))
            # 彻底没有绑定时，报错文案指向作业详情页
            BindingService.unbind(
                org_id=org.id, paper_id=paper.id, unbind_all=True)
            legacy.sheet_template = None
            legacy.save(update_fields=["sheet_template", "updated_at"])
            from apps.common.exceptions import ValidationFailed
            msg = ""
            try:
                import_uploads(
                    org_id=org.id, assignment_id=legacy.id,
                    files=[(f"nobind-{tag}.png", png2)],
                    operator_id=teacher.id)
            except ValidationFailed as exc:
                msg = str(exc)
            check("彻底未绑定时报错指向作业详情页",
                  "作业详情页" in msg, msg)

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
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
