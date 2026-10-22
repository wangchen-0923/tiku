"""验收：公式（计划 U17 输入 / U18 排版与渲染）。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg \
    <py3.12> scripts/verify_math_formula.py

★★ 这个脚本要拦住的是**四类静默错误**，它们都不会抛异常：

    ① **公式被清洗吃掉** —— `求 $a<b$ 的解` 过 nh3 之后变成 `求 $a 的解`。
       HTML5 解析器把 `<b` 当标签开头，`$` 被吞进标签名。不报错、不告警，
       老师只看到题干少了半句。

    ② **折行切断公式** —— `$\\frac{1}{2}$` 从中间折开，两个半边各自
       找不到配对，前端渲染成两段红色原文。字体度量不会告诉你这件事。

    ③ **宽度按源码数** —— `\\frac{1}{2}` 是 12 个 ASCII 字符，
       按「ASCII 0.5 字宽」数出来 6 个字宽，实际渲染只有 0.75 个。
       后果是含公式的题干提前折行，右边留一大片空白，
       而且**选项列数判定**会被误降（4 列被判成 2 列）。

    ④ **两种引擎的折行分叉** —— 预览保留 TeX 按渲染宽度折，
       reportlab 把公式降级成 `1/2` 再折。不显式分档的话，
       同一份卷子「预览 8 行、导出 9 行」，谁也说不清哪个是对的。

  所以下面按「扫描 → 清洗 → 降级 → 宽度 → 折行 → 双档」逐层验，
  最后用静态断言把前端契约钉住（前端渲染点漏改是本批最容易复发的地方）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from apps.common.mathtext import (  # noqa: E402
    find_math_spans,
    has_math,
    mask_math,
    math_width_pt,
    split_atoms,
    strip_math_in_text,
    strip_tex,
    tex_width_em,
    unmask_math,
)
from apps.common.sanitize import sanitize_html  # noqa: E402
from apps.paper import layout as L  # noqa: E402
from apps.paper.export import PaperRenderData, build_layout  # noqa: E402

PASS = 0
FAIL = 0
WEB = ROOT / "web" / "src"


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
#  M1 · 定界符扫描
# ══════════════════════════════════════════════════════════════


def m1_scan() -> None:
    print("\nM1 · 定界符扫描（与前端 scanMath 同构）")

    def spans(s: str) -> list[tuple[int, int]]:
        return find_math_spans(s)

    check("行内 `$x^2+1$` 被识别",
          spans("求 $x^2+1$ 的值") == [(2, 9)], str(spans("求 $x^2+1$ 的值")))
    check("独立公式 `$$…$$` 被识别",
          spans("$$\\frac{1}{2}$$") == [(0, 15)], str(spans("$$\\frac{1}{2}$$")))
    check("`\\[ … \\]` 被识别",
          spans("\\[ E=mc^2 \\]") == [(0, 12)], str(spans("\\[ E=mc^2 \\]")))
    check("`\\( … \\)` 被识别",
          spans("\\(a+b\\)") == [(0, 7)], str(spans("\\(a+b\\)")))
    check("纯中文文本里没有公式",
          spans("求这道题的答案") == [])

    # ★ 下面四条是本脚本存在的核心理由
    check("★★ 货币表述 `$5 到 $10` 不误判成公式",
          spans("价格 $5 到 $10 之间") == [],
          str(spans("价格 $5 到 $10 之间")))
    check("★★ `$a<b$` 里的 `<` 不影响配对",
          spans("求 $a<b$ 的解") == [(2, 7)], str(spans("求 $a<b$ 的解")))
    check("未闭合的 `$x^2` 退回字面文本（不报错）",
          spans("未闭合 $x^2") == [])
    check("`\\$` 是转义美元符，不参与配对",
          spans("单价 \\$5 元") == [], str(spans("单价 \\$5 元")))
    check("`\\text{甲}` 允许公式里带中文",
          spans("设 $\\text{甲}=1$ 成立") == [(2, 14)],
          str(spans("设 $\\text{甲}=1$ 成立")))
    check("行内公式不跨行",
          spans("$a\nb$") == [], str(spans("$a\nb$")))

    check("has_math 对含公式文本为 True", has_math("a$b$c") is True)
    check("has_math 对纯文本为 False", has_math("没有公式") is False)


# ══════════════════════════════════════════════════════════════
#  M2 · 掩码与清洗
# ══════════════════════════════════════════════════════════════


def m2_sanitize() -> None:
    print("\nM2 · 掩码与清洗（公式不能死在白名单里）")

    src = "<p>求 $a<b$ 的解</p>"
    out = sanitize_html(src)
    check("★★ `$a<b$` 过清洗后完整保留",
          "$a<b$" in out, repr(out))
    check("白名单标签 `<p>` 被保留（不是被剥掉）",
          out.startswith("<p>") and out.endswith("</p>"), repr(out))
    check("公式两侧的正文都没丢",
          "求" in out and "的解" in out, repr(out))

    # 幂等：写入路径会重入（先建草稿、再保存）
    check("★★ sanitize_html 幂等（公式不会被二次清洗吃掉）",
          sanitize_html(out) == out, f"{out!r} → {sanitize_html(out)!r}")

    # 安全不能因为「加了公式保护」而放松
    evil = "<p>看这个<script>alert(1)</script>和 $x^2$</p>"
    out_evil = sanitize_html(evil)
    check("★★ XSS 仍然被拦（<script> 不能因为公式保护而漏过）",
          "script" not in out_evil.lower(), repr(out_evil))
    check("危险段落旁边合法公式仍保留", "$x^2$" in out_evil, repr(out_evil))

    evil2 = '<img src="x" onerror="alert(1)"> 配图 $\\frac{1}{2}$'
    out2 = sanitize_html(evil2)
    check("事件属性仍然被剥",
          "onerror" not in out2.lower(), repr(out2))
    check("同一段里的公式仍保留", "\\frac{1}{2}" in out2, repr(out2))

    masked, parts = mask_math("<p>求 $a<b$ 的解</p>")
    check("掩码后不含原始 `$`（否则 nh3 仍会看到 `<b`）",
          "$" not in masked, repr(masked))
    check("unmask 还原无损",
          unmask_math(masked, parts) == "<p>求 $a<b$ 的解</p>",
          repr(unmask_math(masked, parts)))
    check("无公式时掩码是空操作",
          mask_math("<p>纯文本</p>") == ("<p>纯文本</p>", []))


# ══════════════════════════════════════════════════════════════
#  M3 · 降级文本（reportlab / Word 回退）
# ══════════════════════════════════════════════════════════════


def m3_strip() -> None:
    print("\nM3 · 公式降级成纯文本（reportlab / Word 回退）")

    cases = [
        ("\\frac{1}{2}", "1/2"),
        ("\\frac{x+1}{2}", "(x+1)/2"),
        ("\\sqrt{3}", "√3"),
        ("\\sqrt{x+1}", "√(x+1)"),
        ("x^{2}", "x²"),
        ("x_{1}", "x₁"),
        ("30^{\\circ}", "30°"),
        ("a \\times b \\ge 0", "a × b ≥ 0"),
        ("\\text{速度}", "速度"),
        ("\\overline{AB}", "AB"),
        ("y=x^{2}+2x-1", "y=x²+2x-1"),
        ("S=\\pi r^{2}", "S=π r²"),
    ]
    for tex, want in cases:
        got = strip_tex(tex)
        check(f"`{tex}` → `{want}`", got == want, f"得到 {got!r}")

    check("★★ 降级后**没有残留反斜杠**（否则纸上会印 `\\frac`）",
          "\\" not in strip_tex("\\frac{\\sqrt{3}}{2}"),
          repr(strip_tex("\\frac{\\sqrt{3}}{2}")))
    check("方程组降级用「；」分隔",
          strip_tex("\\begin{cases} x+y=1 \\\\ x-y=3 \\end{cases}")
          == "x+y=1 ； x-y=3",
          repr(strip_tex("\\begin{cases} x+y=1 \\\\ x-y=3 \\end{cases}")))

    mixed = strip_math_in_text("求 $\\frac{1}{2}$ 与 $x^{2}$ 的和为 $5$")
    check("混排文本里的公式被就地降级、定界符一并去掉",
          mixed == "求 1/2 与 x² 的和为 5", repr(mixed))
    check("没有公式时原样返回（不改动任何字符）",
          strip_math_in_text("普通题干 a<b") == "普通题干 a<b")
    check("降级后可读性：不含 `$`",
          "$" not in mixed, repr(mixed))


# ══════════════════════════════════════════════════════════════
#  M4 · 宽度估算
# ══════════════════════════════════════════════════════════════


def m4_width() -> None:
    print("\nM4 · 宽度估算（不能按 TeX 源码字符数）")

    frac = tex_width_em("\\frac{1}{2}")
    src_units = len("\\frac{1}{2}") * 0.5      # 按旧口径会数成 6 个字宽
    check("★★ 分数宽度远小于按源码字符数的估算",
          frac < src_units / 3, f"{frac:.2f} em vs 源码 {src_units:.1f} 字宽")
    check("分数宽度在合理区间（0.6–1.2 em）", 0.6 <= frac <= 1.2, f"{frac:.2f}")

    check("两个字符的公式比单字符宽",
          tex_width_em("ab") > tex_width_em("a"),
          f"{tex_width_em('ab'):.2f} vs {tex_width_em('a'):.2f}")
    check("根号比被开方数宽（多一个根号头）",
          tex_width_em("\\sqrt{x}") > tex_width_em("x"),
          f"{tex_width_em('\\sqrt{x}'):.2f} vs {tex_width_em('x'):.2f}")
    check("上标只多一点点（不重复计基底宽度）",
          tex_width_em("x^{2}") < tex_width_em("x") + 1.0,
          f"{tex_width_em('x^{2}'):.2f}")
    check("`\\text{中文}` 按中文 1 字宽算",
          abs(tex_width_em("\\text{甲乙}") - 2.0) < 0.01,
          f"{tex_width_em('\\text{甲乙}'):.2f}")
    check("pt 换算正确（10pt 下 1 em = 10pt）",
          abs(math_width_pt("x", 10.0) - 5.5) < 0.01,
          f"{math_width_pt('x', 10.0):.2f}")

    atoms = split_atoms("求$x^2$值")
    check("原子切分把公式整段当一个原子",
          len(atoms) == 3 and atoms[1][0] == "$x^2$", str(atoms))
    check("原子宽度等于公式渲染宽度（不是源码长度）",
          abs(atoms[1][1] - tex_width_em("x^2")) < 1e-9, str(atoms[1]))


# ══════════════════════════════════════════════════════════════
#  M5 · 折行
# ══════════════════════════════════════════════════════════════


def m5_wrap() -> None:
    print("\nM5 · 折行（公式不能被切断）")

    # 窄列强制折行，公式必须整体落在某一行里
    text = "求" * 6 + "$\\frac{1}{2}$" + "值" * 6
    lines = L.wrap_text_lines(text, width_mm=24.0, font_pt=10.5)
    joined = "".join(lines)
    check("折行后内容无丢失（除换行外与原文一致）",
          joined.replace("\n", "") == text, f"{joined!r}")
    check("★★ `$` 数量是偶数（公式没有被劈成两半）",
          joined.count("$") % 2 == 0, f"`$` × {joined.count('$')}")
    with_math = [ln for ln in lines if "\\frac{1}{2}" in ln]
    check("★★ `\\frac{1}{2}$` 完整出现在同一行里",
          len(with_math) == 1 and "$\\frac{1}{2}$" in with_math[0],
          str(lines))

    # 宽度感知：同样内容，公式感知后的行数应少于按源码字符数的行数
    fit_wide = L.wrap_text_lines("$\\frac{1}{2}$", width_mm=10.0, font_pt=10.5)
    check("短公式在 10mm 宽里能一行放下（源码计数法放不下）",
          len(fit_wide) == 1, str(fit_wide))

    check("空文本返回空列表", L.wrap_text_lines("", 100.0, 10.5) == [])
    check("只有换行的文本保留空行结构",
          L.wrap_text_lines("甲\n\n乙", 100.0, 10.5) == ["甲", "", "乙"],
          str(L.wrap_text_lines("甲\n\n乙", 100.0, 10.5)))

    # 选项列数判定：含公式的 4 个短选项应当仍是 4 列
    texts = ["$\\frac{1}{2}$", "$\\frac{1}{3}$", "$\\frac{1}{4}$", "$\\frac{1}{5}$"]
    cols, wrapped = L._auto_option_cols(
        texts, width_mm=150.0, font_pt=9.0)
    check("★★ 4 个短公式选项判成 4 列（按源码计数会误降成 2 列）",
          cols == 4 and not wrapped, f"cols={cols} wrapped={wrapped}")


# ══════════════════════════════════════════════════════════════
#  M6 · tex / plain 双档
# ══════════════════════════════════════════════════════════════


def _render_data(stem: str) -> PaperRenderData:
    return PaperRenderData(
        title="公式测试卷",
        subtitle="满分：10 分",
        sections=[{
            "order": 1,
            "title": "一、单项选择题",
            "type": "SINGLE_CHOICE",
            "seqs": [1],
            "types": {1: "SINGLE_CHOICE"},
            "stems": {1: stem},
            "scores": {1: 10.0},
            "opt_texts_by_seq": {1: ["1/2", "1/3", "1/4", "1/5"]},
            "force_letter_by_seq": {1: False},
            "figures_by_seq": {},
        }],
        items={
            1: {
                "seq": 1, "score": 10.0, "type": "SINGLE_CHOICE",
                "stem_text": stem, "options": [], "answer": {},
                "answer_text": "", "force_letter": False,
            },
        },
        config={},
        assets={},
    )


def _all_lines(lay) -> list[str]:
    """收集卷面上所有会印出来的文字行。

    ★ 块的字段是 `stem_lines` / `title_lines` / `option_cells[*]["lines"]`，
      没有统一的 `.lines` —— 用 `getattr(b, "lines", ...)` 会永远拿到空列表，
      于是「公式有没有被降级」这类断言全都变成空字符串比对，
      看起来像功能没做，实际是取值取错了。
    """
    out: list[str] = []
    for page in lay.pages:
        for b in page.blocks:
            out.extend(str(x) for x in (getattr(b, "stem_lines", None) or []))
            out.extend(str(x) for x in (getattr(b, "title_lines", None) or []))
            out.extend(
                str(x) for x in (getattr(b, "caption_lines", None) or [])
            )
            for cell in (getattr(b, "option_cells", None) or []):
                out.extend(str(x) for x in (cell.get("lines") or []))
    return out


def m6_engine() -> None:
    print("\nM6 · tex / plain 双档（预览与导出各自自洽）")

    stem = "求 $\\frac{1}{2}+\\frac{1}{3}$ 的值"
    data = _render_data(stem)

    lay_tex = build_layout(data, math_render="tex")
    lay_plain = build_layout(data, math_render="plain")

    lines_tex = "\n".join(_all_lines(lay_tex))
    lines_plain = "\n".join(_all_lines(lay_plain))

    check("★★ tex 档保留 `$…$` 原文（前端 / Node 要的就是它）",
          "$" in lines_tex, repr(lines_tex[:80]))
    check("tex 档公式片段完整", "\\frac{1}{2}" in lines_tex, repr(lines_tex[:80]))
    check("★★ plain 档里没有残留 `$`（reportlab 画不出 TeX）",
          "$" not in lines_plain, repr(lines_plain[:80]))
    check("plain 档没有残留反斜杠命令",
          "\\frac" not in lines_plain, repr(lines_plain[:80]))
    check("plain 档把公式降成了可读文本",
          "1/2" in lines_plain and "1/3" in lines_plain,
          repr(lines_plain[:80]))

    # 两档都必须能出页，且不因公式而丢内容
    check("两档都产出了页面", bool(lay_tex.pages) and bool(lay_plain.pages))
    check("tex 档没有 blocking issue",
          not any(i.get("blocking") for i in lay_tex.issues),
          str(lay_tex.issues))

    # 大题标题也支持公式（U17 接的第二处编辑入口）
    data2 = _render_data("无公式题干")
    data2.sections[0]["title"] = "三、计算题（$F=ma$ 应用）"
    lay_t2 = build_layout(data2, math_render="tex")
    lay_p2 = build_layout(data2, math_render="plain")
    check("★★ 大题标题里的公式在 tex 档保留",
          "$F=ma$" in "\n".join(_all_lines(lay_t2)),
          "\n".join(_all_lines(lay_t2))[:100])
    check("★★ 大题标题里的公式在 plain 档被降级",
          "$" not in "\n".join(_all_lines(lay_p2)),
          "\n".join(_all_lines(lay_p2))[:100])

    # plan_layout 的注释里承诺过：math_render 不进 layout_config
    cfg = L.normalize_config({"math_render": "plain"})
    check("★★ `math_render` 不能从 layout_config 传入（否则客户端能改分页）",
          "math_render" not in cfg, str(sorted(cfg))[:80])


# ══════════════════════════════════════════════════════════════
#  M7 · 前端契约（静态）
# ══════════════════════════════════════════════════════════════


def m7_frontend() -> None:
    print("\nM7 · 前端契约（静态断言）")

    math_ts = read(WEB / "utils" / "math.ts")
    check("`web/src/utils/math.ts` 存在且导出统一入口 renderRich",
          "export function renderRich" in math_ts)
    check("渲染统一走 `output: 'html'`（不重复输出 MathML）",
          "output: 'html'" in math_ts, "")
    check("KaTeX 关闭 trust（禁止用户 TeX 注入 HTML）",
          "trust: false" in math_ts)
    check("HTML 场景只在文本节点替换（DOMParser + TreeWalker）",
          "createTreeWalker" in math_ts and "DOMParser" in math_ts)

    main_ts = read(WEB / "main.ts")
    check("★★ KaTeX 样式在 main.ts 引入（字体随包打包，不依赖 CDN）",
          "katex/dist/katex.min.css" in main_ts)

    pkg = read(ROOT / "web" / "package.json")
    check("package.json 已声明 katex 依赖", '"katex"' in pkg)

    create_drawer = read(WEB / "components" / "QuestionCreateDrawer.vue")
    check("建题抽屉用 MathInput 承载题干", "<MathInput" in create_drawer)
    check("建题抽屉放了共享工具条", "<MathToolbar" in create_drawer)
    check("★ 填空答案**故意**不做公式输入（会跟学生手打字符串比对）",
          "不要写公式" in create_drawer)

    compose = read(WEB / "views" / "PaperComposeView.vue")
    check("组卷页的大题标题接了公式输入",
          "<MathInput" in compose and 'toolbar="popover"' in compose)

    toolbar = read(WEB / "components" / "MathToolbar.vue")
    check("★★ 工具条按钮用 mousedown.prevent（否则失焦后选区归零）",
          toolbar.count("@mousedown.prevent") >= 3,
          f"出现 {toolbar.count('@mousedown.prevent')} 次")

    math_input = read(WEB / "components" / "MathInput.vue")
    check("输入框获得焦点时登记为活动目标",
          "setActiveMathTarget" in math_input)
    check("卸载时只清自己的登记（不误清别的字段）",
          "clearMathTarget" in math_input)
    check("已给出定界符未闭合的提示", "unclosedMath" in math_input)

    # 反例：不允许把公式定界符在输入阶段就渲染成 HTML 存库
    check("★★ 输入组件不把 KaTeX HTML 写回 modelValue（存的是 TeX 原文）",
          "renderToString" not in math_input,
          "MathInput 里不该出现 renderToString")


# ══════════════════════════════════════════════════════════════
#  M8 · 六处渲染点（漏一处就会出现「这边好、那边是源码」）
# ══════════════════════════════════════════════════════════════

#: 计划 §D 组 U18 点名的六处渲染点（答题卡单列在后面说明）
RENDER_POINTS: tuple[tuple[str, str, str], ...] = (
    ("题目详情", "components/QuestionDetailDrawer.vue", "<MathText"),
    ("试卷预览", "components/paper/PaperPreviewCanvas.vue", "<MathText"),
    ("答题页", "views/StudentAnswerView.vue", "<MathText"),
    ("阅卷页", "views/AssignmentReviewView.vue", "<MathText"),
    ("导出源（预览画布）", "views/PaperExportView.vue", "PaperPreviewCanvas"),
    ("题库列表", "views/QuestionListView.vue", "<MathText"),
)


def m8_points() -> None:
    print("\nM8 · 六处渲染点")

    for name, rel, needle in RENDER_POINTS:
        src = read(WEB / rel)
        check(f"{name} → `{rel}` 走统一渲染",
              needle in src, f"未找到 {needle}")

    # 阅卷 / 标注 / 审核 这些「顺手看题干」的地方也要一致
    for name, rel in (
        ("公共题审核", "views/PublicReviewView.vue"),
        ("知识点标注（逐题）", "views/QuestionMarkView.vue"),
        ("知识点标注（队列）", "views/KnowledgeMarkView.vue"),
        ("知识树标注队列", "views/KnowledgeView.vue"),
        ("组卷页卷面列表", "views/PaperComposeView.vue"),
    ):
        check(f"{name} 也统一渲染", "<MathText" in read(WEB / rel), rel)

    # ★ 全站不允许再有业务组件裸用 v-html —— 那是「公式漏渲染」的唯一复发入口
    #   ★ 先剥掉 HTML 注释再扫：注释里写「不要用 v-html」不算违规
    #     （这一处踩过：按行首字符过滤会把注释**续行**算成代码）
    leftover: list[str] = []
    for p in WEB.rglob("*.vue"):
        if p.name in ("MathText.vue", "MathToolbar.vue"):
            continue  # 这两个是渲染器本身
        text = re.sub(r"<!--.*?-->", "", p.read_text(encoding="utf-8"), flags=re.S)
        for line in text.splitlines():
            stripped = line.strip()
            if "v-html" in stripped and not stripped.startswith(("//", "/*", "*")):
                leftover.append(f"{p.name}: {stripped[:60]}")
    check("★★ 除渲染器本身外，业务组件里已无 `v-html`",
          not leftover, "; ".join(leftover[:4]))

    # 答题卡：几何由 OMR 模板决定，**不含题干文字**，只有题号与气泡。
    sheet = read(WEB / "components" / "SheetPreview.vue")
    check("答题卡预览不含题干文字（公式无从渲染，属于设计如此）",
          "stem_text" not in sheet and "stem_html" not in sheet)

    # 摘要截断必须公式安全（后端三处 + 前端一处）
    for rel in ("apps/paper/serializers.py", "apps/practice/serializers.py",
                "apps/questionbank/serializers.py"):
        check(f"{rel} 的摘要截断走 truncate_keep_math",
              "truncate_keep_math" in read(ROOT / "server" / rel), rel)
    check("前端 `truncateMath` 与后端同名逻辑同构",
          "export function truncateMath" in math_ts_of())

    # 导出引擎分档
    export_py = read(ROOT / "server" / "apps" / "paper" / "export.py")
    check("★★ PDF 导出走 plain 档（reportlab 画不出 TeX）",
          'build_layout(data, math_render="plain")' in export_py)
    check("★★ 预览走 tex 档（与 Node 渲染服务同档）",
          'build_layout(data, math_render="tex")' in export_py
          or 'out["math_render"] = "tex"' in export_py)


def math_ts_of() -> str:
    return read(WEB / "utils" / "math.ts")


def main() -> int:
    print("=" * 74)
    print("  公式（U17 输入 / U18 排版）验收")
    print("=" * 74)

    m1_scan()
    m2_sanitize()
    m3_strip()
    m4_width()
    m5_wrap()
    m6_engine()
    m7_frontend()
    m8_points()

    print("\n" + "=" * 74)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
