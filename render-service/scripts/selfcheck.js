/**
 * 渲染服务自检（不依赖 Django，可单独跑）。
 *
 *     node scripts/selfcheck.js            只查 HTML 与字体
 *     node scripts/selfcheck.js --pdf      额外真跑一次 Puppeteer 出 PDF
 *
 * ★ `--pdf` 是可选的原因：CI 的轻量镜像里没有 Chromium，
 *   但**字体内嵌与公式 SSR 这两件事必须能在没有浏览器的环境里验证** ——
 *   它们才是「PDF 里出现方框」的真正原因。
 */

import { fontReport } from '../src/fonts.js'
import { buildHtml } from '../src/html.js'
import { hasMath, scanMath } from '../src/math.js'

let pass = 0
let fail = 0

function check(label, cond, extra = '') {
  if (cond) {
    pass += 1
    console.log(`  [OK]   ${label}`)
  } else {
    fail += 1
    console.log(`  [FAIL] ${label}   ${extra}`)
  }
}

console.log('='.repeat(70))
console.log('  渲染服务自检')
console.log('='.repeat(70))

// ── 1. 字体 ──
console.log('\nG1 · 字体内嵌（PDF 出现方框的头号原因）')
const fr = fontReport()
check('KaTeX woff2 已内嵌为 data URI', fr.embedded_fonts > 0, JSON.stringify(fr))
check('★★ 没有残留 `url(fonts/…)`（残留的那一个就是渲染时取不到的）',
  fr.leftover_relative_urls === 0, JSON.stringify(fr))
check('CSS 体积在合理区间（200KB–3MB）',
  fr.katex_css_bytes > 200_000 && fr.katex_css_bytes < 3_000_000,
  `${fr.katex_css_bytes} bytes`)

// ── 2. 公式扫描（与前后端同构） ──
console.log('\nG2 · 公式扫描（必须与 web/src/utils/math.ts 同构）')
check('行内 `$x^2+1$`', scanMath('求 $x^2+1$ 的值').length === 3,
  JSON.stringify(scanMath('求 $x^2+1$ 的值')))
check('★★ 货币 `$5 到 $10` 不误判', !hasMath('价格 $5 到 $10 之间'))
check('★★ `$a<b$` 仍算公式', hasMath('求 $a<b$ 的解'))
check('未闭合 `$x^2` 退回字面文本', !hasMath('未闭合 $x^2'))
check('独立公式 `$$…$$`', hasMath('$$\\frac{1}{2}$$'))

// ── 3. HTML 生成 ──
console.log('\nG3 · HTML 生成（mm 坐标照抄，不重排）')

function fixtureLayout() {
  return {
    config: { base_font_pt: 10.5 },
    page_width_mm: 210,
    page_height_mm: 297,
    margin_mm: 20,
    usable_width_mm: 170,
    usable_height_mm: 257,
    header_height_mm: 24,
    figure_gap_mm: 1,
    header: {
      title: {
        text: '公式测试卷', lines: ['公式测试卷'],
        y_mm: 20, font_pt: 16, line_h_mm: 8,
      },
      subtitle: { text: '满分：10 分', y_mm: 30, font_pt: 10.5 },
      student_items: [
        { label: '班级：', x_mm: 0, underline_x_mm: 14,
          underline_w_mm: 40, y_mm: 38 },
      ],
      student_extra: null,
      notes: { lines: [], y_mm: 40, font_pt: 9, line_h_mm: 6 },
      rules: [44],
      height_mm: 24,
    },
    pages: [{
      page_no: 1,
      blocks: [
        {
          kind: 'stem', y_mm: 50, height_mm: 8,
          title_lines: ['一、选择题'],
        },
        {
          kind: 'q_stems', y_mm: 60, height_mm: 12, line_h_mm: 5.5,
          stem_lines: ['1. 求 $\\frac{1}{2}+\\frac{1}{3}$ 的值'],
        },
        {
          kind: 'choice_row', y_mm: 76, height_mm: 6,
          option_font_pt: 9, option_cols: 4,
          option_cells: [
            { x_mm: 0, y_mm: 0, lines: ['A. $\\frac{5}{6}$'] },
            { x_mm: 42.5, y_mm: 0, lines: ['B. $\\frac{2}{5}$'] },
          ],
        },
      ],
      page_number: { text: '第 1 页 共 1 页', y_mm: 285, align: 'center' },
    }],
  }
}

const html = buildHtml(fixtureLayout(), { title: '测试' })
check('输出是完整 HTML 文档', html.startsWith('<!DOCTYPE html>'))
check('★★ 公式被 SSR 成 KaTeX 静态节点', /class="katex"/.test(html))
check('★★ 正文里没有残留 `$\\frac`（漏渲染的直接证据）',
  !html.includes('$\\frac'), '')
check('独立公式/行内公式都渲染了', (html.match(/class="katex"/g) || []).length >= 3,
  `${(html.match(/class="katex"/g) || []).length} 处`)
check('纸型写进 `@page`（避免 Puppeteer 默认 A4 覆盖）',
  /--zf-page-w:\s*210\.000mm/.test(html), '')
check('★ 行不重排：`white-space: pre` 存在', html.includes('white-space: pre'))
check('中文 font-family 链已写死', html.includes('Noto Sans CJK SC'))
check('配图缺失时印占位而不是留白',
  buildHtml({
    ...fixtureLayout(),
    pages: [{
      page_no: 1,
      blocks: [{
        kind: 'figure', x_mm: 20, y_mm: 60, width_mm: 60,
        image_h_mm: 30, asset_id: 99, caption_lines: [], missing: false,
      }],
      page_number: null,
    }],
  }, { assets: {} }).includes('［配图缺失］'))

// ── 4. 可选：真出一次 PDF ──
const wantPdf = process.argv.includes('--pdf')
if (wantPdf) {
  console.log('\nG4 · Puppeteer 出 PDF')
  try {
    const { renderPdf, shutdown } = await import('../src/pdf.js')
    const raw = await renderPdf(html, { timeoutMs: 30000 })
    // ★ puppeteer 23 的 `page.pdf()` 返回 **Uint8Array**，不是 Buffer。
    //   直接 `.toString()` 会得到 "37,80,68,70,45,…" 这种逗号列表，
    //   于是「PDF 魔术字」这条断言会假失败 —— 踩过一次。
    const buf = Buffer.from(raw)
    check('PDF 字节非空', buf.length > 1000, `${buf.length} bytes`)
    check('PDF 魔术字正确（%PDF-）', buf.subarray(0, 5).toString() === '%PDF-',
      buf.subarray(0, 8).toString())
    await shutdown()
  } catch (e) {
    check('Puppeteer 渲染成功', false, e instanceof Error ? e.message : String(e))
  }
} else {
  console.log('\nG4 · 跳过 Puppeteer（加 --pdf 才跑）')
}

console.log('\n' + '='.repeat(70))
console.log(`  通过 ${pass} / 失败 ${fail}`)
console.log('='.repeat(70))
process.exit(fail === 0 ? 0 : 1)
