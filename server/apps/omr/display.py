"""扫描页展示图与批改标注图。

★ display 图是**整页 300dpi 校正图**：0..page_width_mm × 0..page_height_mm。
  四角定位块必须完整保留——它们通常内缩 16mm、边长 6mm，
  外沿距纸边 13mm，满足「至少 5mm」的安全线。

★ 标注图分两层：

    1. **学生选中**的选项 → 半透明**填充**：
       绿色 = 选中且正确；红色 = 选中且错误。
    2. 答错时，把**正确答案所在的选项**用绿色**空心框**标出来。

  ★★ 正确答案必须用**空心框**，不能用填充：
     填充会盖住学生原来的笔迹，老师就看不出学生到底涂了什么——
     而「他涂了哪个、正确答案在哪」并列才是教师核对时需要的信息。
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

DISPLAY_DPI = 300
MM_PER_INCH = 25.4
DISPLAY_PX_PER_MM = DISPLAY_DPI / MM_PER_INCH
#: 四角定位块外沿距图像边缘的最小安全距离（规格 2.1）
MIN_ANCHOR_MARGIN_MM = 5.0
#: 标注色（BGR）
COLOR_CORRECT = (60, 180, 75)
COLOR_WRONG = (40, 40, 220)
#: 正确答案框的线宽（px）。空心框要够粗才在 300dpi 整页图上看得见，
#: 但又要细到不遮住框内的学生笔迹。
CORRECT_BOX_THICKNESS = 3


def page_of(layout: dict[str, Any], page_no: int | None) -> dict[str, Any]:
    pages = layout.get("pages") or []
    idx = max(1, int(page_no or 1)) - 1
    if idx >= len(pages):
        idx = 0
    return pages[idx] if pages else {}


def page_size_mm(layout: dict[str, Any]) -> tuple[float, float]:
    return (float(layout.get("page_width_mm") or 210.0),
            float(layout.get("page_height_mm") or 297.0))


def anchor_margin_mm(layout: dict[str, Any]) -> float:
    """四角锚点外沿到纸边的最小距离（mm）。

    规格要求 ≥5mm；标准 A4 模板锚点中心 16mm、边长 6mm → 13mm。
    """
    page_w, page_h = page_size_mm(layout)
    anchors = layout.get("anchors") or {}
    defaults = float(layout.get("anchor_size_mm") or 6.0)
    worst = math.inf
    for key in ("TL", "TR", "BL", "BR"):
        a = anchors.get(key)
        if not a:
            continue
        x = float(a.get("x_mm") or 0)
        y = float(a.get("y_mm") or 0)
        size = float(a.get("size_mm") or defaults)
        worst = min(worst, x - size / 2, y - size / 2,
                    page_w - x - size / 2, page_h - y - size / 2)
    return float(worst) if math.isfinite(worst) else 0.0


def validate_anchor_margins(layout: dict[str, Any]) -> None:
    margin = anchor_margin_mm(layout)
    if margin < MIN_ANCHOR_MARGIN_MM:
        raise ValueError(
            f"四角定位块外沿距图像边缘仅 {margin:.1f}mm，"
            f"低于安全线 {MIN_ANCHOR_MARGIN_MM:.0f}mm"
        )


def encode_png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:                       # pragma: no cover
        raise ValueError("PNG 编码失败")
    return buf.tobytes()


def _font(size: int):
    candidates = (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    from PIL import ImageFont

    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:      # noqa: BLE001
                continue
    return ImageFont.load_default()


def _draw_legend(image_bgr: np.ndarray, show_correct_box: bool = False) -> np.ndarray:
    """右下角图例（用 PIL 画中文）。

    ★ 文案随内容变：本页没有答错题时不写「绿框=正确答案位置」，
      否则老师会去找一个不存在的绿框。
    """
    from PIL import Image, ImageDraw

    h, w = image_bgr.shape[:2]
    size = max(14, min(28, w // 70))
    font = _font(size)
    text = ("绿=答对 红=答错 绿框=正确答案位置" if show_correct_box
            else "绿=答对 红=答错")
    pad = max(8, size // 2)
    try:
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:      # noqa: BLE001
        tw, th = len(text) * size, size
    box_w, box_h = tw + pad * 2, th + pad * 2
    x0, y0 = max(0, w - box_w - 12), max(0, h - box_h - 12)

    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (x0, y0, x0 + box_w, y0 + box_h), radius=6,
        fill=(255, 255, 255, 220), outline=(120, 120, 120, 220), width=1)
    draw.text((x0 + pad, y0 + pad), text, font=font, fill=(20, 20, 20, 255))
    out = Image.alpha_composite(img, overlay).convert("RGB")
    return cv2.cvtColor(np.array(out), cv2.COLOR_RGB2BGR)


def _labels_of(response) -> list[str]:
    if response is None:
        return []
    aj = response.answer_json or {}
    labels = aj.get("sheet_labels") or aj.get("original_labels") or []
    if labels:
        return [str(x).strip().upper() for x in labels]
    raw = response.answer_raw or response.answer_normalized or ""
    return [p.strip().upper() for p in str(raw).replace("，", ",").split(",")
            if p.strip()]


def answer_labels(answer: dict[str, Any] | None) -> list[str]:
    """标准答案 → 卷面选项字母。

    ★ 用 `normalize_choice` 归一，而不是直接拿字符串比：
      题库答案可能写成 `"A,C"` / `"AC"` / `["A","C"]`，卡面只认字母。
    """
    if not answer:
        return []
    raw = answer.get("value")
    if raw is None:
        return []
    from apps.grading.normalize import normalize_choice

    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        s = str(raw)
        parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
        if len(parts) == 1 and len(parts[0]) > 1:
            parts = list(parts[0])
    out: list[str] = []
    for p in parts:
        lab = normalize_choice(p)
        if lab and lab not in out:
            out.append(lab)
    return out


def annotate_page(
    image_bgr: np.ndarray,
    layout: dict[str, Any],
    *,
    page_no: int | None,
    responses_by_sheet_seq: dict[int, Any],
    scores_by_item_id: dict[int, Any],
    correct_by_item_id: dict[int, list[str]] | None = None,
) -> np.ndarray:
    """在整页校正图上画学生选项 + 答错题的正确答案位置。

    `responses_by_sheet_seq` 的键是**卷面题号**（layout objective.seq），
    与 `Response.answer_json["sheet_seq"]` 一致。

    `correct_by_item_id` 是 `{paper_item_id: ["A", ...]}` 的标准答案字母。
    缺省时退化为「只画学生选项」，不画正确答案框——旧调用方不必改。
    """
    out = image_bgr.copy()
    page = page_of(layout, page_no)
    page_w, page_h = page_size_mm(layout)
    h, w = out.shape[:2]
    sx = w / page_w if page_w else DISPLAY_PX_PER_MM
    sy = h / page_h if page_h else DISPLAY_PX_PER_MM
    overlay = out.copy()
    drew = 0
    #: 正确答案的空心框。**必须**在 `addWeighted` 之后画到 `out` 上：
    #: 画进 overlay 会被 0.4 的透明度削弱，绿色会变成灰绿，看不清。
    correct_frames: list[tuple[int, int, int, int]] = []
    correct_by_item_id = correct_by_item_id or {}

    for q in page.get("objectives") or []:
        seq = int(q.get("seq") or 0)
        resp = responses_by_sheet_seq.get(seq)
        selected = set(_labels_of(resp))
        score = (scores_by_item_id.get(resp.paper_item_id)
                 if resp is not None else None)
        # ★ 只标「有作答且有判定」的题：漏涂题连 Response 都没有，
        #   拿不到 paper_item_id，也就查不到分数与标准答案。
        #   漏涂题的「正确答案在哪」由图例之外的空白表达，不在这里硬凑。
        if score is None or score.is_correct is None:
            continue
        is_wrong = score.is_correct is False
        correct = (set(correct_by_item_id.get(resp.paper_item_id) or [])
                   if resp is not None else set())
        color = COLOR_CORRECT if score.is_correct else COLOR_WRONG

        for o in q.get("options") or []:
            label = str(o.get("label") or "").strip().upper()
            cx = float(o.get("x_mm") or 0) * sx
            cy = float(o.get("y_mm") or 0) * sy
            ow = float(o.get("w_mm") or 4.0) * sx
            oh = float(o.get("h_mm") or 3.0) * sy
            x0 = int(round(cx - ow / 2))
            y0 = int(round(cy - oh / 2))
            x1 = int(round(cx + ow / 2))
            y1 = int(round(cy + oh / 2))

            if label in selected:
                cv2.rectangle(overlay, (x0, y0), (x1, y1), color, -1)
                drew += 1
            elif is_wrong and label in correct:
                # 学生没选中，但这是正确答案 → 空心框
                correct_frames.append((x0, y0, x1, y1))

    if drew:
        cv2.addWeighted(overlay, 0.40, out, 0.60, 0, dst=out)
    for (x0, y0, x1, y1) in correct_frames:
        cv2.rectangle(out, (x0, y0), (x1, y1), COLOR_CORRECT,
                      CORRECT_BOX_THICKNESS)
    return _draw_legend(out, show_correct_box=bool(correct_frames))


def png_dimensions(data: bytes) -> tuple[int, int]:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图片解码失败")
    return int(img.shape[1]), int(img.shape[0])


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图片解码失败")
    return img


def image_bytes(image_bgr: np.ndarray, fmt: str = ".png") -> bytes:
    ext = fmt if fmt.startswith(".") else f".{fmt}"
    ok, buf = cv2.imencode(ext, image_bgr)
    if not ok:                       # pragma: no cover
        raise ValueError(f"图片编码失败：{ext}")
    return buf.tobytes()


def bytes_io(data: bytes):
    return io.BytesIO(data)
