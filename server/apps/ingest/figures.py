"""配图归属（纯函数，无 IO / 无数据库）。

★★ 为什么单独拆出来（2026-10-18 修复）：

    旧实现直接在 `pipeline._bind_figures` 里做绑定，判据是
    「`anchors` 的下标 ≈ `raw_items` 的下标」。这个假设在真实试卷上
    不成立：

      · 图注行（`第6题图`）本身又被题号正则匹配成一道题；
      · 卷头/说明被剔除后，`raw_items` 少了几条；
      · 分析/计算题被抽成题组后，一条锚点对应多条 item。

    三者叠加后，`anchors[5]` 可能指向第 13 题 —— 这就是
    「第 6 题图被绑到第 13 题」的根因。

    修复后的判据按**优先级**排列：

      1. 图注 `第 X 题图`（最高优先级，与图距离最近的那条）
      2. 选项字母 A–D（选项图片；只认本题题号范围内的字母）
      3. 空间位置：图片垂直中心落在哪道题的 y 区间内 / 同行的右侧
      4. 图片上方最近的一道题（最后兜底）

    本模块只产出「哪张图归哪道题、挂题干还是选项」，上传与落库仍在
    `pipeline` 里做 —— 这样绑定规则可以脱离数据库穷举测试。
"""

from __future__ import annotations

import math
from typing import Any

#: 图注与图片的最大距离（PDF 点）。真试卷图注通常在图下方 5–60pt。
CAPTION_MAX_DIST = 140.0

#: 选项字母与图片的最大距离。选项图常见「字母在左/下 5–40pt」。
OPTION_LABEL_MAX_DIST = 80.0

#: 图片与题目 y 区间的容差（图片顶边略高于题号行时仍算本题）。
Y_TOLERANCE = 14.0

#: 绑定方法（与 `QuestionImage.binding_method` 的取值对齐）。
M_CAPTION = "CAPTION"
M_OPTION_POS = "OPTION_POS"
M_SEQUENCE = "SEQUENCE"
M_MANUAL = "MANUAL"


def _rect(obj: dict[str, Any]) -> tuple[float, float, float, float]:
    bbox = obj.get("bbox") or (0, 0, 0, 0)
    try:
        return (float(bbox[0]), float(bbox[1]),
                float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError, IndexError):
        x = float(obj.get("x") or 0)
        y = float(obj.get("y") or 0)
        w = float(obj.get("width") or 0)
        h = float(obj.get("height") or 0)
        return (x, y, x + w, y + h)


