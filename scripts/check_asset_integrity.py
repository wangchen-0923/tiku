"""资产完整性检查 —— 数据库记录与存储文件是否对得上。

★ 为什么需要这个脚本（2026-10-21 实测触发）：

  `scan_card.raw_asset_id` 指向 81 条 `asset` 记录，但磁盘上一个文件都没有；
  同时磁盘上躺着 128 个 `SCAN_CARD` 文件，数据库里**零记录**。
  两类问题同时存在：

    · **悬空记录**（DB 有、文件无）—— 读取时报 FileNotFound，
      表现为「识别任务莫名失败」「预览图打不开」，日志里只有一句 I/O 错误。
    · **孤儿文件**（文件有、DB 无）—— 白占磁盘，且清理任务永远不会删它，
      因为清理是按 DB 记录走的。

  这件事直接卡住了一个开发任务：想用存量 51 张卡验证「新几何对识别质量的影响」，
  没有原图就重跑不了，只能放弃。

★★ 判据必须按 **storage backend** 走，不能自己拼路径：

  本地后端时 `oss_key` 相对 `ZF_STORAGE_ROOT`；
  OSS 后端时它是对象键，本地文件系统上本来就不该有文件 ——
  那种情况下"全部悬空"是正常的，脚本要能区分，否则会天天误报。

用法：

    python scripts/check_asset_integrity.py               # 摘要
    python scripts/check_asset_integrity.py --md out.md   # 写报告
    python scripts/check_asset_integrity.py --kind SCAN_CARD --list 20
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from apps.media.models import Asset  # noqa: E402


def storage_root() -> Path:
    """本地存储根目录。

    ★ 与 `settings.MEDIA_ROOT` 分开取：本项目实际写入的是
      `ZF_STORAGE_ROOT`（实测为 `<repo>/.zf-storage`），
      而 `MEDIA_ROOT` 指向 `server/media`（空的）。
      用错根目录会把"全部悬空"误报成灾难。
    """
    raw = os.environ.get("ZF_STORAGE_ROOT", "")
    if raw:
        return Path(raw)
    return SERVER.parent / ".zf-storage"


def is_local_backend() -> bool:
    backend = os.environ.get("ZF_STORAGE_BACKEND", "")
    if not backend:
        backend = (getattr(settings, "ZF_OBJECT_STORAGE", {}) or {}).get(
            "backend", "local")
    return str(backend).lower() in ("local", "", "filesystem")


def scan(kind_filter: str = "") -> dict:
    qs = Asset.objects.all()
    if kind_filter:
        qs = qs.filter(kind=kind_filter)
    rows = list(qs.values_list("id", "kind", "oss_key", "byte_size"))

    root = storage_root()
    local = is_local_backend()

    by_kind: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "ok": 0, "missing": 0})
    missing: list[dict] = []
    db_keys: set[str] = set()

    for aid, kind, key, size in rows:
        s = by_kind[kind]
        s["total"] += 1
        db_keys.add(key)
        if not local:
            # 对象存储：本地没有文件是正常的，不判定
            continue
        p = root / key
        if p.is_file():
            s["ok"] += 1
        else:
            s["missing"] += 1
            missing.append({"id": aid, "kind": kind, "oss_key": key,
                            "byte_size": size})

    # 孤儿文件：只在本地后端、且限定同一 kind 的目录下才有意义
    orphans: list[str] = []
    if local and kind_filter:
        base = root / "zf"
        if base.is_dir():
            for p in base.rglob(f"{kind_filter}/*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if rel not in db_keys:
                    orphans.append(rel)

    return {
        "local": local,
        "root": str(root),
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "missing": missing,
        "orphans": orphans,
    }


def render_md(d: dict) -> str:
    L = ["# 资产完整性报告", ""]
    L.append(f"- 存储后端：{'本地' if d['local'] else '对象存储'}")
    L.append(f"- 存储根目录：`{d['root']}`")
    L.append("")
    L.append("| 类型 | 记录数 | 文件存在 | 文件缺失 |")
    L.append("|---|---|---|---|")
    for k, v in sorted(d["by_kind"].items(), key=lambda x: -x[1]["total"]):
        L.append(f"| {k} | {v['total']} | {v['ok']} | {v['missing']} |")
    L.append("")
    if d["missing"]:
        L.append("## 悬空记录（DB 有、文件无）")
        L.append("")
        L.append("★★ 这类记录一旦被读取就会抛 I/O 错误，且报错信息看不出是"
                 "「文件没了」还是「逻辑错了」。")
        L.append("")
        L.append("| asset_id | 类型 | oss_key | 字节 |")
        L.append("|---|---|---|---|")
        for r in d["missing"][:50]:
            L.append(f"| {r['id']} | {r['kind']} | `{r['oss_key']}` | "
                     f"{r['byte_size']} |")
        if len(d["missing"]) > 50:
            L.append(f"| … | 共 {len(d['missing'])} 条 | | |")
        L.append("")
    if d["orphans"]:
        L.append("## 孤儿文件（文件有、DB 无）")
        L.append("")
        L.append("★ 清理任务按 DB 记录走，所以这些文件永远不会被删，只增不减。")
        L.append("")
        for o in d["orphans"][:50]:
            L.append(f"- `{o}`")
        if len(d["orphans"]) > 50:
            L.append(f"- … 共 {len(d['orphans'])} 个")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="", help="只查某一类型，如 SCAN_CARD")
    ap.add_argument("--md", default="")
    ap.add_argument("--list", type=int, default=0, help="打印前 N 条明细")
    args = ap.parse_args()

    d = scan(args.kind)
    print(f"存储后端={'本地' if d['local'] else '对象存储'} 根={d['root']}")
    for k, v in sorted(d["by_kind"].items(), key=lambda x: -x[1]["total"]):
        flag = "  ← 有缺失" if v["missing"] else ""
        print(f"  {k:<18} 记录 {v['total']:>4}  存在 {v['ok']:>4}  "
              f"缺失 {v['missing']:>4}{flag}")
    if d["orphans"]:
        print(f"  孤儿文件 {len(d['orphans'])} 个（DB 无对应记录）")
    if args.list:
        for r in d["missing"][: args.list]:
            print(f"   MISS asset={r['id']} {r['kind']} {r['oss_key']}")
    if args.md:
        Path(args.md).write_text(render_md(d), encoding="utf-8")
        print(f"已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
