"""环境自检：确认前端代理、登录、各页面依赖的接口都通。"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

OK = []
BAD = []


# ══════════════════════════════════════════════════════════════
#  0. 数据库 / Redis / MEDIA 环境摘要与端口校验
# ══════════════════════════════════════════════════════════════

def _port_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_runtime_env() -> None:
    """打印一行环境摘要；PostgreSQL 模式端口不通直接失败。"""
    engine = (os.environ.get("ZF_DB_ENGINE") or "").strip().lower()
    require_pg = "--require-postgres" in sys.argv
    if engine == "sqlite":
        db = "sqlite:" + str(
            Path(r"D:\tiku\server") / "db.sqlite3.dev-only")
        print(f"[ENV] DB={db} | REDIS="
              f"{os.environ.get('ZF_REDIS_URL') or 'redis://127.0.0.1:6379/0'}"
              f" | MEDIA={os.environ.get('ZF_MEDIA_ROOT') or 'D:\\tiku\\server\\media'}")
        print("[WARN] 当前是 SQLite：仅用于迁移生成/自检，不是本项目正式开发库")
        if require_pg:
            print("[FAIL] 要求 PostgreSQL，但当前 ZF_DB_ENGINE=sqlite")
            sys.exit(2)
        return

    host = os.environ.get("ZF_DB_HOST") or "127.0.0.1"
    port = int(os.environ.get("ZF_DB_PORT") or 5432)
    name = os.environ.get("ZF_DB_NAME") or "zhifou"
    redis = os.environ.get("ZF_REDIS_URL") or "redis://127.0.0.1:6379/0"
    media = os.environ.get("ZF_MEDIA_ROOT") or r"D:\tiku\server\media"
    print(f"[ENV] DB=postgres:{host}:{port}/{name} | REDIS={redis} | MEDIA={media}")
    if not _port_ok(host, port):
        print(
            f"[FAIL] PostgreSQL 端口不通：{host}:{port}\n"
            f"       请设置 ZF_DB_PORT=15433（容器 tiku_postgres_15433），"
            f"不要用 5432（那是另一个项目）。\n"
            f"       启动：docker compose -f deploy/compose.db.yml up -d"
        )
        sys.exit(2)
    print(f"[OK] PostgreSQL 端口可达：{host}:{port}")


check_runtime_env()


def _q(u):
    if "?" not in u:
        return u
    b, _, q = u.partition("?")
    return f"{b}?{quote(q, safe='=&%')}"


def call(url, *, method="GET", body=None, token=None, timeout=30):
    req = urllib.request.Request(_q(url), method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"raw": str(e)}


def check(label, cond, detail=""):
    (OK if cond else BAD).append(label)
    print(f"  {'✓' if cond else '✗'} {label}"
          + (f"  —— {detail}" if detail and not cond else ""))


PROXY = "http://127.0.0.1:5173"
API = f"{PROXY}/api/v1"

print("=" * 68)
print("环境自检 · 知否")
print("=" * 68)

print()
print("【1】前端 dev server")
# ★★ 这里**不能**用上面的 `call()`（那是给 JSON 接口用的）。
#
#   `urllib` 读 `text/html` 且响应没有 `Content-Length` 时会抛/挂，
#   我第一版拿它去 GET `/`，得到「HTTP 404」——
#   而同一地址用 `Invoke-WebRequest` 明明是 **200 + 完整 HTML**。
#   ★ 差点把「检查工具的脾气」当成「环境故障」报出去。
#   这里改用只检查「连得上 + 拿得到 HTML」。
try:
    with urllib.request.urlopen(f"{PROXY}/", timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
        ct = r.headers.get("Content-Type", "")
        has_mount = 'id="app"' in html
        check("GET / 返回 HTML 且含挂载点 #app",
              r.status == 200 and "text/html" in ct and has_mount,
              f"HTTP {r.status} ct={ct} #app={has_mount} len={len(html)}")
except Exception as e:  # noqa: BLE001
    check("GET / 返回 HTML", False, f"{type(e).__name__}: {e}")

print()
print("【2】★ 前端代理 → 后端（浏览器走的就是这条链路）")
st, j = call(f"{PROXY}/health/")
check("GET /health/ 经代理 → 200", st == 200, f"HTTP {st} {str(j)[:120]}")

print()
print("【3】登录")
st, j = call(f"{API}/auth/login/", method="POST",
             body=admin_login())
tok = (j.get("data") or {}).get("access_token") if st == 200 else None
check("POST /api/v1/auth/login/ → 200 且有 token", bool(tok),
      f"HTTP {st} {str(j)[:180]}")

if not tok:
    print()
    print("  ★ 登录失败，后面跳过")
    print(f"\n通过 {len(OK)} 项，失败 {len(BAD)} 项")
    sys.exit(1)

print()
print("【4】页面依赖的接口（全部经前端代理）")
pages = [
    ("题库列表   /questions", f"{API}/questions/?page_size=3"),
    ("题库统计   /questions", f"{API}/questions/stats/"),
    ("知识体系   /knowledge", f"{API}/knowledge/courses/"),
    ("知识树     /knowledge", f"{API}/knowledge/courses/1/tree/"),
    ("知识库健康 /knowledge", f"{API}/knowledge/health/"),
    ("试卷列表   发布流程", f"{API}/papers/?page_size=3"),
    ("作业列表   发布流程", f"{API}/assignments/?page_size=3"),
    ("答题卡样式 /sheets", f"{API}/sheet/styles/"),
    ("答题卡模板 /sheets", f"{API}/sheet/templates/"),
    ("班级列表   发布流程", f"{API}/org/classrooms/?page_size=3"),
]
for label, url in pages:
    st, j = call(url, token=tok)
    code = (j or {}).get("code", "?")
    ok = st == 200 and code == "OK"
    mk = "✓" if ok else "✗"
    (OK if ok else BAD).append(label)
    print(f"  {mk} {label:<26} HTTP {st}  code={code}")

print()
print("【5】关键数据是否在位")
st, j = call(f"{API}/questions/stats/", token=tok)
d = (j.get("data") or {}) if st == 200 else {}
check("可组卷题数 > 0（真实题库已就位）",
      isinstance(d.get("usable_total"), int) and d.get("usable_total") > 0,
      f"usable_total={d.get('usable_total')}")
diff = d.get("difficulty") or {}
check("难度健康度字段在位（题库页橙色告警用）",
      "needs_annotation" in diff, str(sorted(diff.keys())))
check("难度健康度字段为布尔（题库页告警可读）",
      isinstance(diff.get("needs_annotation"), bool),
      f"needs_annotation={diff.get('needs_annotation')}")

st, j = call(f"{API}/knowledge/courses/1/nodes/", token=tok)
nodes = (j.get("data") or {}).get("items") if st == 200 else None
check("知识点 44 个", isinstance(nodes, list) and len(nodes) == 44,
      f"count={len(nodes) if isinstance(nodes, list) else '?'}")

st, j = call(f"{API}/questions/?page_size=1&keyword=齿轮", token=tok)
n = (j.get("data") or {}).get("total") if st == 200 else None
check("★★ 搜索真的在筛（keyword 收窄 total）",
      isinstance(n, int) and 0 < n < (d.get("usable_total") or 0),
      f"total={n}")

print()
print("=" * 68)
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    for b in BAD:
        print(f"  ✗ {b}")
else:
    print("  ✓ 环境就绪，可以开始验收")
print("=" * 68)
sys.exit(1 if BAD else 0)
