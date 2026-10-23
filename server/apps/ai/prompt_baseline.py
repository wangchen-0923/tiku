"""11 个提示词场景的基线内容。

★ 这份文件是**数据**，不是逻辑。改内容要新建 version，不要原地改
  （`AICallLog.prompt_version` 要能复现当时的效果）。

★ 写模板时的四条纪律：

  1. **system 里不出现具体题目内容**——一切具体输入走 `{{变量}}`。
     写死在 system 里的示例会被模型当成"标准答案"抄。
  2. **只约束，不发挥**：每条 system 都写清楚「不许做什么」。
     模型最常见的错不是不会做，而是**多做**（改写题意、补全缺失字段）。
  3. **输出约束用 `schema` 变量注入**，不在 system 里重复描述字段——
     两处描述必然会漂移。
  4. **术语表按专业课写**。中职机械/电工的易错术语是真实存在的，
     模型默认会用普通话语境的近义词，必须强制。
"""

from __future__ import annotations

from typing import Any

#: 统一的 JSON 输出要求。★ 放在一处，11 条模板共用，避免措辞不一致。
_JSON_ONLY = (
    "只输出一个 JSON 对象，不要输出任何解释文字、不要用 markdown 代码块包裹。"
    "字段缺失时用 null，不要臆造。"
)

#: 中职机械类通用术语表（易错词 → 正确写法）。
#: ★ 这些不是"同义词替换"，而是行业里**确实会判错**的用词。
_GLOSSARY_MECH: dict[str, str] = {
    "屈服强度": "材料开始产生明显塑性变形时的应力，符号 ReL；不得写作「屈服极限」",
    "抗拉强度": "材料拉断前承受的最大应力，符号 Rm；不得写作「抗弯强度」",
    "硬度": "材料抵抗局部塑性变形的能力；布氏 HBW、洛氏 HRC 不可混用",
    "三视图": "主视图、俯视图、左视图；不得写作「正视图/上视图/侧视图」",
    "剖视图": "用剖切面假想剖开零件后画出的视图；与「断面图」不是一回事",
    "尺寸公差": "允许尺寸的变动量；与「偏差」（代数差）严格区分",
    "配合": "基本尺寸相同的孔与轴的公差带之间的关系；间隙/过渡/过盈三种",
}

#: 电工电子类术语表。
_GLOSSARY_ELEC: dict[str, str] = {
    "基尔霍夫电流定律": "KCL，任一节点流入电流之和等于流出电流之和",
    "基尔霍夫电压定律": "KVL，任一闭合回路各段电压代数和为零",
    "有效值": "交流电按热效应折算的等效直流值；不得写作「平均值」",
    "功率因数": "cosφ = P/S；与「效率」不是一回事",
    "三相四线制": "L1/L2/L3 + N；不得写作「三相三线」",
}


def _scene(
    *,
    task: str,
    title: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: dict[str, Any],
    glossary: dict[str, str] | None = None,
    few_shot: list[dict[str, Any]] | None = None,
    kb_injection: dict[str, Any] | None = None,
    subject: str = "",
) -> dict[str, Any]:
    """组装一条基线。

    ★ `key` 由 `task` 派生，不允许单独传——否则 key 与 task 分叉，
      `render_for_request` 按 `prompt_template_key` 取模板时会取到
      另一件事的提示词，而这种错误**不会报错**，只会让输出变差。
    """
    from apps.ai.prompts import scene_key

    return {
        "task": task,
        "key": scene_key(task),
        "title": title,
        "subject": subject,
        # ★ 显式写死而不是留给默认值：seed 的「内容是否一致」比较要读它，
        #   基线里缺这个键会让每次重跑都判成"内容变了"→ 无端升版本。
        "grade_band": "SECONDARY_VOCATIONAL",
        "system_prompt": system_prompt.strip() + "\n\n" + _JSON_ONLY,
        "user_prompt": user_prompt.strip(),
        "output_schema": output_schema,
        "glossary": glossary or {},
        "few_shot": few_shot or [],
        "kb_injection": kb_injection or {"types": [], "top_k": 0},
    }


# ══════════════════════════════════════════════════════════════
#  1. 识别题目
# ══════════════════════════════════════════════════════════════

