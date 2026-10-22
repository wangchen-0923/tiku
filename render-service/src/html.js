/**
 * 排版 JSON → 完整 HTML 文档（给 Puppeteer 打印）。
 *
 * ══════════════════════════════════════════════════════════
 *  这个模块只做一件事：把 mm 坐标照抄成 CSS
 * ══════════════════════════════════════════════════════════
 *
 * 分工是死的，不要越界：
 *
 *   Django `paper/layout.py`  → 分页、折行、每块每行的毫米坐标
 *   `web/.../PaperPreviewCanvas.vue` → 照 JSON 画（浏览器预览）
 *   **本文件** → 照 JSON 画（打印成 PDF）
 *
 * 也就是说：**折行、分页、行数一个都不许在这里算**。
 * Node 侧只要算了一次，预览和 PDF 就会开始分叉，
 * 而那种分叉表现为「预览 3 页、导出 4 页」，比公式没渲染还难查。
 *
 * ★ 长度单位直接用 `mm` / `pt`：Chromium 打印时 1mm 就是 1mm，
 *   不需要像前端那样乘一个 `scale`。换算是万恶之源。
 *
 * ★ `white-space: pre` 是关键：行是排好版的，浏览器不许再折。
 *   不写这一条，Puppeteer 会按容器宽度重新折行，
 *   于是「排版 JSON 里的第 2 行」在 PDF 里被折成两行，坐标全错。
 */

import { CJK_FONT_STACK, katexCss } from './fonts.js'
import { escapeHtml, renderLine } from './math.js'

/** 1pt = 25.4/72 mm */
const PT_TO_MM = 25.4 / 72

/** 基线 → CSS `top`（CSS 的 top 是盒子顶，不是基线） */
function topOfBaseline(yMm, fontPt) {
  return yMm - fontPt * PT_TO_MM * 0.82
}

/** 分组标题行高（与 `layout.section_title_lines` 同口径） */
function sectionLineH(b, baseFont) {
  return b.line_h_mm || (baseFont + 1.5) * 1.9 * PT_TO_MM
}

/** 题干行高 */
function stemLineH(b, baseFont) {
  return b.line_h_mm || baseFont * 1.5 * PT_TO_MM
}

function mm(v) {
  return `${Number(v || 0).toFixed(3)}mm`
}

function abs(leftMm, topMm, extra = '') {
  return `left:${mm(leftMm)};top:${mm(topMm)};${extra}`
}

/**
 * 生成一页的 HTML。
 *
 * @param {object} page     排版 JSON 的 `pages[i]`
 * @param {object} layout   排版 JSON 整体（取 margin / 页宽高等）
 * @param {boolean} isFirst 是否首页（卷头只画在首页）
 */
