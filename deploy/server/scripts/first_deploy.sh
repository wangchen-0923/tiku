#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  知否题库 · 首次部署脚本（OpenCloudOS 9 + 宝塔面板 + Docker Compose）
# ───────────────────────────────────────────────────────────────────
#  用法（在宝塔「终端」里一行执行）：
#      bash /opt/zhifou/scripts/first_deploy.sh
#
#  本脚本做的事（按顺序，任何一步失败都会立刻停下并打印原因）：
#      1. 检查 Docker / Docker Compose 是否可用
#      2. 检查内存与磁盘，给出 4G 内存下的取舍建议
#      3. 创建 /opt/zhifou 与 /www/wwwroot/zhifou/dist 目录
#      4. 检查 .env（没有就从模板复制，并强制要求先填密钥）
#      5. 校验 docker-compose.yml
#      6. 拉取/构建镜像（首次含 OCR 模型，约 5–15 分钟）
#      7. 启动 db / redis 并等待健康
#      8. 执行数据库迁移 migrate
#      9. 收集静态文件 collectstatic
#     10. 启动全部服务 up -d
#     11. 自检 http://127.0.0.1:8000/health/
#
#  ★ 本脚本不会删除任何已有数据，也不会替你停掉任何服务。
# ═══════════════════════════════════════════════════════════════════

# ── 维护负责人：software-engineer-ops（运维侧）──
# 本脚本不修改、也不依赖修改：docker-compose.yml / Dockerfile / .env.prod.example /
# requirements.prod.txt / settings/ —— 这些由 software-engineer（容器侧）维护。
set -euo pipefail

export COMPOSE_PROJECT_NAME="zhifou"
export LC_ALL="${LC_ALL:-C.UTF-8}"

# ── 路径契约（与 Docker 侧完全一致，不要改）──
readonly APP_DIR="${APP_DIR:-/opt/zhifou}"
readonly SPA_DIR="${SPA_DIR:-/www/wwwroot/zhifou/dist}"
readonly COMPOSE_FILE="${APP_DIR}/docker-compose.yml"
readonly ENV_FILE="${APP_DIR}/.env"
readonly ENV_EXAMPLE="${APP_DIR}/.env.prod.example"
readonly BACKUP_DIR="${APP_DIR}/backups"
readonly STATIC_DIR="${APP_DIR}/staticfiles"
readonly MEDIA_DIR="${APP_DIR}/media"
# 宿主机映射端口：默认 8000；被别的程序占用时在 .env 里设 ZF_WEB_HOST_PORT。
# ★ 三处必须一致：compose 的 ports、.env 的 ZF_WEB_HOST_PORT、Nginx 的 proxy_pass。
ZF_WEB_HOST_PORT="$(
    grep -E '^[[:space:]]*ZF_WEB_HOST_PORT[[:space:]]*=' "$ENV_FILE" 2>/dev/null \
        | tail -n 1 | cut -d '=' -f 2- | tr -d ' \r' || true
)"
readonly HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${ZF_WEB_HOST_PORT:-8000}/health/}"
readonly TOTAL_STEPS="11"

# ── 颜色（宝塔终端支持；管道重定向时自动关闭）──
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_BOLD=""; C_OFF=""
fi

hr()    { printf '%s\n' "────────────────────────────────────────────────────────"; }
info()  { printf '%s[说明]%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
ok()    { printf '%s[完成]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()  { printf '%s[警告]%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()   { printf '%s[错误]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()   { err "$*"; hr; err "脚本已停止。请把上面红色的提示整段复制给技术人员。"; exit 1; }
step()  { printf '\n%s[第 %s/%s 步]%s %s\n' "$C_BOLD" "$1" "$TOTAL_STEPS" "$C_OFF" "$2"; hr; }

# 组合命令：统一带项目名 zhifou + 显式指定 compose 文件
# （显式给 -f 是为了不管你在哪个目录下敲命令都能找到它，
#   同时 compose 里的相对路径会以 /opt/zhifou 为基准解析）
dc() { docker compose -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"; }

cmd_exists() { command -v "$1" >/dev/null 2>&1; }

# Windows 换行符自检：CRLF 会让 bash 报 “\r: command not found”
self_crlf_check() {
    local self="${BASH_SOURCE[0]:-}"
    [ -n "$self" ] && [ -f "$self" ] || return 0
    if LC_ALL=C grep -q $'\r' "$self" 2>/dev/null; then
        err "这个脚本文件里混进了 Windows 换行符（CRLF），bash 执行会报错。"
        err "请先执行下面这一行修复，再重新运行本脚本："
        err "    sed -i 's/\r\$//' $self"
        exit 1
    fi
}

# 读取 .env 里某个键的值（找不到就用默认值）
read_env() {
    local key="$1" default="${2:-}" val=""
    if [ -f "$ENV_FILE" ]; then
        val="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2- || true)"
        val="$(printf '%s' "$val" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        # 去掉成对引号
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
    fi
    printf '%s' "${val:-$default}"
}

# 这个服务在 docker-compose.yml 里存在吗
have_service() {
    local svc="$1" all=""
    all="$(dc config --services 2>/dev/null || true)"
    printf '%s\n' "$all" | grep -qx "$svc"
}

# 等待某个服务变成 healthy（最多等 max 秒）
wait_healthy() {
    local svc="$1" max="${2:-120}" waited="0" cid="" status=""
    info "等待 ${svc} 就绪（最多 ${max} 秒）……"
    while [ "$waited" -lt "$max" ]; do
        cid="$(dc ps -q "$svc" 2>/dev/null | head -n 1 || true)"
        if [ -n "$cid" ]; then
            status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)"
            case "$status" in
                healthy)
                    ok "${svc} 已就绪（healthy）"
                    return 0
                    ;;
                none)
                    ok "${svc} 没有配置健康检查，跳过等待"
                    return 0
                    ;;
                unhealthy)
                    err "${svc} 健康检查失败，下面是它最近 60 行日志："
                    dc logs --tail=60 "$svc" || true
                    return 1
                    ;;
            esac
        fi
        sleep 3
        waited=$((waited + 3))
    done
    err "等 ${svc} 超过 ${max} 秒还是没就绪。下面是它最近 60 行日志："
    dc logs --tail=60 "$svc" || true
    return 1
}

