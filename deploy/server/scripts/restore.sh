#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  知否题库 · 数据恢复脚本（危险操作，会覆盖现有数据）
# ───────────────────────────────────────────────────────────────────
#  用法（在宝塔「终端」里执行）：
#      bash /opt/zhifou/scripts/restore.sh                    # 列出可恢复的备份并选一个
#      bash /opt/zhifou/scripts/restore.sh 20261020-030000    # 恢复指定时间戳的那一组
#      bash /opt/zhifou/scripts/restore.sh /opt/zhifou/backups/db-20261020-030000.dump
#      bash /opt/zhifou/scripts/restore.sh --help
#
#  ⚠⚠⚠ 危 险 提 示 ⚠⚠⚠
#      ● 本脚本会用备份里的数据【完全覆盖】现在的数据库和 media 文件。
#      ● 恢复之后，备份时间点【之后】新录入的题目、学生作答、上传的图片都会消失。
#      ● 脚本会先把警告打印出来，你必须亲手输入 yes 才会继续。
#      ● 本脚本只覆盖「知否自己的库 / media / staticfiles」，
#        不会动服务器上任何其它目录，也不会 rm -rf 任何用户目录。
#
#  建议：恢复前先跑一次 bash /opt/zhifou/scripts/backup.sh 留一份当前状态。
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
readonly BACKUP_DIR="${BACKUP_DIR:-${APP_DIR}/backups}"
readonly MEDIA_DIR="${APP_DIR}/media"
readonly STATIC_DIR="${APP_DIR}/staticfiles"
# 宿主机映射端口：默认 8000；被别的程序占用时在 .env 里设 ZF_WEB_HOST_PORT。
ZF_WEB_HOST_PORT="$(
    grep -E '^[[:space:]]*ZF_WEB_HOST_PORT[[:space:]]*=' "$ENV_FILE" 2>/dev/null \
        | tail -n 1 | cut -d '=' -f 2- | tr -d ' \r' || true
)"
readonly HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${ZF_WEB_HOST_PORT:-8000}/health/}"

TARGET="${1:-}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_BOLD=""; C_OFF=""
fi

