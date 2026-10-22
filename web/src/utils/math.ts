/**
 * 公式（TeX）解析与渲染 —— 前端唯一入口。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么是「TeX 原文存库、各处就地渲染」
 * ══════════════════════════════════════════════════════════
 *
 * 备选方案是「存 KaTeX 生成的 HTML」。否决的三条理由：
 *
 *  ① **后端清洗会毁掉它**。`apps/common/sanitize.py` 的白名单里没有
 *     `class`，也没有 `<math>`；KaTeX 的输出到达后端会被洗成
 *     一堆没有 class 的 `<span>`，公式彻底散架。
 *     要放行就得给白名单开 `class` —— 那是 XSS 的口子，不能开。
 *
 *  ② **改不了**。老师想在题干里把 `\frac` 改成小数，存 HTML 就只能
 *     重新排版整个公式；存 TeX 就是在文本框里改几个字符。
 *
 *  ③ **导出源要的是 TeX**。PDF 走 Node 侧 KaTeX SSR，Word 走文本降级，
 *     两者都需要原始 TeX。存 HTML 等于把 TeX 丢了。
 *
 * 所以：**`$…$` / `$$…$$` 直接写在 `stem_text` / `content_text` /
 * `explanation_text` 里**，每次显示时渲染。`stem_hash` 用 TeX 原文算，
 * 同一道题两次录入的哈希天然一致。
 *
 * ══════════════════════════════════════════════════════════
 *  行内 `$…$` 的误判防护
 * ══════════════════════════════════════════════════════════
 *
 * 中文试卷里 `$` 也可能只是货币符号：「单价 $5 到 $10 之间」。
 * 直接把它当公式，会渲染出一个诡异的斜体 `5 到 `。
 *
 * 判据（`_looksLikeTex`）：**含反斜杠命令，或整段不含中文**。
 *   · `$x^2$`      → 是（无中文）
 *   · `$\frac{1}{2}$` → 是（有反斜杠）
 *   · `$\text{甲}$`   → 是（有反斜杠，允许 `\text` 里带中文）
 *   · `$5 到 `     → 否（有中文、无反斜杠）→ 原样保留美元符号
 *
 * ★ 残余误判：`$5$`（单个数字、无中文）会被当公式渲染成斜体 5。
 *   2026-10 扫描题库 1946 题，`$` 出现 0 次，暂按此实现。
 */

import katex from 'katex'

/** 一段文本切出来的片段 */
export interface MathSegment {
  /** `text` = 原样文本；`math` = TeX 公式 */
  type: 'text' | 'math'
  /** 原文（math 段是**不含定界符**的 TeX） */
  value: string
  /** 是否独立公式（`$$…$$` / `\[…\]`），决定 KaTeX 的 displayMode */
  display: boolean
}

const CJK_RE = /[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uf900-\ufaff]/

/**
 * 这一段 `$…$` 里的内容像不像 TeX。
 *
 * 见文件头「行内 `$…$` 的误判防护」。
 */
function looksLikeTex(inner: string): boolean {
  if (!inner.trim()) return false
  if (inner.includes('\\')) return true
  // 含中文又不带命令 → 更可能是货币符号之间的一句话
  if (CJK_RE.test(inner)) return false
  // 不允许内联公式跨行
  return !inner.includes('\n')
}

/**
 * 把一段混合文本切成 `text` / `math` 片段。
 *
 * 支持的定界符：`$$…$$`、`\[…\]`（独立公式）、`$…$`、`\(…\)`（行内）。
 * `\$` 是转义的美元符号，不参与配对。
 *
 * ★ **未闭合时一律退回字面文本** —— 老师打了一半 `$` 就保存，
 *   不应该让整页渲染成红字报错。
 */