# 等到 PostgreSQL 真的能接受连接为止。
# ★ 为什么不能只靠 wait_healthy：
#   docker compose run 不会等 depends_on 里的 condition: service_healthy
#   （--wait 只有 up 才有），冷启动的 PG 要 initdb 5–20 秒才收连接。
#   不显式等，migrate 就会报 could not connect to server。
wait_pg_ready() {
    local max="${1:-180}" waited="0"
    local u
    u="$(read_env POSTGRES_USER "$(read_env ZF_DB_USER "zhifou")")"
    info "等待 PostgreSQL 真正可以接受连接（最多 ${max} 秒）……"
    while [ "$waited" -lt "$max" ]; do
        if dc exec -T db pg_isready -U "$u" >/dev/null 2>&1; then
            ok "PostgreSQL 已可接受连接（pg_isready 通过）"
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    err "等了 ${max} 秒 PostgreSQL 还是不接受连接。下面是 db 的日志："
    dc logs --tail=60 db || true
    return 1
}

# 非交互式确认（cron / 管道场景默认跳过）
confirm() {
    local prompt="$1" ans=""
    if [ ! -t 0 ]; then
        warn "当前不是交互式终端，自动跳过：${prompt}"
        return 1
    fi
    printf '%s' "$prompt"
    read -r ans || ans=""
    case "$ans" in
        y | Y | yes | YES) return 0 ;;
        *) return 1 ;;
    esac
}

# HTTP GET（curl 优先，没有就 wget）
http_get() {
    local url="$1"
    if cmd_exists curl; then
        curl -fsS --max-time 10 "$url"
    elif cmd_exists wget; then
        wget -qO- --timeout=10 "$url"
    else
        die "系统里既没有 curl 也没有 wget，没法做健康检查。请先执行：dnf install -y curl"
    fi
}

# ═══════════════════════════════════════════════════════════════════
step "1" "环境体检：Docker / Docker Compose"
# ═══════════════════════════════════════════════════════════════════
self_crlf_check

if [ "$(id -u)" -ne 0 ]; then
    die "请用 root 用户执行（宝塔终端默认就是 root）。先执行：sudo -i"
fi
ok "当前是 root 用户"

if ! cmd_exists docker; then
    err "没有检测到 docker 。OpenCloudOS 9 上请依次执行下面几行安装："
    err "    dnf install -y dnf-plugins-core"
    err "    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo"
    err "    dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
    err "    systemctl enable --now docker"
    exit 1
fi
ok "docker 已安装：$(docker --version 2>/dev/null || true)"

if ! docker compose version >/dev/null 2>&1; then
    err "docker 装了，但缺少 compose 插件。请执行："
    err "    dnf install -y docker-compose-plugin"
    err "    （或者用宝塔面板：软件商店 → 搜索「Docker管理器」→ 安装）"
    exit 1
fi

# ★ 硬门槛：必须是 Compose v2 或更高（v2 / v3 / v5 … 都可以，v1 不行）。
#   老版本 docker-compose v1（命令中间是「横杠」）不认识 docker-compose.yml 里的
#   name: 顶层字段和 depends_on: condition: service_healthy，会启动失败且报错很难懂。
#   ★ 判据是「大版本 >= 2」，不能写成「== 2」——实测 Compose 已经到 v5 了，
#     写死 2 会把装了新版的机器全部误杀（它们完全能跑这套配置）。
compose_ver="$(docker compose version --short 2>/dev/null | head -n 1 || true)"
if [ -z "$compose_ver" ]; then
    compose_ver="$(docker compose version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1 || true)"