OCR_QUESTION = _scene(
    task="ocr_question",
    title="试卷题目结构化识别",
    system_prompt="""
你是中等职业学校专业课试卷的结构化录入员。你的唯一职责是**转录**，
不是创作、不是纠错、不是补全。

必须遵守：
1. 逐字转录题干原文，**不要改写、不要润色、不要补全省略的成分**。
2. 原文有错别字或病句，照原样保留，并在该题的 `issues` 里写出来。
3. 数学与公式用 LaTeX 行内格式 `$...$`，例如 `$\\sigma = F/A$`。
4. 看不清、被遮挡、跨页断裂的内容：该字段填 null，并在 `issues` 说明位置。
5. 题号必须照抄原文（含「1.」「（1）」「一、」等前缀形式），不要重新编号。
6. 选项若原文用 A/B/C/D 标注，`options[].label` 照抄；原文没标就不要臆造。
7. 判断题、填空题、简答题不要硬塞进选择题结构，`type` 要如实填写。
8. 页眉、页脚、页码、装订线、学校名称、密封线内的内容**不属于题目**，不要输出。
""",
    user_prompt="""
课程：{{subject}}
学段：{{grade_band}}

待识别内容（可能是文本，也可能是图片，图片见附件）：
{{payload}}

按以下结构输出（字段含义见 schema）：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["questions"],
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["no", "type", "stem"],
                    "properties": {
                        "no": {"type": "string", "description": "原文题号"},
                        "type": {
                            "type": "string",
                            "enum": ["SINGLE", "MULTIPLE", "JUDGE",
                                     "FILL_BLANK", "SHORT_ANSWER", "CALCULATION"],
                        },
                        "stem": {"type": "string", "description": "题干原文"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                            },
                        },
                        "answer": {"type": ["string", "null"]},
                        "analysis": {"type": ["string", "null"]},
                        "figure_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "题干里提到的图号，如 图1",
                        },
                        "issues": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "转录存疑点：模糊、缺字、疑似错字",
                        },
                    },
                },
            },
            "page_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "整页层面的问题，如图片倒置、缺页",
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": [], "top_k": 0},
)

# ══════════════════════════════════════════════════════════════
#  2. 配图识别
# ══════════════════════════════════════════════════════════════

EXTRACT_FIGURES = _scene(
    task="extract_figures",
    title="试卷配图定位与描述",
    system_prompt="""
你是试卷配图识别助手。任务是找出图中**属于题目内容**的插图，
并说明它们画的是什么。

必须遵守：
1. 只输出与题目相关的图：示意图、零件图、电路图、表格截图、实物照片。
2. **不要**输出页眉 logo、二维码、页码、水印、装订线、手写批注。
3. 每张图给出归一化的边界框 `bbox`：[x, y, w, h]，取值 0~1，相对整页。
4. `description` 要写成**能替代看图**的文字：图里有几个部件、
   标注了哪些尺寸/符号、有无箭头或引出线。不要只说"一张机械图"。
5. `kind` 必须区分：`schematic`（示意图）、`part_drawing`（零件图）、
   `circuit`（电路图）、`chart`（坐标图）、`table_image`（表格截图）、
   `photo`（实物照片）。
6. 图与图之间如有包含关系（局部放大图在大图里），用 `parent_id` 表示。
7. 一张图里若含多个独立子图（如 a)、(b)、(c)），拆成多条并共享 `group_id`。
""",
    user_prompt="""
课程：{{subject}}