hr()   { printf '%s\n' "════════════════════════════════════════════════════════"; }
info() { printf '%s[说明]%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
ok()   { printf '%s[完成]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[警告]%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()  { printf '%s[错误]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()  { err "$*"; hr; err "恢复已停止，数据没有被改动。"; exit 1; }

dc() { docker compose -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"; }

usage() {
    cat <<'TXT'
知否题库 · 数据恢复脚本（会覆盖现有数据）

用法：
  bash /opt/zhifou/scripts/restore.sh                 列出备份让你选
  bash /opt/zhifou/scripts/restore.sh 20261020-030000  恢复该时间戳的那一组备份
  bash /opt/zhifou/scripts/restore.sh /path/to/db-xxx.dump  只恢复这个数据库文件
  bash /opt/zhifou/scripts/restore.sh --help

恢复过程中你必须手动输入 yes 才会真正执行。
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

read_env() {
    local key="$1" default="${2:-}" val=""
    if [ -f "$ENV_FILE" ]; then
        val="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2- || true)"
        val="$(printf '%s' "$val" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
    fi
    printf '%s' "${val:-$default}"
}

have_service() {
    local svc="$1" all=""
    all="$(dc config --services 2>/dev/null || true)"
    printf '%s\n' "$all" | grep -qx "$svc"
}

# 交互输入必须允许失败（用户直接 Ctrl+D / 非终端），否则 set -e 会误杀
ask() {
    local prompt="$1" ans=""
    printf '%s' "$prompt"
    read -r ans || ans=""
    printf '%s' "$ans"
}

list_backups() {
    info "下面是 ${BACKUP_DIR} 里现有的数据库备份（时间倒序）："
    local found
    found="$(ls -1t "${BACKUP_DIR}"/db-*.dump 2>/dev/null || true)"
    if [ -z "$found" ]; then
        err "${BACKUP_DIR} 里没有任何 db-*.dump 备份文件。"
        err "请先执行备份： bash ${APP_DIR}/scripts/backup.sh"
        exit 1
    fi
    printf '%s\n' "$found" | sed 's/^/    /'
}

# ═══════════════════════════════════════════════════════════════════
self_crlf_check

case "$TARGET" in
    -h | --help) usage; exit 0 ;;
esac

[ "$(id -u)" -eq 0 ] || die "请用 root 用户执行（宝塔终端默认就是 root）。"
command -v docker >/dev/null 2>&1 || die "没有检测到 docker。"
docker info >/dev/null 2>&1 || die "docker 守护进程没在运行，请先执行 systemctl start docker"
[ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE"
[ -d "$BACKUP_DIR" ] || die "备份目录不存在：$BACKUP_DIR"
cd "$APP_DIR"

# ── 定位备份文件 ──
db_dump=""
media_tar=""
static_tar=""
ts=""

if [ -z "$TARGET" ]; then
    list_backups
    printf '\n'
    ts="$(ask "请输入要恢复的时间戳（形如 20261020-030000）：")"
    [ -n "$ts" ] || die "没有输入时间戳，已退出。"
elif [ -f "$TARGET" ]; then
    db_dump="$TARGET"
    ts="$(basename "$TARGET")"
    ts="${ts#db-}"; ts="${ts%.dump}"
    info "已指定备份文件：$db_dump"
    if [ -f "${BACKUP_DIR}/media-${ts}.tar.gz" ]; then
        media_tar="${BACKUP_DIR}/media-${ts}.tar.gz"
    fi
    if [ -f "${BACKUP_DIR}/static-${ts}.tar.gz" ]; then
        static_tar="${BACKUP_DIR}/static-${ts}.tar.gz"
    fi
else
    ts="$TARGET"
fi

if [ -z "$db_dump" ]; then
    db_dump="${BACKUP_DIR}/db-${ts}.dump"
    [ -f "$db_dump" ] || die "找不到备份文件：$db_dump（可用 bash $0 不带参数查看列表）"
    if [ -f "${BACKUP_DIR}/media-${ts}.tar.gz" ]; then
        media_tar="${BACKUP_DIR}/media-${ts}.tar.gz"
    fi
    if [ -f "${BACKUP_DIR}/static-${ts}.tar.gz" ]; then
        static_tar="${BACKUP_DIR}/static-${ts}.tar.gz"
    fi
fi

[ -f "$db_dump" ] || die "数据库备份文件不存在：$db_dump"
head -c 5 "$db_dump" | grep -q 'PGDMP' || die "$db_dump 不是有效的 PostgreSQL 备份（开头应该 PGDMP）。"

db_user="$(read_env POSTGRES_USER "$(read_env ZF_DB_USER "zhifou")")"
db_name="$(read_env POSTGRES_DB "$(read_env ZF_DB_NAME "zhifou")")"

# ═══════════════════════════════════════════════════════════════════
hr
warn "⚠⚠⚠  下 面 是 危 险 操 作 ，请 认 真 看 完  ⚠⚠⚠"
hr
# ═══════════════════════════════════════════════════════════════════
warn "你即将把系统数据恢复到下面这份备份："
echo ""
echo "    数据库备份文件： $db_dump  （$(du -h "$db_dump" | awk '{print $1}')）"
echo "    media 备份文件： ${media_tar:-（该时间戳没有 media 备份，media 保持原样）}"
echo "    staticfiles：    ${static_tar:-（该时间戳没有，保持原样）}"
echo ""
warn "恢复目标是这台服务器上的："
echo "    数据库： 容器 db 里的库 ${db_name}（用户 ${db_user}）"
echo "    media：  ${MEDIA_DIR}"
echo "    static： ${STATIC_DIR}"
echo ""
warn "★ 恢复之后：备份时间点之后新增的题目、学生作答、上传的扫描件都会【丢失】。"
warn "★ 现有数据会被【覆盖】，不是合并。"
warn "★ 本脚本不会删除服务器上其它任何文件，只覆盖上面列出的三处。"
echo ""
warn "强烈建议：先按 Ctrl+C 退出，执行 bash ${APP_DIR}/scripts/backup.sh 留一份当前状态再回来。"
echo ""

if [ ! -t 0 ]; then
    die "当前不是交互式终端（无法确认），已自动退出。请在宝塔终端里手动执行本脚本。"
fi

ans="$(ask "如果确定要继续，请原样输入 yes（输入其它任何内容都会退出）：")"
if [ "$ans" != "yes" ]; then
    die "你输入的是「${ans}」，不是 yes，已安全退出，数据未做任何改动。"
fi
ok "已确认，开始恢复。"

# ═══════════════════════════════════════════════════════════════════
hr
info "第 1/4 步：停掉应用容器（保留数据库）"
hr
# ═══════════════════════════════════════════════════════════════════
stopped=""
for s in web worker_io worker_calc beat redis; do
    if have_service "$s"; then
        info "停止 ${s} ……"
        dc stop "$s" || true
        stopped="${stopped} ${s}"
    fi
done
[ -n "$stopped" ] || warn "没有需要停止的应用服务。"
ok "应用容器已停止：${stopped:-（无）}"

# ═══════════════════════════════════════════════════════════════════
hr
info "第 2/4 步：恢复数据库"
hr
# ═══════════════════════════════════════════════════════════════════
if ! have_service db; then
    die "compose 里没有 db 服务，无法恢复数据库。"
fi

if ! dc ps --status=running --services 2>/dev/null | grep -qx db; then
    info "db 容器没在运行，先启动……"
    dc up -d db
    sleep 8
fi

info "正在把 $db_dump 恢复到库 ${db_name}（--clean 会先清掉同名表）……"
info "这一步可能要 1–5 分钟，请不要关闭窗口。"
if dc exec -T db pg_restore -U "$db_user" -d "$db_name" --clean --if-exists --no-owner <"$db_dump"; then
    ok "数据库恢复完成"
else
    err "pg_restore 返回了非 0 退出码。"
    err "注意：PostgreSQL 恢复时经常会有「对象不存在 / 已存在」这类提示，"
    err "      并不代表一定失败。请人工核对数据条数后再决定是否重来："
    err "      cd $APP_DIR && docker compose -p zhifou exec -T db psql -U $db_user -d $db_name -c 'select count(*) from questionbank_question;'"
    if [ ! -t 0 ]; then
        exit 1
    fi
    cont="$(ask "仍要继续恢复 media 吗？(y/N)：")"
    case "$cont" in
        y | Y | yes | YES) ;;
        *) die "已停止。数据库可能处于部分恢复状态，请用上面的核对命令确认。" ;;
    esac
fi

# ═══════════════════════════════════════════════════════════════════
hr
info "第 3/4 步：恢复 media / staticfiles"
hr
# ═══════════════════════════════════════════════════════════════════
if [ -n "$media_tar" ] && [ -f "$media_tar" ]; then
    info "解压 $media_tar 覆盖到 ${APP_DIR}/media ……"
    tar -xzf "$media_tar" -C "$APP_DIR"
    ok "media 已恢复（现有同名文件被覆盖，备份里没有的文件仍保留）"
else
    warn "没有 media 备份文件，跳过（现有 media 保持原样）。"
fi

if [ -n "$static_tar" ] && [ -f "$static_tar" ]; then
    info "解压 $static_tar 覆盖到 ${APP_DIR}/staticfiles ……"
    tar -xzf "$static_tar" -C "$APP_DIR"
    ok "staticfiles 已恢复"
else
    warn "没有 staticfiles 备份文件，跳过（稍后可用 collectstatic 重建）。"
fi

# 权限修正：容器里的应用用户可能不是 root
if [ -d "$MEDIA_DIR" ]; then
    chmod -R u+rwX,go+rX "$MEDIA_DIR" 2>/dev/null || true
fi

# ═══════════════════════════════════════════════════════════════════
hr
info "第 4/4 步：启动全部服务并自检"
hr
# ═══════════════════════════════════════════════════════════════════
dc up -d
sleep 8
dc ps | sed 's/^/    /' || true

health_ok="0"
body=""
for i in $(seq 1 30); do
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null || true)"
    else
        body="$(wget -qO- --timeout=10 "$HEALTH_URL" 2>/dev/null || true)"
    fi
    if printf '%s' "$body" | grep -q '"status"'; then
        health_ok="1"
        break
    fi
    sleep 2
done

if [ "$health_ok" = "1" ]; then
    ok "健康检查通过，返回：$body"
else
    err "健康检查没通过。下面是后端日志："
    dc logs --tail=80 web || true
fi

hr
printf '%s%s 恢复流程结束%s\n' "$C_BOLD" "$C_GREEN" "$C_OFF"
hr
info "请立刻人工核对这几件事："
echo "    1) 打开 /admin/ 能登录，题目数量和你预期一致"
echo "    2) 随便打开一道带配图的题，图片能正常显示"
echo "    3) 打开 /health/ 能看到 {\"status\":\"ok\",...}"
echo ""
info "核对数据条数："
echo "    cd $APP_DIR && docker compose -p zhifou exec -T db psql -U $db_user -d $db_name -c 'select count(*) from questionbank_question;'"
hr