fi
compose_major="$(printf '%s' "$compose_ver" | grep -oE '^[0-9]+' || true)"
# 非数字守卫：空串或其它脏数据不能进下面的 -ge，否则会报 integer expression expected
case "$compose_major" in
    '' | *[!0-9]*) compose_major="" ;;
esac
if [ -n "$compose_major" ] && [ "$compose_major" -ge 2 ]; then
    ok "docker compose 版本 ${compose_ver}（>= v2，符合要求）"
else
    err "docker compose 必须是 >= v2，当前识别到：${compose_ver:-（识别不出来）}"
    err "老版本 docker-compose v1（命令中间是横杠）会让知否启动失败。"
    err "请执行：  dnf install -y docker-compose-plugin"
    err "装完再确认：  docker compose version     ← 注意是空格，不是横杠"
    exit 1
fi
if cmd_exists docker-compose; then
    warn "检测到系统里还装了老版 docker-compose（带横杠）。请用不带横杠的 docker compose。"
fi

if ! docker info >/dev/null 2>&1; then
    info "docker 服务没在运行，尝试启动……"
    systemctl start docker || die "启动 docker 失败，请执行 systemctl status docker 看报错。"
    sleep 3
    docker info >/dev/null 2>&1 || die "docker 守护进程起不来，请执行 journalctl -u docker -n 50 看原因。"
fi
ok "docker 守护进程正在运行"

# ═══════════════════════════════════════════════════════════════════
step "2" "环境体检：内存与磁盘（4G 内存要精打细算）"
# ═══════════════════════════════════════════════════════════════════
mem_total="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
mem_avail="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
info "内存：总共 ${mem_total} MB，可用 ${mem_avail} MB"
info "磁盘（/opt 所在分区）："
df -h /opt | sed 's/^/    /'

if [ "$mem_total" -le 4096 ]; then
    warn "这是一台 4G 内存的机器。知否本身要跑 PostgreSQL + Redis + 后端 + 2 个后台任务进程，"
    warn "如果宝塔里还装着用不到的 MySQL / PHP / 其它网站，内存会很紧张。"
    info "当前占内存最多的程序（前 8 个）："
    ps -eo comm,rss --sort=-rss --no-headers 2>/dev/null | head -n 8 |
        awk '{printf "    %-28s %6.0f MB\n", $1, $2/1024}' || true
    warn "★ 如果确认下面这些服务你根本用不到，可以手工停掉释放内存"
    warn "  （脚本不会替你执行，请自己确认后再敲）："
    echo "      systemctl stop mysqld  && systemctl disable mysqld    # 宝塔自带的 MySQL"
    echo "      systemctl stop php-fpm && systemctl disable php-fpm   # PHP"
    echo "      systemctl stop redis   && systemctl disable redis     # 系统里的 Redis（容器里另有一个）"
fi

if [ "${mem_avail:-0}" -lt 1500 ]; then
    warn "可用内存不足 1.5G，构建镜像时可能会被系统杀掉。建议先停掉上面那些无关服务再继续。"
    if ! confirm "内存偏低，仍要继续吗？(y/N)："; then
        die "已按你的选择停止。先清理内存后再重跑本脚本。"
    fi
fi

disk_avail_mb="$(df -Pm /opt | awk 'NR==2 {print $4}')"
if [ "${disk_avail_mb:-0}" -lt 10240 ]; then
    warn "磁盘可用空间不足 10G（${disk_avail_mb} MB）。镜像 + OCR 模型 + 数据库大约需要 8–12G。"
    if ! confirm "磁盘偏小，仍要继续吗？(y/N)："; then
        die "已按你的选择停止。请先扩容磁盘或清理空间。"
    fi
fi
ok "资源体检结束"

# ═══════════════════════════════════════════════════════════════════
step "3" "创建目录"
# ═══════════════════════════════════════════════════════════════════
mkdir -p "$APP_DIR" "$SPA_DIR" "$BACKUP_DIR" "$STATIC_DIR" "$MEDIA_DIR" "${APP_DIR}/scripts" "${APP_DIR}/nginx"
ok "已确保这些目录存在："
printf '    %s\n' "$APP_DIR" "$SPA_DIR" "$BACKUP_DIR" "$STATIC_DIR" "$MEDIA_DIR"