def _rect_distance(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """两个矩形之间的最短距离（相交为 0）。"""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _center_y(rect: tuple[float, float, float, float]) -> float:
    return (rect[1] + rect[3]) / 2.0


def _item_y_range(item: dict[str, Any]) -> tuple[float, float]:
    y0 = item.get("y0")
    y1 = item.get("y1")
    try:
        y0 = float(y0) if y0 is not None else 0.0
    except (TypeError, ValueError):
        y0 = 0.0
    try:
        y1 = float(y1) if y1 is not None else y0 + 1.0
    except (TypeError, ValueError):
        y1 = y0 + 1.0
    if y1 <= y0:
        y1 = y0 + 1.0
    return y0, y1


def _label_owner(
    label: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """字母标注属于哪道题。

    ★★ 必须**先按 y 区间**判，不能只看抽取行号：
      PDF 的文字块顺序不等于阅读顺序（后插入的 A/B/C/D 往往被排到
      所有题干之后），按行号会把第 15 题的选项字母判给第 18 题。
      行号只在没有坐标（Word OCR）时兜底。
    """
    page = label.get("page")
    lab_rect = _rect(label)
    lab_y = (lab_rect[1] + lab_rect[3]) / 2.0

    by_y: list[tuple[float, dict[str, Any]]] = []
    for it in items:
        if it.get("page") != page:
            continue
        y0, y1 = _item_y_range(it)
        if y0 - Y_TOLERANCE <= lab_y <= y1 + Y_TOLERANCE:
            by_y.append((y0, it))
    if by_y:
        return max(by_y, key=lambda pair: pair[0])[1]

    line = label.get("line_index")
    if line is None:
        return None
    for it in items:
        if it.get("page") != page:
            continue
        start = it.get("line_start", -1)
        end = it.get("line_end", -1)
        if start is not None and end is not None and start <= line <= end:
            return it
    return None


def resolve_caption_item(
    caption: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """图注题号 → 题目。

    ★ 题号可能跨大题从 1 重排（`printed_seq` 重复），所以：
      先按「同页 + 题号相同」筛；同页仍有多个时，取**图注上方最近**的那个。
      同页没有才退到全篇唯一匹配。
    """
    seq = caption.get("seq")
    page = caption.get("page")
    if seq is None:
        return None
    same_page = [
        it for it in items
        if it.get("printed_seq") == seq and it.get("page") == page
    ]
    candidates = same_page or [
        it for it in items if it.get("printed_seq") == seq
    ]
    if not candidates:
        return None
    cap_y = _rect(caption)[1]
    above = []
    for it in candidates:
        y0, _y1 = _item_y_range(it)
        if y0 <= cap_y + 2:
            above.append((y0, it))
    if above:
        return max(above, key=lambda pair: pair[0])[1]
    return min(candidates,
               key=lambda it: abs(_item_y_range(it)[0] - cap_y))


def _nearest_option_label(
    fig_rect: tuple[float, float, float, float],
    page: int,
    items: list[dict[str, Any]],
    labels: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """找图片旁最近的选项字母，并确认它属于某道题。

    ★★ 必须返回「字母 + 它所属的题」：只拿字母不拿题，就会把
      第 15 题选项图的 A/B/C/D 挂到第 18 题（实测缺陷）。
    """
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for lb in labels:
        if lb.get("page") != page:
            continue
        dist = _rect_distance(fig_rect, _rect(lb))
        if dist > OPTION_LABEL_MAX_DIST:
            continue
        owner = _label_owner(lb, items)
        if owner is None:
            continue
        if best is None or dist < best[0]:
            best = (dist, lb, owner)
    if best is None:
        return None
    return best[1], best[2]


def _spatial_item(
    fig_rect: tuple[float, float, float, float],
    page: int,
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """空间位置兜底：同行右侧 → y 区间包含 → 上方最近 → 本页第一题。"""
    same_page = [it for it in items if it.get("page") == page]
    if not same_page:
        return None
    same_page.sort(key=lambda it: _item_y_range(it)[0])

    fig_top, fig_bottom = fig_rect[1], fig_rect[3]
    fig_mid = _center_y(fig_rect)

    # ① 图片垂直中心落在题目 y 区间内：这一条覆盖「图在题目右侧」
    inside = [
        it for it in same_page
        if _item_y_range(it)[0] - Y_TOLERANCE <= fig_mid
        <= _item_y_range(it)[1] + Y_TOLERANCE
    ]
    if inside:
        # 多题区间重叠（题组）时取最近上方的题
        return max(inside, key=lambda it: _item_y_range(it)[0])

    # ② 图片在题目下方，但仍在「本题到下题之间」：取上方最近
    above = [
        it for it in same_page
        if _item_y_range(it)[0] <= fig_top + 2
    ]
    if above:
        return max(above, key=lambda it: _item_y_range(it)[0])

    # ③ 图片在整页第一题上方：归本页第一题
    return same_page[0]


def bind_figures(
    figures: list[dict[str, Any]],
    *,
    captions: list[dict[str, Any]] | None = None,
    option_labels: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]],
    page_heights: dict[int, float] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把图绑到题。

    参数 `items` 每项至少要有：`index` / `page` / `y0` / `y1` /
    `line_start` / `line_end` / `printed_seq` / `kind` / `options`。

    返回 `(bindings, issues)`：
      `bindings[i]` = `{item_index, attach_point, method, confidence}`
      `issues` 是人能看懂的告警（例如「图片未归属」）。
    """
    captions = captions or []
    option_labels = option_labels or []
    page_heights = page_heights or {}
    bindings: list[dict[str, Any]] = []
    issues: list[str] = []

    # ★★ 图注先做**一对一**分配：一张图注只属于离它最近的那张图。
    #
    #   不能「每张图各自找最近的图注」：第 13 题图注离第 15 题的
    #   四个选项图也很近（<140pt），四张选项图会全部被第 13 题抢走。
    #   先按「图注→图」贪心配对，配对过的图注不再参与其他图的判定。
    caption_of_image: dict[int, dict[str, Any]] = {}
    pairs: list[tuple[float, int, dict[str, Any]]] = []
    for cap in captions:
        cap_rect = _rect(cap)
        for i, fig in enumerate(figures):
            if int(fig.get("page") or 1) != int(cap.get("page") or 1):
                continue
            d = _rect_distance(cap_rect, _rect(fig))
            if d <= CAPTION_MAX_DIST:
                pairs.append((d, i, cap))
    pairs.sort(key=lambda p: p[0])
    used_caps: set[int] = set()
    for d, i, cap in pairs:
        if i in caption_of_image or id(cap) in used_caps:
            continue
        caption_of_image[i] = cap
        used_caps.add(id(cap))

    # 每张图先独立判宿主；选项图片的「字母」同时参与宿主判定。
    for i, fig in enumerate(figures):
        page = int(fig.get("page") or 1)
        fig_rect = _rect(fig)
        host: dict[str, Any] | None = None
        attach = "STEM"
        method = M_SEQUENCE
        confidence = 0.60

        # ── 1) 图注（最高优先级）──
        caption = caption_of_image.get(i)
        caption_host = False
        if caption is not None:
            host = resolve_caption_item(caption, items)
            if host is not None:
                caption_host = True
                method = M_CAPTION
                confidence = 0.98
                attach = (
                    "MATERIAL" if host.get("kind") == "group" else "STEM"
                )

        # ── 2) 选项字母（选项图片）──
        near = _nearest_option_label(fig_rect, page, items, option_labels)
        if near is not None:
            label, owner = near
            if host is None:
                # 字母所属题就是宿主：这能把「第 15 题选项图」从
                # 空间上更近的第 18 题手里抢回来
                host = owner
                method = M_OPTION_POS
                confidence = 0.90
                attach = f"OPTION_{str(label.get('text') or '').upper()}"
            elif owner is host or (
                caption_host
                and _rect_distance(fig_rect, _rect(label)) <= 30.0
            ):
                # 图注已定宿主 → 字母只决定挂到哪个选项。
                # ★ 字母的 y 可能落在上一题（图在题右侧、字母贴图），
                #   所以图注宿主 + 字母紧贴图片时以「贴图」为准（I8 用例）。
                attach = f"OPTION_{str(label.get('text') or '').upper()}"

        # ── 3) 空间位置 ──
        if host is None:
            host = _spatial_item(fig_rect, page, items)

        if host is None:
            issues.append(f"FIGURE_{i + 1}_UNBOUND: 第 {page} 页的图未归属")
            bindings.append({
                "item_index": None, "attach_point": attach,
                "method": method, "confidence": 0.0,
            })
            continue

        bindings.append({
            "item_index": int(host["index"]),
            "attach_point": attach,
            "method": method,
            "confidence": confidence,
        })

    # ── 4) 选项图片成组推断（无字母时的兜底）──
    #
    # 一份题有 4 个选项、又恰好有 4 张图落在本题范围内 → 按阅读顺序
    # 挂 A/B/C/D。只在「图片数 == 选项数」时做，单张题干图不会误判。
    by_item: dict[int, list[int]] = {}
    for i, b in enumerate(bindings):
        if b.get("item_index") is not None:
            by_item.setdefault(int(b["item_index"]), []).append(i)
    for item in items:
        idxs = by_item.get(int(item["index"]), [])
        labels = [str(o.get("label") or "").upper()
                  for o in (item.get("options") or [])]
        # ★ 选项图没有文字选项行（字母印在图里/OCR 读不到）时：
        #   选择题恰好 4 张图 → 按 A–D；判断题 2 张 → A/B。
        if not labels:
            qtype = str(item.get("qtype") or "")
            if qtype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE") \
                    and len(idxs) == 4:
                labels = ["A", "B", "C", "D"]
            elif qtype == "TRUE_FALSE" and len(idxs) == 2:
                labels = ["A", "B"]
        if len(idxs) < 2 or len(idxs) != len(labels):
            continue
        # 只有「全部仍挂题干」才推断，已被字母明确指派的不要覆盖
        if any(bindings[i].get("attach_point") != "STEM" for i in idxs):
            continue
        ordered = sorted(
            idxs,
            key=lambda i: (_rect(figures[i])[1], _rect(figures[i])[0]),
        )
        for label, i in zip(labels, ordered):
            bindings[i]["attach_point"] = f"OPTION_{label}"
            bindings[i]["method"] = M_OPTION_POS
            bindings[i]["confidence"] = max(
                float(bindings[i].get("confidence") or 0), 0.75)

    return bindings, issues
