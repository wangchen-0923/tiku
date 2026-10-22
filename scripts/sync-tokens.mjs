/**
 * 从 `docs/tech/11-前端实现规范与设计令牌.md` 第二节提取设计令牌，
 * 生成 `web/src/styles/tokens.css` 与 `web/src/styles/zf-base.css`。
 *
 * ★★ 为什么要有这个脚本：
 *
 *   11 文档规定令牌「一律不得改值、不得删减」，且是**唯一值来源**。
 *   但如果 `tokens.css` 是当初手抄的一份拷贝，那「唯一」就只是
 *   说说而已——改文档的人不会想起来同步代码，改代码的人也不会
 *   回头改文档，两边迟早分叉，而分叉后没人知道哪个是对的。
 *
 *   有了这个脚本：**文档是唯一来源，代码是生成物**。
 *   改令牌的流程变成「改文档 → 跑本脚本」，不可能分叉。
 *
 * 用法：
 *     node scripts/sync-tokens.mjs           # 生成
 *     node scripts/sync-tokens.mjs --check   # 只校验是否已同步（CI 用）
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const DOC = join(ROOT, 'docs', 'tech', '11-前端实现规范与设计令牌.md')
const OUT_DIR = join(ROOT, 'web', 'src', 'styles')

const CHECK = process.argv.includes('--check')

/** 取「## 二、」到「## 三、」之间的所有 ```css 代码块。 */
function extractSecondSection(md) {
  const lines = md.split('\n')
  const start = lines.findIndex((l) => l.startsWith('## 二、'))
  const end = lines.findIndex((l) => l.startsWith('## 三、'))
  if (start < 0 || end < 0 || end <= start) {
    throw new Error('未能在文档里定位「## 二、」到「## 三、」的范围')
  }
  const blocks = []
  let cur = null
  for (const line of lines.slice(start, end)) {
    if (line.trim().startsWith('```')) {
      if (cur === null) cur = []
      else {
        blocks.push(cur)
        cur = null
      }
    } else if (cur !== null) {
      cur.push(line)
    }
  }
  if (blocks.length < 5) {
    throw new Error(`预期至少 5 个代码块，实际 ${blocks.length} 个`)
  }
  return blocks
}

// ★★ 反引号：这里用普通字符串数组而不是模板字面量。
//
//   头部说明里要写 Markdown 行内代码（带反引号），而写在模板字面量
//   里会把模板提前终止 —— 报 `SyntaxError: Unexpected identifier`。
//   用数组 + join 就完全绕开这个陷阱。
const TOKENS_HEADER =
  [
    '/* ══════════════════════════════════════════════════════════════════',
    '   知否 · 设计令牌（唯一值来源）',
    '   ══════════════════════════════════════════════════════════════════',
    '',
    '   ★★ 本文件是**生成物**，逐字来自 docs/tech/11-前端实现规范与设计令牌.md',
    '   第二节。文档原文：',
    '',
    '     「色值、圆角、间距、阴影、字体、字号、行高、字重、动效曲线',
    '       一律不得改值、不得删减。」',
    '',
    '   ★★ 不要手工编辑本文件。要改变量：',
    '     1. 改 11 文档第二节（并走评审）',
    '     2. 跑 node scripts/sync-tokens.mjs',
    '     3. 跑 node scripts/sync-tokens.mjs --check 确认已同步',
    '',
    '   生成命令：node scripts/sync-tokens.mjs',
    '   ══════════════════════════════════════════════════════════════════ */',
    '',
    '',
  ].join('\n')

const BASE_HEADER =
  [
    '/* ══════════════════════════════════════════════════════════',
    '   知否 · 全局基础（同样是生成物，来自 11 文档第二节块 5）',
    '   ══════════════════════════════════════════════════════════ */',
    '',
    '',
  ].join('\n')

