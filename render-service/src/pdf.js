/**
 * Puppeteer 渲染：HTML → PDF。
 *
 * ══════════════════════════════════════════════════════════
 *  三条必须遵守的运行约束（计划 §3.2 写死的）
 * ══════════════════════════════════════════════════════════
 *
 *  1. **浏览器实例常驻复用**。每次请求 `puppeteer.launch()` 要 800ms–1.5s，
 *     一份卷子的渲染本身才 200ms 左右。不复用的话服务基本没法用。
 *     但复用要防「启动失败被缓存」：launch 失败必须把缓存的 Promise 清掉，
 *     否则一次失败会让服务永久 500（后面每次拿到的都是同一个 rejected）。
 *
 *  2. **单次并发上限 2**。Chromium 一个 page 打印要 100–200MB 峰值内存，
 *     并发 10 就是 2GB，容器会被 OOM killer 干掉。超出的请求排队，
 *     不返回 503 —— 用户点的是一次「导出 PDF」，等 3 秒比报错好。
 *
 *  3. **30s 超时**。超时后必须**主动关掉 page**：只 reject Promise 不管 page，
 *     那个 page 会一直占着渲染进程，几次之后浏览器就成了僵尸。
 */

import puppeteer from 'puppeteer'

const CONCURRENCY = Math.max(1, Number(process.env.RENDER_CONCURRENCY || 2))
const DEFAULT_TIMEOUT_MS = Number(process.env.RENDER_TIMEOUT_MS || 30000)

let browserPromise = null
let browserRef = null

// ── 计数信号量（够用就好，不引第三方包） ──
let active = 0
const waiting = []

function acquire() {
  if (active < CONCURRENCY) {
    active += 1
    return Promise.resolve()
  }
  return new Promise((resolve) => waiting.push(resolve))
}

function release() {
  const next = waiting.shift()
  if (next) {
    next()          // 名额直接转交，active 不变
  } else {
    active -= 1
  }
}

/** 拿浏览器；失败时**清缓存**，让下次能重新拉起 */
export async function getBrowser() {
  if (browserPromise) return browserPromise

  browserPromise = puppeteer.launch({
    headless: true,
    // ★ 容器里必须 --no-sandbox（Docker 默认没有 user namespace）
    //   --disable-dev-shm-usage：容器 /dev/shm 常只有 64MB，不关会崩
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--font-render-hinting=none',
    ],
    // 允许用镜像里自带的 Chromium（不依赖包管理器下载的那份）
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
  })

  try {
    browserRef = await browserPromise
    browserRef.on('disconnected', () => {
      browserPromise = null
      browserRef = null
    })
    return browserRef
  } catch (e) {
    // ★ 关键：不清理的话，一次启动失败会让服务永久不可用
    browserPromise = null
    browserRef = null
    throw e
  }
}

/** 预热（启动时调用一次，把 1s 冷启动挪到进程启动期） */
export async function warmup() {
  await getBrowser()
}

/**
 * HTML → PDF。
 *
 * @param {string} html
 * @param {{timeoutMs?: number}} opts
 * @returns {Promise<Buffer>}
 */
export async function renderPdf(html, opts = {}) {
  const timeoutMs = opts.timeoutMs || DEFAULT_TIMEOUT_MS
  await acquire()
  try {
    return await renderInner(html, timeoutMs)
  } finally {
    release()
  }
}

async function renderInner(html, timeoutMs) {
  const browser = await getBrowser()
  const page = await browser.newPage()
  let closed = false

  const closePage = async () => {
    if (closed) return
    closed = true
    await page.close().catch(() => {})
  }

  // 看门狗：超时后**主动关页面**，让挂住的 setContent / pdf 立即失败
  let timer = null
  const watchdog = new Promise((_, reject) => {
    timer = setTimeout(() => {
      void closePage()
      reject(new Error(`渲染超时（${timeoutMs}ms）`))
    }, timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()
  })

  try {
    const work = (async () => {
      await page.setContent(html, { waitUntil: 'load', timeout: timeoutMs })
      // ★ 必须等字体真正就绪：`load` 事件不代表 web font 已经能用。
      //   不等的话偶发「公式字体还没加载完就打印」→ 方框。
      await page.evaluate(() => document.fonts.ready.then(() => true))
      return page.pdf({
        printBackground: true,
        // ★ 用 `@page { size: … }`（HTML 里按纸型写死了），
        //   不在这里传 width/height，避免两处口径
        preferCSSPageSize: true,
        timeout: timeoutMs,
      })
    })()

    return await Promise.race([work, watchdog])
  } finally {
    if (timer) clearTimeout(timer)
    await closePage()
  }
}

/** 关浏览器（进程退出时用） */
export async function shutdown() {
  if (browserRef) {
    await browserRef.close().catch(() => {})
  }
  browserPromise = null
  browserRef = null
}

export const stats = () => ({
  concurrency: CONCURRENCY,
  active,
  waiting: waiting.length,
  browser: browserRef ? 'warm' : 'cold',
})