function pageHtml(page, layout, isFirst) {
  const baseFont = layout.config.base_font_pt ?? 10.5
  const margin = layout.margin_mm
  const parts = []

  // ── 卷头 ──
  if (isFirst && layout.header) {
    const h = layout.header
    const title = h.title || {}
    for (let i = 0; i < (title.lines || []).length; i += 1) {
      const y = topOfBaseline(
        (title.y_mm ?? 0) + i * (title.line_h_mm ?? 0),
        title.font_pt ?? baseFont,
      )
      parts.push(
        `<div class="ln ttl" style="${abs(0, y)}` +
          `width:100%;text-align:center;font-size:${title.font_pt}pt;` +
          `font-weight:700">${renderLine(title.lines[i])}</div>`,
      )
    }

    const sub = h.subtitle || {}
    if (sub.text) {
      const y = topOfBaseline(sub.y_mm ?? 0, sub.font_pt ?? baseFont)
      parts.push(
        `<div class="ln sub" style="${abs(0, y)}` +
          `width:100%;text-align:center;font-size:${sub.font_pt}pt">` +
          `${renderLine(sub.text)}</div>`,
      )
    }

    for (const item of h.student_items || []) {
      const y = topOfBaseline(item.y_mm ?? 0, baseFont)
      parts.push(
        `<div class="ln" style="${abs(margin + (item.x_mm ?? 0), y)}` +
          `font-size:${baseFont}pt">${renderLine(item.label)}</div>`,
      )
      parts.push(
        `<div class="ul" style="${abs(
          margin + (item.underline_x_mm ?? 0),
          (item.y_mm ?? 0) + 1.8,
        )}width:${mm(item.underline_w_mm)}"></div>`,
      )
    }

    if (h.student_extra && h.student_extra.text) {
      const y = topOfBaseline(h.student_extra.y_mm ?? 0, baseFont)
      parts.push(
        `<div class="ln" style="${abs(margin, y)}` +
          `font-size:${baseFont}pt">${renderLine(h.student_extra.text)}</div>`,
      )
    }

    const notes = h.notes || {}
    for (let i = 0; i < (notes.lines || []).length; i += 1) {
      const y = topOfBaseline(
        (notes.y_mm ?? 0) + i * (notes.line_h_mm ?? 0),
        notes.font_pt ?? baseFont,
      )
      parts.push(
        `<div class="ln notes" style="${abs(margin, y)}` +
          `font-size:${notes.font_pt}pt;line-height:${mm(notes.line_h_mm)}">` +
          `${renderLine(notes.lines[i])}</div>`,
      )
    }

    for (const ry of h.rules || []) {
      parts.push(
        `<div class="rule" style="${abs(margin, ry)}` +
          `width:${mm(layout.usable_width_mm)}"></div>`,
      )
    }
  }

  // ── 正文块 ──
  for (const b of page.blocks || []) {
    if (b.kind === 'stem') {
      for (let i = 0; i < (b.title_lines || []).length; i += 1) {
        const y = topOfBaseline(
          b.y_mm + i * sectionLineH(b, baseFont),
          baseFont + 1.5,
        )
        parts.push(
          `<div class="ln sec" style="${abs(margin, y)}` +
            `font-size:${baseFont + 1.5}pt">${renderLine(b.title_lines[i])}</div>`,
        )
      }
    } else if (b.kind === 'q_stems') {
      for (let i = 0; i < (b.stem_lines || []).length; i += 1) {
        const lh = stemLineH(b, baseFont)
        parts.push(
          `<div class="ln" style="${abs(margin, topOfBaseline(b.y_mm + i * lh, baseFont))}` +
            `font-size:${baseFont}pt;line-height:${mm(lh)}">` +
            `${renderLine(b.stem_lines[i])}</div>`,
        )
      }
    } else if (b.kind === 'choice_row') {
      const optFont = b.option_font_pt || baseFont - 1.5
      const lh = stemLineH(b, baseFont)
      for (const cell of b.option_cells || []) {
        for (let li = 0; li < (cell.lines || []).length; li += 1) {
          parts.push(
            `<div class="ln" style="${abs(
              margin + (cell.x_mm ?? 0),
              topOfBaseline(b.y_mm + (cell.y_mm ?? 0) + li * lh, optFont),
            )}font-size:${optFont}pt;line-height:${mm(lh)}">` +
              `${renderLine(cell.lines[li])}</div>`,
          )
        }
      }
    } else if (b.kind === 'subjective') {
      const lh = stemLineH(b, baseFont)
      if (b.seq_label) {
        parts.push(
          `<div class="ln" style="${abs(
            margin,
            topOfBaseline(b.y_mm + lh * 0.9, baseFont),
          )}font-size:${baseFont}pt">${escapeHtml(b.seq_label)}</div>`,
        )
      }
      for (let i = 0; i < (b.stem_lines || []).length; i += 1) {
        const y = topOfBaseline(
          b.y_mm + (b.seq_label ? lh : 0) + i * lh,
          baseFont,
        )
        parts.push(
          `<div class="ln" style="${abs(margin, y)}` +
            `font-size:${baseFont}pt;line-height:${mm(lh)}">` +
            `${renderLine(b.stem_lines[i])}</div>`,
        )
      }
    } else if (b.kind === 'figure') {
      parts.push(figureHtml(b, baseFont, layout.figure_gap_mm ?? 1))
    } else if (b.kind === 'blank') {
      parts.push(
        `<div class="blank" style="${abs(margin, b.y_mm)}` +
          `width:${mm(layout.usable_width_mm)};height:${mm(b.height_mm)};` +
          `font-size:${(baseFont - 2).toFixed(1)}pt">` +
          `空白区 ${b.height_mm}mm</div>`,
      )
    }
  }

  // ── 页码 ──
  if (page.page_number) {
    parts.push(
      `<div class="pno" style="top:${mm(page.page_number.y_mm)};` +
        `text-align:${page.page_number.align || 'center'}">` +
        `${escapeHtml(page.page_number.text)}</div>`,
    )
  }

  return (
    `<div class="page" style="width:${mm(layout.page_width_mm)};` +
    `height:${mm(layout.page_height_mm)}">${parts.join('')}</div>`
  )
}