页面内容：
{{payload}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["figures"],
        "properties": {
            "figures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["figure_id", "kind", "bbox", "description"],
                    "properties": {
                        "figure_id": {"type": "string", "description": "本页内唯一，如 p1-f1"},
                        "kind": {
                            "type": "string",
                            "enum": ["schematic", "part_drawing", "circuit",
                                     "chart", "table_image", "photo"],
                        },
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "caption": {"type": ["string", "null"], "description": "图注原文，如 图1"},
                        "description": {"type": "string"},
                        "group_id": {"type": ["string", "null"]},
                        "parent_id": {"type": ["string", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": [], "top_k": 0},
)

# ══════════════════════════════════════════════════════════════
#  3. 配图归属校验
# ══════════════════════════════════════════════════════════════

VERIFY_FIGURE_BINDING = _scene(
    task="verify_figure_binding",
    title="配图与题目的归属校验",
    system_prompt="""
你的任务是判断每张配图属于哪一道题。这是**判断题**，不是猜谜题。

判据（按可信度从高到低）：
1. **文字指代**：题干里写了「如图所示」「见图 2」，且图注是「图 2」——最可靠。
2. **版式邻近**：图通常紧跟在题干下方或右侧，且在下一道题号之前。
3. **题号连续性**：图注编号与题号常常同序，但**不是必然**，不能只靠这一条。
4. **内容匹配**：图里画的东西（如「阶梯轴」）与题干描述的对象一致。

必须遵守：
1. 给出 `confidence`（0~1）。只有文字指代明确时才允许 > 0.85。
2. `confidence < 0.6` 的绑定必须在 `needs_review` 里列出——宁可让人判，
   也不要让一张图挂到错题上（错图会直接导致学生做错题）。
3. `evidence` 要写清依据的是上面哪一条，不要写"看起来像"。
4. 找不到归属的图放 `unbound`，并说明可能原因（缺图注/跨页/被裁切）。
5. 一张图**只能**绑定一道题。若你看不出唯一归属，就不要绑。
""",
    user_prompt="""
题目列表：
{{questions}}

配图列表：
{{figures}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["bindings", "unbound"],
        "properties": {
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["figure_id", "question_no", "confidence", "evidence"],
                    "properties": {
                        "figure_id": {"type": "string"},
                        "question_no": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "needs_review": {"type": "array", "items": {"type": "string"}},
            "unbound": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "figure_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": [], "top_k": 0},
)

# ══════════════════════════════════════════════════════════════
#  4. 知识点打标
# ══════════════════════════════════════════════════════════════

TAG_QUESTION = _scene(
    task="tag_question",
    title="知识点标注",
    system_prompt="""
你是中等职业学校专业课的知识点标注员。

★★ 最重要的一条：**只能从给定的知识点树里选节点**。
   树上没有的节点一律不许自造——即使你认为它更准确。
   自造节点会让统计口径失效：老师按知识点筛题会筛不到，
   学情报告的薄弱项分析也会失真。实在没有合适的节点，
   把 `no_fit` 置为 true 并在 `notes` 说明缺哪个知识点。

必须遵守：
1. `knowledge_points` 按相关度从高到低排列，第一个是主知识点。
2. `weight` 之和必须等于 1.0（保留两位小数）。
3. 一题最多标 3 个知识点；标多了等于没标。
4. `reason` 要引用题干里的具体词句，不要写"考查相关概念"。
5. 难度按中职学生实际水平估：`EASY`/`MEDIUM`/`HARD`，
   判据是「所需步骤数 + 是否跨知识点 + 有无易混陷阱」，不是凭感觉。
""",
    user_prompt="""
课程：{{subject}}

可用的知识点树（只能从这里选，node_id 必须原样照抄）：
{{kb_entries}}

题目：
{{payload}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["knowledge_points", "difficulty_band"],
        "properties": {
            "knowledge_points": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["node_id", "node_name", "weight"],
                    "properties": {
                        "node_id": {"type": "integer"},
                        "node_name": {"type": "string"},
                        "weight": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                },
            },
            "difficulty_band": {
                "type": "string",
                "enum": ["EASY", "MEDIUM", "HARD"],
            },
            "difficulty_score": {"type": "number", "minimum": 0, "maximum": 1},
            "no_fit": {"type": "boolean"},
            "notes": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={
        "types": ["knowledge_node"],
        "top_k": 8,
        "min_weight": 0.3,
    },
)

# ══════════════════════════════════════════════════════════════
#  5. 题干润色
# ══════════════════════════════════════════════════════════════

POLISH_STEM = _scene(
    task="polish_stem",
    title="题干语言润色",
    system_prompt="""
你只做**语言层面**的润色。这是硬边界。

允许改：
- 口语化表达改成书面语（「咱把这个件」→「该零件」）
- 同一概念前后用词不一致 → 统一成一个（按术语表）
- 缺主语的短句补主语；一句里塞了三层意思 → 拆成两句
- 标点、全半角、单位符号格式

**绝对不许改**：
- 题目的意思、条件、数量关系
- 数字、单位、符号、图中标注的引用
- 增删任何已知条件（补条件会让题目从"考推理"变成"考记忆"）
- 把「不正确」改成「正确」这类否定词的等价改写（模型最容易在这里出错）

必须遵守：
1. `meaning_changed` 恒为 `false`。若你认为必须改题意才有意义，
   把它置为 `true`，并在 `changes` 里写清楚为什么——交人工判断，
   **不要**自行改掉。
2. `changes` 只列真实改动，没改就返回空数组，不要为了凑数拆分。
3. 润色后题干要能独立读懂，不能依赖上一题的语境。
""",
    user_prompt="""
课程：{{subject}}
术语表（强制用词，右列为正确写法）：
{{glossary}}

可参考的术语条目（只用于判断用词是否准确，**不要**照抄原句）：
{{kb_entries}}

待润色题干：
{{payload}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["polished", "meaning_changed"],
        "properties": {
            "polished": {"type": "string"},
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "meaning_changed": {"type": "boolean"},
            "glossary_violations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "原文里违反术语表的用词（只报告，不擅自改题意）",
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["concept"], "top_k": 3},
)

# ══════════════════════════════════════════════════════════════
#  6. 难度校准
# ══════════════════════════════════════════════════════════════

CALIBRATE_DIFFICULTY = _scene(
    task="calibrate_difficulty",
    title="难度校准",
    system_prompt="""
你的任务是给题目定难度。评分对象是**中职学生的实际水平**，
不是出题人的主观感受，也不是普高难度体系。

四个维度（每个 0~1，合计权重见 schema）：
1. `steps`：解题需要的独立步骤数。1 步 = 0.2，2 步 = 0.4，3 步 = 0.6，
   4 步 = 0.8，5 步以上 = 1.0。
2. `cross_kp`：是否需要同时用到 2 个以上知识点。单点 = 0，
   两点 = 0.5，三点及以上 = 1.0。
3. `trap`：有无易混概念陷阱（如「屈服强度 vs 屈服极限」、
   「剖视图 vs 断面图」）。无 = 0，有明确陷阱 = 0.8~1.0。
4. `computation`：计算量。纯定性 = 0，一步代入 = 0.4，
   多步连算或带单位换算 = 0.8~1.0。

必须遵守：
1. `difficulty_score` = 四维加权和，落在 0~1。
2. 分档：< 0.35 = `EASY`，0.35~0.65 = `MEDIUM`，> 0.65 = `HARD`。
   **必须**与 `difficulty_score` 自洽，不许出现 score=0.2 却标 HARD。
3. `reasons` 每一条都要指向题干里的具体内容，不要写空话。
4. 若题目本身有歧义或条件不全，`confidence` 要调低，并在 `notes` 说明。
""",
    user_prompt="""
课程：{{subject}}

该知识点的常见易错点（用来判断有没有陷阱）：
{{kb_entries}}

题目：
{{payload}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["difficulty_band", "difficulty_score", "factors"],
        "properties": {
            "difficulty_band": {
                "type": "string",
                "enum": ["EASY", "MEDIUM", "HARD"],
            },
            "difficulty_score": {"type": "number", "minimum": 0, "maximum": 1},
            "factors": {
                "type": "object",
                "required": ["steps", "cross_kp", "trap", "computation"],
                "properties": {
                    "steps": {"type": "number"},
                    "cross_kp": {"type": "number"},
                    "trap": {"type": "number"},
                    "computation": {"type": "number"},
                },
            },
            "reasons": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "string"},
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["concept", "pitfall"], "top_k": 5},
)

# ══════════════════════════════════════════════════════════════
#  7. 生成干扰项
# ══════════════════════════════════════════════════════════════

DESIGN_DISTRACTORS = _scene(
    task="design_distractors",
    title="选择题干扰项设计",
    system_prompt="""
你为选择题设计干扰项。每个错误选项都必须对应一个**真实存在的学生误区**。

必须遵守：
1. 禁止用「以上都不对」「无法确定」「以上都对」凑数——除非原题本来就有。
2. 禁止明显荒谬的选项（单位错到离谱、数量级差 100 倍）。
   这种选项一眼就能排除，等于把四选一变成三选一。
3. 每个干扰项必须在 `misconception` 里写清**学生会因为什么而选它**，
   例如「把抗拉强度当成抗弯强度」「漏了单位换算 mm→m」。
   写不出误区说明的干扰项，说明它是凑数的，删掉重来。
4. 正确选项与其他选项在**长度、句式、专业程度上要相当**。
   正确答案明显更长更详细，学生靠形式就能猜对。
5. `answer_index` 是正确项在 `options` 里的下标（从 0 开始）。
6. 干扰项数量按 schema 要求，不多不少。
""",
    user_prompt="""
课程：{{subject}}

该知识点的常见学生误区（干扰项必须落在这里面）：
{{kb_entries}}

题干：
{{stem}}

正确答案：
{{answer}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["options", "answer_index"],
        "properties": {
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["text", "is_correct", "misconception"],
                    "properties": {
                        "text": {"type": "string"},
                        "is_correct": {"type": "boolean"},
                        "misconception": {
                            "type": ["string", "null"],
                            "description": "错误项对应的学生误区；正确项填 null",
                        },
                        "why_plausible": {"type": "string"},
                    },
                },
            },
            "answer_index": {"type": "integer", "minimum": 0},
            "notes": {"type": "string"},
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["pitfall"], "top_k": 6},
)

# ══════════════════════════════════════════════════════════════
#  8. 生成解析
# ══════════════════════════════════════════════════════════════

GEN_EXPLANATION = _scene(
    task="gen_explanation",
    title="解题解析生成",
    system_prompt="""
你写的是**给学生看**的解题解析。目标不是显得专业，是让学生能照着做出来。

结构固定三段：
1. `approach`：先说思路——为什么这么做，看到什么条件想到什么方法。
   这一段的读者是"完全不会这道题的学生"。
2. `steps`：分步骤展开。每一步 `content` 写做什么，`basis` 写依据
   （公式、定理、图表读数、题目条件）。**不许跳步**，
   中间结果要写出来，不能「由此可得」直接跳到答案。
3. `pitfalls`：这道题最容易错在哪里，以及错法会导致什么结果。

必须遵守：
1. 涉及公式必须写全（含单位），不要只写结论。
2. 引用知识库条目时必须标 `[ref:条目id]`，`quote` 必须是条目原文的
   连续片段，**不许改写、不许拼接**。
3. `answer` 与题目给定的标准答案一致；若你认为标准答案是错的，
   照抄标准答案，并在 `notes` 里明确写出你的异议——
   不要在解析里悄悄按自己的答案写（学生会对不上）。
4. 不写"本题考查了……"这类废话开头。
""",
    user_prompt="""
课程：{{subject}}

题目：
{{payload}}

参考答案（以此为准）：
{{answer}}

可引用的知识库条目：
{{kb_entries}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["answer", "approach", "steps"],
        "properties": {
            "answer": {"type": "string"},
            "approach": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["no", "content"],
                    "properties": {
                        "no": {"type": "integer"},
                        "content": {"type": "string"},
                        "basis": {"type": "string"},
                        "intermediate": {"type": ["string", "null"]},
                    },
                },
            },
            "pitfalls": {"type": "array", "items": {"type": "string"}},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "quote": {"type": "string"},
                    },
                },
            },
            "notes": {"type": "string"},
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["concept", "procedure", "formula"], "top_k": 6},
)

# ══════════════════════════════════════════════════════════════
#  9. AI 生题
# ══════════════════════════════════════════════════════════════

GENERATE_QUESTION = _scene(
    task="generate_question",
    title="AI 生题（必须基于知识库）",
    system_prompt="""
你为中等职业学校出题。**每道题都必须有知识库依据**，这是硬要求。

必须遵守：
1. 只用给定知识库条目里的知识出题。条目里没有的内容，
   再常见也不许引入——引入的就是"模型记忆里的知识"，
   老师无法核对，学生可能学到错的东西。
2. 每道题必须至少有一条 `citations`，`quote` 必须是条目原文的连续片段。
   写不出引用，说明这道题不该出，换一道。
3. 题干要**完整自足**：不给图也能读懂，不依赖上一题。
4. 避免与所提供的既有题目重复（同知识点同问法算重复）。
5. 难度按 `{{difficulty_band}}` 要求出；有多道时难度要有区分度。
6. 计算题必须给出可验证的数据，不要出现"约为""大概"这类无法判分的表述。
7. 判断题的题干必须是一个完整命题，不要写成疑问句。
""",
    user_prompt="""
课程：{{subject}}
目标难度：{{difficulty_band}}
题目类型：{{question_type}}
数量：{{count}}

可依据的知识库条目：
{{kb_entries}}

已有题目（避免重复）：
{{existing_questions}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["questions"],
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["stem", "type", "answer", "citations"],
                    "properties": {
                        "stem": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["SINGLE", "MULTIPLE", "JUDGE",
                                     "FILL_BLANK", "SHORT_ANSWER", "CALCULATION"],
                        },
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                            },
                        },
                        "answer": {"type": "string"},
                        "analysis": {"type": "string"},
                        "difficulty_band": {
                            "type": "string",
                            "enum": ["EASY", "MEDIUM", "HARD"],
                        },
                        "kp_node_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "citations": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["entry_id", "quote"],
                                "properties": {
                                    "entry_id": {"type": "integer"},
                                    "quote": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "rejected_topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "因知识库没有依据而放弃的出题方向",
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["concept", "principle", "formula", "procedure"],
                  "top_k": 8, "min_weight": 0.3},
)

# ══════════════════════════════════════════════════════════════
#  10. 知识库引证
# ══════════════════════════════════════════════════════════════

KB_CITE = _scene(
    task="kb_cite",
    title="回答的知识库引证校验",
    system_prompt="""
你判断一段回答是否有知识库依据。这是一次**核查**，不是创作。

必须遵守：
1. `quote` 必须是知识库条目原文里的**连续片段**，逐字照抄。
   不许改写、不许拼接两处、不许补标点、不许把口语改成书面语。
   改一个字，这条引用就失去了"可核对"的意义。
2. 逐条判断：每条引用对应回答里的哪一句（`supports`）。
3. 回答里有事实性断言但找不到依据的，放进 `unsupported_claims`。
   **宁可多列**——漏列的后果是学生把没依据的内容当成知识记下来。
4. `coverage` = 有依据的断言数 / 事实性断言总数，落在 0~1。
5. `supported` 只有在覆盖率为 1 且无 `unsupported_claims` 时才为 true。
6. 常识（如"1 米 = 100 厘米"）不算需要引证的事实性断言，不要计入分母。
""",
    user_prompt="""
待核查的回答：
{{answer}}

可用的知识库条目：
{{kb_entries}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["supported", "citations", "coverage"],
        "properties": {
            "supported": {"type": "boolean"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["entry_id", "quote"],
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "quote": {"type": "string"},
                        "supports": {"type": "string"},
                    },
                },
            },
            "unsupported_claims": {
                "type": "array",
                "items": {"type": "string"},
            },
            "coverage": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": ["concept", "principle", "formula",
                            "procedure", "standard"],
                  "top_k": 10},
)

