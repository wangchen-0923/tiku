"""试卷导出服务（`P1-60` / 契约 v3）。

★★ 本模块是**唯一的组装点**：把 `Paper` + `PaperItem` 快照
  变成「渲染器能吃的三段数据」：

      sections   分节结构（题型分组 + 题干 + 选项文本 + 分值）
      items      {卷面题号: {stem/options/score/type/answer/answer_text}}
      config     归一化后的 `layout_config`

  ★ PDF、Word、答案 PDF、前端预览 JSON **全部走这一份**。

★★ 快照优先（`snapshot_json` 优先于实时 `Question`）：
  已考过卷子的题干/答案不能因为题库事后改题而变。

★★ v3 题型判据：
  · 选择题 = `type` 为 SINGLE_CHOICE / MULTIPLE_CHOICE
  · 判断题 = `type == TRUE_FALSE` —— **即使快照里有 A/B 选项也不印**
  · 填空题 = `type == FILL_BLANK` —— 序号与题干同段
  · 其余 = 主观题
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from apps.common.mathtext import strip_math_in_text
from apps.paper import layout as L
from apps.paper.models import Paper, PaperItem
from apps.paper.textutil import prefer_text
from apps.questionbank.answer_text import answer_text_of

#: 题干里**自带选项**的检测正则（≥2 个 `A.` / `B.` 形式标记）
_STEM_HAS_OPTIONS_RE = re.compile(r"[A-Da-d][.．、]")

#: 答案标签形状（`A.` / `B.`），用于识别「只有标签没有内容」的选项
_ANSWER_LABEL_RE = re.compile(r"^[A-Ha-h][.．、]$")


def stem_has_embedded_options(stem: str) -> bool:
    """题干是否已经印出了选项（≥2 个 `A.` / `B.` 形式标记）。"""
    if not stem:
        return False
    return len(_STEM_HAS_OPTIONS_RE.findall(stem)) >= 2


@dataclass
class PaperRenderData:
    """渲染所需的全部数据。"""

    title: str
    subtitle: str
    sections: list[dict[str, Any]] = field(default_factory=list)
    items: dict[int, dict[str, Any]] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    #: 配图字节：`{asset_id: bytes}`
    assets: dict[int, bytes] = field(default_factory=dict)


def resolve_config(
    paper: Paper,
    layout_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**唯一**的参数解析入口：GET / POST / 预览 / 导出 / 答案 PDF 都走这里。"""
    raw = dict(layout_config or {})
    if raw and paper.layout_config:
        merged = dict(paper.layout_config or {})
        merged.update(raw)
        raw = merged
    elif not raw and paper.layout_config:
        raw = dict(paper.layout_config)
    return L.normalize_config(raw)


def load_figure_assets(org_id: int, asset_ids: list[int]) -> dict[int, bytes]:
    """预取配图字节。只读、按机构校验；缺图不抛错（渲染器印占位）。"""
    from apps.media.services import MediaService

    out: dict[int, bytes] = {}
    for aid in sorted(set(int(x) for x in asset_ids if x)):
        try:
            out[aid] = MediaService.read_bytes(org_id=org_id, asset_id=aid)
        except Exception:  # noqa: BLE001
            continue
    return out


def _opt_text(o: dict[str, Any]) -> str:
    return prefer_text(o.get("content_text"), o.get("content_html"))


