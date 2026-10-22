#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  知否题库 · 代码更新脚本（改完代码 / 上传新版本后执行）
# ───────────────────────────────────────────────────────────────────
#  用法（在宝塔「终端」里执行）：
#      bash /opt/zhifou/scripts/update.sh              # 完整更新（默认）
#      bash /opt/zhifou/scripts/update.sh --no-migrate # 只换代码，不动数据库
#      bash /opt/zhifou/scripts/update.sh --no-build   # 代码已构建好，只重启
#      bash /opt/zhifou/scripts/update.sh --help       # 看说明
#
#  流程：拉/拷代码 → 重新构建镜像 → migrate → collectstatic → up -d → 健康检查
#
#  ★ 本脚本不会删除任何已有数据。migrate 只会「加表加字段」，不会清数据。
#  ★ 更新前强烈建议先跑一次：bash /opt/zhifou/scripts/backup.sh
# ═══════════════════════════════════════════════════════════════════

# ── 维护负责人：software-engineer-ops（运维侧）──
# 本脚本不修改、也不依赖修改：docker-compose.yml / Dockerfile / .env.prod.example /
# requirements.prod.txt / settings/ —— 这些由 software-engineer（容器侧）维护。
set -euo pipefail

export COMPOSE_PROJECT_NAME="zhifou"
export LC_ALL="${LC_ALL:-C.UTF-8}"

readonly APP_DIR="${APP_DIR:-/opt/zhifou}"
readonly COMPOSE_FILE="${APP_DIR}/docker-compose.yml"
readonly ENV_FILE="${APP_DIR}/.env"
readonly STATIC_DIR="${APP_DIR}/staticfiles"
# 宿主机映射端口：默认 8000；被别的程序占用时在 .env 里设 ZF_WEB_HOST_PORT。
ZF_WEB_HOST_PORT="$(
    grep -E '^[[:space:]]*ZF_WEB_HOST_PORT[[:space:]]*=' "$ENV_FILE" 2>/dev/null \
        | tail -n 1 | cut -d '=' -f 2- | tr -d ' \r' || true
)"
readonly HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${ZF_WEB_HOST_PORT:-8000}/health/}"

DO_PULL="1"
DO_BUILD="1"
DO_MIGRATE="1"
DO_STATIC="1"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_BOLD=""; C_OFF=""
fi

hr()   { printf '%s\n' "────────────────────────────────────────────────────────"; }
info() { printf '%s[说明]%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
ok()   { printf '%s[完成]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[警告]%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()  { printf '%s[错误]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()  { err "$*"; hr; err "更新已停止。请把上面红色的提示整段复制给技术人员。"; exit 1; }
step() { printf '\n%s[第 %s/6 步]%s %s\n' "$C_BOLD" "$1" "$C_OFF" "$2"; hr; }

dc() { docker compose -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"; }

cmd_exists() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'TXT'
知否题库 · 代码更新脚本

用法：
  bash /opt/zhifou/scripts/update.sh [选项]

选项：
  --no-migrate   跳过数据库迁移（只是改了前端或静态内容时用）
  --no-build     跳过重新构建镜像（镜像已经构建好了，只想重启）
  --no-pull      不自动 git pull（代码由你手工上传）
  --no-static    跳过 collectstatic（后台样式没变时省几十秒）
  --help         显示这段说明
TXT
}

self_crlf_check() {
    local self="${BASH_SOURCE[0]:-}"
    [ -n "$self" ] && [ -f "$self" ] || return 0
    if LC_ALL=C grep -q $'\r' "$self" 2>/dev/null; then
        err "这个脚本文件里混进了 Windows 换行符（CRLF），bash 执行会报错。"
        err "请先执行： sed -i 's/\r\$//' $self"
        exit 1
    fi
}

have_service() {
    local svc="$1" all=""
    all="$(dc config --services 2>/dev/null || true)"
    printf '%s\n' "$all" | grep -qx "$svc"
}

http_get() {
    local url="$1"
    if cmd_exists curl; then
        curl -fsS --max-time 10 "$url"
    elif cmd_exists wget; then
        wget -qO- --timeout=10 "$url"
    else
        die "系统里既没有 curl 也没有 wget。请先执行：dnf install -y curl"
    fi
}

# ── 解析参数 ──
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-migrate) DO_MIGRATE="0" ;;
        --no-build)   DO_BUILD="0" ;;
        --no-pull)    DO_PULL="0" ;;
        --no-static)  DO_STATIC="0" ;;
        -h | --help)  usage; exit 0 ;;
        *)            err "不认识的参数：$1"; usage; exit 1 ;;
    esac
    shift
