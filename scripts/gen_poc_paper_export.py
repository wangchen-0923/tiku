"""生成「试卷导出 v3」POC 样张（临时卷，跑完删除）。

样张包含：短选项（自动 4 列）、中等选项（自动 2 列）、长选项（自动 1 列）、
判断题、填空题、主观题、2 张配图、3 个空白块、分组汇总、考生信息 5 项。

产物（`docs/poc/试卷导出v3/`）：

    ｜ 样卷-A4.pdf / .docx
    ｜ 样卷-答案.pdf               —— 独立答案 PDF
    ｜ layout.json                 —— 预览 / PDF / Word 共用
    ｜ page-01.png …               —— 300dpi 截图
    ｜ 文字坐标.txt / 一致性核对.txt / Word校验.txt / 导出告警.txt

★★ 临时卷 / 临时配图在 finally 删除。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from PIL import Image, ImageDraw  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.common.constants import FigureReviewStatus  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.media.services import MediaService, get_backend  # noqa: E402
from apps.paper.export import (  # noqa: E402
    export_answer_pdf,
    export_docx,
    export_pdf,
    preview_layout_json,
)
from apps.paper.models import Paper  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question, QuestionImage  # noqa: E402

ORG_ID = 1
POC_TITLE = "__POC_试卷导出v3__"
OUT = Path(r"D:\tiku\docs\poc\试卷导出v3")
PY = sys.executable

CFG = {
    "page_size": "A4",
    "orientation": "PORTRAIT",
    "margin_mm": 20,
    "line_spacing": 1.5,
    "base_font_pt": 10.5,
    "title_font_pt": 16,
    "title_text": "2026 秋季《机械基础》期中考试（v3.1 样卷）",
    "show_title": True,
    "student_fields": ["学号", "座号", "班级", "姓名", "得分"],
    "student_line_extra": "考场：第三考场",
    "notes_text": (
        "注意事项：\n"
        "1. 请用黑色签字笔在答题卡上作答，卷面作答无效。\n"
        "2. 考试时间 90 分钟，满分 100 分。\n"
        "3. 作图题请使用铅笔，保持卷面整洁。"
    ),
    "show_notes": True,
    "show_page_number": True,
    "page_number_align": "center",
    "answer_bracket": True,
    "show_score": True,
    "option_font_pt": 12,
    "option_cols_by_seq": {},
    "blank_blocks": [
        {"id": "b-after-5", "after_seq": 5, "height_mm": 30},
        {"id": "b-after-12", "after_seq": 12, "height_mm": 25},
        {"id": "b-end", "after_seq": 31, "height_mm": 40},
    ],
}


def make_png(kind: int) -> bytes:
    """400×300 / 200dpi 示意图（50.8×38.1mm）。"""
    im = Image.new("RGB", (400, 300), (245, 248, 252))
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, 390, 290], outline=(70, 90, 120), width=3)
    if kind == 1:
        d.ellipse([90, 60, 310, 240], outline=(30, 90, 170), width=6)
        d.ellipse([140, 110, 260, 190], outline=(30, 90, 170), width=5)
        for i in range(8):
            import math
            a = i * math.pi / 4
            d.line([200 + 110 * math.cos(a), 150 + 90 * math.sin(a),
                    200 + 142 * math.cos(a), 150 + 117 * math.sin(a)],
                   fill=(30, 90, 170), width=7)
    else:
        d.rectangle([70, 170, 330, 210], fill=(205, 220, 240),
                    outline=(30, 90, 170), width=4)
        d.polygon([(200, 45), (310, 165), (90, 165)],
                  outline=(180, 60, 50), width=5)
    buf = io.BytesIO()
    im.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def _q_ids(qs) -> list[int]:
    return [q.id for q in qs]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    admin = User.objects.filter(username="admin").first()
    admin_id = admin.id if admin else None

    for old in Paper.objects.for_org(ORG_ID).filter(title=POC_TITLE):
        old.hard_delete()

    from django.db.models import Count, Max
    from django.db.models.functions import Length

    short_qs = list(
        Question.objects.for_org(ORG_ID)
        .filter(type="SINGLE_CHOICE", status="APPROVED")
        .annotate(_ml=Max(Length("options__content_text")),
                  _nc=Count("options"))
        .filter(_nc__gte=4, _ml__lte=10).order_by("id")[:10])
    if not short_qs:
        print("题库不足：没有短选项选择题")
        return 2
    course_id = short_qs[0].course_id

    def pick(ml_min, ml_max, n):
        return list(
            Question.objects.for_org(ORG_ID)
            .filter(type="SINGLE_CHOICE", status="APPROVED",
                    course_id=course_id)
            .annotate(_ml=Max(Length("options__content_text")),
                      _nc=Count("options"))
            .filter(_nc__gte=4, _ml__gte=ml_min, _ml__lte=ml_max)
            .exclude(id__in=_q_ids(short_qs))
            .order_by("id")[:n])

    medium_qs = pick(11, 22, 5)
    long_qs = pick(31, 80, 5)
    tf_qs = list(
        Question.objects.for_org(ORG_ID)
        .filter(type="TRUE_FALSE", status="APPROVED", course_id=course_id)
        .order_by("id")[:5])
    fill_qs = list(
        Question.objects.for_org(ORG_ID)
        .filter(type="FILL_BLANK", status="APPROVED", course_id=course_id)
        .order_by("id")[:3])
    subj_qs = list(
        Question.objects.for_org(ORG_ID)
        .filter(type="SHORT_ANSWER", status="APPROVED", course_id=course_id)
        .order_by("id")[:3])
    temp_qids: list[int] = []
    if not subj_qs:
        # ★ 题库里没有简答题：造 3 道临时题（finally 删除），否则样张缺题型
        from apps.questionbank.services import QuestionService

        for i in range(1, 4):
            q = QuestionService.create_question(
                org_id=ORG_ID, course_id=course_id,
                question_type="SHORT_ANSWER",
                stem_text=f"试述齿轮传动中模数与齿数的关系（第 {i} 问）。",
                answer={"text": "模数×齿数=分度圆直径；齿数过少会根切。"},
                created_by_id=admin_id, status="APPROVED",
                check_duplicate=False)
            temp_qids.append(q.id)
        subj_qs = list(
            Question.objects.for_org(ORG_ID)
            .filter(id__in=temp_qids).order_by("id"))
    choice_qs = short_qs + medium_qs + long_qs
    print("题目：", f"选择={len(choice_qs)} 判断={len(tf_qs)} "
          f"填空={len(fill_qs)} 简答={len(subj_qs)}")
    if len(choice_qs) < 20 or not tf_qs or not fill_qs or not subj_qs:
        print("题库不足，无法生成完整样张")
        return 2

    paper = None
    img_ids: list[int] = []
    asset_ids: list[int] = []
    try:
        # 2 张配图挂到前两道选择题
        for idx, q in enumerate(choice_qs[:2], 1):
            data = make_png(idx)
            asset = MediaService.upload_directly(
                org_id=ORG_ID, kind="QUESTION_FIGURE",
                filename=f"poc-v3-fig-{idx}.png", data=data,
                mime="image/png", created_by_id=admin_id,
                width=400, height=300, dpi=200)
            asset_ids.append(asset.id)
            im = QuestionImage.objects.create(
                org_id=ORG_ID, question=q, asset=asset,
                attach_point="STEM", seq_in_question=idx,
                caption=f"图 {idx} 齿轮传动示意图",
                src_path="MANUAL",
                review_status=FigureReviewStatus.CONFIRMED,
                confirm_method="MANUAL_CAPTURE")
            img_ids.append(im.id)

        paper = PaperService.create_paper(
            org_id=ORG_ID, course_id=course_id, title=POC_TITLE,
            purpose="EXAM", compose_mode="MANUAL",
            layout_config=CFG, created_by_id=admin_id)

        PaperService.add_questions(
            org_id=ORG_ID, paper_id=paper.id,
            items=[{"question_id": q.id, "score": 3,
                    "section_title": "一、单项选择题"} for q in choice_qs],
            section_title="一、单项选择题", operator_id=admin_id,
            allow_unapproved=True)
        PaperService.add_questions(
            org_id=ORG_ID, paper_id=paper.id,
            items=[{"question_id": q.id, "score": 2,
                    "section_title": "二、判断题"} for q in tf_qs],
            section_title="二、判断题", operator_id=admin_id,
            allow_unapproved=True)
        PaperService.add_questions(
            org_id=ORG_ID, paper_id=paper.id,
            items=[{"question_id": q.id, "score": 4,
                    "section_title": "三、填空题"} for q in fill_qs],
            section_title="三、填空题", operator_id=admin_id,
            allow_unapproved=True)
        PaperService.add_questions(
            org_id=ORG_ID, paper_id=paper.id,
            items=[{"question_id": q.id, "score": 10,
                    "section_title": "四、简答题"} for q in subj_qs],
            section_title="四、简答题", operator_id=admin_id,
            allow_unapproved=True)
        paper.refresh_from_db()

        # 1 道长选项题手动 2 列，演示「手动指定不降级、按列宽折行」
        if long_qs:
            seq = next((it.seq for it in paper.items.all()
                        if it.question_id == long_qs[0].id), None)
            if seq:
                CFG["option_cols_by_seq"] = {str(seq): 2}
                Paper.objects.filter(pk=paper.id).update(layout_config=CFG)

        pdf, iss1 = export_pdf(paper, layout_config=CFG)
        docx, iss2 = export_docx(paper, layout_config=CFG)
        ans, iss3 = export_answer_pdf(paper, layout_config=CFG)
        lay = preview_layout_json(paper, layout_config=CFG)
        if not pdf or not docx or not ans:
            print("导出为空：", iss1, iss2, iss3)
            return 3

        pdf_path = OUT / "样卷-A4.pdf"
        docx_path = OUT / "样卷-A4.docx"
        ans_path = OUT / "样卷-答案.pdf"
        pdf_path.write_bytes(pdf)
        docx_path.write_bytes(docx)
        ans_path.write_bytes(ans)
        (OUT / "layout.json").write_text(
            json.dumps(lay, ensure_ascii=False, indent=1), encoding="utf-8")
        _all_issues = list(iss1) + list(iss2) + list(iss3)
        _seen: set[tuple[str, str]] = set()
        _lines: list[str] = []
        for i in _all_issues:
            key = (str(i.get("code")), str(i.get("message")))
            if key in _seen:
                continue
            _seen.add(key)
            _lines.append(
                f"[{'阻断' if i.get('blocking') else '告警'}] "
                f"{i.get('code')} {i.get('message')}")
        (OUT / "导出告警.txt").write_text("\n".join(_lines),
                                         encoding="utf-8")

        import pymupdf

        doc = pymupdf.open(str(pdf_path))
        for i, page in enumerate(doc):
            page.get_pixmap(dpi=300).save(str(OUT / f"page-{i + 1:02d}.png"))
        doc.close()
        ad = pymupdf.open(str(ans_path))
        for i, page in enumerate(ad):
            page.get_pixmap(dpi=300).save(
                str(OUT / f"answer-{i + 1:02d}.png"))
        ad.close()

        diag = subprocess.run(
            [PY, r"D:\tiku\scripts\diag_pdf_text_pos.py", str(pdf_path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        (OUT / "文字坐标.txt").write_text(
            diag.stdout + "\n" + diag.stderr, encoding="utf-8")

        cons = subprocess.run(
            [PY, r"D:\tiku\scripts\diag_paper_layout_consistency.py",
             str(pdf_path), str(OUT / "layout.json")],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        (OUT / "一致性核对.txt").write_text(
            cons.stdout + "\n" + cons.stderr, encoding="utf-8")
        print(cons.stdout)
        print(f"样张：{pdf_path} / {docx_path} / {ans_path}")
        print(f"页数：{lay['page_count']}；选择题 {len(choice_qs)}；"
              f"判断 {len(tf_qs)}；填空 {len(fill_qs)}；"
              f"简答 {len(subj_qs)}")
        return 0 if cons.returncode == 0 else 4
    finally:
        if paper is not None:
            try:
                paper.hard_delete()
            except Exception as exc:  # noqa: BLE001
                print("删除临时卷失败：", exc)
        if img_ids:
            QuestionImage.objects.filter(id__in=img_ids).hard_delete()
        if temp_qids:
            for q in Question.objects.filter(id__in=temp_qids):
                q.hard_delete()
        if asset_ids:
            for a in list(Asset.objects.filter(id__in=asset_ids)):
                try:
                    get_backend().delete(a.oss_key)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    a.hard_delete()
                except Exception as exc:  # noqa: BLE001
                    print("删除临时配图失败：", exc)
        print("临时卷 / 临时配图已删除")


if __name__ == "__main__":
    sys.exit(main())