function main() {
  if (!existsSync(DOC)) {
    console.error(`✗ 找不到文档：${DOC}`)
    process.exit(1)
  }
  const blocks = extractSecondSection(readFileSync(DOC, 'utf8'))
  const tokens = [
    ...blocks[0],
    '',
    ...blocks[1],
    '',
    ...blocks[2],
    '',
    ...blocks[3],
  ].join('\n')
  const base = blocks[4].join('\n')

  const files = [
    { path: join(OUT_DIR, 'tokens.css'), body: TOKENS_HEADER + tokens + '\n' },
    { path: join(OUT_DIR, 'zf-base.css'), body: BASE_HEADER + base + '\n' },
  ]

  const nTokens = tokens
    .split('\n')
    .filter((l) => l.trim().startsWith('--zf')).length

  let drift = 0
  for (const f of files) {
    const exists = existsSync(f.path)
    const old = exists ? readFileSync(f.path, 'utf8') : null
    if (CHECK) {
      if (old !== f.body) {
        console.error(`✗ 未同步：${f.path}`)
        drift++
      }
    } else {
      mkdirSync(dirname(f.path), { recursive: true })
      writeFileSync(f.path, f.body, 'utf8')
    }
  }

  if (CHECK) {
    if (drift) {
      console.error(`\n  ${drift} 个文件与文档不一致。跑 node scripts/sync-tokens.mjs 修正。`)
      process.exit(1)
    }
    console.log(`✓ 令牌已同步（${nTokens} 个）`)
  } else {
    console.log(`✓ 已生成 ${files.length} 个文件，共 ${nTokens} 个令牌`)
    for (const f of files) console.log(`    ${f.path.replace(ROOT, '.')}`)
  }

  // ★★ 引用校验：任何地方 `var(--zf-xxx)` 用了一个**不存在的令牌**
  //    都必须失败。
  //
  //    为什么需要它：`var(--zf-nonexistent)` 在 CSS 里**不报错**，
  //    只是让那条声明失效——颜色/间距悄悄变回默认值，
  //    肉眼往往当成「设计就是这样」。我写第一版 app.css 时凭记忆
  //    写了 `--zf-duration-normal` / `--zf-ease-out` / `--zf-weight-bold`
  //    三个不存在的名字，全部静默失效。
  const defined = new Set(
    [...tokens.matchAll(/(--zf-[a-z0-9-]+)\s*:/g)].map((m) => m[1]),
  )
  const offenders = []
  for (const file of walk(join(ROOT, 'web', 'src'))) {
    if (!/\.(css|vue|ts)$/.test(file)) continue
    const raw = readFileSync(file, 'utf8')
    // ★★ 先剥掉注释再检查引用。
    //
    //   我在注释里写了「我原来写成 var(--zf-leading-relaxed, 1.7)」
    //   作为说明——那是**举例**，不是引用。不剥注释的话，
    //   检查器会对自己的说明文字报错，而**假阳性会让人不再看告警**
    //   （与「错误告警比不告警更糟」是同一个道理）。
    const text = raw
      .replace(/\/\*[\s\S]*?\*\//g, '')      // CSS / JS 块注释
      .replace(/(^|[^:])\/\/[^\n]*/g, '$1')  // JS 行注释
      .replace(/<!--[\s\S]*?-->/g, '')       // HTML / Vue 模板注释
    for (const m of text.matchAll(/var\((--zf-[a-z0-9-]+)/g)) {
      if (!defined.has(m[1])) {
        offenders.push(`${file.replace(ROOT, '.')}: ${m[1]}`)
      }
    }
  }
  if (offenders.length) {
    console.error(`\n✗ 引用了 ${offenders.length} 处未定义的令牌：`)
    for (const o of offenders) console.error(`    ${o}`)
    process.exit(1)
  }
  console.log(`✓ 令牌引用全部有定义（检查 ${defined.size} 个）`)

  // ★★ 模板注释语法校验：前端里不能出现 Django 的注释定界符。
  //
  //    这已经是**第三次**犯同一类错了，三次分属三种模板系统：
  //
  //      Django  `{% comment %}`（多行）/ `{# #}`（**仅单行**）
  //              → 多行写 `{# #}` 不解析，注释原文渲染到页面上
  //      Django  `{% comment %}` 块**内部**再写 `{# #}`
  //              → 单行注释吃到 `{% endcomment %}`，报 Invalid block tag
  //      Vue     注释是 HTML 注释，写 `{# #}` 同样原样渲染
  //
  //    ★ 共同点：**三者都不报错**，只是把注释当文本显示出来。
  //      所以必须靠机械检查，靠眼睛看截图会漏（我就漏过一次）。
  const vueOffenders = []
  for (const file of walk(join(ROOT, 'web'))) {
    if (file.includes('node_modules') || file.includes(`${sep}dist${sep}`)) {
      continue
    }
    // 只查会被渲染的模板与标记文件；`.ts` 里的字符串不算
    if (!/\.(vue|html)$/.test(file)) continue
    const text = readFileSync(file, 'utf8')
    for (const [i, line] of text.split('\n').entries()) {
      if (/\{#|\#\}|\{%/.test(line)) {
        vueOffenders.push(
          `${file.replace(ROOT, '.')}:${i + 1}  ${line.trim().slice(0, 60)}`,
        )
      }
    }
  }
  if (vueOffenders.length) {
    console.error(
      `\n✗ 前端模板里出现 ${vueOffenders.length} 处 Django 风格注释定界符` +
        `（它们会被**原样渲染**到页面上）：`,
    )
    for (const o of vueOffenders) console.error(`    ${o}`)
    console.error('    → Vue 用 HTML 注释；Django 用 comment 标签')
    process.exit(1)
  }
  console.log('✓ 前端模板无 Django 风格注释（不会渲染出注释原文）')

  // ══════════════════════════════════════════════════════════
  //  4) 模板里不能出现 Markdown 加粗
  // ══════════════════════════════════════════════════════════
  //
  //  ★ 同一类错误的另一种形态：**在 HTML/Vue 模板里写 `**粗体**`**。
  //    HTML 不认 Markdown，所以星号会**原样显示**成 `**机器不确定**`。
  //
  //    ★ 我实测踩到过两次（知识体系页的说明文案、批改页的提示文案）——
  //      都是在**从文档里抄文案**时带进来的。文档里 `**` 是加粗，
  //      粘到模板里就变成了字面量。
  //
  //    ★ 和上面那条一样：**不报错**，只是难看。所以同样靠机械检查。
  //      只查 `<template>` 段，且先剥掉 HTML 注释 ——
  //      `script` 段里的 `**` 可能是注释或字符串，不算问题。
  const mdOffenders = []
  for (const file of walk(join(ROOT, 'web'))) {
    if (file.includes('node_modules') || file.includes(`${sep}dist${sep}`)) {
      continue
    }
    if (!/\.vue$/.test(file)) continue
    const text = readFileSync(file, 'utf8')
    const m = text.match(/<template>([\s\S]*?)<\/template>/)
    if (!m) continue
    const body = m[1].replace(/<!--[\s\S]*?-->/g, '')
    const re = /\*\*[^*\n]{1,40}\*\*/g
    let hit
    while ((hit = re.exec(body)) !== null) {
      // 换算成文件内的行号，便于定位
      const line = text.slice(0, m.index + m[0].indexOf(body) + hit.index)
        .split('\n').length
      mdOffenders.push(
        `${file.replace(ROOT, '.')}:${line}  ${hit[0].slice(0, 50)}`,
      )
    }
  }
  if (mdOffenders.length) {
    console.error(
      `\n✗ 前端模板里出现 ${mdOffenders.length} 处 Markdown 加粗` +
        `（HTML 不认，星号会原样显示）：`,
    )
    for (const o of mdOffenders) console.error(`    ${o}`)
    console.error('    → 用 <b>…</b> 或 <strong>…</strong>')
    process.exit(1)
  }
  console.log('✓ 前端模板无 Markdown 加粗（不会显示字面星号）')
}

/** 递归列出目录下的所有文件。 */
function walk(dir) {
  const out = []
  if (!existsSync(dir)) return out
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

main()
