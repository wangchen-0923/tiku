# 知否 · 试卷渲染服务镜像
#
# ★★ 这个镜像里**必须**装中文字体，否则线上 PDF 满页方框、
#    而本地（Windows 有雅黑）一切正常 —— 这正是计划 §3.2 点名的坑。
#
# ★ 用 debian-slim 而不是 alpine：Chromium 在 musl 上要额外编译，
#   puppeteer 官方只对 glibc 出二进制。省下的那点镜像体积不值得。
#
# 构建：
#     docker build -t zhifou-render:1.0.0 render-service
#
# 运行（推荐用 deploy/compose.render.yml）：
#     docker run --rm -p 7001:7001 \
#       -e RENDER_TOKEN=change-me zhifou-render:1.0.0

FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PUPPETEER_CACHE_DIR=/usr/local/share/puppeteer \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    RENDER_PORT=7001

# ── ① 中文字体 ──
# fonts-noto-cjk 是 PDF 里中文能否正常显示的唯一保障。
# ★ 不装的话 Chromium **不报错**，只是画出方框 —— 这种失败最难发现。
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-noto-cjk \
      fonts-noto-cjk-extra \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── ② Chromium 运行期依赖 ──
# 缺任何一个都会让浏览器起不来（报 "Failed to launch the browser process"）
RUN apt-get update && apt-get install -y --no-install-recommends \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── ③ 依赖（先只拷 manifest，利用层缓存） ──
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev || npm install --omit=dev

# ── ④ 浏览器本体 ──
# ★ 用 `puppeteer browsers install` 而不是让 npm 装依赖时顺手下：
#   后者在 CI 里常因网络中断留下半份浏览器，报错信息还很难懂。
RUN npx puppeteer browsers install chrome

COPY src ./src
COPY scripts ./scripts

EXPOSE 7001

# 健康检查打 /health：它同时会暴露字体内嵌情况，
# 出问题时 `docker inspect` 就能看到，不用进容器翻日志
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.RENDER_PORT||7001)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/server.js"]