export function scanMath(input: string): MathSegment[] {
  const s = String(input ?? '')
  const out: MathSegment[] = []
  let buf = ''
  let i = 0

  const flushText = (): void => {
    if (buf) {
      out.push({ type: 'text', value: buf, display: false })
      buf = ''
    }
  }
  const pushMath = (tex: string, display: boolean): void => {
    flushText()
    out.push({ type: 'math', value: tex, display })
  }

  while (i < s.length) {
    const c = s[i]
    const next = s[i + 1]

    // ── 转义字符：`\$` `\\` 等原样保留，跳过配对判断 ──
    if (c === '\\' && next !== undefined) {
      // `\(…\)` / `\[…\]`：行内 / 独立公式
      if (next === '(' || next === '[') {
        const close = next === '(' ? '\\)' : '\\]'
        const end = s.indexOf(close, i + 2)
        if (end > i) {
          pushMath(s.slice(i + 2, end), next === '[')
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

    // ── `$$…$$` 独立公式 ──
    if (next === '$') {
      const end = s.indexOf('$$', i + 2)
      if (end > i + 1) {
        const tex = s.slice(i + 2, end)
        if (tex.trim()) {
          pushMath(tex, true)
          i = end + 2
          continue
        }
      }
      buf += '$$'
      i += 2
      continue
    }

    // ── `$…$` 行内公式 ──
    const end = s.indexOf('$', i + 1)
    if (end > i + 1) {
      const tex = s.slice(i + 1, end)
      if (looksLikeTex(tex)) {
        pushMath(tex, false)
        i = end + 1
        continue
      }
    }
    buf += '$'
    i += 1
  }

  flushText()
  return out
}

/** 这段文本里有没有公式（含未闭合的 `$` 不算）。 */
export function containsMath(text: string | null | undefined): boolean {
  return scanMath(String(text ?? '')).some((seg) => seg.type === 'math')
}

// ══════════════════════════════════════════════════════════
//  渲染
// ══════════════════════════════════════════════════════════

/**
 * 渲染缓存的容量。
 *
 * ★ 必须有：`.vue` 里 `computed` 会在每次依赖变化时重算，
 *   一个 50 题的列表页一次渲染要做 50 次以上 `renderToString`。
 *   题型里「同一道公式反复出现」（如 π、√2）极常见。
 */
const CACHE_LIMIT = 600
const cache = new Map<string, string>()

/**
 * 单条 TeX → KaTeX HTML。
 *
 * ★ `output: 'html'` —— 不要 MathML 那份。KaTeX 默认同时输出
 *   MathML（给读屏器）和 HTML（给眼睛），DOM 体积翻倍；
 *   试卷页一屏几十个公式，而且 `v-html` 里塞 `<math>` 在某些
 *   旧版 WebView 上会污染行高。视觉正确优先。
 *
 * ★ `throwOnError: false` —— 语法错时 KaTeX 会渲染成红色原文，
 *   而不是抛异常让整个列表白屏。校验走 `validateTex()`。
 */
export function renderTex(tex: string, display = false): string {
  const key = (display ? 'D' : 'I') + tex
  const hit = cache.get(key)
  if (hit !== undefined) return hit

  let html: string
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
    // 理论上走不到（throwOnError:false 已兜住），留一层保险
    html = `<code class="zf-math__err">${escapeHtml(tex)}</code>`
  }

  if (cache.size >= CACHE_LIMIT) {
    // Map 保持插入序，删最早的一个即可（够用的近似 LRU）
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
  cache.set(key, html)
  return html
}

/** HTML 转义（**只**用于我们自己的文本片段，不用于 TeX 输出） */
export function escapeHtml(s: string): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 纯文本 → 可直接 `v-html` 的安全 HTML（文本转义 + 公式渲染）。
 *
 * 用于「没有富文本，只有纯文本」的场景：题干快照、答案、解析。
 */
export function renderMathText(text: string | null | undefined): string {
  const segs = scanMath(String(text ?? ''))
  if (!segs.length) return ''
  return segs
    .map((seg) =>
      seg.type === 'math'
        ? renderTex(seg.value, seg.display)
        : escapeHtml(seg.value).replace(/\n/g, '<br>'),
    )
    .join('')
}

/**
 * 已有 HTML → 只在**文本节点**里渲染公式。
 *
 * ★★ 为什么不用正则替换整段 HTML：题干里可能有 `<img src="a$b.png">`、
 *    `<a href="?q=$1">`。正则分不清「这是正文的 `$`」还是
 *    「这是属性值里的 `$`」，替换进去就是一堆坏属性。
 *    `DOMParser` 走真正的 HTML5 解析，只对文本节点动手，属性天然免疫。
 *
 * ★ 已经渲染过的节点（`.katex`）会跳过 —— 保证幂等，
 *   后端直接下发 SSR 好的 HTML 时不会二次渲染。
 */
export function renderMathInHtml(html: string | null | undefined): string {
  const src = String(html ?? '')
  if (!src) return ''
  if (!src.includes('$') && !src.includes('\\(') && !src.includes('\\[')) {
    return src
  }

  const doc = new DOMParser().parseFromString(
    `<body>${src}</body>`,
    'text/html',
  )
  const body = doc.body

  const walker = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT)
  const targets: Text[] = []
  let node = walker.nextNode()
  while (node) {
    const t = node as Text
    if (
      t.nodeValue &&
      (t.nodeValue.includes('$') ||
        t.nodeValue.includes('\\(') ||
        t.nodeValue.includes('\\[')) &&
      !(t.parentElement?.closest('.katex'))
    ) {
      targets.push(t)
    }
    node = walker.nextNode()
  }

  for (const t of targets) {
    const segs = scanMath(t.nodeValue || '')
    if (!segs.some((s) => s.type === 'math')) continue
    const frag = doc.createDocumentFragment()
    for (const seg of segs) {
      if (seg.type === 'text') {
        frag.appendChild(doc.createTextNode(seg.value))
      } else {
        const holder = doc.createElement('span')
        holder.innerHTML = renderTex(seg.value, seg.display)
        frag.appendChild(holder)
      }
    }
    t.parentNode?.replaceChild(frag, t)
  }

  return body.innerHTML
}

/**
 * 统一入口：自动判断「这段是纯文本还是 HTML」。
 *
 * 判据与后端 `sanitize.looks_like_html` 同构（真标签形状）。
 * 题库里 `stem_html` 大量与 `stem_text` 同值（都是纯文本），
 * 所以不能只看字段名。
 */
const TAG_SHAPE = /<\/?([a-zA-Z][a-zA-Z0-9]*)(?:\s+[a-zA-Z:_-][a-zA-Z0-9:_.-]*\s*=|\s*\/?>)/
const KNOWN_TAGS = new Set([
  'p', 'br', 'b', 'strong', 'em', 'i', 'u', 's', 'del', 'ins', 'sub', 'sup',
  'span', 'div', 'hr', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr',
  'td', 'th', 'img', 'a', 'code', 'pre', 'blockquote',
])

export function looksLikeHtml(v: string | null | undefined): boolean {
  const s = String(v ?? '')
  if (!s.includes('<')) return false
  const m = TAG_SHAPE.exec(s)
  return !!m && KNOWN_TAGS.has(m[1].toLowerCase())
}

/**
 * ★ 全站唯一的「把一段题干/选项/解析变成 HTML」的函数。
 *
 * 六处渲染点（题目详情、试卷预览、答题页、阅卷页、答题卡、导出页）
 * 都必须走这里，否则「详情里公式好好的、答题页里是 `$x^2$` 原文」
 * 这类不一致迟早出现。
 */
export function renderRich(v: string | null | undefined): string {
  const s = String(v ?? '')
  if (!s) return ''
  return looksLikeHtml(s) ? renderMathInHtml(s) : renderMathText(s)
}

// ══════════════════════════════════════════════════════════
//  校验与提示
// ══════════════════════════════════════════════════════════

export interface TexCheck {
  ok: boolean
  message: string
}

/** 校验一条 TeX 能不能渲染（给输入组件的即时提示用）。 */
export function validateTex(tex: string): TexCheck {
  if (!tex.trim()) return { ok: true, message: '' }
  try {
    katex.renderToString(tex, {
      throwOnError: true,
      strict: 'ignore',
      displayMode: false,
      output: 'html',
    })
    return { ok: true, message: '' }
  } catch (e) {
    const raw = e instanceof Error ? e.message : String(e)
    // KaTeX 的消息带一段「KaTeX parse error: …」前缀与位置，掐短一点
    return { ok: false, message: raw.replace(/^KaTeX parse error:\s*/, '') }
  }
}

/**
 * 截断到 `n` 个字符，但**不切断公式**（与后端 `truncate_keep_math` 同构）。
 *
 * 列表页的题干预览会截断。裸 `slice(0, 60)` 落在 `$\frac{1}{2}$` 中间时，
 * 拿到的是 `$\frac{1` —— 定界符不配对，整段退化成字面文本，
 * 看起来像公式功能坏了，其实是截断的锅。
 *
 * 落在公式中间就扩到公式结束（最多再多 40 字）；扩不了就整段去掉。
 */
export function truncateMath(s: string | null | undefined, n: number): string {
  const src = String(s ?? '').trim()
  if (!src) return ''
  const limit = Math.max(0, Math.floor(n))
  if (src.length <= limit) return src

  // 先算出每个公式片段的位置
  let cursor = 0
  const spans: Array<[number, number]> = []
  for (const seg of scanMath(src)) {
    const len =
      seg.type === 'math'
        ? seg.value.length + (seg.display ? 4 : 2)
        : seg.value.length
    if (seg.type === 'math') spans.push([cursor, cursor + len])
    cursor += len
  }

  let cut = limit
  for (const [start, end] of spans) {
    if (start >= cut) break
    if (end > cut) {
      cut = end <= limit + 40 ? end : start
      break
    }
  }
  let out = src.slice(0, cut).replace(/\s+$/, '')
  if ((out.match(/(?<!\\)\$/g) || []).length % 2 === 1) {
    out = out.slice(0, out.lastIndexOf('$')).replace(/\s+$/, '')
  }
  return out + (cut < src.length ? '…' : '')
}

/** 这段文本里有几处公式没闭合（`$` 单数个 / `$$` 单数个）。 */
export function unclosedMath(text: string | null | undefined): number {
  const s = String(text ?? '')
  let bad = 0
  // `$$` 先配对，剩下的单 `$` 再配对
  const dollars = (s.match(/(?<!\\)\$/g) || []).length
  if (dollars % 2 !== 0) bad += 1
  const parens = (s.match(/\\\(/g) || []).length
  const parensClose = (s.match(/\\\)/g) || []).length
  if (parens !== parensClose) bad += 1
  const brackets = (s.match(/\\\[/g) || []).length
  const bracketsClose = (s.match(/\\\]/g) || []).length
  if (brackets !== bracketsClose) bad += 1
  return bad
}

/** 光标位置是否已经在某个公式里（决定插入时要不要补定界符）。 */
export function inMathAt(text: string, pos: number): boolean {
  const s = String(text ?? '')
  let cursor = 0
  for (const seg of scanMath(s)) {
    const start = cursor
    // math 段的 `value` 不含定界符，源码长度要补回去
    cursor += seg.value.length + (seg.type === 'math' ? (seg.display ? 4 : 2) : 0)
    if (seg.type === 'math' && pos > start && pos <= cursor) return true
  }
  return false
}

// ══════════════════════════════════════════════════════════
//  工具条片段
// ══════════════════════════════════════════════════════════

export interface MathSnippet {
  /** 按钮上显示的符号（可以直接是一小段 TeX，用 KaTeX 渲染成图标） */
  label: string
  /** 插入的模板；`§` 是「选中内容 / 光标位置」的占位符 */
  snippet: string
  /** 悬停提示 */
  title: string
}

/**
 * 公式工具条。按**中职数学/物理/机械/电子**的实际用频排序。
 *
 * `§` 占位符规则：有选中内容就替换成选中的内容，没有就变成空，
 * 光标停在 `§` 原来的位置。
 */
export const MATH_SNIPPETS: MathSection[] = [
  {
    name: '常用',
    items: [
      { label: '\\frac{a}{b}', snippet: '\\frac{§}{}', title: '分数' },
      { label: '\\sqrt{x}', snippet: '\\sqrt{§}', title: '根号' },
      { label: 'x^{n}', snippet: '^{§}', title: '上标 / 乘方' },
      { label: 'x_{n}', snippet: '_{§}', title: '下标' },
      { label: '\\times', snippet: '\\times ', title: '乘号 ×' },
      { label: '\\div', snippet: '\\div ', title: '除号 ÷' },
      { label: '\\pm', snippet: '\\pm ', title: '正负 ±' },
      { label: '\\cdot', snippet: '\\cdot ', title: '点乘 ·' },
      { label: '\\ge', snippet: '\\ge ', title: '大于等于 ≥' },
      { label: '\\le', snippet: '\\le ', title: '小于等于 ≤' },
      { label: '\\ne', snippet: '\\ne ', title: '不等于 ≠' },
      { label: '\\approx', snippet: '\\approx ', title: '约等于 ≈' },
    ],
  },
  {
    name: '几何 / 机械',
    items: [
      { label: '\\pi', snippet: '\\pi ', title: '圆周率 π' },
      { label: '\\angle', snippet: '\\angle ', title: '角 ∠' },
      { label: '\\triangle', snippet: '\\triangle ', title: '三角形 △' },
      { label: '\\perp', snippet: '\\perp ', title: '垂直 ⊥' },
      { label: '\\parallel', snippet: '\\parallel ', title: '平行 ∥' },
      { label: 'x^{\\circ}', snippet: '^{\\circ}', title: '度 °' },
      { label: '\\overline{AB}', snippet: '\\overline{§}', title: '上划线（线段 AB）' },
      { label: '\\vec{F}', snippet: '\\vec{§}', title: '向量' },
      { label: '\\alpha', snippet: '\\alpha ', title: 'α' },
      { label: '\\beta', snippet: '\\beta ', title: 'β' },
      { label: '\\theta', snippet: '\\theta ', title: 'θ' },
      { label: '\\omega', snippet: '\\omega ', title: 'ω' },
    ],
  },
  {
    name: '函数 / 单位',
    items: [
      { label: '\\sin', snippet: '\\sin ', title: '正弦' },
      { label: '\\cos', snippet: '\\cos ', title: '余弦' },
      { label: '\\tan', snippet: '\\tan ', title: '正切' },
      { label: '\\log', snippet: '\\log ', title: '对数' },
      { label: '\\lim', snippet: '\\lim_{§}', title: '极限' },
      { label: '\\sum', snippet: '\\sum_{§}', title: '求和' },
      { label: '\\int', snippet: '\\int_{§}', title: '积分' },
      { label: '\\rightarrow', snippet: '\\rightarrow ', title: '箭头 →' },
      { label: '\\infty', snippet: '\\infty ', title: '无穷 ∞' },
      { label: '\\text{…}', snippet: '\\text{§}', title: '公式里的中文' },
      { label: '\\begin{cases}', snippet: '\\begin{cases} § \\end{cases}', title: '方程组' },
      { label: '\\left( \\right)', snippet: '\\left( § \\right)', title: '自适应括号' },
    ],
  },
]

export interface MathSection {
  name: string
  items: MathSnippet[]
}
