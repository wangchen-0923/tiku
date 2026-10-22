/**
 * Vite 配置。
 *
 * ★★ 为什么用 dev proxy 而不是配 CORS：
 *
 *   前端跑在 `5173`，后端在 `8001` —— 跨源。两条路：
 *     1. 后端加 `django-cors-headers` 并开 CORS
 *     2. 前端 dev server 代理 `/api` 到后端
 *
 *   选 2 的理由：
 *     · **不动后端**：CORS 是生产配置，为本地开发改它，容易在
 *       上线时把 `CORS_ALLOW_ALL_ORIGINS` 一起带上去
 *     · **同源**：浏览器看来都在 `5173`，Cookie / 预检 / 混合内容
 *       这些坑一律不出现
 *     · 生产部署时由 Nginx 反代承担同样职责，所以「前端相对路径
 *       请求 `/api/v1/...`」这个写法**开发与生产一致**
 */

import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  /**
   * ★★ dev server 绑哪个地址。
   *
   *   默认 `127.0.0.1`：**只监听本机**。这是安全默认值——
   *   开发机上往往有未提交的代码与测试数据，不该默认对整个局域网开放。
   *
   *   ★ 但**学生答题必须走局域网**：学生在自己手机上扫码，
   *     `127.0.0.1` 在手机上指向手机自己，永远打不开。
   *     所以要跑通「学生扫码答题」这条链路，必须放开监听：
   *
   *         # web/.env.local
   *         VITE_DEV_HOST=0.0.0.0
   *         VITE_STUDENT_BASE_URL=http://192.168.1.10:5173
   *
   *     `VITE_STUDENT_BASE_URL` 决定**二维码里编什么地址**，
   *     `VITE_DEV_HOST` 决定**服务是否接受来自局域网的连接**，
   *     两者要一起设，缺一个都会打不开。
   */
  const host = env.VITE_DEV_HOST?.trim() || '127.0.0.1'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host,
      // ★ 端口固定 5173；若被占用 Vite 会自己 +1，
      //   所以下面 `strictPort` 打开，宁可报错也不要静默换端口
      //   （换端口会让「给你的链接」失效）。
      port: 5173,
      strictPort: true,
      // ★ 放开监听时允许局域网主机名访问（Vite 默认只允许 IP/localhost）
      allowedHosts: host === '127.0.0.1' ? undefined : true,
      watch: {
        /**
         * ★★ 忽略「原子写入」留下的临时目录/文件。
         *
         *   有的编辑器/工具保存文件时是「先写临时文件再 rename」
         *   （临时目录形如 `.index.ts.<随机>.tmpdir/`）。这些临时项
         *   落在 `src/` 里，会被 Vite 的 watcher 盯上；而它们出现与消失
         *   只在毫秒级，Windows 上很容易撞 `EBUSY` 直接**把 dev server
         *   打崩**（实测 2026-10-17，进程退出码 1）。
         *
         *   ★ 临时文件本来就不该触发 HMR —— 忽略它们既修崩溃，也少几次
         *     无意义的整页重载。
         */
        ignored: ['**/.*.tmpdir/**', '**/*.tmp', '**/*.tmpdir/**'],
      },
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8001',
          changeOrigin: true,
        },
        '/health': {
          target: 'http://127.0.0.1:8001',
          changeOrigin: true,
        },
        // 媒体与静态资源（答题卡 PDF、题目配图）
        '/media': {
          target: 'http://127.0.0.1:8001',
          changeOrigin: true,
        },
        '/static': {
          target: 'http://127.0.0.1:8001',
          changeOrigin: true,
        },
      },
    },
  }
})
