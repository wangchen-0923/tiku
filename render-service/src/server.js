/**
 * 知否 · 试卷渲染服务（内网常驻）。
 *
 * ══════════════════════════════════════════════════════════
 *  接口契约（计划 §3.2，不要改）
 * ══════════════════════════════════════════════════════════
 *
 *   POST /render
 *     Header: X-Render-Token: <共享密钥>   ← 内网共享密钥，**不走用户 JWT**
 *     Body:   { layout: {...排版 JSON...},
 *               assets: { "12": "data:image/png;base64,..." },
 *               title: "期中试卷" }
 *     →  200 application/pdf
 *
 *   GET /health
 *     →  { ok, browser, fonts, concurrency, renders, uptime_s }
 *
 * ══════════════════════════════════════════════════════════
 *  为什么鉴权用共享密钥而不是 JWT
 * ══════════════════════════════════════════════════════════
 *
 * 这个服务**不该知道用户是谁**。它的输入是一份排版 JSON，输出是 PDF 字节；
 * 越权判断、机构隔离、试卷归属全在 Django 侧（那里有 `for_org()` 和权限类）。
 *
 * 如果这里也解析 JWT，等于开了第二个鉴权入口：两处判据迟早会不一致，
 * 而不一致的那一次就是越权。共享密钥只回答一个问题：
 * 「这个请求是不是从我们的 Django 打过来的」。
 *
 * ★ 没有配置 `RENDER_TOKEN` 时**拒绝启动**，不静默放行。
 *   一个无鉴权的渲染服务放在内网，就是一台「谁都能用」的 PDF 生成器。
 */

import crypto from 'node:crypto'
import http from 'node:http'

import express from 'express'

import { fontReport } from './fonts.js'
import { buildHtml } from './html.js'
import { renderPdf, shutdown, stats, warmup } from './pdf.js'

const PORT = Number(process.env.RENDER_PORT || 7001)
const TOKEN = (process.env.RENDER_TOKEN || '').trim()
const ALLOW_INSECURE = process.env.RENDER_ALLOW_INSECURE === '1'
const MAX_BODY = process.env.RENDER_MAX_BODY || '64mb'
const STARTED_AT = Date.now()

if (!TOKEN && !ALLOW_INSECURE) {
  console.error(
    '[render] 缺少 RENDER_TOKEN。渲染服务拒绝以无鉴权状态启动。\n' +
      '         本地调试可设 RENDER_ALLOW_INSECURE=1，生产必须配共享密钥。',
  )
  process.exit(1)
}

if (!TOKEN && ALLOW_INSECURE) {
  console.warn('[render] 警告：RENDER_ALLOW_INSECURE=1，本次运行不校验 X-Render-Token')
}

const app = express()
app.disable('x-powered-by')
// 配图走 base64，一份含 10 张图的卷子 JSON 可以到 20MB+
app.use(express.json({ limit: MAX_BODY }))

let renders = 0
let failures = 0

/** 定长比较，避免用 `===` 泄露密钥长度前缀（inner 服务也照做，成本为零） */
function tokenOk(given) {
  if (!TOKEN) return true
  const a = Buffer.from(String(given || ''))
  const b = Buffer.from(TOKEN)
  if (a.length !== b.length) return false
  return crypto.timingSafeEqual(a, b)
}

app.get('/health', (req, res) => {
  res.json({
    ok: true,
    service: 'zhifou-render',
    version: '1.0.0',
    uptime_s: Math.round((Date.now() - STARTED_AT) / 1000),
    renders,
    failures,
    fonts: fontReport(),
    ...stats(),
  })
})

app.post('/render', async (req, res) => {
  if (!tokenOk(req.get('X-Render-Token'))) {
    res.status(401).json({ code: 'RENDER.UNAUTHORIZED', message: '无效的渲染令牌' })
    return
  }

  const layout = req.body?.layout
  if (!layout || !Array.isArray(layout.pages) || !layout.pages.length) {
    res.status(400).json({
      code: 'RENDER.INVALID_LAYOUT',
      message: 'layout.pages 为空 —— 排版 JSON 不合法或试卷没有题',
    })
    return
  }

  try {
    const html = buildHtml(layout, {
      assets: req.body?.assets || {},
      title: req.body?.title || '',
    })
    const pdf = await renderPdf(html, {
      timeoutMs: Number(req.body?.timeout_ms) || undefined,
    })
    renders += 1
    res.setHeader('Content-Type', 'application/pdf')
    res.setHeader('Content-Length', String(pdf.length))
    res.setHeader('X-Render-Engine', 'NODE_PUPPETEER')
    res.setHeader('X-Render-Pages', String(layout.pages.length))
    res.end(pdf)
  } catch (e) {
    failures += 1
    const msg = e instanceof Error ? e.message : String(e)
    // 超时回 504：Django 侧据此走 reportlab 回退，回 500 会被当成 bug 报上来
    const timeout = /超时|timeout/i.test(msg)
    res.status(timeout ? 504 : 500).json({
      code: timeout ? 'RENDER.TIMEOUT' : 'RENDER.FAILED',
      message: msg,
    })
  }
})

const server = http.createServer(app)

// ★ 30s 超时之外再加一道 HTTP 层兜底：请求体都传不完的连接不能一直挂着
server.setTimeout(120000)

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[render] 监听 0.0.0.0:${PORT}  并发上限 ${stats().concurrency}`)
  // 预热：把 ~1s 的浏览器冷启动挪到进程启动期，第一个真实请求就不用等
  warmup()
    .then(() => console.log('[render] 浏览器已就绪'))
    .catch((e) => console.error('[render] 浏览器启动失败：', e.message))
})

let closing = false
async function stop(signal) {
  if (closing) return
  closing = true
  console.log(`[render] 收到 ${signal}，正在关闭…`)
  server.close()
  await shutdown()
  process.exit(0)
}

process.on('SIGTERM', () => void stop('SIGTERM'))
process.on('SIGINT', () => void stop('SIGINT'))
