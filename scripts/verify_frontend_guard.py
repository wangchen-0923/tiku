"""验收：前端路由守卫与角色等级（2026-10-22 「整站打不开」事故的防回归）。

    <python> scripts/verify_frontend_guard.py

不需要启动浏览器、不需要数据库 —— 全静态，跑一次不到 1 秒。

★★ 这个脚本要拦住的是**两类静默错误**，它们的共同点是：
   **不抛异常、不报错、浏览器控制台一片干净**，而后果是整站不可用。

    ① **守卫互踢 → 标签页永远转圈**
       未登录访问任意页 → 送去 Login；Login 有 `anonymousOnly` 跳过登录判断，
       但没跳过等级校验，未登录 `levelOf(undefined)=0 < 10` → 送去 Forbidden；
       Forbidden 没有 `anonymousOnly`，`isAnonymous` 又把它送回 Login —— 死循环。
       每次重定向都要加载一次路由组件，主线程被无限任务占满，
       `DOMContentLoaded` 永不触发，浏览器表现就是「一直转圈」。
       ★ 用 token 开发时 `isAnonymous` 恒为假，这条路径从不被走到。

    ② **角色名查不到等级 → 全部 403**
       `LEVEL` 的键是等级名（`SCHOOL` / `SYSTEM`），
       而调用方传的是角色名（`ORG_ADMIN` / `PLATFORM_ADMIN`）。
       `LEVEL['ORG_ADMIN']` 不存在 → `?? 0` → 学校平台账户与系统管理员
       被判成最低级 → 每个页面都跳 403。
       ★ `TEACHER` 与 `STUDENT` 恰好同名，所以「教师能用、管理员不能」，
         看起来像账号权限问题，不像代码缺陷。

  两处都必须靠机器盯着：项目里没有前端 e2e 基础设施，
  而后端 `verify_*` 脚本摸不到路由守卫。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"
SERVER = ROOT / "server"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


# ══════════════════════════════════════════════════════════════
#  G1 · 前后端角色等级表必须逐字一致
# ══════════════════════════════════════════════════════════════


def _backend_rank() -> dict[str, int]:
    """从 `apps/common/permissions.py` 读出 `ROLE_RANK`（走 ast，不 import）。"""
    src = read(SERVER / "apps" / "common" / "permissions.py")
    if not src:
        return {}

    tree = ast.parse(src)
    consts: dict[str, int] = {}
    rank: dict[str, int] = {}

    def take_assign(target: ast.expr, value: ast.expr) -> None:
        """收 `LEVEL_X = 20` 与 `ROLE_RANK = {...}` 两种赋值。

        ★ 必须同时处理 `Assign` 与 `AnnAssign`：项目里写的是
          `ROLE_RANK: dict[str, int] = {...}`（带注解），
          只认 `Assign` 会静默拿到空表 —— 第一版就栽在这，
          而脚本自己不报错，只会把「后端无角色」当成结论。
        """
        if isinstance(target, ast.Name) and target.id.startswith("LEVEL_"):
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                consts[target.id] = value.value
        if not (isinstance(target, ast.Name) and target.id == "ROLE_RANK"):
            return
        if not isinstance(value, ast.Dict):
            return
        for k, v in zip(value.keys, value.values):
            if not isinstance(k, ast.Constant):
                continue
            key = str(k.value)
            if isinstance(v, ast.Constant) and isinstance(v.value, int):
                rank[key] = int(v.value)
            elif isinstance(v, ast.Name):
                rank[key] = consts.get(v.id, -1)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                take_assign(tgt, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            take_assign(node.target, node.value)

    return rank


def _frontend_rank() -> tuple[dict[str, int], dict[str, int]]:
    """从 `constants/permissions.ts` 读出 `LEVEL` 与 `ROLE_RANK`（正则）。"""
    src = read(WEB / "constants" / "permissions.ts")
    level: dict[str, int] = {}
    rank: dict[str, int] = {}

    m = re.search(r"export const LEVEL = \{(.*?)\}\s*as const", src, re.S)
    if m:
        for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1)):
            level[k] = int(v)

    m = re.search(r"const ROLE_RANK: Record<string, number> = \{(.*?)\n\}", src, re.S)
    if m:
        for k, v in re.findall(r"(\w+)\s*:\s*LEVEL\.(\w+)", m.group(1)):
            rank[k] = level.get(v, -1)

    return level, rank


def g1_rank() -> None:
    print("\nG1 · 角色等级表：前端必须与后端 ROLE_RANK 逐字一致")

    backend = _backend_rank()
    level, frontend = _frontend_rank()

    check("后端 ROLE_RANK 可解析出角色", len(backend) >= 4, f"得到 {backend}")
    check("前端 ROLE_RANK 存在（不是只有 LEVEL）", len(frontend) >= 4, f"得到 {frontend}")
    check(
        "★ 角色集合完全一致",
        set(backend) == set(frontend),
        f"后端 {sorted(backend)} / 前端 {sorted(frontend)}",
    )

    for role in sorted(backend):
        check(
            f"★ {role} 等级 = {backend[role]}",
            frontend.get(role) == backend[role],
            f"前端算出 {frontend.get(role)}",
        )

    # ★ 光有表还不够：`levelOf` 必须真的读这张表。
    ts = read(WEB / "constants" / "permissions.ts")
    body = re.search(r"export function levelOf[\s\S]{0,400}?\n\}", ts)
    body_src = body.group(0) if body else ""
    check("★ levelOf 查的是 ROLE_RANK（不是直接查 LEVEL）", "ROLE_RANK[" in body_src, body_src[:80])
    check(
        "levelOf 保留按等级名兜底（历史调用方传 'SCHOOL'/'SYSTEM'）",
        "LEVEL as Record<string, number>" in body_src or "LEVEL[key]" in body_src,
    )
    check(
        "LEVEL 与 ROLE_RANK 的键名**故意不同**（这正是曾经的坑，别「统一」掉）",
        {"SCHOOL", "SYSTEM"} <= set(level) and "ORG_ADMIN" in frontend,
        f"LEVEL 键 {sorted(level)}",
    )


# ══════════════════════════════════════════════════════════════
#  G2 · 守卫结构：三条硬约束
# ══════════════════════════════════════════════════════════════


def g2_guard() -> None:
    print("\nG2 · 守卫结构：匿名页豁免、上限兜底、Forbidden 自洽")

    src = read(WEB / "router" / "index.ts")
    check("路由器文件可读", bool(src))

    # 取守卫函数体（从 beforeEach 到 afterEach 之前）
    guard = re.search(r"router\.beforeEach\(([\s\S]*?)\n\}\)", src)
    g = guard.group(0) if guard else ""

    check("守卫函数可解析", bool(g))

    idx_anon = g.find("if (to.meta.anonymousOnly) {")
    idx_need = g.find("const need =")
    check(
        "★ 匿名页（登录页）在等级校验**之前**被放行",
        idx_anon != -1 and idx_need != -1 and idx_anon < idx_need,
        f"anonymousOnly@{idx_anon} / need@{idx_need}",
    )
    check(
        "★ 放行语句真的是 return true",
        "if (to.meta.anonymousOnly) {\n    return true" in g.replace("\r\n", "\n"),
    )

    check(
        "★ 存在重定向硬上限（redirectHops）",
        "redirectHops" in src and re.search(r"if \(redirectHops > \d+\)", src) is not None,
    )
    check(
        "重定向计数会在导航落地后归零（afterEach）",
        re.search(r"router\.afterEach\([\s\S]{0,200}?redirectHops = 0", src) is not None,
    )

    # Forbidden 必须 public，否则「等级不足」的用户被送到这里后又判一次
    forb = re.search(r"name: 'Forbidden',[\s\S]{0,400}?meta: \{([^}]*)\}", src)
    meta = forb.group(1) if forb else ""
    check("★ Forbidden 路由标了 public: true", "public: true" in meta, meta.strip())
    check(
        "守卫里对 to.name === 'Forbidden' 有自环兜底",
        "to.name === 'Forbidden'" in g,
    )

    # Login 必须 still anonymousOnly
    login = re.search(r"name: 'Login',[\s\S]{0,400}?meta: \{([^}]*)\}", src)
    check(
        "Login 仍标 anonymousOnly: true",
        "anonymousOnly: true" in (login.group(1) if login else ""),
    )


# ══════════════════════════════════════════════════════════════
#  G3 · minLevel 取值只能用 LEVEL.*，不许写魔法数字
# ══════════════════════════════════════════════════════════════


def _strip_ts_comments(src: str) -> str:
    """剥掉 TS 注释。

    ★ 必须先剥：本文件的注释里就写着 `minLevel: LEVEL.SCHOOL` 作为说明，
      不剥的话静态断言会把「文档里的示例」当成「代码里的写法」，
      命中一堆假阳性（和批 3 的缺陷 13、批 4 的缺陷 15 是同一类错）。
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"//[^\n]*", "", src)


def g3_minlevel() -> None:
    print("\nG3 · minLevel 只允许引用 LEVEL 常量")

    src = _strip_ts_comments(read(WEB / "router" / "index.ts"))
    raw = re.findall(r"minLevel:\s*([^,\n}]+)", src)
    raw = [r.strip() for r in raw if r.strip() and not r.strip().startswith("$")]
    check("至少有一个路由声明了 minLevel", len(raw) >= 1, f"得到 {raw}")
    bad = [r for r in raw if not r.startswith("LEVEL.")]
    check("★ 没有裸数字 minLevel（写 20 而不写 LEVEL.SCHOOL 迟早对不上）", not bad, f"{bad}")

    used = {r.split(".")[-1] for r in raw if r.startswith("LEVEL.")}
    level, _ = _frontend_rank()
    unknown = sorted(u for u in used if u not in level)
    check("引用的 LEVEL.* 都真实存在", not unknown, f"未知 {unknown}")


def main() -> int:
    print("=" * 74)
    print("  前端路由守卫与角色等级 验收")
    print("=" * 74)

    g1_rank()
    g2_guard()
    g3_minlevel()

    print("\n" + "=" * 74)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
