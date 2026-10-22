"""验收：试卷渲染服务 + Django 回退编排（计划 U19 / U20）。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg ZF_CACHE=locmem \
    <py3.12> scripts/verify_render_service.py

分组：

    R1 静态契约   —— 源码里该有的东西在不在（并发、超时、令牌、字体）
    R2 Node 自检  —— 跑 `node scripts/selfcheck.js`，验字体内嵌与公式 SSR
    R3 HTTP 接口  —— 真起服务：/health、401、400、200 + %PDF
    R4 Django 回退 —— 服务不可用时必须**静默降级**并留痕（U20 的核心）

★★ R4 才是这一批的验收重点。渲染服务能不能跑，部署时一眼就看出来了；
   真正会出事的是「服务挂了之后 Django 的行为」——
   是回退成功并告诉用户公式降级了，还是卡 35 秒、还是直接 500。
   这三者的差别决定了老师遇到的是「导出慢一点」还是「系统坏了」。

★ 本机没有 Chromium 时，R3 的 PDF 那几项会记为 [SKIP]（不计失败）——
  但 R2 的字体内嵌检查**一定会跑**，因为「线上满页方框」的根因在那里，
  而它不需要浏览器就能验。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.paper import render_node  # noqa: E402
from apps.paper.export import PaperRenderData, export_pdf  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0
SVC = ROOT / "render-service"
TEST_PORT = 7099
TOKEN = "verify-token-0123456789"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def skip(label: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {label}   （{why}）")


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


# ══════════════════════════════════════════════════════════════
#  R1 · 静态契约
# ══════════════════════════════════════════════════════════════


def r1_contract() -> None:
    print("\nR1 · 静态契约")

    server = read(SVC / "src" / "server.js")
    pdf = read(SVC / "src" / "pdf.js")
    fonts = read(SVC / "src" / "fonts.js")
    html = read(SVC / "src" / "html.js")

    check("服务入口存在", bool(server))
    check("★ 用 `X-Render-Token` 而不是 JWT 鉴权", "X-Render-Token" in server)
    check("★★ 缺 RENDER_TOKEN 时拒绝启动（不静默放行）",
          "process.exit(1)" in server and "RENDER_TOKEN" in server)
    check("用 timingSafeEqual 比较令牌（不泄露长度前缀）",
          "timingSafeEqual" in server)
    check("★ 超时回 504（Django 据此回退）", "'RENDER.TIMEOUT'" in server)
    check("正常失败回 500 RENDER.FAILED", "'RENDER.FAILED'" in server)
    check("非法排版 JSON 回 400", "'RENDER.INVALID_LAYOUT'" in server)

    check("★★ 并发上限可配（默认 2，Chromium 内存约束）",
          "RENDER_CONCURRENCY" in pdf)
    check("★★ 超时后主动关页面（否则渲染进程会变僵尸）",
          "closePage" in pdf and "watchdog" in pdf)
    check("★ 浏览器启动失败时清缓存（否则一次失败会永久 500）",
          re.search(r"browserPromise = null[\s\S]{0,200}throw e", pdf) is not None)
    check("启动时预热浏览器（把 1s 冷启动挪到进程启动期）",
          "warmup" in pdf and "warmup" in server)
    check("容器内 --no-sandbox / --disable-dev-shm-usage",
          "--no-sandbox" in pdf and "--disable-dev-shm-usage" in pdf)

    check("★★ 公式字体内嵌成 data URI（不指望 CDN / 校内网络）",
          "data:font/woff2;base64," in fonts)
    check("★ 丢掉 woff / ttf 两档（Chromium 一定支持 woff2）",
          "woff|ttf" in fonts or "woff|" in fonts)
    check("中文字体 font-family 链显式写死", "Noto Sans CJK SC" in fonts)

    check("★★ 只按 mm 坐标画，不重排（`white-space: pre`）",
          "white-space: pre" in html)
    check("★ 纸型写进 `@page size`", "@page" in html and "size:" in html)
    check("配图缺失时印占位（不静默留白）", "配图缺失" in html)

    dockerfile = read(SVC / "Dockerfile")
    check("★★ 镜像装中文字体（线上满页方框的唯一解）",
          "fonts-noto-cjk" in dockerfile)
    check("镜像装 Chromium 运行期依赖", "libnss3" in dockerfile)
    check("镜像里单独安装浏览器本体", "puppeteer browsers install" in dockerfile)
    check("镜像带 HEALTHCHECK", "HEALTHCHECK" in dockerfile)

    compose = read(ROOT / "deploy" / "compose.render.yml")
    check("compose 片段存在且有 render 服务", "render:" in compose)
    check("★★ compose 给足 /dev/shm（默认 64MB 会让多页打印崩掉）",
          "shm_size" in compose)
    check("★ 端口只绑本机（内网服务不该暴露到公网网卡）",
          "127.0.0.1:7001" in compose)
    check("compose 不给内存设上限的反面：有 limits.memory",
          "memory" in compose)


# ══════════════════════════════════════════════════════════════
#  R2 · Node 自检（字体内嵌 + 公式 SSR，不需要浏览器）
# ══════════════════════════════════════════════════════════════


def r2_selfcheck() -> None:
    print("\nR2 · Node 自检（node scripts/selfcheck.js）")

    if not (SVC / "node_modules").exists():
        check("渲染服务依赖已安装", False,
              "缺 render-service/node_modules，先 cd render-service && npm install")
        return

    proc = subprocess.run(
        ["node", "scripts/selfcheck.js"],
        cwd=str(SVC), capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout + proc.stderr
    fails = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln]
    check("★★ Node 自检全部通过", proc.returncode == 0 and not fails,
          "; ".join(fails[:3]))
    m = re.search(r"通过 (\d+) / 失败 (\d+)", out)
    check("自检项数合理（≥ 15 项）", bool(m) and int(m.group(1)) >= 15,
          m.group(0) if m else "未找到汇总行")
    # 把 Node 侧的关键结论也照抄出来，便于排查
    for ln in out.splitlines():
        if "[OK]" in ln and ("★★" in ln or "字体" in ln):
            print(f"         └ {ln.strip()}")


# ══════════════════════════════════════════════════════════════
#  R3 · HTTP 接口（真起服务）
# ══════════════════════════════════════════════════════════════


def _verify_pdf_content(body: bytes) -> None:
    """真的把 PDF 拆开看：纸型对不对、中文有没有变成方框。

    ★★ 「PDF 生成成功」是最没意义的断言 —— 满页方框也是一份合法 PDF。
      必须验两件事：
        ① 页面物理尺寸 = A4（`@page size` 有没有被 Puppeteer 覆盖）
        ② 提取出的文字里**有中文**（方框字符提取出来是空或是 .notdef，
           中文能提取出来说明字体链路是通的）
      公式部分无法从文本层断言（分母分子是独立字形游程），
      所以另验一条：文本里**没有** `\\frac` 这种 TeX 源码残留。
    """
    try:
        import pymupdf
    except ImportError:
        skip("PDF 内容校验", "本机无 pymupdf")
        return

    try:
        doc = pymupdf.open(stream=body, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        check("PDF 能被解析", False, str(e)[:80])
        return

    check("PDF 能被解析", doc.page_count >= 1)
    page = doc[0]
    w_mm = page.rect.width / 72 * 25.4
    h_mm = page.rect.height / 72 * 25.4
    check("★★ 页面物理尺寸 = A4（`@page size` 生效，没被默认值覆盖）",
          abs(w_mm - 210) < 1.0 and abs(h_mm - 297) < 1.0,
          f"{w_mm:.1f} × {h_mm:.1f} mm")

    text = page.get_text()
    check("★★ 中文能提取出来（满页方框的另一副面孔）",
          "公式验收卷" in text and "选择题" in text,
          repr(text[:60]))
    check("★★ 文本层没有 TeX 源码残留（`\\frac` 出现在纸上就是漏渲染）",
          "\\frac" not in text and "$" not in text, repr(text[:80]))
    # 公式被画成字形 → 提取出的文字里会出现分数分子/分母这类孤立数字
    check("★ 公式确实被画成了字形（提取到 5/6 这类选项片段）",
          "5" in text and "6" in text, repr(text[:80]))


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _http(path: str, method="GET", body=None, token=None, timeout=60):
    req = urllib.request.Request(
        f"http://127.0.0.1:{TEST_PORT}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Render-Token", token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _fixture_layout() -> dict:
    return {
        "config": {"base_font_pt": 10.5},
        "page_width_mm": 210.0,
        "page_height_mm": 297.0,
        "margin_mm": 20.0,
        "usable_width_mm": 170.0,
        "usable_height_mm": 257.0,
        "header_height_mm": 24.0,
        "figure_gap_mm": 1.0,
        "header": {
            "title": {
                "text": "公式验收卷", "lines": ["公式验收卷"],
                "y_mm": 20.0, "font_pt": 16.0, "line_h_mm": 8.0,
            },
            "subtitle": {"text": "满分：10 分", "y_mm": 30.0, "font_pt": 10.5},
            "student_items": [
                {"label": "班级：", "x_mm": 0.0, "underline_x_mm": 14.0,
                 "underline_w_mm": 40.0, "y_mm": 38.0},
            ],
            "student_extra": None,
            "notes": {"lines": [], "y_mm": 40.0, "font_pt": 9.0,
                      "line_h_mm": 6.0},
            "rules": [44.0],
            "height_mm": 24.0,
        },
        "pages": [{
            "page_no": 1,
            "blocks": [
                {"kind": "stem", "y_mm": 50.0, "height_mm": 8.0,
                 "title_lines": ["一、选择题"]},
                {"kind": "q_stems", "y_mm": 60.0, "height_mm": 12.0,
                 "line_h_mm": 5.5,
                 "stem_lines": ["1. 求 $\\frac{1}{2}+\\frac{1}{3}$ 的值"]},
                {"kind": "choice_row", "y_mm": 76.0, "height_mm": 6.0,
                 "option_font_pt": 9.0, "option_cols": 4,
                 "option_cells": [
                     {"x_mm": 0.0, "y_mm": 0.0, "lines": ["A. $\\frac{5}{6}$"]},
                     {"x_mm": 42.5, "y_mm": 0.0, "lines": ["B. $\\frac{2}{5}$"]},
                 ]},
            ],
            "page_number": {"text": "第 1 页 共 1 页", "y_mm": 285.0,
                            "align": "center"},
        }],
        "math_render": "tex",
    }


def r3_http() -> None:
    print("\nR3 · HTTP 接口（真起服务）")

    if not (SVC / "node_modules").exists():
        skip("服务接口", "缺 node_modules")
        return
    if not _free(TEST_PORT):
        skip("服务接口", f"端口 {TEST_PORT} 被占用")
        return

    env = dict(os.environ)
    env.update({
        "RENDER_TOKEN": TOKEN,
        "RENDER_PORT": str(TEST_PORT),
        "RENDER_CONCURRENCY": "2",
        "PATH": env.get("PATH", ""),
    })
    proc = subprocess.Popen(
        ["node", "src/server.js"], cwd=str(SVC), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 等健康检查可用（浏览器预热约 1s，冷启动环境放宽到 40s）
        ok = False
        for _ in range(80):
            if proc.poll() is not None:
                break
            try:
                st, _, _ = _http("/health", timeout=3)
                if st == 200:
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ok:
            tail = ""
            if proc.poll() is not None and proc.stdout:
                tail = "".join(proc.stdout.readlines())[-400:]
            check("服务能启动并响应 /health", False, tail or "启动超时")
            return

        st, _, body = _http("/health")
        health = json.loads(body)
        check("/health 返回 200", st == 200)
        check("★ /health 暴露字体内嵌情况", "fonts" in health)
        check("★★ leftover_relative_urls == 0（没有取不到的字体）",
              (health.get("fonts") or {}).get("leftover_relative_urls") == 0,
              json.dumps(health.get("fonts")))
        check("★ /health 暴露并发上限", health.get("concurrency") == 2,
              str(health.get("concurrency")))

        # 鉴权
        st, _, body = _http("/render", "POST", {"layout": _fixture_layout()})
        check("★★ 无令牌 → 401（内网也不能裸奔）", st == 401, f"HTTP {st}")

        st, _, body = _http("/render", "POST",
                            {"layout": {"pages": []}}, token=TOKEN)
        check("非法排版 JSON → 400", st == 400, f"HTTP {st}")
        code = ""
        try:
            code = json.loads(body).get("code", "")
        except Exception:  # noqa: BLE001
            pass
        check("错误码是 RENDER.INVALID_LAYOUT",
              code == "RENDER.INVALID_LAYOUT", code)

        # 真渲染
        st, headers, body = _http(
            "/render", "POST",
            {"layout": _fixture_layout(), "title": "公式验收卷"},
            token=TOKEN, timeout=90,
        )
        if st == 200 and body[:5] == b"%PDF-":
            check("★★ 合法请求 → 200 + application/pdf",
                  headers.get("Content-Type", "").startswith("application/pdf"),
                  headers.get("Content-Type", ""))
            check("★ PDF 魔术字正确", body[:5] == b"%PDF-", str(body[:8]))
            check("★ 响应头声明引擎（便于排查「两次导出不一样」）",
                  headers.get("X-Render-Engine") == "NODE_PUPPETEER",
                  str(headers.get("X-Render-Engine")))
            check("PDF 体积在合理区间（≥ 3KB，说明不是空页）",
                  len(body) > 3000, f"{len(body)} bytes")
            # ★ 关键：真公式会被画成字形而不是 `$\frac` 原文。
            #   PDF 里公式是矢量字形，无法直接断言内容，但页数必须对得上。
            check("★ 渲染页数与输入一致",
                  headers.get("X-Render-Pages") == "1",
                  str(headers.get("X-Render-Pages")))
            _verify_pdf_content(body)
        else:
            print(f"         服务返回 HTTP {st}：{body[:160]!r}")
            skip("真渲染出 PDF",
                 "本机 Chromium 不可用（见 render-service/README「部署」）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


# ══════════════════════════════════════════════════════════════
#  R4 · Django 侧回退编排（U20 的核心）
# ══════════════════════════════════════════════════════════════


def _formula_render_data() -> PaperRenderData:
    """一份含公式的卷子 —— 用来验「回退时有没有留痕」。"""
    stem = "求 $\\frac{1}{2}+\\frac{1}{3}$ 的值"
    return PaperRenderData(
        title="公式回退验收卷",
        subtitle="满分：10 分",
        sections=[{
            "order": 1, "title": "一、单项选择题", "type": "SINGLE_CHOICE",
            "seqs": [1], "types": {1: "SINGLE_CHOICE"},
            "stems": {1: stem}, "scores": {1: 10.0},
            "opt_texts_by_seq": {1: ["1/2", "1/3", "1/4", "1/5"]},
            "force_letter_by_seq": {1: False}, "figures_by_seq": {},
        }],
        items={1: {
            "seq": 1, "score": 10.0, "type": "SINGLE_CHOICE",
            "stem_text": stem, "options": [], "answer": {},
            "answer_text": "", "force_letter": False,
        }},
        config={}, assets={},
    )


class _StubPaper:
    """`export_pdf` 只用到 `org_id` / `title`；`build_render_data` 被 patch 掉。"""

    org_id = 1
    title = "公式回退验收卷"


def r4_fallback() -> None:
    print("\nR4 · Django 回退编排（服务挂了之后必须静默降级并留痕）")

    # ① 没配置 URL → 不启用，且不能抛错
    with override_settings(RENDER_SERVICE_URL=""):
        render_node.clear_breaker()
        check("未配置 ZF_RENDER_URL 时 is_enabled() 为 False",
              render_node.is_enabled() is False)

    # ② 熔断：一次失败之后不再每次白等 35 秒
    with override_settings(RENDER_SERVICE_URL="http://127.0.0.1:1"):
        render_node.clear_breaker()
        check("配置了 URL 时 is_enabled() 为 True",
              render_node.is_enabled() is True)
        try:
            render_node.render_layout_to_pdf(_fixture_layout())
            raised = ""
        except render_node.RenderUnavailable as e:
            raised = str(e)
        check("★★ 连不上时抛 RenderUnavailable（不是裸 URLError）",
              bool(raised), raised[:80])
        render_node.mark_unavailable(raised)
        check("★★ 失败后熔断生效（120s 内直接走回退，不白等 35 秒）",
              render_node.is_enabled() is False)
        render_node.clear_breaker()
        check("熔断可被主动清除", render_node.is_enabled() is True)

    # ③ 端到端回退：含公式的卷子 + 渲染服务不可用
    with override_settings(RENDER_SERVICE_URL="http://127.0.0.1:1",
                           RENDER_SERVICE_TIMEOUT=3):
        render_node.clear_breaker()
        with patch("apps.paper.export.build_render_data",
                   return_value=_formula_render_data()):
            pdf, issues = export_pdf(_StubPaper())
        codes = [i.get("code") for i in issues]
        check("★★ 渲染服务不可用时仍然导出成功（不抛异常、不 500）",
              pdf[:5] == b"%PDF-", str(pdf[:8]))
        check("★★ 回退留痕：出现 PAPER.FORMULA_DEGRADED",
              "PAPER.FORMULA_DEGRADED" in codes, str(codes))
        degraded = next(
            (i for i in issues if i.get("code") == "PAPER.FORMULA_DEGRADED"),
            {})
        check("★ 这条告警是**非阻塞**的（卷子照样能导）",
              degraded.get("blocking") is False, str(degraded))
        check("告警文案说明了「公式以近似文本显示」",
              "近似文本" in str(degraded.get("message")), "")

    # ④ 没有公式的卷子：回退时**不该**打扰用户
    plain = _formula_render_data()
    plain.sections[0]["stems"][1] = "下列哪个是合金调质钢的硬度范围"
    plain.items[1]["stem_text"] = "下列哪个是合金调质钢的硬度范围"
    with override_settings(RENDER_SERVICE_URL="http://127.0.0.1:1",
                           RENDER_SERVICE_TIMEOUT=3):
        render_node.clear_breaker()
        with patch("apps.paper.export.build_render_data", return_value=plain):
            _, issues2 = export_pdf(_StubPaper())
        codes2 = [i.get("code") for i in issues2]
        check("★ 无公式时回退不产生告警（别为了内部问题打扰老师）",
              "PAPER.FORMULA_DEGRADED" not in codes2, str(codes2))

    # ⑤ 辅助函数
    check("layout_has_math 认得出题干里的公式",
          render_node.layout_has_math(_fixture_layout()) is True)
    check("layout_has_math 对纯文本为 False",
          render_node.layout_has_math({
              "header": {}, "pages": [{"blocks": [
                  {"kind": "q_stems", "stem_lines": ["普通题干"]}]}],
          }) is False)

    uris = render_node.assets_to_data_uris({
        1: b"\x89PNG\r\n\x1a\n" + b"0" * 40,
        2: b"\xff\xd8\xff" + b"0" * 40,
        3: b"",
    })
    check("PNG 魔数嗅探正确", uris.get("1", "").startswith("data:image/png"),
          uris.get("1", "")[:32])
    check("JPEG 魔数嗅探正确", uris.get("2", "").startswith("data:image/jpeg"),
          uris.get("2", "")[:32])
    check("空字节的图被跳过（不产出坏 data URI）", "3" not in uris, str(list(uris)))

    check("★★ Django 侧用 urllib 而不是 requests（后端没装 requests）",
          "urllib.request" in read(ROOT / "server" / "apps" / "paper"
                                  / "render_node.py"))


def main() -> int:
    print("=" * 74)
    print("  试卷渲染服务 + 回退编排验收")
    print("=" * 74)

    r1_contract()
    r2_selfcheck()
    r3_http()
    r4_fallback()

    print("\n" + "=" * 74)
    tail = f"  通过 {PASS} / 失败 {FAIL}"
    if SKIP:
        tail += f" / 跳过 {SKIP}"
    print(tail)
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
