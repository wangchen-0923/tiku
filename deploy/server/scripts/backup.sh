#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  知否题库 · 数据备份脚本
# ───────────────────────────────────────────────────────────────────
#  用法（在宝塔「终端」里执行）：
#      bash /opt/zhifou/scripts/backup.sh                 # 备份一次
#      bash /opt/zhifou/scripts/backup.sh --keep-days 14  # 保留 14 天（默认 7 天）
#      bash /opt/zhifou/scripts/backup.sh --install-cron  # 写入每天凌晨 3 点自动备份
#      bash /opt/zhifou/scripts/backup.sh --help
#
#  备份什么：
#      1. PostgreSQL 全库（pg_dump -Fc 自定义格式）
#      2. media 目录：题目配图、扫描件、答题卡 PDF
#      3. staticfiles 目录：后台样式（其实可以靠 collectstatic 重建，但一起备更省心）
#
#  备份到哪里： /opt/zhifou/backups/
#      文件名形如  db-20261020-030000.dump / media-20261020-030000.tar.gz
#
#  保留策略（重要，请务必看懂）：
#      ★ 只删除 /opt/zhifou/backups 目录里、
#      ★ 且文件名严格匹配 db-*.dump / media-*.tar.gz / static-*.tar.gz 的、
#      ★ 且修改时间超过 7 天（默认）的文件。
#      ★ 其它任何目录下的任何文件都不会被碰。
#      每天备份一次时，「保留 7 天」正好等于「保留最近 7 份」。
#
#  ★ 强烈建议：备份完之后把 /opt/zhifou/backups 里的文件再下载一份到本地电脑，
#    服务器硬盘坏了就什么都没了。
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
readonly CRON_FILE="/etc/cron.d/zhifou-backup"

KEEP_DAYS="${KEEP_DAYS:-7}"
INSTALL_CRON="0"

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
die()  { err "$*"; hr; err "备份已停止。请把上面红色的提示整段复制给技术人员。"; exit 1; }

dc() { docker compose -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"; }

usage() {
    cat <<'TXT'
知否题库 · 数据备份脚本

用法：
  bash /opt/zhifou/scripts/backup.sh [选项]

选项：
  --keep-days N    备份保留 N 天（默认 7）。只清理 /opt/zhifou/backups 里
                   由本脚本生成的 db-*.dump / media-*.tar.gz / static-*.tar.gz
  --install-cron   写入系统定时任务：每天凌晨 3:00 自动备份一次
  --help           显示这段说明
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

# 人类可读的文件大小
human_size() {
    local f="$1"
    if [ -f "$f" ]; then
        du -h "$f" 2>/dev/null | awk '{print $1}'
    else
        printf '%s' "-"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep-days)
            shift || { err "--keep-days 后面要跟数字"; exit 1; }
            KEEP_DAYS="$1"
            printf '%s' "$KEEP_DAYS" | grep -qE '^[0-9]+$' || { err "--keep-days 必须是数字，收到：$KEEP_DAYS"; exit 1; }
            ;;
        --install-cron) INSTALL_CRON="1" ;;
        -h | --help)    usage; exit 0 ;;
        *)              err "不认识的参数：$1"; usage; exit 1 ;;
    esac
    shift
done