# ═══════════════════════════════════════════════════════════════════
step "4" "检查配置文件 docker-compose.yml 与 .env"
# ═══════════════════════════════════════════════════════════════════
if [ ! -f "$COMPOSE_FILE" ]; then
    err "找不到 $COMPOSE_FILE"
    err "请先把 docker-compose.yml 上传到 $APP_DIR 目录下，再重跑本脚本。"
    err "（宝塔上传：文件 → 进入 /opt/zhifou → 上传）"
    exit 1
fi
ok "找到 $COMPOSE_FILE"

# ── 先处理 .env（compose 校验依赖它，顺序不能反）──
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        cp -n "$ENV_EXAMPLE" "$ENV_FILE"
        warn "没有找到 $ENV_FILE，已经从模板 $ENV_EXAMPLE 复制了一份。"
    else
        err "既没有 $ENV_FILE，也没有模板 $ENV_EXAMPLE。"
        err "请把 .env.prod.example 上传到 $APP_DIR 目录下，再重跑本脚本。"
        exit 1
    fi
fi

# 占位符检查：凡是 `键=<...>` 这种形式的都算没填（<请生成> / <改成你的域名> 等）
PLACEHOLDER_RE='^[A-Za-z_][A-Za-z0-9_]*=<[^>]*>|CHANGE_ME|YOUR_'
if grep -nE "$PLACEHOLDER_RE" "$ENV_FILE" >/dev/null 2>&1; then
    err "$ENV_FILE 里还有没填写的占位符，下面的行必须改："
    grep -nE "$PLACEHOLDER_RE" "$ENV_FILE" | sed 's/^/    /' || true
    hr
    err "生成密钥的办法（在终端里执行，把输出结果填进 .env 对应位置）："
    err "    ZF_SECRET_KEY     →  openssl rand -base64 48"
    err "    ZF_PII_KEK        →  openssl rand -base64 32"
    err "    POSTGRES_PASSWORD →  openssl rand -hex 16"
    err "    ZF_DB_PASSWORD    →  必须和 POSTGRES_PASSWORD 一模一样"
    err "    ZF_ALLOWED_HOSTS  →  填你自己的域名，例如 tiku.xxx.cn"
    err "编辑 .env 的办法（宝塔）：文件 → 进入 /opt/zhifou → 双击 .env → 改完点保存"
    exit 1
fi
ok ".env 里没有发现未填写的占位符"

# 关键变量非空检查
secret_key="$(read_env ZF_SECRET_KEY "")"
allowed_hosts="$(read_env ZF_ALLOWED_HOSTS "")"
if [ -z "$secret_key" ]; then
    err ".env 里 ZF_SECRET_KEY 是空的，后端启动会直接报错。"
    err "请先执行：openssl rand -base64 48   然后把结果填进 .env 的 ZF_SECRET_KEY="
    exit 1
fi
if [ -z "$allowed_hosts" ]; then
    err ".env 里 ZF_ALLOWED_HOSTS 是空的，生产配置要求必须填（填你的域名，多个用英文逗号隔开）。"
    err "例如：ZF_ALLOWED_HOSTS=zhifou.example.com"
    exit 1
fi
ok "ZF_SECRET_KEY 与 ZF_ALLOWED_HOSTS 均已填写"
info "ZF_ALLOWED_HOSTS = ${allowed_hosts}"
warn "核对一下：上面这个域名必须和你实际访问的域名完全一致（不带 https://），否则访问会 400。"

# ── 访问协议：默认 https；只有「域名未备案、临时用公网 IP 试用」才走 http ──
# ★ 和后端 settings 用的是同一个开关（ZF_ALLOW_INSECURE_HTTP），
#   避免出现「脚本按 https 检查、实际却是 http 访问」这种自相矛盾的误报。
allow_insecure_http="$(read_env ZF_ALLOW_INSECURE_HTTP "0")"
check_scheme="https"
if [ "$allow_insecure_http" = "1" ]; then
    check_scheme="http"
    warn "检测到 ZF_ALLOW_INSECURE_HTTP=1，下面的自检按 http 进行。"
    warn "这是【备案未通过期间用公网 IP 试用】的临时模式：密码明文传输，不能长期开启。"
fi

# ── 数据库账号两组变量必须一致 ──
# compose 用 POSTGRES_USER / POSTGRES_PASSWORD 去「建库」，
# 后端用 ZF_DB_USER / ZF_DB_PASSWORD 去「连库」。
# 两边不一致时会出现「建了 A 用户、却拿 B 用户去连」→ role does not exist。
pg_user_cfg="$(read_env POSTGRES_USER "")"
db_user_cfg="$(read_env ZF_DB_USER "")"
pg_pass_cfg="$(read_env POSTGRES_PASSWORD "")"
db_pass_cfg="$(read_env ZF_DB_PASSWORD "")"
if [ -n "$pg_user_cfg" ] && [ -n "$db_user_cfg" ] && [ "$pg_user_cfg" != "$db_user_cfg" ]; then
    err ".env 里 POSTGRES_USER=${pg_user_cfg} 与 ZF_DB_USER=${db_user_cfg} 不一致。"
    err "前者是「建库用的账号」，后者是「连库用的账号」，不一样会报 role 不存在。"
    err "请把它们改成同一个值（推荐都用 zhifou），改完保存再重跑本脚本。"
    exit 1
