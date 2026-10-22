"""OMR 自检与样本回归（21 文档任务三 · 4.3）。

把「这张卡识别不了」从口头问题变成**可复现、可定位、可回归**的工程问题。

用法：

    # 仓库自带样本（真实手机照片 + 合成 A5 卡）
    python scripts/omr_selfcheck.py --report reports/omr.html
    # 只调一张，输出三张调试图
    python scripts/omr_selfcheck.py --debug-card photo_1280_tilted

    # 换目录
    python scripts/omr_selfcheck.py --fixtures D:/omr-samples

样本目录约定（每个样本一对同名文件）：

    <name>.png / .jpg / .pdf        卡图
    <name>.expect.json              {
                                      "layout_json": {...},   # 该卡的模板布局
                                      # 或 "template_id": 3,  # 从库里取（需连库）
                                      "student_no": "20265001",
                                      "answers": {"1": "A", "2": "BC"},
                                      "status": "AUTO_OK" | "NEEDS_REVIEW" | "FAILED"
                                    }

输出：

  · 每张样本：定位成功/失败、学号、逐题答案、置信度、状态
  · 汇总：通过率、失败分类（定位 / 页标 / 学号 / 涂点 / 分辨率）
  · 失败时保存调试图：artifacts/omr-debug/{name}/-anchors.png / -warped.png / -samples.txt
  · HTML 报告（给教研看得懂的那种，不是一堆 JSON）

★★ 退出码非 0 表示有失败，可直接接 CI。
  —— 这条比报告本身重要：没人会每天点开 html 看，
     但 CI 变红一定会有人看。

★★ 本脚本**只做判定，不改线上阈值**。标定在 `omr_calibrate.py`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
# ★ 本机 Django/reportlab 等只在 .devdeps；追加到末尾，避免覆盖系统包。
sys.path.append(str(ROOT / ".devdeps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}

#: 失败分类（与 4.3 的汇总口径一致）
FAIL_LOCATE = "定位"
FAIL_PAGE = "页标"
FAIL_STUDENT = "学号"
FAIL_BUBBLE = "涂点"
FAIL_RESOLUTION = "分辨率"
FAIL_OTHER = "其他"


@dataclass
class Result:
    name: str
    ok: bool
    status: str = ""
    page_no: int | None = None
    student_no: str = ""
    answers_ok: int = 0
    answers_total: int = 0
    wrong: list[str] = field(default_factory=list)
    confidence: float = 0.0
    fail_reason: str = ""
    fail_class: str = ""
    error: str = ""


def load_image(path: Path):
    """读入一张图为 BGR ndarray；PDF 取第一页（300dpi）。"""
    import cv2
    import numpy as np

    if path.suffix.lower() == ".pdf":
        import fitz

        doc = fitz.open(str(path))
        page = doc[0]
        zoom = 300 / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        return cv2.cvtColor(
            arr.reshape(pix.height, pix.width, pix.n)[:, :, :3],
            cv2.COLOR_RGB2BGR)
    raw = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"图像解码失败：{path.name}")
    return img


def _layout_for(expect: dict[str, Any]) -> dict[str, Any]:
    """取样本的模板布局：内联优先，其次按 template_id 查库。"""
    layout = expect.get("layout_json")
    if layout:
        return layout
    tid = expect.get("template_id")
    if not tid:
        raise ValueError("expect.json 缺 layout_json / template_id")
    from apps.sheet.models import SheetTemplate
    from apps.org.models import Org

    org_id = int(expect.get("org_id") or 1)
    tpl = SheetTemplate.objects.for_org(org_id).filter(pk=int(tid)).first()
    if tpl is None:
        # 兜底：模板可能不属于该 org（样本库常见）
        tpl = SheetTemplate.objects.filter(pk=int(tid)).first()
    if tpl is None:
        raise ValueError(f"template_id={tid} 不存在")
    return tpl.layout_json or {}


def classify_failure(fail_reason: str) -> str:
    """把 `fail_reason` 归到五类之一（汇总要用）。"""
    msg = fail_reason or ""
    if "分辨率" in msg or "长边" in msg:
        return FAIL_RESOLUTION
    if "定位" in msg or "四角" in msg or "锚点" in msg:
        return FAIL_LOCATE
    if "页" in msg and ("号" in msg or "标" in msg):
        return FAIL_PAGE
    if "学号" in msg:
        return FAIL_STUDENT
    if "涂" in msg or "多项" in msg or "填充" in msg:
        return FAIL_BUBBLE
    return FAIL_OTHER


def run_one(name: str, image_path: Path, expect: dict[str, Any],
            cfg, *, debug_dir: Path | None) -> Result:
    """识别一张样本并与期望比对。"""
    from apps.omr.pipeline import recognize_page

    layout = _layout_for(expect)
    img = load_image(image_path)
    res = recognize_page(img, layout, cfg)

    want_status = str(expect.get("status") or "AUTO_OK")
    want_no = str(expect.get("student_no") or "")
    want_answers = {str(k): str(v).upper()
                    for k, v in (expect.get("answers") or {}).items()}
    got_answers = {
        str(a.get("seq")): "".join(str(x).upper() for x in (a.get("labels") or []))
        for a in (res.get("answers") or [])
    }

    wrong = []
    for seq, want in want_answers.items():
        got = got_answers.get(seq, "")
        if got != want:
            wrong.append(f"第{seq}题 期望{want or '空'} 实得{got or '空'}")

    # ★ 判定口径：状态必须一致；学号（非空时）必须一致；
    #   逐题答案必须全部一致。三者任一不符即失败——
    #   「答案对了但状态是 NEEDS_REVIEW」不算通过：它会被推进人工队列，
    #   而样本库的意义正是守住「这张卡本来应该静默通过」。
    status_ok = str(res.get("status")) == want_status
    no_ok = (not want_no) or (str(res.get("recognized_student_no") or "") == want_no)
    ok = status_ok and no_ok and not wrong

    r = Result(
        name=name, ok=ok, status=str(res.get("status") or ""),
        page_no=res.get("page_no"), student_no=str(
            res.get("recognized_student_no") or ""),
        answers_ok=len(want_answers) - len(wrong),
        answers_total=len(want_answers), wrong=wrong,
        confidence=float(res.get("overall_confidence") or 0),
        fail_reason=str(res.get("fail_reason") or ""),
        fail_class=classify_failure(str(res.get("fail_reason") or "")),
    )
    if debug_dir is not None:
        save_debug(debug_dir, name, img, res, layout)
    return r


def save_debug(out_dir: Path, name: str, img, res: dict[str, Any],
               layout: dict[str, Any]) -> None:
    """保存三张调试图（21 文档 4.4）。

    ★ 存到 `artifacts/omr-debug/`，**不进 media**：
      这些图是排障产物，不是业务资产，混进 media 会被引用计数和
      清理任务当成「孤儿」反复处理。
    """
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)
    rect = res.get("rectify")
    try:
        # 1) 原图 + 检测到的四角
        canvas = img.copy()
        corners = getattr(rect, "corners", None) if rect is not None else None
        if corners is not None:
            pts = corners.astype("int32").reshape(-1, 1, 2)
            cv2.polylines(canvas, [pts], True, (0, 0, 255), 4)
        cv2.imwrite(str(out_dir / f"{name}-anchors.png"), canvas)

        # 2) 校正后整页 + 采样框（这里只画矩形，不重算采样点）
        if rect is not None:
            warped = rect.warped
            if len(warped.shape) == 2:
                warped = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
            scale = float(getattr(rect, "scale", 0) or 0)
            page_w = float(layout.get("page_width_mm") or 210.0)
            if scale <= 0:
                scale = warped.shape[1] / page_w
            page = (layout.get("pages") or [{}])[0]
            for obj in page.get("objectives") or []:
                for b in obj.get("bubbles") or []:
                    x1 = int(float(b["x_mm"]) * scale)
                    y1 = int(float(b["y_mm"]) * scale)
                    x2 = int((float(b["x_mm"]) + float(b.get("w_mm") or 0)) * scale)
                    y2 = int((float(b["y_mm"]) + float(b.get("h_mm") or 0)) * scale)
                    cv2.rectangle(warped, (x1, y1), (x2, y2), (0, 200, 0), 1)
            cv2.imwrite(str(out_dir / f"{name}-warped.png"), warped)

        # 3) 每题每个选项的填充分数（文本报告；画条形图收益不大且易失真）
        lines = []
        for a in res.get("answers") or []:
            ratios = a.get("fill_ratios")
            lines.append(
                f"q{a.get('seq')}: labels={a.get('labels')} "
                f"conf={a.get('confidence')} ratios={ratios}")
        (out_dir / f"{name}-samples.txt").write_text(
            "\n".join(lines) or "（无采样数据）", encoding="utf-8")
    except Exception as exc:      # noqa: BLE001 —— 调试图失败不该让自检崩
        (out_dir / f"{name}-debug-error.txt").write_text(
            f"{exc}\n{traceback.format_exc()}", encoding="utf-8")


def collect(fixtures: Path) -> list[tuple[str, Path, dict]]:
    """扫描样本目录，返回 `[(name, image_path, expect), ...]`。"""
    out = []
    for p in sorted(fixtures.iterdir()):
        if p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        exp = p.with_suffix(".expect.json")
        if not exp.exists():
            # 允许 `xxx.png` 对应 `xxx.expect.json`（同名不同后缀）
            exp = fixtures / f"{p.stem}.expect.json"
        if not exp.exists():
            print(f"[skip] {p.name}：缺少 {exp.name}")
            continue
        try:
            data = json.loads(exp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[skip] {p.name}：expect.json 不是合法 JSON（{exc}）")
            continue
        out.append((p.stem, p, data))
    return out


def write_report(path: Path, rows: list[Result]) -> None:
    """生成 HTML 报告。"""
    total = len(rows)
    passed = sum(1 for r in rows if r.ok)
    rate = (passed / total) if total else 0.0
    cats = Counter(r.fail_class for r in rows if not r.ok and r.fail_class)

    def esc(s: Any) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    tr = []
    for r in rows:
        color = "#e8f7ee" if r.ok else "#fdecea"
        tr.append(
            f'<tr style="background:{color}">'
            f"<td>{esc(r.name)}</td>"
            f'<td>{"通过" if r.ok else "失败"}</td>'
            f"<td>{esc(r.status)}</td><td>{esc(r.page_no or '')}</td>"
            f"<td>{esc(r.student_no)}</td>"
            f"<td>{r.answers_ok}/{r.answers_total}</td>"
            f"<td>{r.confidence:.3f}</td>"
            f"<td>{esc(r.fail_reason or r.error)}</td>"
            f"<td>{esc('；'.join(r.wrong[:6]))}</td></tr>")

    cat_rows = "".join(
        f"<li>{esc(k)}：{v} 张</li>" for k, v in cats.most_common()) or "<li>无</li>"
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>OMR 自检报告</title>
<style>
body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}
th{{background:#f5f5f5}}
.big{{font-size:22px;font-weight:600}}
</style></head><body>
<h1>OMR 自检报告</h1>
<p class="big">通过 {passed}/{total}（{rate:.1%}）</p>
<p>样本目录：{esc(path.parent)}　生成时间：{__import__("datetime").datetime.now():%Y-%m-%d %H:%M}</p>
<h2>失败分类</h2><ul>{cat_rows}</ul>
<h2>逐张结果</h2>
<table><thead><tr>
<th>样本</th><th>结果</th><th>状态</th><th>页码</th><th>学号</th>
<th>答案</th><th>置信度</th><th>失败原因</th><th>差异</th>
</tr></thead><tbody>{''.join(tr)}</tbody></table>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="OMR 样本自检与回归")
    ap.add_argument(
        "--fixtures", default="server/apps/omr/tests/fixtures",
        help="样本目录（默认用仓库里已有的真实/合成样本）",
    )
    ap.add_argument("--report", default="reports/omr.html",
                    help="HTML 报告输出路径")
    ap.add_argument("--debug-card", default="",
                    help="只调试某一张样本（名字），输出三张调试图")
    ap.add_argument("--channel", default="UPLOAD",
                    choices=["UPLOAD", "PHONE"], help="阈值通道")
    ap.add_argument("--min-pass-rate", type=float, default=0.0,
                    help="通过率低于该值时返回非 0（CI 用，默认只要有失败就非 0）")
    args = ap.parse_args()

    # ★★ 先验依赖，再谈跑样本。
    #
    #   识别链路硬依赖 cv2 / numpy / PyMuPDF。宿主环境缺包时，
    #   如果等到 `load_image()` 才炸，看到的是一屏 numpy 二进制
    #   不匹配的堆栈 —— 看起来像「我的样本有问题」，其实是环境问题。
    #   这里提前说清楚缺什么、装什么。
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import fitz  # noqa: F401
    except Exception as exc:      # noqa: BLE001 —— 任何导入失败都是环境问题
        print("✗ 识别依赖不可用，自检跑不起来：")
        print(f"  {type(exc).__name__}: {exc}")
        print("  需要 opencv-python / numpy / PyMuPDF。")
        print("  提示：numpy 二进制不匹配（No module named "
              "'numpy._core._multiarray_umath'）说明装的是"
              "和解释器不匹配的轮子，重装这两个包即可。")
        return 3

    fixtures = Path(args.fixtures)
    if not fixtures.exists():
        print(f"样本目录不存在：{fixtures}")
        print("仓库自带样本在 server/apps/omr/tests/fixtures；"
              "要新造样本用 scripts/gen_omr_fixtures.py"
              "（注意它是**重新生成**那批样本，会覆盖同名 png）")
        return 2

    import django

    django.setup()

    from apps.omr.pipeline import threshold_for

    cfg = threshold_for(None, org_id=None, channel=args.channel)
    samples = collect(fixtures)
    if not samples:
        print(f"{fixtures} 下没有可用样本（需要 <name>.png + <name>.expect.json）")
        return 2

    debug_dir = None
    if args.debug_card:
        debug_dir = ROOT / "artifacts" / "omr-debug" / args.debug_card
        samples = [s for s in samples if s[0] == args.debug_card] or samples[:1]

    rows: list[Result] = []
    for name, path, expect in samples:
        try:
            r = run_one(name, path, expect, cfg, debug_dir=debug_dir)
        except Exception as exc:      # noqa: BLE001 —— 单样本异常不中断整批
            r = Result(name=name, ok=False, error=str(exc)[:300],
                       fail_class=classify_failure(str(exc)))
        rows.append(r)
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {name} status={r.status} "
              f"no={r.student_no} answers={r.answers_ok}/{r.answers_total} "
              f"{('· ' + r.fail_reason) if not r.ok else ''}")

    write_report(Path(args.report), rows)
    passed = sum(1 for r in rows if r.ok)
    print(f"\n通过 {passed}/{len(rows)}（{passed / len(rows):.1%}）"
          f"  报告：{args.report}")

    if debug_dir is not None:
        print(f"调试图：{debug_dir}")

    if args.min_pass_rate and passed / len(rows) < args.min_pass_rate:
        return 1
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
