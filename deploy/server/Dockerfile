# ══════════════════════════════════════════════════════════════
#  知否 · 后端生产镜像
#
#  ★ 为什么必须容器化：本项目 requires-python >= 3.12，
#    而 OpenCloudOS 9 / CentOS 系自带 Python 往往是 3.9 / 3.11，
#    编译安装既慢又容易把系统自带的 yum/dnf 搞坏。
#    用官方 Python 镜像是最省事、最不容易出错的做法。
#
#  ★ 构建上下文（重要）：
#      本文件必须放在 /opt/zhifou/Dockerfile
#      后端源码必须放在 /opt/zhifou/server
#      构建命令必须在 /opt/zhifou 目录执行
#    因为下面用的是 `COPY ./server/ /app/`。
#
#  ★ 首次构建会慢（5～15 分钟）：rapidocr-onnxruntime + onnxruntime
#    + opencv 加起来约 200MB，且要从 PyPI 下载。这是正常的，不要中断。
# ══════════════════════════════════════════════════════════════

FROM python:3.12-slim-bookworm

# ── 环境变量 ──
# TZ                    容器时区（日志、定时任务按北京时间）
# PYTHONUNBUFFERED      print 立刻刷到日志，否则 docker logs 看不到实时输出
# PYTHONDONTWRITEBYTECODE 不生成 .pyc，镜像更小、挂载卷里不留垃圾
# DJANGO_SETTINGS_MODULE  默认用生产配置（manage.py 会用到）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=zhifou.settings.serverprod

# ★ 如果你的服务器 apt-get update 一直卡住（国内机器常见），
#   把下面这行的注释符号 `#` 去掉，改用腾讯云 Debian 镜像站。
#   其它云厂商请把 mirrors.cloud.tencent.com 换成你自己的镜像站域名。
# RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources

# ── 系统依赖（每个都写了「谁在用」，不要随便删）──
#   libgomp1        OpenCV / onnxruntime 的 OpenMP 运行库。★ 缺了会直接
#                   ImportError: libgomp.so.1: cannot open shared object file
#   libglib2.0-0    OpenCV 运行期依赖（slim 镜像默认不带）
#   fonts-noto-cjk  ReportLab 导出中文 PDF 用。★ 缺了不会报错，但会回落到
#                   内置 STSong-Light，字体观感差（textutil.py 有回落逻辑）
#   tzdata          时区数据（Asia/Shanghai 依赖它）
#   curl            ★ web 服务的 docker healthcheck 要用它探活 /health/
RUN set -eux; \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime; \
    echo "${TZ}" > /etc/timezone; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        fonts-noto-cjk \
        tzdata \
        curl \
    ; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python 依赖 ──
# 先单独 COPY 依赖清单，是为了利用 Docker 缓存：
# 只改代码不改依赖时，这一步不会重新跑（省 5～10 分钟）。
COPY requirements.prod.txt /app/requirements.prod.txt

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.prod.txt

# ★★ 必须放在依赖装完之后、且是最后一步：
#    rapidocr-onnxruntime 会自动拉入 **opencv-python**（带 GUI 依赖的版本），
#    它和我们要的 opencv-python-headless 装的是同一个 `cv2` 目录，
#    谁后装谁生效。这里强制重装一次 headless，确保容器里最终是
#    无 GUI 版本（否则会报 ImportError: libGL.so.1）。
RUN pip install --no-cache-dir --force-reinstall --no-deps \
    "opencv-python-headless>=4.9,<5.0"

# ── 应用代码 ──
COPY ./server/ /app/

# collectstatic 与媒体写入的目标目录（实际内容由 compose 的卷挂载提供）
RUN mkdir -p /app/staticfiles /app/media

# gunicorn 监听端口。★ 不要用 80：80 交给宿主机 Nginx。
EXPOSE 8000

# 默认启动命令（docker-compose.yml 里会再写一次，两处保持一致）
CMD ["gunicorn", "zhifou.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--threads", "2", \
     "--worker-class", "gthread", \
     "--timeout", "300", \
     "--graceful-timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