fi
if [ -n "$pg_pass_cfg" ] && [ -n "$db_pass_cfg" ] && [ "$pg_pass_cfg" != "$db_pass_cfg" ]; then
    err ".env 里 POSTGRES_PASSWORD 与 ZF_DB_PASSWORD 不一样。"
    err "这两个必须是同一串密码，否则后端连不上数据库。"
    exit 1
fi
ok "数据库账号两组变量一致（POSTGRES_* 与 ZF_DB_* 对得上）"

# ── 再校验 docker-compose.yml（.env 已就绪，变量替换才不会失败）──
if ! dc config >/dev/null 2>&1; then
    err "docker-compose.yml 校验失败，下面是详细信息："
    dc config >/dev/null
    err "最常见的原因：.env 里还有变量没填（例如 POSTGRES_PASSWORD）。"
    exit 1
fi
ok "docker-compose.yml 校验通过，服务清单："
dc config --services 2>/dev/null | sed 's/^/    /' || true

# ═══════════════════════════════════════════════════════════════════
step "5" "拉取 / 构建镜像"
# ═══════════════════════════════════════════════════════════════════
cd "$APP_DIR"
info "开始拉取基础镜像（PostgreSQL / Redis 等），失败也会继续尝试本地构建……"
dc pull --ignore-buildable 2>&1 | tail -n 20 || warn "拉取镜像时有失败项，继续尝试本地构建。"

hr
warn "下面开始构建后端镜像。"
warn "★ 首次构建要下载 OCR 识别模型（约 200MB），通常需要 5–15 分钟，"
warn "★ 这期间终端看起来像卡住了是正常的，请不要关闭窗口、不要断网。"
hr
if ! dc build 2>&1 | tail -n 40; then
    err "镜像构建失败。常见原因："
    err "    1) 服务器访问不了 pip 源 → 检查网络，或改用国内镜像源"
    err "    2) 磁盘空间不够 → 执行 df -h 看看"
    err "    3) 内存不足被系统杀掉 → 停掉无关服务后重跑"
    exit 1
fi
ok "镜像构建完成"

# ═══════════════════════════════════════════════════════════════════
step "6" "启动数据库与缓存（db / redis）"
# ═══════════════════════════════════════════════════════════════════
started_any="0"
if have_service db; then
    dc up -d db
    started_any="1"
else
    warn "docker-compose.yml 里没有名为 db 的服务，跳过。"
fi
if have_service redis; then
    dc up -d redis
    started_any="1"
else
    warn "docker-compose.yml 里没有名为 redis 的服务，跳过。"
fi
[ "$started_any" = "1" ] || warn "既没有 db 也没有 redis 服务，后面的 migrate 可能会失败。"

# ═══════════════════════════════════════════════════════════════════
step "7" "等待数据库与缓存就绪"
# ═══════════════════════════════════════════════════════════════════
if have_service db; then
    wait_healthy db 180 || die "数据库没起来，先按上面的日志排查，再重跑本脚本。"
    # ★ 必须再等一次「真的能连」：run 不会等 healthcheck，PG 冷启动要 5–20 秒
    wait_pg_ready 180 || die "数据库连不上，migrate 会失败。先按上面的日志排查，再重跑本脚本。"
fi
if have_service redis; then
    wait_healthy redis 120 || die "Redis 没起来，先按上面的日志排查，再重跑本脚本。"
fi

# ═══════════════════════════════════════════════════════════════════
step "8" "执行数据库迁移 migrate（建表）"
# ═══════════════════════════════════════════════════════════════════
info "这一步会在空数据库里建好所有表，通常需要 1–3 分钟……"
if ! dc run --rm web python manage.py migrate; then
    err "数据库迁移失败。下面是后端最近 60 行日志："
    dc logs --tail=60 web || true
    err "常见原因：db 服务没起来 / .env 里的数据库账号密码和 docker-compose.yml 不一致。"
    exit 1
fi
ok "数据库表已建好"

# ═══════════════════════════════════════════════════════════════════
step "9" "收集静态文件 collectstatic（决定 /admin/ 有没有样式）"
# ═══════════════════════════════════════════════════════════════════
info "把后台管理页面的 CSS / JS 导出到 $STATIC_DIR ……"
if ! dc run --rm web python manage.py collectstatic --noinput; then
    err "collectstatic 失败。下面是后端最近 60 行日志："
    dc logs --tail=60 web || true
    exit 1