/**
 * 一张配图（含图注）。
 *
 * ★ 图片字节由 Django 侧以 data URI 放进 `assets`，key 是 `asset_id`。
 *   取不到就印「［配图缺失］」占位 —— 与预览、reportlab 的行为一致，
 *   绝不静默留白（留白会被误认为「这题本来就没图」）。
 */
function figureHtml(b, baseFont, gapMm) {
  const x = b.x_mm ?? 0
  const y = b.y_mm ?? 0
  const w = b.width_mm ?? 0
  const h = b.image_h_mm ?? 0
  const uri = b.asset_id ? b.__asset_uri : ''
  const captionFont = baseFont - 1.5
  const captionTop = h + gapMm * 0.6
  const lineH = b.caption_line_h_mm || captionFont * 1.4 * PT_TO_MM

  const inner = []
  if (uri && !b.missing) {
    inner.push(
      `<img src="${uri}" style="display:block;width:${mm(w)};height:${mm(h)}" />`,
    )
  } else {
    inner.push(
      `<div class="figmiss" style="width:${mm(w)};height:${mm(h)};` +
        `font-size:${captionFont}pt">［配图缺失］</div>`,
    )
  }
  for (let i = 0; i < (b.caption_lines || []).length; i += 1) {
    inner.push(
      `<div class="ln cap" style="left:0;top:${mm(captionTop + i * lineH)};` +
        `width:100%;text-align:center;font-size:${captionFont}pt;` +
        `line-height:${mm(lineH)}">${renderLine(b.caption_lines[i])}</div>`,
    )
  }

  return (
    `<div class="fig" style="left:${mm(x)};top:${mm(y)};width:${mm(w)}">` +
    `${inner.join('')}</div>`
  )
}

/** 页面公用 CSS（打印口径） */
function baseCss() {
  return `
@page { size: var(--zf-page-w) var(--zf-page-h); margin: 0 }
* { box-sizing: border-box }
html, body { margin: 0; padding: 0; background: #fff }
body {
  font-family: ${CJK_FONT_STACK};
  color: #111827;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.page {
  position: relative;
  overflow: hidden;
  background: #fff;
  break-after: page;
  page-break-after: always;
}
.page:last-child { break-after: auto; page-break-after: auto }
.ln { position: absolute; white-space: pre }
.ttl { font-weight: 700 }
.sub, .pno { color: #6b7280 }
.notes { color: #4b5563 }
.sec { font-weight: 700 }
.ul { position: absolute; height: 0; border-bottom: 1px solid #111827 }
.rule { position: absolute; border-top: 1px solid #9aa3ad }
.blank {
  position: absolute; display: flex; align-items: center;
  justify-content: center; border: 1px dashed #cbd5e1;
  color: #94a3b8; background: #fafafa;
}
.fig { position: absolute }
.figmiss {
  display: flex; align-items: center; justify-content: center;
  border: 1px dashed #b9c0c9; color: #6b7280; background: #f8fafc;
}
.cap { color: #4b5563; overflow: hidden }
.pno {
  position: absolute; left: 0; width: 100%;
  padding: 0 12px; font-size: 8pt;
}
`
}

/**
 * 渲染整份文档。
 *
 * @param {object} layout 排版 JSON（`export/?layout=1` 的产物）
 * @param {object} opts   `{ assets: {assetId: dataURI}, pixelRatio }`
 * @returns {string} 完整 HTML
 */
export function buildHtml(layout, opts = {}) {
  const assets = opts.assets || {}

  // 把图片字节挂到块上（`figureHtml` 只管画，不去查表）
  const pages = (layout.pages || []).map((p) => ({
    ...p,
    blocks: (p.blocks || []).map((b) =>
      b.kind === 'figure' && b.asset_id
        ? { ...b, __asset_uri: assets[String(b.asset_id)] || assets[b.asset_id] || '' }
        : b,
    ),
  }))

  const body = pages
    .map((p, i) => pageHtml(p, layout, i === 0))
    .join('')

  const pw = layout.page_width_mm
  const ph = layout.page_height_mm

  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>${escapeHtml(opts.title || '试卷')}</title>
<style>${katexCss()}</style>
<style>
:root { --zf-page-w: ${mm(pw)}; --zf-page-h: ${mm(ph)} }
${baseCss()}
</style></head>
<body>${body}</body></html>`
}
