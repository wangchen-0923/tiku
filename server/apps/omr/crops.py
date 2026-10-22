"""主观题切图服务（21 文档任务一 · 2.3）。

★★ 这个模块只做一件事：**把答题区从整页校正图上裁下来**。

  它不做识别、不做评分、不碰状态机。切图是「给老师省一次找位置」，
  所以它的失败必须**可降级**——切不出来不影响阅卷（前端回退整页图 +
  按 `rect_json` 高亮），只记告警。

★★ 几何的单一事实源是答题卡的 `layout_json`（`apps/sheet/layout.py` 产出）：

    pages[i].subjective[]   {seq, x_mm, y_mm, w_mm, h_mm, sub_blocks[]}
    pages[i].essays[]       {seq, x_mm, y_mm, start_row, end_row,
                             cell_mm, cell_gap_mm, cols}
    pages[i].fill_blanks[]  {seq, lines:[{y_mm, blanks:[{x1_mm,x2_mm}]}]}

  ★ 这里**不重新算排版**。一旦重算，切图位置就会与实际印出来的卡
    差半个字——而这类偏差在界面上看起来像「图片没对齐」，实际是
    「老师批的不是那个人写的那一题」。

★★ 幂等键 `(scan_card, paper_item, part_index)`：

  · 同模板版本重复调用 → 跳过（不算失败，计入 `skipped`）
  · 模板版本变了 → 先删旧切图再生成（版式变了，旧坐标就是错的）
  · `force=True` → 无条件重建
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from apps.common.constants import AssetKind
from apps.omr.models import ScanCard, SubjectiveCrop

logger = logging.getLogger(__name__)

#: 四周留白（mm）。★ 2mm 是「不把相邻题的笔迹框进来」与
#: 「不把本行末尾的字切掉」之间的折中，实测有效。
CROP_PAD_MM = 2.0

#: 填空题：从下划线（= 行 `y_mm`）往**上**取多高。
#:
#: ★★ 为什么单独给填空题一套纵向规则（作业板块优化 · 需求 6）：
#:
#:   `fill_blanks.lines[].y_mm` 是**画线的 y**，不是行的顶边。
#:   老师写字是压在线上、往上长；按「四周各 2mm」统一外扩，
#:   下边会多出 3.6mm 空白（够到下一行），上边只有 6.6mm
#:   （行距实测 8.30mm，距上一行的线只剩 1.7mm，上一行的字就被切进来）。
#:
#:   改成「下边贴线 + 上边按字高」后，切图恰好一行：
#:     bottom = y + 0.4   （线宽 0.5pt ≈ 0.18mm，不切线）
#:     top    = y - 7.6   （距上一行线 0.7mm，够容纳学生写满一行；
#:                          实测行距 8.30mm，再往上就会带上上一行的线）
#:
#:   ★ 6.0mm 试过：中职学生手写常超过 6mm，字顶会被切掉。
#:     7.6mm 是「装得下一个字」与「不串到上一行」之间的实测折中。
FILL_BLANK_ASCENT_MM = 7.6

#: 填空题：下划线**下方**留白。只为保住线本身，不框进下一行。
FILL_BLANK_DESCENT_MM = 0.4

#: 切图最小边长（px）。小于它说明坐标或资产尺寸有问题，记告警但不阻断。
CROP_MIN_PX = 80

#: 填空题的最小**高度**（px）。
#:
#: ★ 单行填空按「线下 0.4 + 线上 6.0」只有 6.4mm，300dpi 下 ≈ 76px，
#:   用 `CROP_MIN_PX` 判会每次都刷「切图过小」告警 —— 而它是正常几何。
#:   这里只判高度；宽度仍用 `CROP_MIN_PX`（太窄说明 x 坐标算错了）。
CROP_MIN_PX_FILL_BLANK = 40

#: 切图最大宽度（px）。超过则等比缩小——手机端看 300dpi 整宽图是浪费流量。
CROP_MAX_WIDTH = 2000

#: JPEG 质量
CROP_JPEG_QUALITY = 85

#: 客观题题型：这些不切图。
OBJECTIVE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
})

#: 告警码（与 21 文档 2.3 一致）
W_NO_DISPLAY = "PAPER.CROP_NO_DISPLAY"
W_TOO_SMALL = "PAPER.CROP_TOO_SMALL"
W_NO_GEOMETRY = "PAPER.CROP_NO_GEOMETRY"


# ══════════════════════════════════════════════════════════════
#  几何：layout_json → 矩形（mm）
# ══════════════════════════════════════════════════════════════


def _clamp_rect(
    rect: dict[str, float], page_w: float, page_h: float, *,
    pad_x: float = CROP_PAD_MM, pad_y: float = CROP_PAD_MM,
) -> dict[str, float]:
    """把矩形夹进页面范围，并按 `pad_x` / `pad_y` 往外留白。

    ★ 横向与纵向的留白**分开传**：填空题的下边必须贴着自己的下划线
      （见 `FILL_BLANK_ASCENT_MM` 的说明），统一 2mm 会把下一行框进来。
    """
    x = float(rect["x_mm"]) - pad_x
    y = float(rect["y_mm"]) - pad_y
    w = float(rect["w_mm"]) + pad_x * 2
    h = float(rect["h_mm"]) + pad_y * 2

    x = max(0.0, min(x, page_w))
    y = max(0.0, min(y, page_h))
    w = max(0.0, min(w, page_w - x))
    h = max(0.0, min(h, page_h - y))
    return {
        "x_mm": round(x, 2), "y_mm": round(y, 2),
        "w_mm": round(w, 2), "h_mm": round(h, 2),
    }


def _rect_of_answer(block: dict[str, Any]) -> dict[str, float] | None:
    """解答题：整块矩形（含小题分区）。"""
    if "x_mm" not in block or "w_mm" not in block:
        return None
    if not block.get("w_mm") or not block.get("h_mm"):
        return None
    return {
        "x_mm": float(block["x_mm"]), "y_mm": float(block["y_mm"]),
        "w_mm": float(block["w_mm"]), "h_mm": float(block["h_mm"]),
    }


def _rect_of_essay_part(part: dict[str, Any]) -> dict[str, float] | None:
    """作文：一段（一页上的若干行方格）。

    ★ 高度必须按 `end_row - start_row + 1` 算，不能用 `total_chars`：
      跨页时每页只有一部分行，用总字数算会把整页都框进去。
    """
    rows = int(part.get("end_row", 0)) - int(part.get("start_row", 0)) + 1
    cell = float(part.get("cell_mm") or 0)
    gap = float(part.get("cell_gap_mm") or 0)
    cols = int(part.get("cols") or 0)
    if rows <= 0 or cell <= 0 or cols <= 0:
        return None
    width = cell * cols + gap * (cols - 1)
    return {
        "x_mm": float(part["x_mm"]), "y_mm": float(part["y_mm"]),
        "w_mm": width, "h_mm": rows * (cell + gap),
    }


def _rect_of_fill_line(line: dict[str, Any]) -> dict[str, float] | None:
    """填空题：**单行**的外接矩形（纵向已含书写高度留白）。

    ★ 一行里的多个空是 `x1_mm/x2_mm`，行只有 `y_mm`（**下划线的 y**）。
      所以横向取 min(x1)/max(x2)，纵向从线下 0.4mm 到线上 7.6mm。

    ★★ 为什么按行切而不是整题一图（2026-10-21 决议）：
      一个大题下 (1)(2)(3) 共用一个 seq 时，整题一图会让老师
      无法逐空给分——而 `sections.py` 的 `blank_scores` 早就存了
      每空分值，数据层支持却被切图粒度卡住。
      按行切后，`part_index` = 行号，前端竖向拼接即可还原整题。

    ★ 这个矩形依赖真实行距：`layout` 的 `fill_line_spacing` 从 1.9 提到
      2.3 之后，相邻两行的切图区之间才有 2mm 以上的白带。
      行距若被老师调得过小，切图会切到上一行的笔迹——
      ★ 安全下限**由字号推导**，不是写死 1.6：
        本函数按 8.0mm 高切一行，两行之间还要留 2.0mm 白带，
        所以 line_h ≥ 10.0mm，即 ratio ≥ (10.0 - 1.6) / font_mm。
        见 `apps/sheet/layout.fill_line_ratio_min()`；排版侧行高低于本函数
        的 8.0mm 切图高时出 `FILL_LINE_OVERLAP` 告警，UI 输入框按同一公式
        夹紧。（1.6 只是绝对下限，10pt 下真正的安全下限是 2.39 倍。）
    """
    y = line.get("y_mm")
    if y is None:
        return None
    xs1: list[float] = []
    xs2: list[float] = []
    for seg in line.get("blanks") or []:
        if seg.get("x1_mm") is None or seg.get("x2_mm") is None:
            continue
        xs1.append(float(seg["x1_mm"]))
        xs2.append(float(seg["x2_mm"]))
    if not xs1:
        return None
    top = float(y) - FILL_BLANK_ASCENT_MM
    bottom = float(y) + FILL_BLANK_DESCENT_MM
    return {
        "x_mm": min(xs1), "y_mm": top,
        "w_mm": max(xs2) - min(xs1), "h_mm": bottom - top,
    }


def _rect_of_fill_blank(entry: dict[str, Any]) -> dict[str, float] | None:
    """填空题：该题**所有行**的外接矩形（单行时等价于单行矩形）。

    保留这个函数是为了「一行里有多个空」与「只有一行的题」两种情况
    ——它们切出来本来就是同一块，不需要拆 part。
    """
    xs1: list[float] = []
    xs2: list[float] = []
    ys: list[float] = []
    for line in entry.get("lines") or []:
        y = line.get("y_mm")
        if y is None:
            continue
        ys.append(float(y))
        for seg in line.get("blanks") or []:
            if seg.get("x1_mm") is None or seg.get("x2_mm") is None:
                continue
            xs1.append(float(seg["x1_mm"]))
            xs2.append(float(seg["x2_mm"]))
    if not xs1 or not ys:
        return None
    top = min(ys) - FILL_BLANK_ASCENT_MM
    bottom = max(ys) + FILL_BLANK_DESCENT_MM
    return {
        "x_mm": min(xs1), "y_mm": top,
        "w_mm": max(xs2) - min(xs1), "h_mm": bottom - top,
    }


def blocks_for_page(
    layout: dict[str, Any], page_no: int,
) -> dict[int, list[dict[str, Any]]]:
    """该页的**全部主观题块**，按卷面题号归组。

    返回 `{seq: [{kind, rect, part_index, part_count, page_no}, ...]}`。

    ★ 三种块可能在**同一份卡的不同页**上，所以 `part_index` 按
      「该题在整份卡里的第几段」全局编号——前端按它排序即可拼回整题。
    """
    pages = layout.get("pages") or []
    if not 1 <= page_no <= len(pages):
        return {}
    page = pages[page_no - 1]

    # 先把整份卡的段数算出来（跨页作文要 part_count）
    counts: dict[int, int] = {}
    for pg in pages:
        for e in pg.get("essays") or []:
            counts[int(e.get("seq") or 0)] = counts.get(
                int(e.get("seq") or 0), 0) + 1

    out: dict[int, list[dict[str, Any]]] = {}

    for blk in page.get("subjective") or []:
        seq = int(blk.get("seq") or 0)
        rect = _rect_of_answer(blk)
        if seq and rect:
            out.setdefault(seq, []).append({
                "kind": SubjectiveCrop.KIND_ANSWER, "rect": rect,
                "part_index": 1, "part_count": 1, "page_no": page_no,
            })

    # 作文的段号要按全卡顺序数，所以从第 1 页重新走一遍
    essay_seen: dict[int, int] = {}
    for pno, pg in enumerate(pages, start=1):
        for e in pg.get("essays") or []:
            seq = int(e.get("seq") or 0)
            if not seq:
                continue
            essay_seen[seq] = essay_seen.get(seq, 0) + 1
            if pno != page_no:
                continue
            rect = _rect_of_essay_part(e)
            if rect:
                out.setdefault(seq, []).append({
                    "kind": SubjectiveCrop.KIND_ESSAY, "rect": rect,
                    "part_index": essay_seen[seq],
                    "part_count": counts.get(seq, 1),
                    "page_no": page_no,
                })

    # ★★ 同一道填空题的多个空可能被拆成**多个 entry**（一个空一个 entry，
    #   `seq` 相同）。这里按 seq 合成本页的行列表。
    #
    #   不合并会踩一个静默的数据错误：`part_index` 全是 1，
    #   `update_or_create(scan_card, paper_item, part_index)` 于是互相覆盖，
    #   最后只剩**最后一个空**的切图，前几个空在阅卷页根本看不到。
    fill_by_seq: dict[int, list[dict[str, Any]]] = {}
    for entry in page.get("fill_blanks") or []:
        seq = int(entry.get("seq") or 0)
        if not seq:
            continue
        fill_by_seq.setdefault(seq, []).extend(entry.get("lines") or [])

    # 跨页要正确编号：先统计全卡每道填空题共几行、本页之前已排了几行。
    # （作文的 part_index 也是这么算的，两者同一个语义。）
    fill_total: dict[int, int] = {}
    for pg in pages:
        for entry in pg.get("fill_blanks") or []:
            seq = int(entry.get("seq") or 0)
            if seq:
                fill_total[seq] = (fill_total.get(seq, 0)
                                   + len(entry.get("lines") or []))
    fill_before: dict[int, int] = {}
    for pno, pg in enumerate(pages, start=1):
        if pno >= page_no:
            break
        for entry in pg.get("fill_blanks") or []:
            seq = int(entry.get("seq") or 0)
            if seq:
                fill_before[seq] = (fill_before.get(seq, 0)
                                    + len(entry.get("lines") or []))

    for seq, lines in fill_by_seq.items():
        total = fill_total.get(seq, len(lines))
        base = fill_before.get(seq, 0)
        if len(lines) <= 1:
            # 只有一行（或一行里多个空）：切一块就够，不人为拆 part。
            rect = _rect_of_fill_blank({"lines": lines})
            if rect:
                out.setdefault(seq, []).append({
                    "kind": SubjectiveCrop.KIND_FILL_BLANK, "rect": rect,
                    "part_index": base + 1, "part_count": max(total, 1),
                    "page_no": page_no,
                })
            continue
        # ★ 多行 → 一行一个 part。老师能逐空/逐行看，前端竖向拼接还原整题。
        for offset, line in enumerate(lines):
            rect = _rect_of_fill_line(line)
            if not rect:
                continue
            out.setdefault(seq, []).append({
                "kind": SubjectiveCrop.KIND_FILL_BLANK, "rect": rect,
                "part_index": base + offset + 1,
                "part_count": max(total, len(lines)),
                "page_no": page_no,
            })

    return out


def rect_to_pixels(
    rect: dict[str, float], *, page_w_mm: float, scale: float,
) -> tuple[int, int, int, int]:
    """mm 矩形 → 像素框 `(left, top, right, bottom)`。

    ★ `scale` 由**资产实测宽度 / 页面宽度**得出，不用固定 300dpi：
      手机拍的卡校正后可能不是 300dpi，写死会让切图整体偏移。

    ★ 越界要夹住（`rect_to_pixels` 是纯函数，不做 IO，
      方便单元测试穷举边界）。
    """
    x, y = float(rect["x_mm"]), float(rect["y_mm"])
    w, h = float(rect["w_mm"]), float(rect["h_mm"])
    left = int(round(x * scale))
    top = int(round(y * scale))
    right = int(round((x + w) * scale))
    bottom = int(round((y + h) * scale))
    if left < 0:
        left = 0
    if top < 0:
        top = 0
    if right < left:
        right = left
    if bottom < top:
        bottom = top
    return left, top, right, bottom


# ══════════════════════════════════════════════════════════════
#  切图
# ══════════════════════════════════════════════════════════════


def _load_image(data: bytes):
    """解码为 PIL RGB 图。★ PIL 延迟导入：本模块的几何函数不依赖它。"""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def _crop_bytes(image, box: tuple[int, int, int, int]) -> tuple[bytes, int, int]:
    """裁一块、必要时缩小、编码为 JPEG。返回 `(bytes, w, h)`。"""
    import io

    piece = image.crop(box)
    w, h = piece.size
    if w > CROP_MAX_WIDTH:
        ratio = CROP_MAX_WIDTH / float(w)
        piece = piece.resize(
            (CROP_MAX_WIDTH, max(1, int(round(h * ratio)))),
            resample=1,                      # Image.LANCZOS
        )
        w, h = piece.size
    buf = io.BytesIO()
    if piece.mode != "RGB":
        piece = piece.convert("RGB")
    piece.save(buf, format="JPEG", quality=CROP_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), w, h


def _ensure_display(org_id: int, card: ScanCard) -> int | None:
    """确保有整页校正图。返回 asset_id；失败返回 None（记告警）。"""
    if card.display_asset_id:
        return card.display_asset_id
    if card.rectified_asset_id:
        return card.rectified_asset_id
    try:
        from apps.omr.services import ScanDisplayService

        ScanDisplayService.ensure_display_asset(org_id, card.id)
        card.refresh_from_db(fields=["display_asset", "rectified_asset"])
    except Exception as exc:      # noqa: BLE001 —— 切图是降级功能，不许抛
        logger.warning(
            "%s 卡 %s 无法生成整页校正图：%s", W_NO_DISPLAY, card.id, exc)
        return None
    return card.display_asset_id or card.rectified_asset_id


def _subjective_items(org_id: int, assignment_id: int) -> dict[int, Any]:
    """该任务的**主观题**卷面题：`{seq: PaperItem}`。"""
    from apps.assignment.models import Assignment
    from apps.paper.models import PaperItem

    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("paper_id").first()
    if a is None:
        return {}
    rows = (
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=a.paper_id)
        .select_related("question")
        .order_by("seq")
    )
    return {
        int(it.seq): it for it in rows
        if str(it.question.type or "") not in OBJECTIVE_TYPES
    }


def clear_crops_for_card(org_id: int, card_id: int, *, keep_assets: bool = True) -> int:
    """删掉一张卡的全部切图记录（重新识别时先清）。

    ★ 默认**不删资产**：切图 Asset 可能已被导出包引用，
      且删除要走 `MediaService.delete_asset` 的引用计数。
      这里只解除绑定关系，垃圾回收交给媒体清理任务。
    """
    n = SubjectiveCrop.objects.for_org(org_id).filter(scan_card_id=card_id).delete()
    return int(n[0]) if isinstance(n, tuple) else int(n)


def generate_crops_for_card(
    org_id: int,
    card_id: int,
    *,
    force: bool = False,
    write_when_missing_student: bool = False,
) -> list[SubjectiveCrop]:
    """为一张卡生成主观题切图。

    返回**本次实际生成/更新**的切图列表。无图、无布局、无主观题、
    学生未归属时返回空列表（不抛错——切图缺失是允许的状态）。

    ★ 学生未归属时默认**不生成**：切图按学生聚合展示，
      没有学生就无处可放；等到复核指定学生后再生成（`review_card` 会调）。
    """
    card = (
        ScanCard.objects.for_org(org_id)
        .filter(pk=card_id)
        .select_related("sheet_template", "assignment")
        .first()
    )
    if card is None:
        return []
    if card.status == "VOID":
        return []
    if not card.student_id and not write_when_missing_student:
        return []

    layout = (card.sheet_template.layout_json or {}) if card.sheet_template_id else {}
    if not layout.get("pages"):
        logger.warning("%s 卡 %s 的模板没有展开坐标", W_NO_GEOMETRY, card.id)
        return []

    page_no = int(card.page_no or 1)
    blocks = blocks_for_page(layout, page_no)
    if not blocks:
        return []

    items = _subjective_items(org_id, card.assignment_id)
    if not items:
        return []

    display_id = _ensure_display(org_id, card)
    if not display_id:
        return []

    from apps.media.models import Asset
    from apps.media.services import MediaService

    asset = Asset.objects.for_org(org_id).filter(pk=display_id).first()
    if asset is None or not asset.width:
        logger.warning("%s 卡 %s 的展示图缺少尺寸元数据", W_NO_DISPLAY, card.id)
        return []

    page_w_mm = float(layout.get("page_width_mm") or 210.0)
    page_h_mm = float(layout.get("page_height_mm") or 297.0)
    scale = float(asset.width) / page_w_mm if page_w_mm else 0.0
    if scale <= 0:
        return []

    existing = {
        (c.paper_item_id, c.part_index): c
        for c in SubjectiveCrop.objects.for_org(org_id).filter(scan_card_id=card.id)
    }

    # ★ 只在有活要干时才读整页图（45 人 × 5 题 ≈ 45 次读盘）
    image = None
    out: list[SubjectiveCrop] = []

    for seq, parts in blocks.items():
        item = items.get(seq)
        if item is None:
            # 卡上有块、卷面没有这道主观题：模板与试卷不匹配，跳过
            continue
        # ★★ 段数变化必须重切，即使 `template_version` 没变。
        #   2026-10-21 起填空题由「整题一图」改为「一行一段」：
        #   同一道题的旧记录 part_count=1、新方案 part_count=3。
        #   若只按 template_version 判断复用，会得到「第 1 段是整题图、
        #   第 2/3 段是行图」的混合结果，老师在阅卷页看到的是错位的三段。
        new_count = int(parts[0]["part_count"]) if parts else 1
        old_counts = {
            int(c.part_count or 1)
            for (pid, _pi), c in existing.items() if pid == item.id
        }
        force_item = force or bool(old_counts and new_count not in old_counts)
        for part in parts:
            key = (item.id, int(part["part_index"]))
            old = existing.get(key)
            if old is not None and not force_item \
                    and int(old.template_version) == int(card.template_version):
                continue
            # ★ 填空题的纵向留白已在 `_rect_of_fill_line` /
            #   `_rect_of_fill_blank` 里算好（下边贴线 / 上边一个字高），
            #   这里不能再加 2mm。
            pad_y = (0.0 if part["kind"] == SubjectiveCrop.KIND_FILL_BLANK
                     else CROP_PAD_MM)
            rect = _clamp_rect(
                part["rect"], page_w_mm, page_h_mm, pad_y=pad_y)
            if rect["w_mm"] <= 0 or rect["h_mm"] <= 0:
                continue

            if image is None:
                data = MediaService.read_bytes(org_id=org_id, asset_id=display_id)
                image = _load_image(data)

            left, top, right, bottom = rect_to_pixels(
                rect, page_w_mm=page_w_mm, scale=scale)
            right = min(right, image.size[0])
            bottom = min(bottom, image.size[1])
            min_h = (CROP_MIN_PX_FILL_BLANK
                     if part["kind"] == SubjectiveCrop.KIND_FILL_BLANK
                     else CROP_MIN_PX)
            if right - left < CROP_MIN_PX or bottom - top < min_h:
                logger.warning(
                    "%s 卡 %s 第 %s 题切图仅 %sx%s px（矩形 %s）",
                    W_TOO_SMALL, card.id, seq,
                    right - left, bottom - top, rect,
                )

            jpg, w, h = _crop_bytes(image, (left, top, right, bottom))
            new_asset = MediaService.upload_directly(
                org_id=org_id, kind=AssetKind.SUBJECTIVE_CROP,
                filename=(f"card-{card.id}-q{seq}-p{part['page_no']}"
                          f"-{part['part_index']}.jpg"),
                data=jpg, mime="image/jpeg", width=w, height=h,
            )
            with transaction.atomic():
                obj, _created = SubjectiveCrop.objects.update_or_create(
                    scan_card_id=card.id,
                    paper_item_id=item.id,
                    part_index=int(part["part_index"]),
                    defaults={
                        "org_id": org_id,
                        "assignment_id": card.assignment_id,
                        "student_id": card.student_id,
                        "sheet_seq": seq,
                        "kind": part["kind"],
                        "page_no": int(part["page_no"]),
                        "part_count": int(part["part_count"]),
                        "rect_json": rect,
                        "asset_id": new_asset.id,
                        "template_version": int(card.template_version),
                    },
                )
            out.append(obj)

    return out


def ensure_crops_for_student_item(
    org_id: int, assignment_id: int, student_id: int, paper_item_id: int,
) -> list[SubjectiveCrop]:
    """阅卷接口的**按需补切图**：缺就现生成。

    ★ 为什么阅卷时要兜：历史卡没跑过 `backfill`，或者复核后才指定学生。
      老师点开一道题发现「没有切图」，不该让他去跑命令行。
    """
    have = list(
        SubjectiveCrop.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id,
            paper_item_id=paper_item_id, asset__isnull=False,
        )
    )
    if have:
        return sorted(have, key=lambda c: (c.page_no, c.part_index))

    cards = list(
        ScanCard.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id,
        ).exclude(status="VOID").order_by("page_no", "id")
    )
    for card in cards:
        generate_crops_for_card(org_id, card.id)
    return sorted(
        SubjectiveCrop.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id,
            paper_item_id=paper_item_id, asset__isnull=False,
        ),
        key=lambda c: (c.page_no, c.part_index),
    )


def generate_crops_for_assignment(
    org_id: int, assignment_id: int, *, limit: int = 0, force: bool = False,
) -> dict[str, int]:
    """批量生成（backfill 命令与演示脚本用）。"""
    qs = (
        ScanCard.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, student__isnull=False)
        .exclude(status="VOID")
        .order_by("id")
    )
    if limit:
        qs = qs[:limit]
    ok = fail = skipped = created = 0
    for card in qs:
        try:
            rows = generate_crops_for_card(
                org_id, card.id, force=force, write_when_missing_student=False)
            if rows:
                ok += 1
                created += len(rows)
            else:
                skipped += 1
        except Exception as exc:      # noqa: BLE001 —— 单卡失败不拖垮整批
            logger.warning("生成切图失败 card=%s: %s", card.id, exc)
            fail += 1
    return {"cards_ok": ok, "cards_failed": fail,
            "cards_skipped": skipped, "crops": created}