def build_render_data(
    paper: Paper,
    *,
    layout_config: dict[str, Any] | None = None,
) -> PaperRenderData:
    """从试卷组装渲染数据。"""
    cfg = resolve_config(paper, layout_config)

    items_qs = (
        PaperItem.objects.filter(paper=paper)
        .select_related("question")
        .order_by("seq")
    )

    items: dict[int, dict[str, Any]] = {}
    figures_by_seq: dict[int, list[dict[str, Any]]] = {}
    scales_by_item: dict[int, dict[str, Any]] = {}
    order: list[str] = []
    grouped: dict[str, list[int]] = {}

    raw_rows = list(items_qs)
    for it in raw_rows:
        snap = it.snapshot_json or {}
        q = it.question
        opts = [
            {"label": o.get("label", ""),
             "content_text": o.get("content_text", ""),
             "content_html": o.get("content_html", "")}
            for o in (snap.get("options") or [])
        ]
        if not opts and q is not None:
            opts = [
                {"label": o.label, "content_text": o.content_text,
                 "content_html": o.content_html}
                for o in q.options.order_by("sort_order")
            ]

        stem_text = prefer_text(snap.get("stem_text"),
                                snap.get("stem_html")) \
            or (prefer_text(q.stem_text, q.stem_html) if q else "")
        if snap.get("group_is_first") and snap.get("group_stem_text"):
            stem_text = f"{snap['group_stem_text']}\n{stem_text}".strip()

        qtype = str(snap.get("type") or (q.type if q else "")).upper()

        # ── 配图：快照优先，退回实时题目 ──
        imgs = list(snap.get("images") or [])
        if not imgs and q is not None:
            imgs = [
                {
                    "image_id": im.id,
                    "asset_id": im.asset_id,
                    "attach_point": im.attach_point,
                    "seq_in_question": im.seq_in_question,
                    "caption": im.caption,
                }
                for im in q.images.filter(deleted_at__isnull=True)
                .order_by("seq_in_question")
            ]
        figures_by_seq[it.seq] = [
            {
                "image_id": im.get("image_id"),
                "asset_id": im.get("asset_id"),
                "attach_point": (im.get("attach_point") or "STEM").upper(),
                "caption": im.get("caption") or "",
                "seq_in_question": im.get("seq_in_question") or 0,
                "review_status": im.get("review_status") or "",
            }
            for im in imgs
            if im.get("asset_id")
        ]
        scales_by_item[it.seq] = dict(it.figure_display_scale or {})

        answer = snap.get("answer") or (q.answer if q else {}) or {}
        answer_text = prefer_text(snap.get("answer_text"), None) \
            or (q.answer_text if q else "")
        if not answer_text:
            answer_text = answer_text_of(qtype, answer)

        items[it.seq] = {
            "seq": it.seq,
            "score": float(it.score) if it.score is not None else 0.0,
            "type": qtype,
            "stem_text": stem_text,
            "options": opts,
            "answer": answer,
            "answer_text": answer_text,
            # ★ 选择题且题干自带选项 → 只印字母（防止选项印两遍）
            "force_letter": bool(
                qtype in L.CHOICE_TYPES
                and stem_has_embedded_options(stem_text)),
        }

        t = it.section_title or "未分节"
        if t not in grouped:
            grouped[t] = []
            order.append(t)
        grouped[t].append(it.seq)

    # ── 批量取 Asset 元数据（宽高/dpi）──
    asset_ids = {
        f["asset_id"]
        for figs in figures_by_seq.values() for f in figs
        if f.get("asset_id")
    }
    meta: dict[int, dict[str, Any]] = {}
    if asset_ids:
        from apps.media.models import Asset

        for a in Asset.objects.for_org(paper.org_id).filter(pk__in=asset_ids):
            meta[a.id] = {
                "width_px": a.width,
                "height_px": a.height,
                "dpi": a.dpi,
                "mime": a.mime,
            }

    def _figure_payload(seq: int, f: dict[str, Any]) -> dict[str, Any]:
        m = meta.get(int(f["asset_id"])) or {}
        sc = scales_by_item.get(seq) or {}
        raw_scale = (sc.get(f.get("image_id"))
                     or sc.get(str(f.get("image_id")))
                     or sc.get(f["asset_id"])
                     or sc.get(str(f["asset_id"]))
                     or 1.0)
        try:
            scale = float(raw_scale)
        except (TypeError, ValueError):
            scale = 1.0
        return {
            **f,
            "width_px": m.get("width_px"),
            "height_px": m.get("height_px"),
            "dpi": m.get("dpi"),
            "mime": m.get("mime") or "",
            "scale": scale,
            "missing": not (m.get("width_px") and m.get("height_px")),
        }

    for seq in figures_by_seq:
        figures_by_seq[seq] = [
            _figure_payload(seq, f) for f in figures_by_seq[seq]
        ]

    sections: list[dict[str, Any]] = []
    for i, title in enumerate(order, 1):
        seqs = grouped[title]
        sample = items[seqs[0]]
        types = {sq: items[sq]["type"] for sq in seqs}
        scores = {sq: items[sq]["score"] for sq in seqs}
        stems = {sq: items[sq]["stem_text"] for sq in seqs}

        opt_texts_by_seq: dict[int, list[str]] = {}
        force_letter_by_seq: dict[int, bool] = {}
        for sq in seqs:
            qtype = types[sq]
            if qtype not in L.CHOICE_TYPES:
                continue
            force = bool(items[sq].get("force_letter"))
            texts = [_opt_text(o) for o in (items[sq].get("options") or [])]
            if force and not texts:
                texts = [""] * 4       # 题干自带选项但没有逐项内容：留 A–D
            opt_texts_by_seq[sq] = texts
            force_letter_by_seq[sq] = force

        sections.append({
            "order": i,
            "title": title,
            "type": sample.get("type", ""),
            "seqs": seqs,
            "types": types,
            "stems": stems,
            "scores": scores,
            "opt_texts_by_seq": opt_texts_by_seq,
            "force_letter_by_seq": force_letter_by_seq,
            "figures_by_seq": {
                sq: figures_by_seq.get(sq) or []
                for sq in seqs if figures_by_seq.get(sq)
            },
        })

    subtitle = ""
    if paper.total_score:
        subtitle = f"满分：{float(paper.total_score):g} 分"
    # ★ v3.1：期望难度只在内部/分析里用，**不印在卷面上**。
    #   `difficulty_expect` 字段保留不动。

    return PaperRenderData(
        title=paper.title or "试卷",
        subtitle=subtitle,
        sections=sections,
        items=items,
        config=cfg,
        assets={},
    )


