"""自有云服务器生产配置。

★ 与 `production.py` 的关系：本文件只做**最小覆盖**，
  其余全部沿用 production 的安全策略（HTTPS 强制、HSTS、安全 Cookie 等）。

★ 为什么要单独一个文件而不是改 production.py：
  production.py 是通用生产基线；本文件只解决「前面挂了 Nginx 反向代理」
  这一件事。以后换部署平台（比如容器服务自带负载均衡）时，
  再新建一个 settings 文件即可，互不干扰。

★ 部署步骤（重要）：
  把本文件复制到 `server/zhifou/settings/serverprod.py`，
  即最终路径为 `/app/zhifou/settings/serverprod.py`。
  然后用 `DJANGO_SETTINGS_MODULE=zhifou.settings.serverprod` 启动。
"""

from __future__ import annotations

import os

from .production import *  # noqa: F403

# ★★ 关键：豁免 /health/ 的 HTTPS 跳转。
#
#   production.py 里 `SECURE_SSL_REDIRECT = True`，效果是
#   **所有 http 请求一律 301 跳 https**。
#   而 Nginx 在容器网络里探活走的是 `http://127.0.0.1:8000/health/`
#   （容器内部没有证书，不可能用 https）。
#   不豁免的话，探活拿到的是 301 而不是 200 →
#   healthcheck 判定失败 → 容器被标记 unhealthy →
#   `depends_on: condition: service_healthy` 的其它服务永远起不来。
#
#   ★ 这里的正则是**不带前导斜杠**的路径（Django 的约定）。
SECURE_REDIRECT_EXEMPT = [r"^health/$"]

# ══════════════════════════════════════════════════════════════════
#  ★★ 临时开关：域名还没备案期间，用公网 IP 做小范围试用
# ──────────────────────────────────────────────────────────────────
#  为什么需要它：
#    大陆服务器的 80/443 只放行「已完成接入备案」的域名，域名未备案时
#    请求会被云厂商拦截，压根到不了 Nginx。这时唯一的通路是公网 IP。
#    但 production.py 硬编码了 SECURE_SSL_REDIRECT / *_COOKIE_SECURE，
#    纯 http 访问时会有两个致命后果：
#      1. 所有请求被 301 跳到 https，而没有证书 → 页面打不开；
#      2. 登录 Cookie 带 Secure 属性，http 下浏览器根本不会回传
#         → 表现为「登录成功但一刷新就掉线」。
#
#  怎么用：
#    在 .env 里加一行  ZF_ALLOW_INSECURE_HTTP=1  然后重启 web 容器。
#
#  ★★★ 三条红线，违反任何一条都是安全事故：
#    1. 只在「没有域名 / 域名未备案」的试用期打开；
#    2. 打开期间密码与会话在公网上是【明文传输】，绝不能让校外访问，
#       也不要录入真实学生姓名、身份证号等敏感信息做试跑；
#    3. 域名备案通过、切回 https 之前，必须把这一行删掉并重建容器。
#       忘了删 = 永久以明文模式运行，而且不会有任何报错提示。
#
#  默认（不设这个变量）行为完全不变：强制 HTTPS、安全 Cookie、HSTS 一年。
# ══════════════════════════════════════════════════════════════════
if os.environ.get("ZF_ALLOW_INSECURE_HTTP", "").strip() == "1":
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    # 试用期只给 /health/ 之外的路径放行没意义了，一并留空即可
    SECURE_REDIRECT_EXEMPT = []

# ── CSRF 信任来源（可选）──
# 从环境变量读，多个用英文逗号分隔，**必须带 scheme**（如 https://tiku.xxx.cn）。
# 没有单独设置时，复用 CORS_ALLOWED_ORIGINS（production.py 已从
# ZF_CORS_ORIGINS 解析好，本身就带 scheme）。
# 两者都为空也不影响启动，只是从浏览器管理后台（/admin/）以外的地方
# 发 POST 时可能被 CSRF 拦下。
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ZF_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
] or list(CORS_ALLOWED_ORIGINS)  # noqa: F405
