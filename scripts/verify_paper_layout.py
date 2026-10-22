"""试卷排版引擎 · 契约 v3 几何回归。

★★ v3 与 v2 的**关键差异**（旧断言已删除，不能保留错误语义）：

    · 排列 = **一道题的四个选项怎么摆**（4=一行四列，2=两行两列，1=四行一列）
    · 题干独占全宽；题与题不再横向拼行
    · 判断题无选项；填空题序号与题干同段
    · 考生信息两端对齐，下划线是画出来的线（不是 `___`）
    · 分值只在分组标题的汇总行；主观题无作答区

删掉的旧断言（原因）：
  - 「1x4 = 一列四排 / 一行几道题」「2x2 一行含 2 组」——
    语义相反，v3 已删除 `choice_layout`
  - 「选项框宽高」「选项框随字号变大」—— v3 取消方框
  - 「逐题分值行高」「主观题作答区高度」—— v3 删除逐题分值与作答区

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_paper_layout.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.paper import layout as L  # noqa: E402

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


def make_section(
    *,
    title: str = "一、单项选择题",
    qtype: str = "SINGLE_CHOICE",
    seqs: list[int] | None = None,
    stems: dict[int, str] | None = None,
    opts: dict[int, list[str]] | None = None,
    scores: dict[int, float] | None = None,
) -> dict:
    seqs = seqs or [1]
    return {
        "order": 1,
        "title": title,
        "type": qtype,
        "seqs": seqs,
        "types": {sq: qtype for sq in seqs},
        "stems": stems or {sq: f"第 {sq} 题：题干内容。" for sq in seqs},
        "scores": scores or {sq: 3 for sq in seqs},
        "opt_texts_by_seq": opts if opts is not None else {},
        "force_letter_by_seq": {},
    }


def find_blocks(lay, kind: str):
    return [b for p in lay.pages for b in p.blocks if b.kind == kind]


print("=" * 72)
print("试卷排版引擎 · 契约 v3 几何回归")
print("=" * 72)

# ══════════════════════════════════════════════════════════════
section("L1 · 纸张与方向")
# ══════════════════════════════════════════════════════════════

w, h = L.page_size_mm("A4", "PORTRAIT")
check("A4 纵版 = 210×297", (w, h) == (210.0, 297.0), f"{w}×{h}")
w2, h2 = L.page_size_mm("A4", "LANDSCAPE")
check("★ A4 横版 = 297×210（交换宽高）",
      (w2, h2) == (297.0, 210.0), f"{w2}×{h2}")
check("A3 / B5 尺寸",
      L.page_size_mm("A3", "PORTRAIT") == (297.0, 420.0)
      and L.page_size_mm("B5", "PORTRAIT") == (176.0, 250.0))

# ══════════════════════════════════════════════════════════════
section("L2 · 契约 v3 归一化")
# ══════════════════════════════════════════════════════════════

cfg = L.normalize_config(None)
check("★ 默认考生字段 = 学号/班级/姓名",
      cfg["student_fields"] == ["学号", "班级", "姓名"],
      str(cfg["student_fields"]))
check("★ 默认 `option_cols_by_seq` = {}",
      cfg["option_cols_by_seq"] == {})
check("★ 默认 `show_score` = true", cfg["show_score"] is True)
check("★★ `answer_bracket` 默认 true；显式 false 仍保持 false",
      cfg["answer_bracket"] is True
      and L.normalize_config({"answer_bracket": False})["answer_bracket"]
      is False,
      f"default={cfg['answer_bracket']}")
check("★★ v3 删除的键**不在**归一化结果里",
      not ({"choice_layout", "choice_layout_by_seq", "opt_style",
            "show_answer_space", "answer_space_mm", "stem_mode"}
           & set(cfg)),
      str(sorted(set(cfg))))

cfg_old = L.normalize_config({
    "choice_layout": "4x1", "choice_layout_by_seq": {"1": "1x4"},
    "opt_style": "letter", "show_answer_space": False,
    "answer_space_mm": 10, "unknown_key": 1,
})
check("★ 旧键读到**忽略**（不迁移、不报错）",
      not ({"choice_layout", "choice_layout_by_seq", "opt_style",
            "show_answer_space", "answer_space_mm"} & set(cfg_old)),
      str(sorted(set(cfg_old))))
check("★ 未知键被丢弃", "unknown_key" not in cfg_old)

check("★ 旧「座位号」映射为「座号」",
      L.normalize_config({"student_fields": ["座位号"]})["student_fields"]
      == ["座号"])

_c = L.normalize_config({
    "margin_mm": 999, "line_spacing": 0.2, "base_font_pt": -5,
    "title_font_pt": 99, "title_text": "卷" * 200,
    "notes_text": "注" * 1200,
    "option_cols_by_seq": {"1": 2, "2": "4"},
})
check("★ 数值夹区间", _c["margin_mm"] == 40.0
      and _c["line_spacing"] == 1.0 and _c["base_font_pt"] == 6.0
      and _c["title_font_pt"] == 36.0,
      f"{_c['margin_mm']}/{_c['line_spacing']}/{_c['base_font_pt']}")
check("★ 文本截断", len(_c["title_text"]) == 120
      and len(_c["notes_text"]) == 1000)
check("★ `option_cols_by_seq` 键转 int、值转 int",
      _c["option_cols_by_seq"] == {1: 2, 2: 4},
      str(_c["option_cols_by_seq"]))

for bad in ({"option_cols_by_seq": {"1": 3}},
            {"option_cols_by_seq": {"1": 0}},
            {"option_cols_by_seq": {"abc": 4}}):
    ok = False
    try:
        L.normalize_config(bad)
    except ValueError:
        ok = True
    check(f"★★ 非法 `option_cols_by_seq` 报错：{bad}", ok)

for bad in ({"page_size": "nope"}, {"orientation": "sideways"},
            {"page_number_align": "left"}):
    ok = False
    try:
        L.normalize_config(bad)
    except ValueError:
        ok = True
    check(f"★ 非法枚举报错：{bad}", ok)

# ══════════════════════════════════════════════════════════════
section("L3 · ★★ 选项排列：4 / 2 / 1 的等宽列坐标")
# ══════════════════════════════════════════════════════════════

W = 170.0  # A4 纵版、margin 20


def _choice_lay(opts: dict[int, list[str]], *, cfg_extra=None,
                qtype="SINGLE_CHOICE"):
    sec = make_section(
        qtype=qtype, seqs=sorted(opts),
        stems={sq: "题干" for sq in opts}, opts=opts)
    return L.plan_layout(title="T", sections=[sec],
                         config=cfg_extra or {})


_layouts = {}
# 短选项：一行四列
_short = ["硼", "钨", "碳", "硅"]
_lay4 = _choice_lay({1: _short})
_row4 = find_blocks(_lay4, "choice_row")[0]
_layouts["4"] = _row4
check("★★ 短选项自动判定为 4 列（一行四列）",
      _row4.option_cols == 4, str(_row4.option_cols))
check("★★ 4 列列起点 = 0, 0.25W, 0.5W, 0.75W",
      [round(c["x_mm"], 3) for c in _row4.option_cells]
      == [0.0, round(W / 4, 3), round(W / 2, 3), round(3 * W / 4, 3)],
      str([c["x_mm"] for c in _row4.option_cells]))
check("★ 选项文本是 `A. 内容`（无方框、无 `[A]`）",
      _row4.option_cells[0]["lines"][0] == "A. 硼",
      str(_row4.option_cells[0]["lines"]))

# 中等选项：两行两列
_medium = ["选项内容大约十六个字再加几个字",
           "乙选项内容也比较长一些再加几个",
           "丙选项内容同样比较长一再加几个",
           "丁选项内容也需要两列放再加几个"]
_lay2 = _choice_lay({1: _medium})
_row2 = find_blocks(_lay2, "choice_row")[0]
_layouts["2"] = _row2
check("★★ 中等选项自动判定为 2 列（两行两列）",
      _row2.option_cols == 2, str(_row2.option_cols))
check("★★ 2 列列起点 = 0, 0.5W",
      sorted({round(c["x_mm"], 3) for c in _row2.option_cells})
      == [0.0, 85.0],
      str([c["x_mm"] for c in _row2.option_cells]))
check("★★ 2×2 的 B/D 与 4 列的 C **x 完全相同**（0.5W）",
      _row2.option_cells[1]["x_mm"] == _row4.option_cells[2]["x_mm"]
      == 85.0,
      f"B/D={_row2.option_cells[1]['x_mm']} C={_row4.option_cells[2]['x_mm']}")
check("★ 2×2 的 A/B 在第一行，C/D 在第二行",
      [c["row"] for c in _row2.option_cells] == [0, 0, 1, 1]
      and _row2.option_cells[2]["y_mm"] > _row2.option_cells[0]["y_mm"],
      str([(c["row"], c["col"]) for c in _row2.option_cells]))

# 长选项：四行一列
_long = ["很长" * 20, "乙" + "长" * 45, "丙" * 45, "丁" * 45]
_lay1 = _choice_lay({1: _long})
_row1 = find_blocks(_lay1, "choice_row")[0]
_layouts["1"] = _row1
check("★★ 长选项自动判定为 1 列（四行一列）",
      _row1.option_cols == 1, str(_row1.option_cols))
check("★★ 1 列所有选项 x = 0，且逐行下移",
      all(c["x_mm"] == 0 for c in _row1.option_cells)
      and [c["row"] for c in _row1.option_cells] == [0, 1, 2, 3],
      str([(c["row"], c["x_mm"]) for c in _row1.option_cells]))

# 超长：1 列内折行，不截断
_wrap = ["超" * 60, "乙" * 10, "丙" * 10, "丁" * 10]
_layw = _choice_lay({1: _wrap})
_roww = find_blocks(_layw, "choice_row")[0]
check("★★ 仍超宽 → 1 列内折行、不截断、不省略号",
      _roww.option_cols == 1
      and len(_roww.option_cells[0]["lines"]) >= 2
      and not any("…" in ln for c in _roww.option_cells
                  for ln in c["lines"]),
      f"lines={len(_roww.option_cells[0]['lines'])}")
check("★★ 折行记 `PAPER.OPTION_WRAPPED` 并列出题号",
      any(i["code"] == "PAPER.OPTION_WRAPPED"
          and "第 1" in i["message"] for i in _layw.issues),
      str([i["message"][:60] for i in _layw.issues]))

# 手动覆盖：不自动降级，改为折行
_laym = _choice_lay({1: _short}, cfg_extra={
    "option_cols_by_seq": {1: 2}})
_rowm = find_blocks(_laym, "choice_row")[0]
check("★★ 手动指定 2 列时**不自动降级/升级**",
      _rowm.option_cols == 2, str(_rowm.option_cols))
_long_manual = _choice_lay({1: _long}, cfg_extra={
    "option_cols_by_seq": {1: 4}})
_rm = find_blocks(_long_manual, "choice_row")[0]
check("★★ 手动指定 4 列但放不下 → 4 列内折行 + 告警（不偷偷改）",
      _rm.option_cols == 4
      and any(len(c["lines"]) > 1 for c in _rm.option_cells)
      and any(i["code"] == "PAPER.OPTION_WRAPPED"
              for i in _long_manual.issues),
      f"cols={_rm.option_cols}")

_lay_unknown = L.plan_layout(
    title="T", sections=[make_section(seqs=[1])],
    config={"option_cols_by_seq": {999: 2}})
check("★ `option_cols_by_seq` 不存在的题号 → 忽略 + 告警",
      any(i["code"] == "PAPER.OPTION_COLS_SEQ_UNKNOWN"
          for i in _lay_unknown.issues),
      str([i["code"] for i in _lay_unknown.issues]))

# ══════════════════════════════════════════════════════════════
section("L3b · ★★ 选项字号：测量/行高/降级同步")
# ══════════════════════════════════════════════════════════════

_cfg_default = L.normalize_config(None)
check("★ 默认选项字号 = 正文 − 1.5（10.5 → 9）",
      L.option_font_pt(_cfg_default) == 9.0,
      str(L.option_font_pt(_cfg_default)))
check("★ `option_font_pt=12` 生效",
      L.option_font_pt(L.normalize_config({"option_font_pt": 12})) == 12.0)
check("★ 越界/未知选项字号：夹到 6–24 / 回 None",
      L.normalize_config({"option_font_pt": 99})["option_font_pt"] == 24.0
      and L.normalize_config({"option_font_pt": "abc"})["option_font_pt"]
      is None
      and L.normalize_config({"option_font_pt": None})["option_font_pt"]
      is None)

_opts9 = ["甲乙丙丁戊己庚辛壬"] * 4
_lay9 = _choice_lay({1: _opts9})
_lay12 = _choice_lay({1: _opts9},
                     cfg_extra={"option_font_pt": 12})
_r9 = find_blocks(_lay9, "choice_row")[0]
_r12 = find_blocks(_lay12, "choice_row")[0]
check("★★ 选项字号 12 → 同一组选项从 4 列降为 2 列（测量同步）",
      _r9.option_cols == 4 and _r12.option_cols == 2,
      f"{_r9.option_cols} → {_r12.option_cols}")
check("★★ 选项字号 12 → 选项行变高",
      _r12.height_mm > _r9.height_mm,
      f"{_r9.height_mm} → {_r12.height_mm}")
check("★ 选项字号进入 PDF/Word/预览共用的块字段",
      _r12.option_font_pt == 12.0, str(_r12.option_font_pt))

# ══════════════════════════════════════════════════════════════
section("L4 · ★★ 题干独占全宽 / 判断题 / 填空题 / 主观题")
# ══════════════════════════════════════════════════════════════

_sec_multi = make_section(
    seqs=[1, 2], stems={1: "第一题题干", 2: "第二题题干"},
    opts={1: _short, 2: _short})
_lay_multi = L.plan_layout(title="T", sections=[_sec_multi],
                           config={"answer_bracket": False})
_qs = find_blocks(_lay_multi, "q_stems")
check("★★ 每道题各自一个题干块（不再横向拼两题）",
      len(_qs) == 2 and [b.seq for b in _qs] == [1, 2],
      str([(b.seq, b.stem_lines) for b in _qs]))
check("★ 题干从 `1.` / `2.` 起排",
      _qs[0].stem_lines[0].startswith("1.")
      and _qs[1].stem_lines[0].startswith("2."),
      str([b.stem_lines[0] for b in _qs]))
# 跨页后长题干仍连续
_long_stems = {sq: "题干很长" * 80 for sq in range(1, 6)}
_lay_long = L.plan_layout(
    title="T",
    sections=[make_section(seqs=list(range(1, 6)),
                           stems=_long_stems,
                           opts={sq: _short for sq in range(1, 6)})],
    config={})
check("★★ 长题干允许按行跨页（q_stems 块可多于题数）",
      len(find_blocks(_lay_long, "q_stems")) > 5,
      str(len(find_blocks(_lay_long, "q_stems"))))
check("★ 选项块不拆：每题最多一个 choice_row",
      len(find_blocks(_lay_long, "choice_row")) == 5,
      str(len(find_blocks(_lay_long, "choice_row"))))

# 判断题：即使有 A/B 选项也不印
_tf_sec = make_section(
    title="二、判断题", qtype="TRUE_FALSE", seqs=[1, 2],
    stems={1: "判断题一", 2: "判断题二"},
    opts={1: ["正确", "错误"], 2: ["正确", "错误"]},
    scores={1: 2, 2: 2})
_lay_tf = L.plan_layout(title="T", sections=[_tf_sec],
                        config={"answer_bracket": True})
check("★★ 判断题**没有选项块**（即使快照有正确/错误）",
      not find_blocks(_lay_tf, "choice_row"),
      str([b.kind for b in find_blocks(_lay_tf, "q_stems")]))
check("★★ 判断题题号前照加 `（　　）`",
      find_blocks(_lay_tf, "q_stems")[0].stem_lines[0].startswith(
          "（　　）1."),
      find_blocks(_lay_tf, "q_stems")[0].stem_lines[0][:20])

# 填空题：序号与题干同段
_fill_sec = make_section(
    title="三、填空题", qtype="FILL_BLANK", seqs=[1],
    stems={1: "\n\n  齿轮的模数越大，轮齿越____。"},
    opts={})
_lay_fill = L.plan_layout(title="T", sections=[_fill_sec], config={})
_fill_blocks = find_blocks(_lay_fill, "q_stems")
check("★★ 填空题只有一个题干块（无单独题号块）",
      len(_fill_blocks) == 1 and not find_blocks(_lay_fill, "choice_row"),
      str([(b.kind, b.stem_lines) for b in _fill_blocks]))
check("★★ 填空题序号与题干**同一段**、首部空行被去掉",
      _fill_blocks[0].stem_lines[0].startswith("1. 齿轮的模数越大")
      and _fill_blocks[0].stem_lines[0].count("\n") == 0,
      _fill_blocks[0].stem_lines[0][:40])

# 主观题：题号 + 题干，无作答区
_subj_sec = make_section(
    title="四、简答题", qtype="SHORT_ANSWER", seqs=[1],
    stems={1: "试述齿轮传动中模数与齿数的关系。"}, opts={})
_lay_subj = L.plan_layout(title="T", sections=[_subj_sec], config={})
_subj = find_blocks(_lay_subj, "subjective")
check("★★ 主观题有题号 + 题干，但**没有作答区**",
      len(_subj) == 1
      and _subj[0].stem_lines
      and _subj[0].stem_lines[0].startswith("1.")
      and not hasattr(_subj[0], "answer_h_mm")
      and not find_blocks(_lay_subj, "choice_row"),
      str(_subj[0].stem_lines))

# ══════════════════════════════════════════════════════════════
section("L5 · ★★ 考生信息：固定顺序 + 两端对齐")
# ══════════════════════════════════════════════════════════════


def _header_items(fields: list[str], *, page="A4", margin=20):
    lay = L.plan_layout(
        title="T", sections=[make_section(seqs=[1])],
        config={"page_size": page, "margin_mm": margin,
                "student_fields": fields})
    return lay, lay.header["student_items"]


for fields in (["学号", "班级", "姓名"],
               ["学号", "座号", "班级", "姓名", "得分"]):
    _lay, _items = _header_items(fields)
    _last = _items[-1]
    check(f"★★ 考生信息 {'/'.join(fields)}：首项 x=0",
          _items[0]["x_mm"] == 0.0, str(_items[0]["x_mm"]))
    check(f"★★ 考生信息 {'/'.join(fields)}：末项下划线右端 = W",
          abs(_last["underline_x_mm"] + _last["underline_w_mm"]
              - _lay.usable_width_mm) <= 0.1,
          f"{_last['underline_x_mm'] + _last['underline_w_mm']} vs "
          f"{_lay.usable_width_mm}")
    check(f"★ 标签按固定顺序（不是勾选顺序）",
          [i["name"] for i in _items]
          == [f for f in L.STUDENT_FIELD_OPTIONS if f in fields],
          str([i["name"] for i in _items]))
    check("★ 下划线是数字段（不是 `___` 字符）",
          all("_" not in i["label"] for i in _items)
          and all(i["underline_w_mm"] > 0 for i in _items))

# 5 个字段在 A5 窄页放不下 → 折两行
_lay_narrow, _items_narrow = _header_items(
    ["学号", "座号", "班级", "姓名", "得分"], page="A5", margin=30)
check("★★ 5 个字段窄页放不下 → 折成两行，每行各自两端对齐",
      _lay_narrow.header["student_lines"] >= 2
      and len({i["line_no"] for i in _items_narrow}) >= 2,
      f"lines={_lay_narrow.header['student_lines']}")
for _ln in {i["line_no"] for i in _items_narrow}:
    _line_items = [i for i in _items_narrow if i["line_no"] == _ln]
    check(f"★ 第 {_ln} 行首项 x=0 / 末项右端=W",
          _line_items[0]["x_mm"] == 0.0
          and abs(_line_items[-1]["underline_x_mm"]
                  + _line_items[-1]["underline_w_mm"]
                  - _lay_narrow.usable_width_mm) <= 0.1,
          f"{_line_items[0]['x_mm']} / "
          f"{_line_items[-1]['underline_x_mm'] + _line_items[-1]['underline_w_mm']}")

# ══════════════════════════════════════════════════════════════
section("L6 · ★★ 分组汇总（分值只在标题）")
# ══════════════════════════════════════════════════════════════

_sec_eq = make_section(seqs=[1, 2, 3],
                       scores={1: 3, 2: 3, 3: 3})
_lay_eq = L.plan_layout(title="T", sections=[_sec_eq], config={})
_title_eq = find_blocks(_lay_eq, "stem")[0].title_lines
check("★★ 分值一致：`每题 X 分，共 Y 分`",
      any("每题 3 分" in ln and "共 9 分" in ln for ln in _title_eq),
      str(_title_eq))
check("★ 数字去掉无意义尾零（`3 分` / `9 分`，不是 3.00）",
      all(".00" not in ln for ln in _title_eq), str(_title_eq))

_sec_ne = make_section(seqs=[1, 2], scores={1: 3, 2: 4})
_title_ne = find_blocks(
    L.plan_layout(title="T", sections=[_sec_ne], config={}),
    "stem")[0].title_lines
check("★★ 分值不一致：`共 Y 分`（不出现「每题」）",
      any("共 7 分" in ln for ln in _title_ne)
      and not any("每题" in ln for ln in _title_ne),
      str(_title_ne))

_title_off = find_blocks(
    L.plan_layout(title="T", sections=[_sec_eq],
                  config={"show_score": False}), "stem")
check("★ `show_score=False` 时标题无汇总",
      _title_off and not any("本大题共" in ln
                             for ln in _title_off[0].title_lines),
      str(_title_off[0].title_lines if _title_off else []))

# 一行放不下 → 换行缩进 2 字
_sec_long_title = make_section(
    title="一、单项选择题（本题共二十小题，每小题三分，共六十分，请认真作答）",
    seqs=[1, 2, 3], scores={1: 3, 2: 3, 3: 3})
_title_wrap = find_blocks(
    L.plan_layout(title="T", sections=[_sec_long_title], config={}),
    "stem")[0].title_lines
check("★★ 标题 + 汇总一行放不下 → 换行、缩进 2 字、不截断",
      len(_title_wrap) >= 2
      and _title_wrap[-1].startswith("　　")
      and any("本大题共" in ln for ln in _title_wrap),
      str(_title_wrap))

# 题干里绝不能出现逐题分值
check("★★ 题干里不出现 `（3分）` 这类逐题分值",
      all("（3分）" not in ln
          for b in find_blocks(_lay_eq, "q_stems") for ln in b.stem_lines),
      str([b.stem_lines for b in find_blocks(_lay_eq, "q_stems")][:1]))

# ══════════════════════════════════════════════════════════════
section("L7 · ★★ 空白区 / 配图 / 页码 / 分页不重叠")
# ══════════════════════════════════════════════════════════════

_sec_blank = make_section(
    seqs=list(range(1, 41)),
    stems={sq: "题干" for sq in range(1, 41)},
    opts={sq: _short for sq in range(1, 41)},
    scores={sq: 3 for sq in range(1, 41)})
_lay_b = L.plan_layout(
    title="T", sections=[_sec_blank],
    config={"blank_blocks": [
        {"id": "mid", "after_seq": 20, "height_mm": 200},
        {"id": "ghost", "after_seq": 999, "height_mm": 30},
    ]})
_bl = find_blocks(_lay_b, "blank")
check("★★ 中间题后的空白块存在", any(b.blank_id == "mid" for b in _bl),
      str([(b.blank_id, b.after_seq) for b in _bl]))
_mid = next(b for b in _bl if b.blank_id == "mid")
check("★ 空白块高度与配置一致", _mid.height_mm == 200.0,
      str(_mid.height_mm))
# 单独造一个「放不下」的场景：一题之后放 210mm 空白块（< 可用高 257）
_lay_move = L.plan_layout(
    title="T",
    sections=[make_section(seqs=[1, 2, 3, 4],
                           opts={sq: _short for sq in range(1, 5)})],
    config={"blank_blocks": [{"id": "mv", "after_seq": 4,
                              "height_mm": 200}]})
_mv = find_blocks(_lay_move, "blank")[0]
_mv_page = next(p for p in _lay_move.pages if _mv in p.blocks)
check("★★ 放不下整块 → 移到下一页（从页顶开始）",
      _mv_page.page_no == 2 and _mv.y_mm == _lay_move.margin_mm,
      f"page={_mv_page.page_no} y={_mv.y_mm}")
check("★★ 不存在的 after_seq → 卷尾 + 告警",
      any(i["code"] == "PAPER.BLANK_AFTER_SEQ_UNKNOWN"
          for i in _lay_b.issues)
      and _bl[-1].blank_id == "ghost",
      str([(b.blank_id, b.after_seq) for b in _bl]))

_lay_tall = L.plan_layout(
    title="T", sections=[make_section(seqs=[1])],
    config={"page_size": "A5",
            "blank_blocks": [{"id": "tall", "after_seq": 0,
                              "height_mm": 200}]})
_tall = find_blocks(_lay_tall, "blank")[0]
_tall_page = next(p for p in _lay_tall.pages if _tall in p.blocks)
check("★★ 高过单页空白块夹到可用高并告警",
      _tall.height_mm == _lay_tall.usable_height_mm
      and any(i["code"] == "PAPER.BLANK_TOO_TALL"
              for i in _lay_tall.issues),
      f"{_tall.height_mm}/{_lay_tall.usable_height_mm}")
check("★ 卷首空白块（after_seq=0）在该页正文之前",
      _tall_page.blocks[0].kind == "blank"
      and _tall.y_mm == _lay_tall.margin_mm,
      f"page={_tall_page.page_no} y={_tall.y_mm} "
      f"first={_tall_page.blocks[0].kind}")

_fig = {"image_id": 1, "asset_id": 9, "attach_point": "STEM",
        "width_px": 400, "height_px": 300, "dpi": 200,
        "caption": "图 1 齿轮", "scale": 1.0, "missing": False}
_sec_fig = make_section(seqs=[1, 2], opts={1: _short, 2: _short})
_sec_fig["figures_by_seq"] = {1: [_fig]}
_lay_fig = L.plan_layout(title="T", sections=[_sec_fig], config={})
_fb = find_blocks(_lay_fig, "figure")[0]
check("★★ 图宽 = min(可用宽, 像素宽/dpi×25.4, 可用宽×scale)",
      _fb.width_mm == min(W, 400 / 200 * 25.4, W), str(_fb.width_mm))
check("★★ 高按比例", abs(_fb.image_h_mm / _fb.width_mm - 300 / 400) < 0.01,
      f"{_fb.width_mm}×{_fb.image_h_mm}")

_miss = {"image_id": None, "asset_id": None, "attach_point": "STEM",
         "missing": True, "caption": ""}
_sec_miss = make_section(seqs=[1], opts={1: _short})
_sec_miss["figures_by_seq"] = {1: [_miss]}
_lay_miss = L.plan_layout(title="T", sections=[_sec_miss], config={})
check("★★ 缺图 → 占位 + 非阻断告警",
      find_blocks(_lay_miss, "figure")[0].missing
      and any(i["code"] == "PAPER.FIGURE_MISSING"
              and not i["blocking"] for i in _lay_miss.issues),
      str([i["code"] for i in _lay_miss.issues]))

# 不重叠 / 不越界 / 题号完整
_all_blocks = [b for p in _lay_b.pages for b in p.blocks]
_overlap = []
for p in _lay_b.pages:
    _bs = sorted(p.blocks, key=lambda b: b.y_mm)
    for a, b in zip(_bs, _bs[1:]):
        if a.y_mm + a.height_mm > b.y_mm + 0.01:
            _overlap.append((p.page_no, a.kind, b.kind))
check("★★ 同页块不重叠", not _overlap, str(_overlap[:3]))
check("★★ 块不越出下边距",
      all(b.y_mm + b.height_mm <= _lay_b.page_height_mm
          - _lay_b.margin_mm + 0.01 for b in _all_blocks),
      str([(b.kind, b.y_mm, b.height_mm) for b in _all_blocks
           if b.y_mm + b.height_mm > _lay_b.page_height_mm
           - _lay_b.margin_mm + 0.01][:3]))
_planned = set()
for p in _lay_b.pages:
    for b in p.blocks:
        if b.kind in ("q_stems", "choice_row", "subjective") and b.seq:
            _planned.add(b.seq)
check("★★ 题号一个不少", _planned == set(range(1, 41)),
      f"planned={len(_planned)}")

# 页码
_j = L.layout_to_json(_lay_b)
check("★★ 页码「第 X 页 / 共 Y 页」总页数正确",
      _j["pages"][-1]["page_number"]["text"]
      == f"第 {_j['page_count']} 页 / 共 {_j['page_count']} 页",
      _j["pages"][-1]["page_number"]["text"])
check("★ 页码 y = 页高 − margin/2",
      abs(_j["pages"][0]["page_number"]["y_mm"]
          - (_j["page_height_mm"] - _j["margin_mm"] / 2)) < 0.01)

# ══════════════════════════════════════════════════════════════
section("L8 · JSON 契约 v3")
# ══════════════════════════════════════════════════════════════

check("★★ 顶层不再有 `columns` / `per_group`",
      "columns" not in _j and "per_group" not in _j,
      str(sorted(_j)))
check("★★ header 带 `student_items`，不再有 `student_line`",
      "student_items" in _j["header"]
      and "student_line" not in _j["header"],
      str(sorted(_j["header"])))
check("★ 每页有 page_number", all("page_number" in p for p in _j["pages"]))
_blocks = [b for p in _j["pages"] for b in p["blocks"]]
for key in ("kind", "y_mm", "height_mm", "seq", "title_lines",
            "stem_lines", "line_h_mm", "option_cols", "option_cells"):
    check(f"★ 块含 `{key}`", all(key in b for b in _blocks))
check("★ `choice_row` 的 option_cells 带 lines/x/y",
      all({"lines", "x_mm", "y_mm"} <= set(c)
          for b in _blocks if b["kind"] == "choice_row"
          for c in b["option_cells"]))
check("★★ 旧字段已清除（groups / score_texts / answer_h_mm）",
      all(not ({"groups", "score_texts", "answer_h_mm",
                "seq_opt_positions"} & set(b)) for b in _blocks),
      str(sorted(set().union(*[set(b) for b in _blocks]))[:20]))

# ══════════════════════════════════════════════════════════════
section("L9 · 空与异常输入")
# ══════════════════════════════════════════════════════════════

_empty = L.plan_layout(title="", sections=[], config={})
check("★ 无题 → 1 页且无块",
      len(_empty.pages) == 1 and not _empty.pages[0].blocks)
check("★ 空卷 header 也在", "student_items" in _empty.header)
check("★ 空题号大题被跳过",
      not any(b for p in L.plan_layout(
          title="T",
          sections=[{"title": "空大题", "type": "SINGLE_CHOICE",
                     "seqs": [], "types": {}, "stems": {},
                     "scores": {}, "opt_texts_by_seq": {},
                     "force_letter_by_seq": {}}],
          config={}).pages for b in p.blocks))

import math  # noqa: E402

_nan = L.normalize_config({"margin_mm": float("nan"),
                           "base_font_pt": float("inf")})
check("★★ NaN / inf 夹回默认",
      _nan["margin_mm"] == 20.0 and _nan["base_font_pt"] == 10.5)

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
