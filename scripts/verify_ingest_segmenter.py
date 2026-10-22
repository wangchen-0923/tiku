"""实测 ingest：模式表 / 三级切题 / 质量自检 / 图文一致性。"""

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

from apps.ingest.patterns import (  # noqa: E402
    REFERENCE_WORDS,
    SECTION_TYPE_HINTS,
    has_reference_word,
    infer_section_type,
    match_answer,
    match_explanation,
    match_group_title,
    match_option,
    match_question_number,
    normalize_text,
    strip_option_label,
)
from apps.ingest.quality import (  # noqa: E402
    ALLOWED_OPTION_COUNTS,
    CHOICE_TYPES,
    SegmentItem,
    batch_level_issues,
    detect_regular_option_labels,
    merge_warnings,
    page_level_issues,
    quality_gate,
)
from apps.ingest.segmenter import (  # noqa: E402
    S_HYBRID,
    S_TEXT_LAYER,
    S_VISION,
    W_LAYOUT,
    W_UNCERTAIN,
    W_VISION,
    SegmentError,
    consistency_issues,
    segment_by_layout_clustering,
    segment_by_patterns,
    segment_by_vision,
    segment_questions,
    split_by_gaps,
)

PASS, FAIL = [], []

DOC = """一、选择题
1. 机械制图中，主视图应选择（  ）
A. 最能反映形状特征的方向
B. 任意方向
C. 左视图方向
D. 俯视图方向
答案：A
2. 尺寸标注中，φ 表示（  ）
A. 半径
B. 直径
C. 球面
D. 倒角
答案：B
3. 下列哪种钢属于合金钢（  ）
A. 20 钢
B. 45 钢
C. Q235
D. 40Cr
答案：D
二、判断题
4. 主视图与俯视图长对正。
答案：正确
5. 尺寸公差可以为负值。
答案：错误
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    # ══════════════ I1 模式表 ══════════════
    print("\n【I1】题号 / 大题 / 选项 / 答案模式表")
    m = match_question_number("1. 机械制图")
    check("`1.` 匹配", m and m["seq"] == 1 and m["rest"] == "机械制图", str(m))
    m = match_question_number("1、机械制图")
    check("`1、` 匹配", m and m["seq"] == 1, str(m))
    m = match_question_number("1．机械制图")
    check("全角点 `1．` 匹配", m and m["seq"] == 1, str(m))
    m = match_question_number("(2) 尺寸")
    check("`(2)` 匹配", m and m["seq"] == 2 and m["rest"] == "尺寸", str(m))
    m = match_question_number("（3）公差")
    check("全角 `（3）` 匹配", m and m["seq"] == 3, str(m))
    m = match_question_number("第4题 下列")
    check("`第4题` 匹配", m and m["seq"] == 4 and m["rest"] == "下列", str(m))
    check("普通行不匹配", match_question_number("这是一段正文") is None)
    check("空行不匹配", match_question_number("") is None)
    check("★ 超两位数字不匹配（`123.` 是序号不是题号）",
          match_question_number("123. 某内容") is None)

    check("大题 `一、选择题` 匹配",
          match_group_title("一、选择题") == "一、选择题")
    check("大题 `第二部分` 匹配",
          match_group_title("第二部分 综合题") is not None)
    check("普通行不是大题", match_group_title("1. 机械制图") is None)

    o = match_option("A. 最能反映形状特征")
    check("选项 `A.` 匹配", o and o["label"] == "A"
          and o["content"] == "最能反映形状特征", str(o))
    o = match_option("Ｂ、直径")
    check("★ 全角选项 `Ｂ、` 归一为 B", o and o["label"] == "B", str(o))
    o = match_option("A")
    check("只有标签没内容不算选项", o is None, str(o))

    check("答案 `答案：A`", match_answer("答案：A") == "A")
    check("答案 `【答案】B`", match_answer("【答案】B") == "B")
    check("答案 `答案A`", match_answer("答案A") == "A")
    check("解析 `解析：因为…`",
          match_explanation("解析：因为受力平衡") == "因为受力平衡")
    check("非答案行返回 None", match_answer("下面说法正确的是") is None)

    # ★ 大题标题推断的顺序敏感
    check("★「二、多选题」判为多选（不能被「选择题」抢走）",
          infer_section_type("二、多选题") == "MULTIPLE_CHOICE",
          infer_section_type("二、多选题"))
    check("「一、选择题」判为单选",
          infer_section_type("一、选择题") == "SINGLE_CHOICE")
    check("「三、判断题」判为判断",
          infer_section_type("三、判断题") == "TRUE_FALSE")
    check("「四、填空题」判为填空",
          infer_section_type("四、填空题") == "FILL_BLANK")
    check("「五、计算题」判为计算",
          infer_section_type("五、计算题") == "CALCULATION")
    check("「六、简答题」判为简答",
          infer_section_type("六、简答题") == "SHORT_ANSWER")
    check("无法推断返回 None", infer_section_type("七、其它") is None)
    check("空标题返回 None", infer_section_type("") is None)
    check("★「多选题」在提示表里排在「选择」之前",
          [k for k, _ in SECTION_TYPE_HINTS].index("多选题")
          < [k for k, _ in SECTION_TYPE_HINTS].index("选择"))

    check("normalize_text 全角转半角", normalize_text("ＡＢＣ") == "ABC")
    check("normalize_text 去空白", normalize_text("  内容  ") == "内容")
    check("strip_option_label 去重复标签",
          strip_option_label("A. 内容") == "内容", strip_option_label("A. 内容"))

    # ══════════════ I2 引用词 ══════════════
    print("\n【I2】配图引用词")
    for w in ("如图", "见图", "如图所示", "下表", "图1"):
        check(f"含「{w}」", has_reference_word(f"某题 {w} 所示"))
    check("不含引用词", not has_reference_word("某题只有文字"))
    check("空题干", not has_reference_word(""))
    check("引用词表非空", len(REFERENCE_WORDS) >= 10, str(len(REFERENCE_WORDS)))

    # ══════════════ I3 行距聚类（Level 2 核心）══════════════
    print("\n【I3】行距一维聚类")
    spans = split_by_gaps([0, 10, 20, 60, 70, 80, 130, 140])
    check("★ 中等间距 30、大间距 50 → 切 3 段",
          len(spans) == 3, str(spans))
    check("第一段是前 3 行", spans[0] == (0, 3), str(spans[0]))
    check("第二段是 3–6 行", spans[1] == (3, 6), str(spans[1]))
    check("第三段是 6–8 行", spans[2] == (6, 8), str(spans[2]))

    check("★ 间距全相同 → 只 1 段（无留白可切）",
          split_by_gaps([0, 10, 20, 30]) == [(0, 4)],
          str(split_by_gaps([0, 10, 20, 30])))
    check("单行 → 1 段", split_by_gaps([0]) == [(0, 1)])
    check("空 → 空", split_by_gaps([]) == [])
    check("★ 用中位数而非均值（少数大留白不拉高阈值）",
          len(split_by_gaps([0, 10, 20, 30, 200])) >= 2,
          str(split_by_gaps([0, 10, 20, 30, 200])))

    # ══════════════ I4 ★ Level 1 切题 ══════════════
    print("\n【I4】★ Level 1：题号模式切题")
    lines = DOC.strip().split("\n")
    items = segment_by_patterns(lines)
    check("切出 5 题", len(items) == 5, str(len(items)))
    check("题号 1–5 连续", [i.seq for i in items] == [1, 2, 3, 4, 5],
          str([i.seq for i in items]))

    it1 = items[0]
    check("第 1 题题干正确",
          it1.stem_text.startswith("机械制图中"), it1.stem_text[:20])
    check("第 1 题 4 个选项", len(it1.options) == 4, str(len(it1.options)))
    check("第 1 题选项标签 A–D",
          [o["label"] for o in it1.options] == ["A", "B", "C", "D"],
          str([o["label"] for o in it1.options]))
    check("第 1 题答案 A", it1.answer == "A", it1.answer)
    check("★ 第 1 题带大题标题", it1.group_title == "一、选择题", it1.group_title)
    check("★ 题型从大题标题推断", it1.qtype == "SINGLE_CHOICE", it1.qtype)

    it4 = items[3]
    check("★ 第 4 题（判断题）题型正确", it4.qtype == "TRUE_FALSE", it4.qtype)
    check("判断题答案「正确」", it4.answer == "正确", it4.answer)
    check("判断题无选项（合法）", len(it4.options) == 0, str(len(it4.options)))
    check("判断题带自己的大题标题",
          it4.group_title == "二、判断题", it4.group_title)

    # ★ 题号必须递增：题干里的编号不算新题
    # ★★ 题号「回退」才被当作正文。
    #
    #   我一开始的测试假设「`1.` → `3.` → `2.`」里 `3.` 会被并进题干。
    #   实测不会：`3 > 1` 是**递增**的，实现（正确地）把它当成新题。
    #
    #   这是一个真实的语义边界：**题号跳跃无法与「小问编号」区分**——
    #   `3. 求支反力` 既可能是第 3 题，也可能是第 1 题的第 3 小问。
    #   没有语义信息时不该猜。跳跃靠质量门（题量不符）与人工校对兜住。
    #
    #   能可靠判定的是**回退**：`3.` 之后又出现 `2.`，那一定不是新题。
    tricky = [
        "1. 第一题的内容足够长了",
        "3. 第三题的内容足够长了",
        "2. 这里回退，不是新题，应并入上一题",
    ]
    ti = segment_by_patterns(tricky)
    check("★ 题号回退时不另起一题（挡掉题干内编号）",
          len(ti) == 2, f"{len(ti)} 题：{[i.seq for i in ti]}")
    check("回退的那一行并入**当前题**（第 3 题）的题干",
          "这里回退" in ti[1].stem_text, ti[1].stem_text[:50])

    # 题号跳跃：实现按「新题」处理（无法与真正的新题区分）
    jump = ["1. 第一题的内容足够长了", "3. 第三题的内容足够长了"]
    ji = segment_by_patterns(jump)
    check("★ 题号跳跃视为新题（无语义信息时不猜）",
          len(ji) == 2, f"{len(ji)} 题 —— 交由质量门与人工校对兜住")

    # 跨大题重新从 1 开始
    two_groups = [
        "一、选择题", "1. 第一题内容", "答案：A",
        "二、判断题", "1. 新大题重新编号", "答案：正确",
    ]
    tg = segment_by_patterns(two_groups)
    check("★ 跨大题重新编号时算新题", len(tg) == 2, f"{len(tg)} 题")
    check("两组题的大题标题不同",
          tg[0].group_title != tg[1].group_title,
          f"{tg[0].group_title} / {tg[1].group_title}")

    # 题号之前的内容被丢弃
    lead = ["封面标题", "说明文字", "1. 第一题内容"]
    li = segment_by_patterns(lead)
    check("题号之前的内容不成为题", len(li) == 1, str(len(li)))

    # 解析行
    exp = ["1. 某题内容", "答案：A", "解析：因为受力平衡"]
    ei = segment_by_patterns(exp)
    check("解析被单独提取",
          ei[0].explanation == "因为受力平衡", ei[0].explanation)

    # ══════════════ I5 ★ 质量自检 ══════════════
    print("\n【I5】★ 切题质量自检")
    good = segment_by_patterns(DOC.strip().split("\n"))
    q = quality_gate(good, page_count=1)
    check("★ 含判断题的正常卷子通过（判断题 0 选项合法）",
          q["ok"], str(q["blocking"]))

    check("判断题不在「有选项题型」里",
          "TRUE_FALSE" not in CHOICE_TYPES, str(CHOICE_TYPES))
    check("单选题在「有选项题型」里", "SINGLE_CHOICE" in CHOICE_TYPES)
    check("允许的选项数含 2 和 6",
          2 in ALLOWED_OPTION_COUNTS and 6 in ALLOWED_OPTION_COUNTS)

    # 题量为 0
    q0 = quality_gate([], page_count=1)
    check("★ 0 题不通过", not q0["ok"])
    check("给出 NO_ITEMS", any("NO_ITEMS" in i for i in q0["issues"]),
          str(q0["issues"]))

    # 题量不符
    q1 = quality_gate(good, page_count=1, expected_count=10)
    check("★ 题量偏离期望 ±20% 不通过", not q1["ok"])
    check("给出 QUESTION_COUNT_MISMATCH",
          any("COUNT_MISMATCH" in i for i in q1["issues"]), str(q1["issues"]))
    q1b = quality_gate(good, page_count=1, expected_count=5)
    check("题量符合期望时通过", q1b["ok"], str(q1b["blocking"]))

    # 题干过短
    short = [SegmentItem(seq=1, stem_text="短", qtype="SINGLE_CHOICE",
                         options=[{"label": "A", "content": "甲"}],
                         answer="A")]
    q2 = quality_gate(short, page_count=1)
    check("★ 题干过短不通过", not q2["ok"])
    check("给出 SHORT_STEM", any("SHORT_STEM" in i for i in q2["issues"]),
          str(q2["issues"]))

    # 选项数异常
    odd = [SegmentItem(seq=1,
                       stem_text="这是一道足够长的单选题题干内容",
                       qtype="SINGLE_CHOICE",
                       options=[{"label": "A", "content": "甲"}],
                       answer="A")]
    q3 = quality_gate(odd, page_count=1)
    check("★ 单选只有 1 个选项 → 异常", not q3["ok"])
    check("给出 OPTION_COUNT_ANOMALY",
          any("OPTION_COUNT_ANOMALY" in i for i in q3["issues"]),
          str(q3["issues"]))

    # ★ 无答案不阻断
    noans = [SegmentItem(seq=1, stem_text="这是一道足够长的单选题题干内容",
                         qtype="SINGLE_CHOICE",
                         options=[{"label": l, "content": f"内容{l}"}
                                  for l in "ABCD"])]
    q4 = quality_gate(noans, page_count=1)
    check("★ 无答案**不阻断** ok（教师可补）", q4["ok"],
          f"blocking={q4['blocking']}")
    check("但仍在 issues 里（供高亮）",
          any("NO_ANSWER" in i for i in q4["issues"]), str(q4["issues"]))

    # 单页 0 题
    q5 = quality_gate(good, page_count=3)
    check("★ 第 2 页起无题 → 报警", not q5["ok"])
    check("给出 ZERO_QUESTIONS_ON_PAGE",
          any("ZERO_QUESTIONS_ON_PAGE_2" in i for i in q5["issues"]),
          str(q5["issues"]))

    # 判断题带选项 → 报警
    tf_opt = [SegmentItem(seq=1, stem_text="这是一道足够长的判断题题干内容",
                          qtype="TRUE_FALSE",
                          options=[{"label": "A", "content": "正确"}],
                          answer="正确")]
    q6 = quality_gate(tf_opt, page_count=1)
    check("★ 判断题带选项 → 报警（疑似切题混入）", not q6["ok"])
    check("给出 UNEXPECTED_OPTIONS",
          any("UNEXPECTED_OPTIONS" in i for i in q6["issues"]),
          str(q6["issues"]))

    # 批次级 vs 题级问题分类
    mixed = ["QUESTION_COUNT_MISMATCH: 期望 10", "SHORT_STEM_Q3",
             "ZERO_QUESTIONS_ON_PAGE_2", "NO_ANSWER_Q1"]
    bl = batch_level_issues(mixed)
    check("★ 批次级问题只含批次级（页级另算）",
          bl == ["QUESTION_COUNT_MISMATCH: 期望 10"], str(bl))
    pl = page_level_issues(mixed)
    check("★ 页级问题单独一类（否则校对界面看不到它）",
          pl == ["ZERO_QUESTIONS_ON_PAGE_2"], str(pl))
    check("★ 批次级 + 页级 + 题级 = 全集",
          sorted(bl + pl + [i for i in mixed
                            if i not in bl and i not in pl]) == sorted(mixed))

    # 选项标签跳号
    gap = [SegmentItem(seq=1, stem_text="足够长的题干内容在这里",
                       qtype="SINGLE_CHOICE",
                       options=[{"label": "A", "content": "甲"},
                                {"label": "C", "content": "丙"}])]
    probs = detect_regular_option_labels(gap)
    check("★ 选项标签跳号被检出", len(probs) == 1, str(probs))
    check("问题里带题号与标签",
          "Q1" in probs[0] and "A,C" in probs[0], probs[0])
    check("连续标签无问题",
          not detect_regular_option_labels(good))

    # warnings 回挂
    target = [SegmentItem(seq=7, stem_text="足够长的题干内容在这里")]
    merge_warnings(target, ["SHORT_STEM_Q7", "NO_ITEMS"])
    check("★ 题级问题回挂到对应题", "SHORT_STEM_Q7" in target[0].warnings,
          str(target[0].warnings))
    check("批次级问题不回挂", "NO_ITEMS" not in target[0].warnings)

    # ══════════════ I6 ★ 三级调度 ══════════════
    print("\n【I6】★ 三级切题调度")
    r = segment_questions(DOC.strip().split("\n"), page_count=1)
    check("用 Level 1（TEXT_LAYER）", r["strategy"] == S_TEXT_LAYER,
          r["strategy"])
    check("质量通过", r["quality"]["ok"], str(r["quality"]["blocking"]))
    check("只尝试了 1 级", len(r["attempts"]) == 1, str(len(r["attempts"])))
    check("尝试记录含 level/count/ok",
          all({"level", "count", "ok"} <= set(a) for a in r["attempts"]),
          str(r["attempts"][0]))

    # Level 1 失败（无题号）→ Level 2
    # ★ 段文本必须 >= min_stem_chars(8)，否则质量门判 SHORT_STEM
    #   而 Level 3 不可用 → 只能返回未拆分的 best-effort。
    #   我第一版每段只有 4–5 字，于是「切出 3 段」永远不成立。
    l2_lines = [
        "第一段的文字内容足够长了", "第一段继续补充说明文字",
        "第二段的文字内容也足够长", "第二段继续补充说明文字",
        "第三段的收尾文字足够长",
    ]
    l2_y = [0.0, 10.0, 60.0, 70.0, 130.0]
    r2 = segment_questions(l2_lines, lines_y=l2_y, page_count=1)
    check("★ 无题号时降级到 Level 2", r2["strategy"] == S_HYBRID,
          f"{r2['strategy']}，尝试 {[(a['level'], a['ok']) for a in r2['attempts']]}")
    check("切出 3 段", len(r2["items"]) == 3, str(len(r2["items"])))
    check("★ 每题都带 SEGMENT_BY_LAYOUT 标记（供高亮）",
          all(W_LAYOUT in it.warnings for it in r2["items"]),
          str([it.warnings for it in r2["items"]]))

    # Level 1、2 都失败 → Level 3
    def fake_vision(payload):
        return [
            {"seq": 1, "stem": "视觉切出的第一题内容足够长",
             "type": "SINGLE_CHOICE",
             "options": [{"label": "A", "content": "甲"},
                         {"label": "B", "content": "乙"}],
             "answer": "A", "page": 1},
            {"seq": 2, "stem": "视觉切出的第二题内容足够长",
             "type": "TRUE_FALSE", "answer": "正确", "page": 1},
        ]

    r3 = segment_questions(["毫无题号的一段文字"], vision_fn=fake_vision,
                           page_count=1)
    check("★ 降级到 Level 3（VISION）", r3["strategy"] == S_VISION,
          r3["strategy"])
    check("切出 2 题", len(r3["items"]) == 2, str(len(r3["items"])))
    check("★ 每题带 SEGMENT_BY_VISION 标记",
          all(W_VISION in it.warnings for it in r3["items"]),
          str([it.warnings for it in r3["items"]]))

    # 三级都失败 → UNCERTAIN
    r4 = segment_questions(["只有一行无题号的文字"], page_count=1)
    check("★ 三级都不行 → uncertain 标记",
          r4["quality"].get("uncertain") is True,
          str(r4["quality"].get("uncertain")))
    check("零题时 strategy 为空", r4["strategy"] == "", r4["strategy"])
    check("尝试记录覆盖三级",
          len(r4["attempts"]) >= 2,
          str([(a["level"], a["ok"]) for a in r4["attempts"]]))

    # ══════════════ I7 视觉返回的 Schema 校验 ══════════════
    print("\n【I7】视觉返回必须经 Schema 校验")
    def bad1(payload):
        return ["不是对象"]

    raised = False
    try:
        segment_by_vision(bad1)
    except SegmentError:
        raised = True
    check("★ 返回项不是对象 → 抛错", raised, "不做「尽力猜」")

    raised = False
    try:
        segment_by_vision(lambda p: [{"seq": 1, "type": "SINGLE_CHOICE"}])
    except SegmentError:
        raised = True
    check("★ 缺 stem → 抛错", raised)

    items = segment_by_vision(lambda p: [
        {"seq": 1, "stem": "内容", "bbox": [0.1, 0.2, 0.3, 0.4]},
    ])
    check("bbox 被解析", items[0].text_bbox == (0.1, 0.2, 0.3, 0.4),
          str(items[0].text_bbox))
    bad_bbox = segment_by_vision(lambda p: [
        {"seq": 1, "stem": "内容", "bbox": [0.1, 0.2]},
    ])
    check("★ 非法 bbox 长度 → 置 None（不崩）",
          bad_bbox[0].text_bbox is None, str(bad_bbox[0].text_bbox))

    # ══════════════ I8 图文一致性 ══════════════
    print("\n【I8】图文归属一致性（第三轮迭代）")
    its = [
        SegmentItem(seq=1, stem_text="如图所示的零件，其主视图是"),
        SegmentItem(seq=2, stem_text="该零件的材料牌号是什么"),
    ]
    probs = consistency_issues(its, [{"seq": 2, "bbox": (0, 0, 1, 1), "page": 1}])
    check("★ 有图但题干无引用词 → 报警",
          any("WITHOUT_REFERENCE_WORD_Q2" in p for p in probs), str(probs))
    probs2 = consistency_issues(its, [{"seq": 1, "page": 1}])
    check("★ 题干含「如图」→ 不报警", not probs2, str(probs2))
    probs3 = consistency_issues(its, [{"seq": None, "page": 1}])
    check("未归属的图 → 报警",
          any("UNBOUND" in p for p in probs3), str(probs3))
    probs4 = consistency_issues(its, [{"seq": 99, "page": 1}])
    check("归属到不存在的题 → 报警",
          any("SEQ_NOT_FOUND" in p for p in probs4), str(probs4))

    # ── 汇总 ──
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