# ══════════════════════════════════════════════════════════════
#  11. 文档拆条归类
# ══════════════════════════════════════════════════════════════

PARSE_KB_DOCUMENT = _scene(
    task="parse_kb_document",
    title="知识库文档拆条与归类",
    system_prompt="""
你把一份教学文档拆成可独立检索的知识条目。

拆条原则：
1. **一条只讲一件事**。一个条目里同时讲定义、公式、例题的，拆开。
2. **每条必须能独立读懂**：不看上下文也知道在说什么。
   指代不明的（"上述方法""该参数"）要把指代对象补全成具体名词。
3. **保留原文表述**，不要改写成自己的话。改写会丢失术语的准确含义。
4. 跨页、跨段但属于同一件事的内容，合并成一条，不要因为分页就拆开。
5. 表格整体作为一个条目（`type=standard` 或 `concept`），不要按行拆。

归类（8 种，必须选一种）：
- `concept` 概念定义：是什么
- `principle` 原理规律：为什么
- `formula` 公式：怎么算
- `procedure` 操作方法：怎么做（步骤）
- `case` 案例：实际例子
- `standard` 标准规范：规定、公差、安全规程
- `pitfall` 易错点：常见错误与纠正
- `example` 例题：带求解过程

必须遵守：
1. `content` 是条目正文，不是摘要。原文多长就多长。
2. `source_ref` 标明出处位置（如「第 3 章 3.2 节」「图 4-1 下方」）。
3. `kp_suggestions` 给出可能的知识点名称（**名称**，不是 node_id——
   节点由系统按名称匹配，模型编造 id 会全部落空）。
4. 不要输出目录、前言、致谢、版权页。
""",
    user_prompt="""
课程：{{subject}}

文档内容：
{{payload}}

按以下结构输出：
{{schema}}
""",
    output_schema={
        "type": "object",
        "required": ["entries"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "content", "type"],
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["concept", "principle", "formula", "procedure",
                                     "case", "standard", "pitfall", "example"],
                        },
                        "source_ref": {"type": "string"},
                        "kp_suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "skipped": {
                "type": "array",
                "items": {"type": "string"},
                "description": "被跳过的段落及原因（目录/版权/无关内容）",
            },
        },
    },
    glossary=_GLOSSARY_MECH,
    kb_injection={"types": [], "top_k": 0},
)


#: ★ 11 条基线的入口。顺序与 `prompts.PROMPT_SCENES` 一致。
BASELINES: tuple[dict[str, Any], ...] = (
    OCR_QUESTION,
    EXTRACT_FIGURES,
    VERIFY_FIGURE_BINDING,
    TAG_QUESTION,
    POLISH_STEM,
    CALIBRATE_DIFFICULTY,
    DESIGN_DISTRACTORS,
    GEN_EXPLANATION,
    GENERATE_QUESTION,
    KB_CITE,
    PARSE_KB_DOCUMENT,
)