def build_layout(data: PaperRenderData, *, math_render: str = "tex") -> L.PaperLayout:
    """按渲染数据算排版（PDF/Word/前端预览**共用**）。

    `math_render` 见 `layout.plan_layout` 的说明：
      · `"tex"`   —— 预览 / Node 渲染服务：行里保留 `$…$`，按渲染宽度折行
      · `"plain"` —— reportlab / python-docx：公式先降级成纯文本再折行
    ★ 两档的折行结果**可以不同**，这是有意的：引擎不同，
      能渲染出来的字宽就不同。前端会在 `render_engine=LEGACY` 时提示。
    """
    return L.plan_layout(
        title=data.title,
        sections=data.sections,
        config=data.config,
        subtitle=data.subtitle,
        math_render=math_render,
    )


def _figure_asset_ids(lay: L.PaperLayout) -> list[int]:
    ids: list[int] = []
    for p in lay.pages:
        for b in p.blocks:
            if b.kind == "figure" and b.asset_id:
                ids.append(int(b.asset_id))
    return ids


def export_pdf(
    paper: Paper,
    *,
    layout_config: dict[str, Any] | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """导出试卷 PDF。返回 `(字节, issues)`。

    ★★ 两个引擎，**同一份排版代码**：

        ① NODE_PUPPETEER（首选）—— 排版 JSON（tex 档）发给内网渲染服务，
           KaTeX SSR 成真公式。与前端预览**逐行一致**（同一个几何）。
        ② LEGACY（回退）—— reportlab，公式降级成近似文本（`1/2`、`x²`），
           并按降级后的文本**重新排版**（plain 档），画出来才不会顶出右边界。

    ★ 回退**不报错**，只加一条非阻塞 issue。老师要的是卷子，
      不是「有个服务挂了」的通知。但降级必须留痕 —— 否则
      「同一份卷子两次导出长得不一样、还查不出原因」。
    """
    from apps.paper import render_node
    from apps.paper.render import PaperPdfRenderer

    data = build_render_data(paper, layout_config=layout_config)

    # ① 先算 tex 档：Node 渲染与前端预览共用这一份几何
    lay_tex = build_layout(data, math_render="tex")
    layout_json = L.layout_to_json(lay_tex)
    layout_json["title"] = data.title
    layout_json["subtitle"] = data.subtitle
    layout_json["math_render"] = "tex"

    if render_node.is_enabled() and data.sections:
        try:
            assets = load_figure_assets(
                paper.org_id, _figure_asset_ids(lay_tex))
            pdf = render_node.render_layout_to_pdf(
                layout_json, assets=assets, title=data.title)
            render_node.clear_breaker()
            return pdf, list(lay_tex.issues)
        except render_node.RenderUnavailable as e:
            # 熔断 120s：服务真挂了时，不该让每一次导出都白等 35 秒
            render_node.mark_unavailable(str(e))

    # ② 回退：plain 档重新排版
    lay = build_layout(data, math_render="plain")
    data.assets = load_figure_assets(paper.org_id, _figure_asset_ids(lay))
    r = PaperPdfRenderer(
        title=data.title, pages=lay.pages, items=data.items,
        config=lay.config, page_width_mm=lay.page_width_mm,
        page_height_mm=lay.page_height_mm, margin_mm=lay.margin_mm,
        usable_width_mm=lay.usable_width_mm,
        header_height_mm=lay.header_height_mm,
        header=lay.header, assets=data.assets,
        subtitle=data.subtitle,
    ).render()
    issues = list(lay.issues) + list(r.issues)
    if render_node.layout_has_math(layout_json):
        issues.append(dict(render_node.FORMULA_DEGRADED_ISSUE))
    if not data.sections:
        issues.insert(0, {
            "code": "PAPER.NO_QUESTIONS",
            "message": "这份试卷还没有题，无法导出。请先回组卷页加题。",
            "blocking": True,
        })
    return r.data, issues


def export_docx(
    paper: Paper,
    *,
    layout_config: dict[str, Any] | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """导出 Word（可编辑）。返回 `(字节, issues)`。"""
    from apps.paper.render_docx import render_paper_docx

    data = build_render_data(paper, layout_config=layout_config)
    lay = build_layout(data, math_render="plain")
    data.assets = load_figure_assets(paper.org_id, _figure_asset_ids(lay))
    r = render_paper_docx(
        title=data.title, pages=lay.pages, items=data.items,
        config=lay.config, page_width_mm=lay.page_width_mm,
        page_height_mm=lay.page_height_mm, margin_mm=lay.margin_mm,
        usable_width_mm=lay.usable_width_mm,
        header=lay.header, assets=data.assets,
        subtitle=data.subtitle,
    )
    issues = list(lay.issues) + list(r.issues)
    if not data.sections:
        issues.insert(0, {
            "code": "PAPER.NO_QUESTIONS",
            "message": "这份试卷还没有题，无法导出。请先回组卷页加题。",
            "blocking": True,
        })
    return r.data, issues


def export_answer_pdf(
    paper: Paper,
    *,
    layout_config: dict[str, Any] | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """导出**独立答案 PDF**（只印答案，不含题干/解析）。"""
    from apps.paper.render_answer import render_answer_pdf

    data = build_render_data(paper, layout_config=layout_config)
    if not data.sections:
        return b"", [{
            "code": "PAPER.NO_QUESTIONS",
            "message": "这份试卷还没有题，无法导出答案。",
            "blocking": True,
        }]
    # ★ 答案里的公式降级成近似纯文本：reportlab 没有 KaTeX，
    #   不降级会把 `$\frac{1}{2}$` 原样印到答案页上。
    for it in data.items.values():
        if it.get("answer_text"):
            it["answer_text"] = strip_math_in_text(str(it["answer_text"]))
    r = render_answer_pdf(data)
    issues = list(r.issues)
    if not r.data:
        issues.insert(0, {
            "code": "PAPER.NO_ANSWERS",
            "message": "试卷里没有可导出的答案。",
            "blocking": True,
        })
    return r.data, issues


def preview_layout_json(
    paper: Paper,
    *,
    layout_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """给前端预览的排版 JSON（与 PDF / Word 共用同一份几何）。

    ★ `math_render: "tex"` —— 行数组里保留 `$…$`，由前端 KaTeX 就地渲染。
      预览必须与 Node 渲染服务同档，否则「预览换行位置和导出的 PDF 不一样」。
      PDF / Word 走 `"plain"`（公式降级为纯文本），几何可能略有差异 ——
      这一点由导出结果里的 `render_engine=LEGACY` 明示。
    """
    data = build_render_data(paper, layout_config=layout_config)
    lay = build_layout(data, math_render="tex")
    out = L.layout_to_json(lay)
    out["title"] = data.title
    out["subtitle"] = data.subtitle
    out["math_render"] = "tex"
    return out
