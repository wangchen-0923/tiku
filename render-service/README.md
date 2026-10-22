# 知否 · 试卷渲染服务

把 Django 算好的**排版 JSON** 渲染成 PDF：KaTeX 服务端渲染公式 →
无头 Chromium 打印。是计划 §3.2「公式导出走 B」的实现。

```
Django  paper/layout.py          →  排版 JSON（毫米坐标、折好的行）
   │
   └──POST /render──────────────▶  本服务
                                    ├─ KaTeX SSR（`$…$` → 静态节点）
                                    ├─ 内联 woff2 公式字体
                                    └─ Puppeteer → PDF
   ◀────────application/pdf──────┘
```

## 为什么不在这里排版

**折行、分页、行数一个都不在这里算。** 那是 `server/apps/paper/layout.py`
的职责，前端预览（`PaperPreviewCanvas.vue`）用的是同一份 JSON。
Node 侧只要算过一次，预览和 PDF 就开始分叉 —— 表现为「预览 3 页、
导出 4 页」，比公式没渲染还难查。

所以本服务是**纯渲染器**：mm 进，mm 出，一个像素都不猜。

## 快速开始

```bash
# 1. 装依赖（会下载 Chromium，约 150MB）
npm install

# 2. 自检（不需要浏览器）
npm run selfcheck

# 3. 自检 + 真出一份 PDF
node scripts/selfcheck.js --pdf

# 4. 起服务
RENDER_ALLOW_INSECURE=1 npm start        # 本地，不校验令牌
# 或
RENDER_TOKEN=xxx npm start               # 生产口径
```

```bash
curl -s localhost:7001/health | jq
# { "ok": true, "browser": "warm", "embedded_fonts": 20, ... }

curl -s -X POST localhost:7001/render \
  -H "X-Render-Token: xxx" -H "Content-Type: application/json" \
  -d '{"layout": {...}, "assets": {}}' -o out.pdf
```

## 接口

### `POST /render`

| 项 | 值 |
|---|---|
| Header | `X-Render-Token: <共享密钥>` |
| Body | `{ layout, assets?, title?, timeout_ms? }` |
| 成功 | `200` + `application/pdf` |
| 令牌错 | `401 RENDER.UNAUTHORIZED` |
| 排版 JSON 不合法 | `400 RENDER.INVALID_LAYOUT` |
| 渲染超时 | `504 RENDER.TIMEOUT`（Django 据此回退 reportlab） |
| 其他失败 | `500 RENDER.FAILED` |

`assets` 是 `{ "asset_id": "data:image/png;base64,…" }`。Django 侧
`render_node.assets_to_data_uris()` 已经处理好 MIME 嗅探。

### `GET /health`

```json
{
  "ok": true, "browser": "warm", "concurrency": 2,
  "active": 0, "waiting": 0, "renders": 12, "failures": 0,
  "fonts": { "embedded_fonts": 20, "leftover_relative_urls": 0, "ok": true }
}
```

`fonts.ok` 是判断「线上会不会满页方框」的最快办法：
`leftover_relative_urls > 0` 就说明有一档字体还在按相对路径取，取不到。

## 三个必须解决的坑（都有对应代码）

| 坑 | 现象 | 处理 |
|---|---|---|
| **中文字体** | PDF 里中文全是方框，本地却正常 | Dockerfile 装 `fonts-noto-cjk`；CSS 显式写 `font-family` 链 |
| **公式字体离线** | 数学符号退化成衬线体或方框 | `fonts.js` 把 20 个 woff2 全内嵌成 base64，丢掉 woff/ttf 两档 |
| **服务不可用** | 导出卡 30 秒后失败 | Django 侧熔断 120s + 自动回退 reportlab，并标记 `render_engine=LEGACY` |

## 部署

```bash
# 需要先有 RENDER_TOKEN（与 Django 的 ZF_RENDER_TOKEN 一致）
export RENDER_TOKEN=$(openssl rand -hex 24)
docker compose -f deploy/compose.render.yml up -d --build

# Django 侧
export ZF_RENDER_URL=http://127.0.0.1:7001
export ZF_RENDER_TOKEN=$RENDER_TOKEN
```

内存：常驻约 300MB + 单次打印峰值 200MB，所以 `limit 1.2G` / 并发 2。
`shm_size: 512mb` 必须给 —— 容器默认 64MB，多页打印会以
`Target closed` 崩掉，而报错信息完全看不出是 `/dev/shm` 的问题。

## 验收（必须与 U36 一起做）

1. `curl /health` → `fonts.ok: true`
2. 从 Django 导一份**含公式**的卷子，目视检查：
   - 公式是真公式（可选中、可缩放），不是 `1/2` 这类近似文本
   - 中文不是方框
   - 分页与前端预览一致
3. 把渲染服务停掉再导一次 → 应在 1s 内回退成功，且导出结果里
   出现 `PAPER.FORMULA_DEGRADED`（不是报错、不是空白）