# ═══════════════════════════════════════════════════════════════════
hr
info "开始备份知否题库数据"
hr
# ═══════════════════════════════════════════════════════════════════
self_crlf_check
[ "$(id -u)" -eq 0 ] || die "请用 root 用户执行（宝塔终端默认就是 root）。"
command -v docker >/dev/null 2>&1 || die "没有检测到 docker。"
docker info >/dev/null 2>&1 || die "docker 守护进程没在运行，请先执行 systemctl start docker"
[ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE"
cd "$APP_DIR"

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
ok "备份时间戳：${TS}"
info "备份目录：${BACKUP_DIR}"

# ── 1. 数据库 ──
db_dump="${BACKUP_DIR}/db-${TS}.dump"
if have_service db; then
    db_user="$(read_env POSTGRES_USER "$(read_env ZF_DB_USER "zhifou")")"
    db_name="$(read_env POSTGRES_DB "$(read_env ZF_DB_NAME "zhifou")")"
    info "备份 PostgreSQL：库 ${db_name}（用户 ${db_user}）"

    # 数据库没在跑就先拉起来（这一步不会动数据）
    if ! dc ps --status=running --services 2>/dev/null | grep -qx db; then
        info "db 容器没在运行，先启动……"
        dc up -d db
        sleep 8
    fi

    if dc exec -T db pg_dump -U "$db_user" -Fc "$db_name" >"$db_dump"; then
        ok "数据库备份完成：${db_dump}（$(human_size "$db_dump")）"
    else
        rm -f "$db_dump"
        err "pg_dump 失败。请检查 .env 里的数据库用户名/库名是否和 docker-compose.yml 一致。"
        err "也可以试试：cd $APP_DIR && docker compose -p zhifou exec -T db pg_dump -U postgres -Fc $db_name > /tmp/test.dump"
        exit 1
    fi

    # 校验：pg_dump -Fc 出来的文件开头一定是 PGDMP
    if ! head -c 5 "$db_dump" | grep -q 'PGDMP'; then
        err "备份文件开头不是 PGDMP，说明导出内容不对，这份备份不能用！"
        err "文件：${db_dump}"
        exit 1
    fi
else
    warn "compose 里没有 db 服务，跳过数据库备份。"
    db_dump=""
fi

# ── 2. media ──
# 先确认后端真的把文件写到 /app/media（宿主机 $MEDIA_DIR）。
# 如果 ZF_STORAGE_ROOT 没生效，后端会回落到容器里的 /app/.zf-storage，
# 结果：宿主机这个目录永远空 → 打成空包 → 重建容器后扫描件全丢，而且不报错。
storage_root="$(dc exec -T web python -c "from apps.media.storage import get_backend; print(get_backend().root)" 2>/dev/null || true)"
if [ -z "$storage_root" ]; then
    # 查不动 ≠ 没问题：只警告，不拦人（可能接口改名或 web 没在跑）
    warn "无法自动确认后端媒体存储根（web 容器可能没在跑，或接口改名）。"
    warn "★ 这不是「通过」。请手动执行下面这条，正确输出必须是 /app/media："
    echo "    cd ${APP_DIR} && docker compose -p zhifou exec web python -c \"from apps.media.storage import get_backend; print(get_backend().root)\""
    warn "在确认之前，本脚本继续按 ${MEDIA_DIR} 打包，但那份 media 可能是空包。"
elif [ "$storage_root" = "/app/media" ]; then
    ok "后端媒体存储根 = ${storage_root}，对应宿主机 ${MEDIA_DIR}，打包路径正确"
else
    # 查出来了、而且是错的 —— 必须当场拦住
    err "后端媒体存储根是「${storage_root}」，正确值必须是「/app/media」。"
    err "说明 ZF_STORAGE_ROOT 没生效：上传的文件都落在容器内部，"
    err "宿主机 ${MEDIA_DIR} 会是空的 —— 这份 media 备份必然是空包，"
    err "而且重建容器后上传的扫描件 / 答题卡会全部丢失（整个过程不报错）。"
    err "修法：在 ${ENV_FILE} 里加 ZF_STORAGE_ROOT=/app/media，然后执行："
    err "    cd ${APP_DIR} && docker compose -p zhifou up -d"
    exit 1
fi

media_tar="${BACKUP_DIR}/media-${TS}.tar.gz"
if [ -d "$MEDIA_DIR" ] && [ -n "$(ls -A "$MEDIA_DIR" 2>/dev/null || true)" ]; then
    media_count="$(find "$MEDIA_DIR" -type f 2>/dev/null | wc -l)"
    info "打包 media 目录（题目配图 / 扫描件 / 答题卡），共 ${media_count} 个文件……"
    tar -czf "$media_tar" -C "$APP_DIR" media
    ok "media 备份完成：${media_tar}（$(human_size "$media_tar")）"
else
    rm -f "$media_tar"
    media_tar=""

    # 「目录是空的」有两种完全不同的原因，必须区分开，
    # 否则「ZF_STORAGE_ROOT 没生效」这个错误会被永久、静默地掩盖掉。
    # 判据：去数据库里数一下媒体记录条数。
    #   - 有记录但磁盘没文件  → 一定是存储根错了，这是真故障，必须报错退出
    #   - 没记录且磁盘没文件  → 确实还没人上传过（新环境），警告但继续
    # ★ 表名是 asset，不是 media_asset：
    #   apps/media/models.py:102-103 里 Asset.Meta 显式写了 db_table = "asset"，
    #   不是按 app 名拼出来的。写成 media_asset 会报 relation does not exist，
    #   这条判据就永远走不到、隐患会被漏掉。
    # ★ deleted_at is null：Asset 继承 SoftDeleteModel，软删的记录不算「应该有文件」。
    asset_count="$(dc exec -T db psql -U "$db_user" -d "$db_name" -tAc \
        "select count(*) from asset where deleted_at is null" 2>/dev/null | tr -d '[:space:]' || true)"
    if [ -z "$asset_count" ]; then
        # 兜底：万一表里没有 deleted_at 这一列，退化成不带条件的计数
        asset_count="$(dc exec -T db psql -U "$db_user" -d "$db_name" -tAc \
            "select count(*) from asset" 2>/dev/null | tr -d '[:space:]' || true)"
    fi

    if [ -n "$asset_count" ] && [ "$asset_count" -gt 0 ] 2>/dev/null; then
        err "$MEDIA_DIR 是空的，但数据库里登记了 ${asset_count} 条媒体记录。"
        err "说明文件根本没写到这个目录 —— 极可能是 .env 里 ZF_STORAGE_ROOT 没设成 /app/media，"
        err "文件被写进了容器内部，重建容器就会全部丢失。"
        err "先执行这条确认（输出必须是 /app/media）："
        err "    cd ${APP_DIR} && docker compose -p zhifou exec web python -c \"from apps.media.storage import get_backend; print(get_backend().root)\""
        exit 1
    fi

    # ★ 两种「空」必须写成不同措辞，别让人看混：跳过 ≠ 通过
    if [ "$storage_root" = "/app/media" ]; then
        warn "$MEDIA_DIR 不存在或是空的，本次没有打包 media。"
        warn "【判定：真正的空，属正常】已确认存储根是 /app/media，"
        warn "且数据库里没有媒体记录 —— 说明这台机器确实还没人上传过文件。"
        warn "★ 但如果这个系统已经用了一段时间、老师明明传过图片，那就是故障，"
        warn "  请立刻按《部署手册》第 6 节故障 19 处理，不要当成正常现象忽略。"
    else
        warn "$MEDIA_DIR 不存在或是空的，本次没有打包 media。"
        warn "【判定：无法确认，跳过 ≠ 通过】没能读到媒体存储根，"
        warn "无法证明这是「还没上传」还是「文件写错地方了」。"
        warn "★ 请先手动确认存储根为 /app/media，再重跑本脚本；"
        warn "  否则这份备份会一直缺 media，而且你不会收到任何提示。"
    fi
fi

# ── 3. staticfiles ──
static_tar="${BACKUP_DIR}/static-${TS}.tar.gz"
if [ -d "$STATIC_DIR" ] && [ -n "$(ls -A "$STATIC_DIR" 2>/dev/null || true)" ]; then
    info "打包 staticfiles 目录（后台样式）……"
    tar -czf "$static_tar" -C "$APP_DIR" staticfiles
    ok "staticfiles 备份完成：${static_tar}（$(human_size "$static_tar")）"
else
    warn "$STATIC_DIR 不存在或是空的，跳过（可以用 collectstatic 随时重建）。"
    rm -f "$static_tar"
    static_tar=""
fi

# ── 4. 写一份说明文件，方便以后对得上哪三个文件是一组 ──
manifest="${BACKUP_DIR}/MANIFEST-${TS}.txt"
{
    printf '知否题库备份\n'
    printf '时间：%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf '主机名：%s\n' "$(hostname)"
    printf '数据库：%s\n' "${db_dump:-（无）}"
    printf 'media：%s\n' "${media_tar:-（无）}"
    printf 'staticfiles：%s\n' "${static_tar:-（无）}"
    printf '\n恢复方法：bash %s/scripts/restore.sh %s\n' "$APP_DIR" "$TS"
} >"$manifest"
ok "已生成备份说明：${manifest}"

# ═══════════════════════════════════════════════════════════════════
hr
info "清理 ${KEEP_DAYS} 天之前的旧备份"
hr
# ═══════════════════════════════════════════════════════════════════
info "清理范围（只限下面这三类文件，且只在 ${BACKUP_DIR} 这一层目录里）："
echo "    ${BACKUP_DIR}/db-*.dump"
echo "    ${BACKUP_DIR}/media-*.tar.gz"
echo "    ${BACKUP_DIR}/static-*.tar.gz"
echo "    ${BACKUP_DIR}/MANIFEST-*.txt"

victims="$(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'db-*.dump' -o -name 'media-*.tar.gz' -o -name 'static-*.tar.gz' -o -name 'MANIFEST-*.txt' \) \
    -mtime "+${KEEP_DAYS}" -print | sort || true)"

if [ -z "$victims" ]; then
    ok "没有超过 ${KEEP_DAYS} 天的旧备份，不需要清理。"
else
    warn "下面这些文件将被删除（超过 ${KEEP_DAYS} 天）："
    printf '%s\n' "$victims" | sed 's/^/    /'
    if [ -t 0 ]; then
        if confirm "确认删除上面这些旧备份？(y/N)："; then
            printf '%s\n' "$victims" | while IFS= read -r f; do rm -f "$f"; done
            ok "旧备份已清理"
        else
            info "已跳过清理，旧备份保留。"
        fi
    else
        # cron 等无人值守场景：直接清理（范围已在上面严格限定）
        printf '%s\n' "$victims" | while IFS= read -r f; do rm -f "$f"; done
        ok "非交互模式，已按规则清理旧备份"
    fi
fi

# ═══════════════════════════════════════════════════════════════════
hr
info "当前 ${BACKUP_DIR} 里的备份（按时间倒序）"
hr
# ═══════════════════════════════════════════════════════════════════
ls -lhtr "$BACKUP_DIR" 2>/dev/null | tail -n 30 | sed 's/^/    /' || true
info "磁盘占用：$(du -sh "$BACKUP_DIR" 2>/dev/null | awk '{print $1}')"

# ═══════════════════════════════════════════════════════════════════
hr
printf '%s%s 备份完成 ✔%s\n' "$C_BOLD" "$C_GREEN" "$C_OFF"
hr
# ═══════════════════════════════════════════════════════════════════

if [ "$INSTALL_CRON" = "1" ]; then
    info "准备写入系统定时任务：每天凌晨 3:00 自动备份一次"
    info "将写入文件：${CRON_FILE}"
    info "内容："
    echo "    0 3 * * * root /usr/bin/bash ${APP_DIR}/scripts/backup.sh >> ${APP_DIR}/backups/cron.log 2>&1"
    if confirm "确认写入？(y/N)："; then
        mkdir -p "$BACKUP_DIR"
        cat >"$CRON_FILE" <<EOF
# 知否题库 · 每天凌晨 3:00 自动备份
# 保留 ${KEEP_DAYS} 天，备份目录：${BACKUP_DIR}
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * * root /usr/bin/bash ${APP_DIR}/scripts/backup.sh >> ${BACKUP_DIR}/cron.log 2>&1
EOF
        chmod 0644 "$CRON_FILE"
        systemctl restart crond 2>/dev/null || systemctl restart cron 2>/dev/null || true
        ok "定时任务已写入并重启了 crond。"
        info "确认是否生效： cat ${CRON_FILE}"
        info "第二天看日志： tail -n 50 ${BACKUP_DIR}/cron.log"
    else
        info "已取消写入定时任务。"
    fi
else
    info "想让它每天自动备份，执行下面这一行（会问你确认）："
    echo "    bash ${APP_DIR}/scripts/backup.sh --install-cron"
    hr
    info "也可以自己在宝塔里加：宝塔面板 → 计划任务 → 任务类型选「shell脚本」"
    echo "    执行周期：每天 凌晨 3:00"
    echo "    脚本内容：bash ${APP_DIR}/scripts/backup.sh"
fi

hr
warn "最后一步（很重要）：把 ${BACKUP_DIR} 里的 .dump 文件下载到你自己的电脑上保存一份。"
warn "服务器硬盘出问题时，只有你电脑上的这份能救回来。"