fi
if [ -z "$(ls -A "$STATIC_DIR" 2>/dev/null || true)" ]; then
    warn "宿主机的 $STATIC_DIR 是空的！"
    warn "这说明 docker-compose.yml 可能没有把 staticfiles 目录挂载到宿主机，"
    warn "结果是 /admin/ 打开会没有样式（全是白板）。请检查 docker-compose.yml："
    warn "    卷 zf_static 的 device: 必须是 /opt/zhifou/staticfiles"
    warn "    （它用的是「命名卷 + local bind」写法，普通命名卷宿主机是看不到里面文件的）"
else
    ok "静态文件已生成到 $STATIC_DIR（共 $(find "$STATIC_DIR" -type f | wc -l) 个文件）"
fi

# ═══════════════════════════════════════════════════════════════════
step "10" "启动全部服务"
# ═══════════════════════════════════════════════════════════════════
dc up -d
sleep 5
info "当前容器状态："
dc ps | sed 's/^/    /' || true

# ═══════════════════════════════════════════════════════════════════
step "11" "自检：后端健康检查"
# ═══════════════════════════════════════════════════════════════════
health_ok="0"
for i in $(seq 1 30); do
    if body="$(http_get "$HEALTH_URL" 2>/dev/null)"; then
        if printf '%s' "$body" | grep -q '"status"'; then
            health_ok="1"
            break
        fi
    fi
    sleep 2
done

if [ "$health_ok" != "1" ]; then
    err "访问 $HEALTH_URL 失败或返回内容不对。"
    err "下面是当前容器状态和后端最近 80 行日志："
    dc ps || true
    dc logs --tail=80 web || true
    exit 1
fi
ok "后端健康检查通过，返回：$body"

# ═══════════════════════════════════════════════════════════════════
hr
info "启动后自检：4 项「配了但可能没生效」/「配错了很危险」的检查"
hr
# ═══════════════════════════════════════════════════════════════════

# ── 自检 1：后端启动时自己打印的环境行 ──
env_line="$(dc logs web 2>/dev/null | grep -m1 '\[ENV\]' || true)"
if [ -n "$env_line" ]; then
    ok "后端启动自检行：$env_line"
    warn "核对这三项：DB=postgres:db:5432/zhifou、REDIS 指向 redis、MEDIA=/app/media"
else
    warn "没抓到后端的 [ENV] 自检行（日志可能被截断）。可手工确认："
    echo "    cd $APP_DIR && docker compose -p zhifou logs web | grep -m1 '\\[ENV\\]'"
fi

# ── 自检 2：任务队列模式必须是 queued ──
#    否则 ZF_TASK_MODE=inline 会让重算任务在网页进程里同步跑，
#    worker_calc 一条任务都收不到（不报错，纯静默失效）
task_mode_out="$(dc exec -T web python -c "from apps.analytics.tasks import task_mode, is_queued; print(task_mode(), is_queued())" 2>/dev/null || true)"
if [ -z "$task_mode_out" ]; then
    # 查不动 ≠ 没问题：只警告，不拦人
    warn "自检 2 无法自动完成：读不到任务队列模式（可能接口改名，或 web 容器没在跑）。"
    warn "★ 这不是「通过」。请手动执行下面这条，正确输出必须是 queued True："
    echo "    cd $APP_DIR && docker compose -p zhifou exec web python -c \"from apps.analytics.tasks import task_mode, is_queued; print(task_mode(), is_queued())\""
elif printf '%s' "$task_mode_out" | grep -q 'queued'; then
    ok "任务队列模式：$task_mode_out（worker_calc / worker_io 能收到任务）"
else
    # 查出来了、而且是错的 —— 必须当场拦住
    err "自检 2 失败：任务队列模式是「$task_mode_out」，正确值必须是「queued True」。"
    err "后果：学情重算会在网页进程里同步算，页面变慢，worker_calc 一直空转，而且不报错。"
    err "修法（两步）："
    err "    1) 在 $ENV_FILE 里加上/改成：  ZF_TASK_MODE=queued"
    err "    2) cd $APP_DIR && docker compose -p zhifou up -d"
    exit 1
fi

