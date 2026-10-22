/**
 * 公式（TeX）解析与 SSR —— 与 `web/src/utils/math.ts` 必须同构。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么这里还要再写一遍扫描器
 * ══════════════════════════════════════════════════════════
 *
 * 前端用 KaTeX 在浏览器里渲染，服务端用 KaTeX 在 Node 里渲染。
 * 定界符识别规则哪怕只差一条（比如「`$…$` 里含中文就不算公式」），
 * 就会出现「预览里是分数、导出的 PDF 里是 `$\\frac{1}{2}$` 原文」——
 * 而这种不一致最难查，因为两边各自都跑得好好的。
 *
 * 所以规则在**三处**完全一致，改动必须同时改：
 *   · `web/src/utils/math.ts`          （浏览器）
 *   · `server/apps/common/mathtext.py` （后端排版 / 降级）
 *   · `render-service/src/math.js`     （本文件，PDF）
 *
 * ══════════════════════════════════════════════════════════
 *  为什么 output 只要 'html'
 * ══════════════════════════════════════════════════════════
 *
 * KaTeX 默认同时输出 MathML（给读屏器）与 HTML（给眼睛）。
 * 试卷 PDF 是**印刷品**：MathML 那份会让 DOM 体积翻倍，
 * 而 Chromium 排版时 MathML 的 baseline 与 HTML 部分不完全一致，
 * 混排时偶发 0.5px 的上下抖动。视觉一致优先。
 */

import katex from 'katex'

/** 一段文本切出来的片段：`{type, value, display}` */
const CJK_RE = /[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uf900-\ufaff]/

/**
 * `$…$` 里的内容像不像 TeX（与前后端同一条判据）。
 */
function looksLikeTex(inner) {
  if (!inner.trim()) return false
  if (inner.includes('\\')) return true
  if (CJK_RE.test(inner)) return false
  return !inner.includes('\n')
}

/**
 * 切分混合文本，返回 `[{type:'text'|'math', value, display}]`。
 * 未闭合的定界符一律退回字面文本。
 */
export function scanMath(input) {
  const s = String(input ?? '')
  const out = []
  let buf = ''
  let i = 0

  const flush = () => {
    if (buf) {
      out.push({ type: 'text', value: buf, display: false })
      buf = ''
    }
  }
  const push = (tex, display) => {
    flush()
    out.push({ type: 'math', value: tex, display })
  }

  while (i < s.length) {
    const c = s[i]
    const next = s[i + 1]

    if (c === '\\' && next !== undefined) {
      if (next === '(' || next === '[') {
        const close = next === '(' ? '\\)' : '\\]'
        const end = s.indexOf(close, i + 2)
        if (end > i) {
          push(s.slice(i + 2, end), next === '[')
          i = end + 2
          continue
        }
      }
      buf += c + next
      i += 2
      continue
    }

    if (c !== '$') {
      buf += c
      i += 1
      continue
    }

    if (next === '$') {
      const end = s.indexOf('$$', i + 2)
      if (end > i + 1) {
        const tex = s.slice(i + 2, end)
        if (tex.trim()) {
          push(tex, true)
          i = end + 2
          continue
        }
      }
      buf += '$$'
      i += 2
      continue
    }

    const end = s.indexOf('$', i + 1)
    if (end > i + 1) {
      const tex = s.slice(i + 1, end)
      if (looksLikeTex(tex)) {
        push(tex, false)
        i = end + 1
        continue
      }
    }
    buf += '$'
    i += 1
  }

  flush()
  return out
}

export function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

const cache = new Map()
const CACHE_LIMIT = 2000

/** 单条 TeX → KaTeX HTML（带缓存；同一份卷子里 π、√2 这类会反复出现） */
export function renderTex(tex, display = false) {
  const key = (display ? 'D' : 'I') + tex
  const hit = cache.get(key)
  if (hit !== undefined) return hit

  let html
  try {
    html = katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      errorColor: '#c62828',
      strict: 'ignore',
      trust: false,
      output: 'html',
    })
  } catch {
    html = `<code>${escapeHtml(tex)}</code>`
  }

  if (cache.size >= CACHE_LIMIT) {
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
  cache.set(key, html)
  return html
}

/**
 * 纯文本 → HTML 片段（文本转义 + 公式 SSR）。
 *
 * ★ 排版 JSON 里的行**已经是折好的行**，所以这里不做任何折行，
 *   只把公式换成静态节点。折行是 Django `paper/layout.py` 的职责，
 *   Node 侧再折一次就会和预览对不上。
 */
export function renderLine(line) {
  const segs = scanMath(String(line ?? ''))
  if (!segs.length) return ''
  return segs
    .map((seg) =>
      seg.type === 'math'
        ? renderTex(seg.value, seg.display)
        : escapeHtml(seg.value),
    )
    .join('')
}

/** 这段文本里有没有公式（给自检用） */
export function hasMath(text) {
  return scanMath(String(text ?? '')).some((s) => s.type === 'math')
}