done

# ═══════════════════════════════════════════════════════════════════
step "1" "前置检查"
# ═══════════════════════════════════════════════════════════════════
self_crlf_check
[ "$(id -u)" -eq 0 ] || die "请用 root 用户执行（宝塔终端默认就是 root）。"
cmd_exists docker || die "没有检测到 docker，请先跑首次部署脚本。"
docker info >/dev/null 2>&1 || die "docker 守护进程没在运行，请先执行 systemctl start docker"
[ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE"
dc config >/dev/null 2>&1 || die "docker-compose.yml 有语法错误，请先执行：cd $APP_DIR && docker compose -p zhifou config"
cd "$APP_DIR"
ok "前置检查通过"

# ═══════════════════════════════════════════════════════════════════
step "2" "获取最新代码"
# ═══════════════════════════════════════════════════════════════════
if [ "$DO_PULL" = "1" ] && [ -d "${APP_DIR}/.git" ]; then
    info "检测到 git 仓库，正在拉取最新代码……"
    git -C "$APP_DIR" pull --ff-only || die "git pull 失败（本地有改动？）。解决冲突后再重跑，或加 --no-pull 跳过。"
    ok "代码已更新到最新版本"
elif [ -d "${APP_DIR}/.git" ]; then
    info "按参数跳过 git pull。"
else
    warn "$APP_DIR 不是 git 仓库，脚本不会自动拉代码。"
    warn "请用宝塔「文件」把新代码上传到 $APP_DIR 覆盖同名文件，然后再跑本脚本。"
fi

# ═══════════════════════════════════════════════════════════════════
step "3" "重新构建镜像（web / worker_io / worker_calc）"
# ═══════════════════════════════════════════════════════════════════
if [ "$DO_BUILD" = "0" ]; then
    info "按参数跳过构建。"
else
    # 这四个服务用的是同一个镜像，构建任意一次镜像 tag 就会更新
    targets=()
    for s in web worker_io worker_calc beat; do
        if have_service "$s"; then targets+=("$s"); fi
    done
    if [ "${#targets[@]}" -eq 0 ]; then
        warn "compose 里没找到 web / worker_io / worker_calc / beat，改为构建全部服务。"
        dc build || die "镜像构建失败，请看上面的报错。"
    else
        info "构建服务：${targets[*]}"
        warn "构建过程通常 1–10 分钟（视改动大小），看起来像卡住是正常的，请不要关闭窗口。"
        dc build "${targets[@]}" || die "镜像构建失败，请看上面的报错。"
    fi
    ok "镜像构建完成"
fi

# ═══════════════════════════════════════════════════════════════════
step "4" "数据库迁移 + 收集静态文件"
# ═══════════════════════════════════════════════════════════════════
if [ "$DO_MIGRATE" = "0" ]; then
    info "按参数跳过 migrate。"
else
    info "执行 migrate（只加表加字段，不会删数据）……"
    dc run --rm web python manage.py migrate || die "migrate 失败，请看上面的报错。数据库没有被改动。"
    ok "migrate 完成"
fi

if [ "$DO_STATIC" = "0" ]; then
    info "按参数跳过 collectstatic。"
else
    info "执行 collectstatic（刷新后台管理页面样式）……"
    dc run --rm web python manage.py collectstatic --noinput || die "collectstatic 失败，请看上面的报错。"
    ok "collectstatic 完成（共 $(find "$STATIC_DIR" -type f 2>/dev/null | wc -l) 个文件）"
fi

# ═══════════════════════════════════════════════════════════════════
step "5" "重启服务"
# ═══════════════════════════════════════════════════════════════════
info "用新镜像重启所有容器……"
dc up -d || die "启动失败，请看上面的报错。"
sleep 5
dc ps | sed 's/^/    /' || true
ok "容器已重启"

# ═══════════════════════════════════════════════════════════════════
step "6" "健康检查"
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
    err "健康检查失败。下面是最新的容器状态和后端日志："
    dc ps || true
    dc logs --tail=80 web || true
    err "需要回滚的话：把代码恢复到上一版，然后重跑 bash $APP_DIR/scripts/update.sh"
    err "需要回滚数据的话：bash $APP_DIR/scripts/restore.sh"
    exit 1
fi
ok "健康检查通过，返回：$body"

# ── 更新后复检两项「配了但可能没生效」的配置 ──
hr
info "复检：任务队列模式与媒体存储根"
# 强度分级：查不动 → warn；查出来是错的 → 必须终止
task_mode_out="$(dc exec -T web python -c "from apps.analytics.tasks import task_mode, is_queued; print(task_mode(), is_queued())" 2>/dev/null || true)"
if [ -z "$task_mode_out" ]; then
    warn "无法自动检查任务队列模式（可能接口改名，或 web 没在跑）。★ 不是「通过」，请手动确认："
    echo "    cd $APP_DIR && docker compose -p zhifou exec web python -c \"from apps.analytics.tasks import task_mode, is_queued; print(task_mode(), is_queued())\""
elif printf '%s' "$task_mode_out" | grep -q 'queued'; then
    ok "任务队列模式：$task_mode_out"
else
    err "任务队列模式是「$task_mode_out」，正确值必须是「queued True」。worker_calc 会一直空转。"
    err "修法：在 $ENV_FILE 里加 ZF_TASK_MODE=queued，然后 cd $APP_DIR && docker compose -p zhifou up -d"
    exit 1
fi

storage_root="$(dc exec -T web python -c "from apps.media.storage import get_backend; print(get_backend().root)" 2>/dev/null || true)"
if [ -z "$storage_root" ]; then
    warn "无法自动检查媒体存储根（可能接口改名，或 web 没在跑）。★ 不是「通过」，请手动确认："
    echo "    cd $APP_DIR && docker compose -p zhifou exec web python -c \"from apps.media.storage import get_backend; print(get_backend().root)\""
elif [ "$storage_root" = "/app/media" ]; then
    ok "媒体存储根：$storage_root"
else
    err "媒体存储根是「$storage_root」，正确值必须是「/app/media」。"
    err "上传的文件会落进容器内部，重建容器就丢，且备份出来的 media 是空包。"
    err "修法：在 $ENV_FILE 里加 ZF_STORAGE_ROOT=/app/media，然后 cd $APP_DIR && docker compose -p zhifou up -d"
    exit 1
fi

hr
printf '%s%s 更新完成 ✔%s\n' "$C_BOLD" "$C_GREEN" "$C_OFF"
hr
info "如果这次改动包含 .env 里的环境变量：容器只在创建时读取一次 .env，"
info "本脚本第 5 步的 up -d 已经帮你重建了容器。若你是手工改的 .env 且没跑本脚本，"
info "请补一次： cd $APP_DIR && docker compose -p zhifou up -d"
info "如果页面样式还是旧的，让老师/学生按 Ctrl+F5 强制刷新浏览器（前端文件带 hash，一般不用）。"
info "看实时日志： cd $APP_DIR && docker compose -p zhifou logs -f web"