# ── 自检 3：媒体存储根必须是 /app/media ──
#    否则文件会落到容器可写层（/app/.zf-storage），
#    宿主机 /opt/zhifou/media 永远空 → 备份打成空包、重建容器就丢文件
storage_root="$(dc exec -T web python -c "from apps.media.storage import get_backend; print(get_backend().root)" 2>/dev/null || true)"
case "$storage_root" in
    /app/media)
        ok "媒体存储根：$storage_root（= 宿主机 $MEDIA_DIR，备份打包这个目录是对的）"
        ;;
    "")
        # 查不动 ≠ 没问题：只警告，不拦人
        warn "自检 3 无法自动完成：读不到媒体存储根（可能接口改名，或 web 容器没在跑）。"
        warn "★ 这不是「通过」。请手动执行下面这条，正确输出必须是 /app/media："
        echo "    cd $APP_DIR && docker compose -p zhifou exec web python -c \"from apps.media.storage import get_backend; print(get_backend().root)\""
        ;;
    *)
        # 查出来了、而且是错的 —— 必须当场拦住
        err "自检 3 失败：媒体存储根是「$storage_root」，正确值必须是「/app/media」。"
        err "后果：上传的扫描件 / 答题卡落进容器内部，重建容器就全部丢失 —— 而且不报错；"
        err "      同时 $MEDIA_DIR 一直是空的，备份打出来的 media 是空包。"
        err "修法（两步）："
        err "    1) 在 $ENV_FILE 里加上/改成：  ZF_STORAGE_ROOT=/app/media"
        err "    2) cd $APP_DIR && docker compose -p zhifou up -d"
        exit 1
        ;;
esac

# ── 自检 4（安全）：媒体文件不能被匿名下载 ──
# ★ 判据不是「返回 404」：前端有 try_files ... /index.html 兜底，
#   请求一定会返回 200 + text/html。要看的是 content-type 是不是图片/文件。
check_domain="$(printf '%s' "$allowed_hosts" | cut -d, -f1 | tr -d ' ')"
# ★ 临时 http 模式（IP 试用）下，优先挑第一个像 IP 的条目做探测目标。
#   原因：这时候域名还没备案，任何走域名的 http 请求都会被云厂商拦截页吃掉，
#   探到的内容是拦截页而不是我们的站点，判据全部失真。
if [ "$allow_insecure_http" = "1" ]; then
    ip_hit="$(printf '%s' "$allowed_hosts" | tr ',' '\n' | tr -d ' ' \
        | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n 1 || true)"
    if [ -n "$ip_hit" ]; then
        check_domain="$ip_hit"
    else
        warn "ZF_ALLOW_INSECURE_HTTP=1，但 ZF_ALLOWED_HOSTS 里没有公网 IP 条目。"
        warn "请把公网 IP 加进去，例如：ZF_ALLOWED_HOSTS=124.220.77.198,你的域名"
    fi
fi
info "自检访问地址：${check_scheme}://${check_domain}/"
# ★ 取样优先挑图片：图片被吐出来时 content-type 一定是 image/*，判据最稳。
#   如果挑到 .docx / .txt 之类，口子开着时可能返回 application/octet-stream
#   或 text/plain，容易和正常情况混在一起造成假通过。
media_kind="图片"
media_rel="$(cd "$MEDIA_DIR" 2>/dev/null &&
    find . -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
                     -o -iname '*.gif' -o -iname '*.webp' \) 2>/dev/null |
    head -n 1 | sed 's|^\./||' || true)"
if [ -z "$media_rel" ]; then
    # 找不到图片就回退到任意文件 —— 但必须说清楚取到的是什么，别让人误判
    media_kind="非图片文件"
    media_rel="$(cd "$MEDIA_DIR" 2>/dev/null &&
        find . -type f 2>/dev/null | head -n 1 | sed 's|^\./||' || true)"
fi
if [ -z "$media_rel" ]; then
    # ★ 跳过 ≠ 通过：必须说清楚，否则会被当成「验过了没问题」
    warn "自检 4（安全）【跳过，不是通过】：$MEDIA_DIR 里还没有任何图片文件。"
    warn "现在没有可用来试探的样本，无法判断 /media/ 有没有被打开。"
    warn "★ 等老师上传过第一张配图之后，务必按《部署手册》4.2 节手工补验一次。"
else
    check_url="${check_scheme}://${check_domain}/media/${media_rel}"
    info "自检 4 取样：${media_rel}（${media_kind}）"
    if cmd_exists curl; then
        ct="$(curl -sI --max-time 10 "$check_url" 2>/dev/null | grep -i '^content-type' | tail -n 1 || true)"
        if [ -z "$ct" ]; then
            warn "自检 4（安全）没能自动完成：$check_url 现在访问不通"
            warn "（证书或 Nginx 还没配好，属正常）。配好后请务必按手册第 4 节「反向验收」手工验一次。"
        elif printf '%s' "$ct" | grep -qiE 'image/|application/(pdf|octet-stream)|video/|audio/'; then
            err "自检 4（安全）失败：$check_url 能被免登录直接下载（${ct}）"
            err "说明 Nginx 里开了 /media/ 的静态 alias —— 学生答题卡照片正在裸奔！"
            err "立刻处理：宝塔 → 网站 → 站点 → 设置 → 配置文件，删掉 /media/ 那一段再保存。"
            exit 1
        else
            ok "自检 4（安全）通过：/media/ 没有对外暴露（${ct}）"
        fi
    else
        warn "系统里没有 curl，自检 4（安全）【跳过，不是通过】。请手工执行："
        echo "    curl -sI \"$check_url\" | grep -i '^content-type'"
    fi
