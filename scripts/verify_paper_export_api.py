"""试卷导出端点 · HTTP 端到端（契约 v3）。

★ 覆盖：
  · GET / POST 排版 JSON、PDF、Word
  · POST /export/answer/ 独立答案 PDF（与试卷 PDF 分开）
  · 契约 v3 的 `option_cols_by_seq` / 考生字段 / 汇总开关
  · 非法枚举 400、PATCH 保存回显、无题阻断
  · 配图字节端点

运行（真 PG，服务在 127.0.0.1:8001）：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_paper_export_api.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import unquote

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

B = "http://127.0.0.1:8001/api/v1"
OUT = r"D:\tiku\.zf-storage\paper-export-v3"

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


def call(url, *, method="GET", body=None, token=None, raw=False):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "*/*")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data, timeout=120) as r:
            payload = r.read()
            if raw:
                return r.status, payload, dict(r.headers)
            return r.status, json.loads(payload.decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload, dict(e.headers)
        try:
            return e.code, json.loads(payload.decode("utf-8")), dict(e.headers)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": payload[:300].decode("utf-8", "replace")}, {}


os.makedirs(OUT, exist_ok=True)

print("=" * 72)
print("试卷导出端点 · HTTP 端到端（契约 v3）")
print("=" * 72)

st, j, _ = call(f"{B}/auth/login/", method="POST",
                body=admin_login())
tok = (j.get("data") or {}).get("access_token")
if not tok:
    print("登录失败")
    sys.exit(1)
print(f"登录 OK（token {len(tok)}）")

st, j, _ = call(f"{B}/papers/?page_size=50", token=tok)
_all = (j.get("data") or {}).get("items") or []
usable = [p for p in _all if (p.get("item_count") or 0) > 0]
check("找到有题的试卷", bool(usable), f"{len(usable)} 份")
if not usable:
    sys.exit(1)
paper = max(usable, key=lambda p: p.get("item_count") or 0)
PID = paper["id"]
print(f"  试卷 #{PID}「{paper['title']}」题数={paper['item_count']}")

# ══════════════════════════════════════════════════════════════
section("E1 · GET 排版 JSON（契约 v3）")
# ══════════════════════════════════════════════════════════════

st, j, _ = call(f"{B}/papers/{PID}/export/?layout=1", token=tok)
d = j.get("data") or {}
check("HTTP 200", st == 200, f"HTTP {st}")
check("★★ 顶层无 `columns` / `per_group`",
      "columns" not in d and "per_group" not in d, str(sorted(d)))
check("★★ header 带 `student_items`（无 `student_line`）",
      "student_items" in (d.get("header") or {})
      and "student_line" not in (d.get("header") or {}),
      str(sorted(d.get("header") or {})))
blocks = [b for p in (d.get("pages") or []) for b in (p.get("blocks") or [])]
check("★ 有题干/选项块",
      any(b["kind"] == "q_stems" for b in blocks)
      and any(b["kind"] == "choice_row" for b in blocks),
      str(sorted({b["kind"] for b in blocks})))
check("★★ `choice_row` 用 `option_cells`（无 groups）",
      all("option_cells" in b and "groups" not in b
          for b in blocks if b["kind"] == "choice_row"))
check("★ option_cols ∈ {4,2,1}",
      all(b["option_cols"] in (4, 2, 1) for b in blocks
          if b["kind"] == "choice_row"),
      str(sorted({b["option_cols"] for b in blocks
                  if b["kind"] == "choice_row"})))
check("★ 题干块是 `stem_lines`（一题一块）",
      all(isinstance(b.get("stem_lines"), list)
          for b in blocks if b["kind"] == "q_stems"))
(open(f"{OUT}/layout.json", "w", encoding="utf-8")
 .write(json.dumps(d, ensure_ascii=False, indent=1)))

# ══════════════════════════════════════════════════════════════
section("E2 · GET 导出 PDF / Word")
# ══════════════════════════════════════════════════════════════

st, data, hdr = call(f"{B}/papers/{PID}/export/?format=pdf", token=tok,
                     raw=True)
check("PDF HTTP 200 / %PDF- / attachment",
      st == 200 and data[:5] == b"%PDF-"
      and "attachment" in (hdr.get("Content-Disposition") or ""),
      f"HTTP {st} / {len(data)} 字节")
open(f"{OUT}/paper.pdf", "wb").write(data)

st, data, hdr = call(f"{B}/papers/{PID}/export/?format=docx", token=tok,
                     raw=True)
check("DOCX HTTP 200 / PK / attachment",
      st == 200 and data[:2] == b"PK"
      and "attachment" in (hdr.get("Content-Disposition") or ""),
      f"HTTP {st} / {len(data)} 字节")
open(f"{OUT}/paper.docx", "wb").write(data)

# ══════════════════════════════════════════════════════════════
section("E3 · POST 结构化参数（v3）")
# ══════════════════════════════════════════════════════════════

V3 = {
    "page_size": "A4", "orientation": "PORTRAIT", "margin_mm": 18,
    "line_spacing": 1.6, "base_font_pt": 11, "title_font_pt": 18,
    "title_text": "POST v3 标题",
    "student_fields": ["学号", "班级", "姓名"],
    "student_line_extra": "考场：第三考场",
    "notes_text": "注意事项：\n1. 用黑色签字笔。",
    "show_notes": True,
    "show_page_number": True, "page_number_align": "right",
    "answer_bracket": True, "show_score": True,
    "option_cols_by_seq": {"1": 2},
    "blank_blocks": [{"id": "b1", "after_seq": 1, "height_mm": 25}],
}
st, j, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                body={"format": "layout", "layout_config": V3})
d = j.get("data") or {}
cfg = d.get("config") or {}
check("★★ POST layout 200", st == 200, f"HTTP {st}")
check("★★ `option_cols_by_seq` 回显（键/值）",
      cfg.get("option_cols_by_seq") == {"1": 2},
      str(cfg.get("option_cols_by_seq")))
check("★★ 考生字段回显", cfg.get("student_fields")
      == ["学号", "班级", "姓名"], str(cfg.get("student_fields")))
check("★ 旧键已不在返回 config",
      not ({"choice_layout", "choice_layout_by_seq", "opt_style",
            "show_answer_space", "answer_space_mm"} & set(cfg)),
      str(sorted(set(cfg))))
check("★ 空白块回显", (cfg.get("blank_blocks") or [{}])[0].get("id") == "b1")

st, data, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                   body={"format": "pdf", "layout_config": V3}, raw=True)
check("★★ POST pdf → %PDF-", st == 200 and data[:5] == b"%PDF-",
      f"HTTP {st} / {len(data)} 字节")
open(f"{OUT}/paper-v3-post.pdf", "wb").write(data)
st, data, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                   body={"format": "docx", "layout_config": V3}, raw=True)
check("★★ POST docx → PK", st == 200 and data[:2] == b"PK",
      f"HTTP {st} / {len(data)} 字节")

# ══════════════════════════════════════════════════════════════
section("E4 · 独立答案 PDF")
# ══════════════════════════════════════════════════════════════

st, data, hdr = call(f"{B}/papers/{PID}/export/answer/", method="POST",
                     token=tok, body={"layout_config": V3}, raw=True)
check("★★ POST /export/answer/ → PDF", st == 200 and data[:5] == b"%PDF-",
      f"HTTP {st} / {len(data)} 字节")
open(f"{OUT}/answer.pdf", "wb").write(data)
try:
    import pymupdf

    doc = pymupdf.open(f"{OUT}/answer.pdf")
    at = "".join(p.get_text() for p in doc)
    doc.close()
    check("★★ 答案 PDF 有「参考答案」", "参考答案" in at)
    check("★★ 答案 PDF 不是试卷（不含题干）",
          "参考答案" in at and len(at) < 4000, f"{len(at)} 字符")
except Exception as e:  # noqa: BLE001
    check("答案 PDF 可解析", False, f"{type(e).__name__}: {e}")

st, j, _ = call(f"{B}/papers/{PID}/export/answer/", token=tok)
check("★ 答案端点只接受 POST（GET 被拒）", st in (404, 405), f"HTTP {st}")

# ══════════════════════════════════════════════════════════════
section("E5 · 参数校验")
# ══════════════════════════════════════════════════════════════

st, j, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                body={"format": "layout", "layout_config": {
                    "option_cols_by_seq": {"1": 3}}})
check("★★ 非法 `option_cols_by_seq` 值 → 400 并给出收到的值",
      st == 400 and "3" in (j.get("message") or ""),
      f"HTTP {st} / {j.get('message', '')[:70]}")
st, j, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                body={"format": "layout", "layout_config": {
                    "option_cols_by_seq": {"abc": 4}}})
check("★★ 非整数键 → 400", st == 400,
      f"HTTP {st} / {j.get('message', '')[:60]}")
st, j, _ = call(f"{B}/papers/{PID}/export/", method="POST", token=tok,
                body={"format": "layout", "layout_config": {
                    "page_size": "NOPE"}})
check("★★ 非法纸型 → 400", st == 400, f"HTTP {st}")
st, j, _ = call(f"{B}/papers/{PID}/export/?format=xls", token=tok)
check("★ 非法 GET format → 400", st == 400, f"HTTP {st}")
st, j, _ = call(f"{B}/papers/{PID}/export/?layout=1&margin_mm=500",
                token=tok)
check("★★ 越界数值夹区间", (j.get("data") or {}).get("config", {})
      .get("margin_mm") == 40.0)

# ══════════════════════════════════════════════════════════════
section("E6 · PATCH 保存回显 / 无题阻断")
# ══════════════════════════════════════════════════════════════

_course_id = _all[0].get("course_id") if _all else None
if _course_id:
    st, j, _ = call(f"{B}/papers/", method="POST", token=tok,
                    body={"course_id": _course_id,
                          "title": "verify-v3-临时排版卷"})
    tmp = (j.get("data") or {}).get("id")
    check("★ 建临时卷", st in (200, 201) and bool(tmp),
          f"HTTP {st} / id={tmp}")
    try:
        st, j, _ = call(f"{B}/papers/{tmp}/export/", method="POST",
                        token=tok, body={"format": "pdf"})
        check("★★ 无题可导 → 阻断 400（不出空卷）",
              st == 400 and "没有题" in (j.get("message") or ""),
              f"HTTP {st} / {j.get('message', '')[:60]}")

        # ── 文件名：中文 + `/` + 连续空格，PDF/DOCX/答案同一套规则 ──
        st, j, _ = call(f"{B}/papers/{PID}/answer-key/", token=tok)
        _ak_items = (j.get("data") or {}).get("items") or []
        _qid = _ak_items[0]["question_id"] if _ak_items else None
        if _qid:
            st, j, _ = call(f"{B}/papers/{tmp}/", method="PATCH", token=tok,
                            body={"title": "标题/副标题  测试"})
            check("★ 临时卷改标题（含 / 与连续空格）", st == 200,
                  f"HTTP {st}")
            st, j, _ = call(f"{B}/papers/{tmp}/items/", method="POST",
                            token=tok, body={
                                "items": [{"question_id": _qid, "score": 3}],
                                "section_title": "一、单项选择题",
                                "allow_unapproved": True})
            check("★ 临时卷加 1 道题", st in (200, 201),
                  f"HTTP {st} / {j.get('message', '')[:50]}")
            _expect = {
                "pdf": ("试卷", "pdf", "paper"),
                "docx": ("试卷", "docx", "paper"),
                "answer": ("答案", "pdf", "answer"),
            }
            for _kind, (_label, _ascii_ext, _ascii_kind) in _expect.items():
                _url = (f"{B}/papers/{tmp}/export/answer/"
                        if _kind == "answer"
                        else f"{B}/papers/{tmp}/export/")
                _body = ({"layout_config": V3}
                         if _kind == "answer"
                         else {"format": _kind, "layout_config": V3})
                st, data, hdr = call(_url, method="POST", token=tok,
                                     body=_body, raw=True)
                cd = hdr.get("Content-Disposition") or ""
                m_ascii = re.search(r'filename="([^"]+)"', cd)
                m_utf = re.search(r"filename\*=UTF-8''([^;]+)", cd)
                name = unquote(m_utf.group(1)) if m_utf else ""
                ext = "docx" if _kind == "docx" else "pdf"
                check(f"★★ {_kind} 文件名 = 标题_副标题 测试-{_label}.{ext}",
                      st == 200 and name
                      == f"标题_副标题 测试-{_label}.{ext}"
                      and data[:5] == b"%PDF-" if ext == "pdf"
                      else st == 200 and name
                      == f"标题_副标题 测试-{_label}.{ext}"
                      and data[:2] == b"PK",
                      f"HTTP {st} / {name}")
                check(f"★ {_kind} ASCII 回退 = paper-{tmp}-{_ascii_kind}.{_ascii_ext}",
                      bool(m_ascii) and m_ascii.group(1)
                      == f"paper-{tmp}-{_ascii_kind}.{_ascii_ext}",
                      m_ascii.group(1) if m_ascii else cd[:80])

        st, j, _ = call(f"{B}/papers/{tmp}/", method="PATCH", token=tok,
                        body={"layout_config": V3})
        check("★★ PATCH 保存 v3 配置 → 200", st == 200, f"HTTP {st}")
        st, j, _ = call(f"{B}/papers/{tmp}/", token=tok)
        got = (j.get("data") or {}).get("layout_config") or {}
        check("★★ GET 回显一致（option_cols / 考生字段 / 空白块）",
              got.get("option_cols_by_seq") == {"1": 2}
              and got.get("student_fields") == ["学号", "班级", "姓名"]
              and (got.get("blank_blocks") or [{}])[0].get("id") == "b1",
              str({k: got.get(k) for k in
                   ("option_cols_by_seq", "student_fields",
                    "blank_blocks")}))
        st, j, _ = call(f"{B}/papers/{tmp}/", method="PATCH", token=tok,
                        body={"layout_config": {"option_cols_by_seq":
                                                {"1": 3}}})
        check("★★ PATCH 非法枚举 → 400", st == 400, f"HTTP {st}")
    finally:
        call(f"{B}/papers/{tmp}/", method="DELETE", token=tok)
        st, _, _ = call(f"{B}/papers/{tmp}/", token=tok)
        check("★ 临时卷已删除", st == 404, f"HTTP {st}")
else:
    check("★ 可建临时卷的课程", False, "无 course_id")

# ══════════════════════════════════════════════════════════════
section("E7 · 配图字节端点")
# ══════════════════════════════════════════════════════════════

asset_id = None
st, j, _ = call(f"{B}/sheet/templates/?page_size=50", token=tok)
for t in ((j.get("data") or {}).get("items") or []):
    aid = t.get("asset_id")
    if not aid:
        continue
    st2, raw, _ = call(f"{B}/media/assets/{aid}/content/", token=tok,
                       raw=True)
    if st2 == 200 and raw:
        asset_id = aid
        break
if asset_id:
    st, data, hdr = call(f"{B}/media/assets/{asset_id}/content/",
                         token=tok, raw=True)
    check("★★ 配图字节端点 200", st == 200 and len(data) > 0,
          f"{len(data)} 字节")
    check("★★ `Cache-Control: private, max-age=300`",
          "private" in (hdr.get("Cache-Control") or "")
          and "max-age=300" in (hdr.get("Cache-Control") or ""),
          hdr.get("Cache-Control", ""))
    st, _, _ = call(f"{B}/media/assets/{asset_id}/content/")
    check("★ 未认证 → 401/403", st in (401, 403), f"HTTP {st}")
    st, _, _ = call(f"{B}/media/assets/99999999/content/", token=tok)
    check("★ 不存在 → 404", st == 404, f"HTTP {st}")
else:
    check("★ 有可读 asset", False, "未找到")

# ══════════════════════════════════════════════════════════════
section("E8 · 文件清单")
# ══════════════════════════════════════════════════════════════

import pathlib  # noqa: E402

files = sorted(pathlib.Path(OUT).glob("*"))
for f in files:
    print(f"     {f.name:<24} {f.stat().st_size:>9} B")
check("★ 产出 ≥ 5 个文件", len(files) >= 5, f"{len(files)} 个")

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
