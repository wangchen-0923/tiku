/**
 * 字体：KaTeX 公式字体**内嵌 base64**，中文字体走系统安装 + 显式 font-family。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么公式字体必须内嵌
 * ══════════════════════════════════════════════════════════
 *
 * KaTeX 的 CSS 默认按 `url(fonts/KaTeX_Main-Regular.woff2)` 相对路径取字体。
 * 我们把 HTML 用 `page.setContent()` 直接塞给无头浏览器 —— 没有 base URL、
 * 没有静态目录、校内网络也常常没有外网。字体取不到时 Chromium **不报错**，
 * 只是把所有数学符号画成方框或退化成衬线体，产物「看起来能打开」但没法用。
 *
 * 所以启动时把 woff2 全部读成 data URI 内联进 CSS，一次性字符串常量。
 * woff / ttf 两档直接丢掉：Chromium 一定支持 woff2，留着只是让 CSS 大三倍。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么中文字体不能内嵌
 * ══════════════════════════════════════════════════════════
 *
 * 一个 Noto Sans CJK SC 的 woff2 有 4–8MB，内嵌后每次渲染都要传 10MB+ 的
 * HTML 给浏览器。所以中文走**系统字体**：镜像里装 `fonts-noto-cjk`，
 * 并在 CSS 里显式写 font-family 链（不写就可能落到没有中文字形的默认字体，
 * 得到满页方框 —— 这正是 `paper/textutil.py` 那个坑的镜像版本）。
 */

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let cached = null

function katexDistDir() {
  const pkg = require.resolve('katex/package.json')
  return path.join(path.dirname(pkg), 'dist')
}

/** woff2 → `data:font/woff2;base64,...` */
function toDataUri(file) {
  const buf = fs.readFileSync(file)
  return `data:font/woff2;base64,${buf.toString('base64')}`
}

/**
 * 生成内嵌了字体的 KaTeX CSS（只算一次）。
 */
export function katexCss() {
  if (cached !== null) return cached

  const dist = katexDistDir()
  const cssPath = path.join(dist, 'katex.min.css')
  let css = fs.readFileSync(cssPath, 'utf8')

  const fontDir = path.join(dist, 'fonts')
  const files = fs.readdirSync(fontDir).filter((f) => f.endsWith('.woff2'))

  // ① woff2 → data URI
  for (const f of files) {
    const uri = toDataUri(path.join(fontDir, f))
    css = css.split(`url(fonts/${f})`).join(`url(${uri})`)
  }

  // ② 去掉 woff / ttf 两档（Chromium 一定支持 woff2）
  css = css.replace(
    /,url\(fonts\/[^)]+\.(?:woff|ttf)\)\s*format\("(?:woff|truetype)"\)/g,
    '',
  )

  cached = css
  return cached
}

/** 中文字体链（镜像里装 fonts-noto-cjk；Windows 开发机有雅黑/宋体） */
export const CJK_FONT_STACK = [
  '"ZfCJK"',
  '"Noto Sans CJK SC"',
  '"Noto Sans SC"',
  '"Source Han Sans SC"',
  '"Microsoft YaHei"',
  '"PingFang SC"',
  '"SimSun"',
  'serif',
].join(',')

/**
 * 自检：字体有没有真的内嵌进去。
 *
 * ★ 只统计 `data:` 出现次数是不够的 —— 要确认**没有一个 fonts/ 相对路径残留**，
 *   残留的那一个就正好是渲染时取不到的。
 */
export function fontReport() {
  const css = katexCss()
  const dataUris = (css.match(/data:font\/woff2;base64,/g) || []).length
  const leftover = (css.match(/url\(fonts\//g) || []).length
  return {
    katex_css_bytes: css.length,
    embedded_fonts: dataUris,
    leftover_relative_urls: leftover,
    ok: dataUris > 0 && leftover === 0,
  }
}