fi

# ── 自检 5（正向）：关掉 /media/ 的同时，/static/ 必须还好用 ──
# 只验「图片打不开」是不够的 —— 还得反过来证明后台样式没被顺手弄坏。
static_code=""
if cmd_exists curl; then
    static_code="$(curl -sI --max-time 10 -o /dev/null -w '%{http_code}' \
        "${check_scheme}://${check_domain}/static/admin/css/base.css" 2>/dev/null || true)"
fi
if [ -z "$static_code" ] || [ "$static_code" = "000" ]; then
    warn "自检 5（正向）无法自动完成：${check_scheme}://${check_domain} 访问不通或没有 curl。"
    warn "配好后请手工验：curl -sI ${check_scheme}://${check_domain}/static/admin/css/base.css   必须 200。"
elif [ "$static_code" = "200" ]; then
    ok "自检 5（正向）通过：/static/admin/css/base.css 返回 200（后台样式正常）"
else
    err "自检 5（正向）失败：/static/admin/css/base.css 返回 ${static_code}，正确值必须是 200。"
    err "说明后台管理页面的样式文件取不到，/admin/ 会变成没有样式的白板。"
    err "排查：cd $APP_DIR && docker compose -p zhifou run --rm web python manage.py collectstatic --noinput"
    err "      再确认 /opt/zhifou/staticfiles/admin/css/base.css 这个文件存在。"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════
hr
printf '%s%s 首次部署完成 ✔%s\n' "$C_BOLD" "$C_GREEN" "$C_OFF"
hr

if [ ! -f "${SPA_DIR}/index.html" ]; then
    warn "还没检测到 ${SPA_DIR}/index.html —— 网站现在打开会是 404。"
    warn "请上传前端：把本地 web 目录 npm run build 生成的 dist 里【所有内容】"
    warn "上传到 ${SPA_DIR}（注意 index.html 要直接在 dist 目录下，不能是 dist/dist/index.html）。"
else
    ok "前端文件已就位：${SPA_DIR}/index.html"
fi

# ── 可选：创建管理员账号 ──
hr
info "要不要现在创建一个后台管理员账号？（后面也可以随时再建）"
if confirm "输入 y 创建，直接回车跳过 (y/N)："; then
    admin_user=""
    admin_pwd=""
    printf '管理员用户名（默认 admin）：'
    read -r admin_user || admin_user=""
    [ -n "$admin_user" ] || admin_user="admin"
    while :; do
        printf '管理员密码（至少 10 位，不能是纯数字）：'
        read -rs admin_pwd || admin_pwd=""
        printf '\n'
        if [ "${#admin_pwd}" -lt 10 ]; then
            warn "密码太短（${#admin_pwd} 位），至少 10 位，请重新输入。"
            continue
        fi
        case "$admin_pwd" in
            *[!0-9]*) break ;;
            *) warn "密码不能是纯数字，请重新输入。" ;;
        esac
    done
    info "正在创建管理员账号 ${admin_user} ……"
    if dc run --rm -e DJANGO_SUPERUSER_PASSWORD="$admin_pwd" web \
        python manage.py createsuperuser --noinput --username "$admin_user"; then
        ok "管理员账号 ${admin_user} 创建成功，用它登录 ${allowed_hosts} 的 /admin/"
    else
        warn "创建管理员失败（用户名可能已存在）。你也可以稍后手工执行："
        echo "    cd /opt/zhifou && docker compose -p zhifou run --rm web python manage.py createsuperuser"
    fi
else
    info "已跳过。之后想建管理员，执行："
    echo "    cd /opt/zhifou && docker compose -p zhifou run --rm web python manage.py createsuperuser"
fi

hr
info "还没做完的话，接下来还有 3 件事（按顺序做）："
echo "    1) 宝塔 → 网站 → 站点 → 设置 → 配置文件，粘贴 nginx/zhifou.conf"
echo "       （改 2 处 server_name 和 2 行证书路径；如果已经在手册 2.7 做过就不用重复）"
echo "    2) 宝塔 → 网站 → 站点 → 设置 → SSL，申请 Let's Encrypt 证书"
echo "    3) 上传前端 dist 到 /www/wwwroot/zhifou/dist，再打开 https://你的域名 验收"
hr
info "常用命令备忘："
echo "    看后端日志： cd /opt/zhifou && docker compose -p zhifou logs -f web"
echo "    看容器状态： cd /opt/zhifou && docker compose -p zhifou ps"
echo "    备份数据：   bash /opt/zhifou/scripts/backup.sh"
hr
