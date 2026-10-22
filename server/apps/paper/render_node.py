"""Node 渲染服务客户端 + reportlab 回退编排（计划 U20 / 决策 2）。

## 这份模块负责什么

    排版 JSON（tex 档）  ──POST──▶  渲染服务  ──▶  PDF 字节
            │
            └─ 不可用 / 超时 / 报错 ──▶ reportlab（plain 档）──▶ PDF 字节 + 告警

也就是说：**两个引擎都从同一份 `paper/layout.py` 出发**，只是
`math_render` 档位不同（tex / plain），几何因此各自自洽。
详见 `layout.plan_layout` 的 `math_render` 说明。

## 三条纪律

1. **渲染服务不可用时绝不报错给用户**。老师点「导出 PDF」是为了拿到卷子，
   不是为了知道有个 Node 服务挂了。所以一律回退到 reportlab，
   只把降级写进导出结果的 issue 里。

2. **回退必须留痕**。同一个卷子两次导出长得不一样、还查不出原因，
   是最伤信任的一类 bug。所以：
   - 返回结果带 `render_engine`（`NODE_PUPPETEER` / `LEGACY`）
   - 有公式却用了 LEGACY 时，issue 里明确写「公式以近似文本显示」

3. **令牌不走用户 JWT**。渲染服务只回答「请求是不是 Django 发来的」，
   不回答「这是谁」。越权判断留在 Django（那里才有 `for_org()`）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings
from django.core.cache import cache

from apps.common.mathtext import has_math

#: 渲染引擎标识（会写进导出结果，便于排查「两次导出不一样」）
ENGINE_NODE = "NODE_PUPPETEER"
ENGINE_LEGACY = "LEGACY"

#: 失败熔断：连续失败后 N 秒内直接走 LEGACY，不再每次等 35s 超时
_FAIL_KEY = "zf:render:fail_until"
_FAIL_TTL = 120

#: 图片魔数 → MIME（`Asset.mime` 在 `load_figure_assets` 里没带出来，
#:  这里按字节头判断，免得为了一个 data URI 再查一次库）
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def is_enabled() -> bool:
    """渲染服务是否已配置并处于可用状态（含熔断判断）。"""
    if not getattr(settings, "RENDER_SERVICE_URL", ""):
        return False
    if cache.get(_FAIL_KEY):
        return False
    return True


def mark_unavailable(reason: str) -> None:
    """记一次失败并把服务熔断一段时间。

    ★ 没有熔断的话，服务挂掉之后**每一次**导出都要白等 35 秒 ——
      用户会以为整个系统卡死，而不是「导出慢一点」。
    """
    cache.set(_FAIL_KEY, reason, _FAIL_TTL)


def clear_breaker() -> None:
    cache.delete(_FAIL_KEY)


def _sniff_mime(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def assets_to_data_uris(assets: dict[int, bytes]) -> dict[str, str]:
    """`{asset_id: bytes}` → `{asset_id: data URI}`（渲染服务直接塞进 `<img>`）。"""
    import base64

    out: dict[str, str] = {}
    for aid, raw in (assets or {}).items():
        if not raw:
            continue
        mime = _sniff_mime(raw)
        out[str(aid)] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return out


class RenderUnavailable(RuntimeError):
    """渲染服务不可用（含超时、鉴权失败、返回非 200）。"""


def render_layout_to_pdf(
    layout_json: dict[str, Any],
    *,
    assets: dict[int, bytes] | None = None,
    title: str = "",
) -> bytes:
    """调用渲染服务把排版 JSON 变成 PDF。失败抛 `RenderUnavailable`。

    ★ 用 `urllib` 而不是 `requests`：后端依赖里没有 requests，
      为一个 POST 引一个包不划算。
    """
    url = getattr(settings, "RENDER_SERVICE_URL", "")
    if not url:
        raise RenderUnavailable("未配置 ZF_RENDER_URL")

    payload = json.dumps({
        "layout": layout_json,
        "assets": assets_to_data_uris(assets or {}),
        "title": title,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/render",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Render-Token": getattr(settings, "RENDER_SERVICE_TOKEN", ""),
        },
    )

    timeout = float(getattr(settings, "RENDER_SERVICE_TIMEOUT", 35))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RenderUnavailable(f"渲染服务返回 HTTP {resp.status}")
            data = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise RenderUnavailable(f"渲染服务 HTTP {e.code}：{body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RenderUnavailable(f"连不上渲染服务：{e}") from e

    if not data.startswith(b"%PDF-"):
        raise RenderUnavailable("渲染服务返回的不是 PDF")
    return data


# ══════════════════════════════════════════════════════════════
#  公式检测（决定「回退时要不要告警」）
# ══════════════════════════════════════════════════════════════

_MATH_HINT = re.compile(r"\$|\\\(|\\\[")


def layout_has_math(layout_json: dict[str, Any]) -> bool:
    """这份排版 JSON 里有没有公式。

    ★ 只看「会印在纸上」的行：题干、选项、大题标题、卷头标题、图注。
      答案与解析不在 PDF 主卷上（答案 PDF 另有一份），不必计入。
    """
    def _scan_text(v: Any) -> bool:
        return isinstance(v, str) and bool(_MATH_HINT.search(v)) and has_math(v)

    header = layout_json.get("header") or {}
    title = header.get("title") or {}
    for line in title.get("lines") or []:
        if _scan_text(line):
            return True
    notes = header.get("notes") or {}
    for line in notes.get("lines") or []:
        if _scan_text(line):
            return True
    if _scan_text((header.get("subtitle") or {}).get("text")):
        return True

    for page in layout_json.get("pages") or []:
        for block in page.get("blocks") or []:
            for key in ("title_lines", "stem_lines", "caption_lines"):
                for line in block.get(key) or []:
                    if _scan_text(line):
                        return True
            for cell in block.get("option_cells") or []:
                for line in cell.get("lines") or []:
                    if _scan_text(line):
                        return True
    return False


#: 公式被降级时的告警（非阻塞：卷子照样导出，只是告诉他公式是近似文本）
FORMULA_DEGRADED_ISSUE: dict[str, Any] = {
    "code": "PAPER.FORMULA_DEGRADED",
    "message": (
        "这份试卷含公式，但公式渲染服务当前不可用，PDF 里的公式"
        "以近似文本显示（如 1/2、x²）。需要印刷级公式请稍后重试，"
        "或联系管理员检查渲染服务。"
    ),
    "blocking": False,
}
