"""去掉目录下 Python 源码的 BOM，并做语法自检。

★ 为什么需要这个小工具（而不是每次手工处理）：

  PowerShell 的 `Set-Content -Encoding utf8` **一定会加 BOM**，
  而 Python 解释器遇到 `\\ufeff` 会直接
  `SyntaxError: invalid non-printable character U+FEFF` —— 连报错行号都指错。

  本项目里这个坑已经踩过 3 次（答题卡渲染脚本、org 回归套件、
  docx 渲染器）。所以凡是用 PowerShell 改过 .py 之后，
  跑一遍这个脚本比「记住别用 Set-Content」更可靠。
"""

import ast
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

roots = [pathlib.Path(p) for p in (sys.argv[1:] or [
    r"D:\tiku\server\apps", r"D:\tiku\scripts",
])]

fixed: list[str] = []
broken: list[str] = []

for root in roots:
    if not root.exists():
        continue
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig")
            p.write_bytes(text.encode("utf-8"))
            fixed.append(str(p))
        else:
            text = raw.decode("utf-8", errors="replace")
        try:
            ast.parse(text)
        except SyntaxError as e:
            broken.append(f"{p}:{e.lineno} {e.msg}")

print(f"  扫描 {len(roots)} 个目录")
print(f"  去除 BOM：{len(fixed)} 个")
for f in fixed:
    print(f"    · {f}")
if broken:
    print(f"  ★ 语法错误 {len(broken)} 个：")
    for b in broken:
        print(f"    ✗ {b}")
    sys.exit(1)
print("  ✓ 全部通过语法自检")
