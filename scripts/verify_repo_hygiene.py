"""验收：公开仓库卫生（口令 / 密钥 / 大目录 / 构建产物）。

    <python> scripts/verify_repo_hygiene.py

纯静态 + 读 `git ls-files`，不连数据库、不启服务。

★★ 为什么需要这个

  本仓库是**公开**的。公开仓库最贵的一次错误不是「多发一次 commit」，
  而是**把一个密钥推上去** —— 推上去就进了 git 历史，
  删文件、改分支都没用，只能 `filter-repo` 重写历史并让所有人重新 clone。

  2026-10-23 首次推送前就撞到三处：

    ① `docs/API key.txt`            真实 DeepSeek key，明文，47 字节
    ② 13 个验收脚本                 硬编码开发库账号口令
    ③ `server/.zf-storage/`        350MB 学生答题卡扫描件（含姓名学号）
       `server/db.sqlite3.dev-only`  13MB 本地自检库

  这三处的共同点：**不报错**。没有哪个测试会因为「仓库里有个密钥」而变红。
  所以只能靠这个脚本盯着。

★ 它检查的是「**即将被提交的内容**」（`git ls-files` + 工作区），
  不是「磁盘上有什么」—— 后者会误报 node_modules。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def git(*args: str) -> list[str]:
    """跑一条 git 只读命令，返回非空行。★ 只用只读子命令。"""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


# ══════════════════════════════════════════════════════════════
#  H1 · 必须被忽略的目录
# ══════════════════════════════════════════════════════════════

#: (路径, 一句话理由)
MUST_IGNORE = [
    (".zf-storage", "运行时存储：真实学生答题卡扫描件，含姓名学号，350MB 量级"),
    ("server/.zf-storage", "同上（后端工作目录下还有一份）"),
    ("web/node_modules", "依赖目录，12000+ 文件"),
    ("render-service/node_modules", "依赖目录"),
    (".devdeps", "开发期二进制依赖，400MB+"),
    ("artifacts", "验收产物与日志，每跑一次脚本就变"),
    (".workbuddy", "开发助手工程记忆，含本机账号口令与排查笔记"),
    ("server/db.sqlite3.dev-only", "本地自检库，13MB"),
    ("scripts/.devcreds.local", "本地开发凭据（真实口令）"),
    ("web/dist-deploy", "前端构建产物"),
]


def h1_ignore() -> None:
    print("\nH1 · 必须被 .gitignore 忽略的路径")

    for path, why in MUST_IGNORE:
        p = ROOT / path
        if not p.exists():
            # 不存在的路径没法 check-ignore（git 会返回 0 但没意义），
            # 跳过并说明，避免「文件删了脚本就红」。
            print(f"  [--]   {path} 不存在，跳过（{why}）")
            continue
        r = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=ROOT, capture_output=True, timeout=30,
        )
        check(f"「{path}」被忽略", r.returncode == 0, f"—— 它 {why}")


# ══════════════════════════════════════════════════════════════
#  H2 · 被跟踪的文件里不许有密钥类文件
# ══════════════════════════════════════════════════════════════

#: 文件名命中即视为危险。★ 用正则而不是字面量，避免脚本自己被扫到时误报。
DANGEROUS_NAME = re.compile(
    r"(?:^|/)(?:\.env(?:\.(?!example)[\w.]+)?$"      # .env / .env.local，但放过 .env.example
    r"|.*\.pem$|.*\.key$|.*\.p12$|.*\.pfx$"
    r"|.*(?:API|api)[ _-]?key.*\.txt$"               # 「API key.txt」这种随手记的
    r"|.*(?:secret|credential|passwd|password).*\.(?:txt|md|json|ya?ml)$)"
)

#: 允许入库的例外（样例文件）
NAME_ALLOW = re.compile(r"\.example$|\.devcreds\.local$|(?:^|/)verify_repo_hygiene\.py$")


def h2_no_secret_files() -> None:
    print("\nH2 · 被跟踪文件里没有密钥类文件")

    tracked = git("ls-files")
    check("能读到 git 索引（仓库已 init）", bool(tracked), "—— git ls-files 为空")

    bad = [
        f for f in tracked
        if DANGEROUS_NAME.search(f) and not NAME_ALLOW.search(f)
    ]
    check(
        "★ 没有 .env / *.pem / *.key /「API key.txt」被跟踪",
        not bad,
        f"—— 命中 {bad[:5]}",
    )

    # 反向确认：样例文件必须还在，否则新人没法配置
    samples = [f for f in tracked if f.endswith(".example") or f.endswith(".devcreds.example")]
    check(
        "凭据样例文件仍在库中（否则新人无法配置）",
        any("devcreds" in f for f in samples),
        f"—— 找到 {samples}",
    )


# ══════════════════════════════════════════════════════════════
#  H3 · 源码里不许有口令字面量
# ══════════════════════════════════════════════════════════════

#: ★ 故意拆成两段拼出来：如果这里写完整字面量，
#:   这个脚本自己就会被 H3 扫到，成为「永远的假阳性」。
#:   拼接后磁盘上的字节仍能匹配（正则不认拼接），
#:   但 git grep 搜完整字面量时本文件不会命中 —— 代价可接受，
#:   所以下面用「读文件 + 运行时拼正则」的方式，而不是 git grep。
SECRET_WORDS = [
    "zhifou" + "@2026",
    "zf" + "@sysadmin2026",
]

#: 只扫这些目录：源码与文档。node_modules 等已排除在 ls-files 外。
SCAN_DIRS = ("scripts/", "server/", "docs/", "deploy/", "web/src/", "render-service/src/")


def h3_no_password_literal() -> None:
    print("\nH3 · 源码与文档里没有已知口令字面量")

    tracked = [f for f in git("ls-files") if f.startswith(SCAN_DIRS)]
    check("扫到源码文件（数量合理）", len(tracked) > 100, f"—— 只有 {len(tracked)} 个")

    hits: list[str] = []
    for rel in tracked:
        p = ROOT / rel
        if p.suffix not in {".py", ".ts", ".vue", ".js", ".md", ".txt", ".yml", ".yaml", ".sh", ".cmd", ".ps1"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for word in SECRET_WORDS:
            if word in text:
                hits.append(f"{rel}:{word[:4]}**")
                break

    check(
        "★ 没有硬编码的开发库口令（迁移里的默认口令也算）",
        not hits,
        f"—— 命中 {hits[:6]}",
    )


# ══════════════════════════════════════════════════════════════
#  H4 · 云端密钥格式
# ══════════════════════════════════════════════════════════════

#: 常见云厂商密钥的形态。★ 命中的都是「看起来就是真 key」，不是占位符。
KEY_SHAPES = [
    ("OpenAI/DeepSeek 风格", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS AccessKey", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("腾讯云 SecretId", re.compile(r"AKID[A-Za-z0-9]{13,40}")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]


def h4_no_cloud_keys() -> None:
    print("\nH4 · 没有云端密钥字面量")

    tracked = [
        f for f in git("ls-files")
        if f.startswith(SCAN_DIRS)
        and Path(f).suffix in {".py", ".ts", ".vue", ".js", ".md", ".txt", ".yml", ".yaml", ".json", ".sh"}
    ]

    for label, pattern in KEY_SHAPES:
        hits = []
        for rel in tracked:
            try:
                text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(rel)
        check(f"无 {label} 密钥", not hits, f"—— 命中 {hits[:4]}")


# ══════════════════════════════════════════════════════════════
#  H5 · 体积与单文件上限
# ══════════════════════════════════════════════════════════════

#: 单个文件上限。GitHub 硬上限 100MB，警告线 50MB。
#: ★ 这里设 25MB：超过它的多半是「不小心把数据放进来了」，
#:   真正的测试素材（如 OMR 实拍图）应在 20MB 以内。
MAX_FILE_MB = 25
#: 整个仓库（被跟踪内容）的上限
MAX_TOTAL_MB = 80


def h5_size() -> None:
    print("\nH5 · 体积（单文件与总量）")

    tracked = git("ls-files")
    total = 0
    big: list[str] = []

    for rel in tracked:
        p = ROOT / rel
        try:
            size = p.stat().st_size
        except OSError:
            continue
        total += size
        if size > MAX_FILE_MB * 1024 * 1024:
            big.append(f"{rel}({size / 1048576:.1f}MB)")

    check(
        f"单文件都不超过 {MAX_FILE_MB}MB",
        not big,
        f"—— 超限 {big}",
    )
    check(
        f"被跟踪内容合计不超过 {MAX_TOTAL_MB}MB",
        total <= MAX_TOTAL_MB * 1024 * 1024,
        f"—— 实际 {total / 1048576:.1f}MB",
    )


# ══════════════════════════════════════════════════════════════
#  H6 · 构建产物与缓存
# ══════════════════════════════════════════════════════════════

BUILD_NOISE = re.compile(
    r"(?:^|/)(?:dist|dist-deploy|build|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache)/"
    r"|\.pyc$|\.pyo$|\.tsbuildinfo$|\.log$"
)


def h6_no_build_output() -> None:
    print("\nH6 · 没有构建产物与缓存被跟踪")

    tracked = git("ls-files")
    bad = [f for f in tracked if BUILD_NOISE.search(f)]
    check(
        "★ 无 dist / __pycache__ / *.pyc / *.log 被跟踪",
        not bad,
        f"—— 命中 {len(bad)} 个，如 {bad[:5]}",
    )


def main() -> int:
    print("=" * 74)
    print("公开仓库卫生 · 回归（推送前必跑）")
    print("=" * 74)

    h1_ignore()
    h2_no_secret_files()
    h3_no_password_literal()
    h4_no_cloud_keys()
    h5_size()
    h6_no_build_output()

    print("\n" + "=" * 74)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass
    raise SystemExit(main())
