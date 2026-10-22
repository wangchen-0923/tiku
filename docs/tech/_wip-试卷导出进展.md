# 试卷导出 · 进展与契约（`P1-60` / `P1-60b` / `P1-62` / **契约 v3.1**）

> 中途状态记录。完整设计见 [19-需求确认与增量计划.md](19-需求确认与增量计划.md) §八 第 2 项。
>
> ★★ **v3 已删除旧的 `choice_layout` 语义**。`1x4` / `2x2` / `4x1`
> 曾被解释成「一行放几道题」，**那是错的**；v3 统一为
> 「**一道题的四个选项怎么摆**」，配置键改为 `option_cols_by_seq`
> （4 / 2 / 1）。旧键读到一律忽略，不迁移。

| 项 | 状态 | 验证 |
| --- | --- | --- |
| 排版引擎 `paper/layout.py`（契约 v3.1） | ✅ | `verify_paper_layout.py` **98/98** |
| PDF 渲染 `paper/render.py` | ✅ 全按 JSON 坐标 | `verify_paper_render.py` **52/52** |
| Word 渲染 `paper/render_docx.py` | ✅ 可编辑（段落/表格/图片/页码域） | 同上 |
| 答案 PDF `paper/render_answer.py` | ✅ 只印答案，选择题/判断题 **一行 5 列** | 同上 |
| 组装 `paper/export.py` | ✅ 唯一组装点 + `resolve_config()` | — |
| `GET/POST /papers/{id}/export/` | ✅ GET 保留、POST 结构化参数 | `verify_paper_export_api.py` **45/45** |
| `POST /papers/{id}/export/answer/` | ✅ 独立答案 PDF | 同上 |
| `PATCH /papers/{id}/` 保存版式 | ✅ 保存前 `normalize_config`、非法枚举 400 | 同上 |
| `GET /media/assets/{id}/content/` | ✅ 认证 + 本机构校验 + 私有缓存 | 同上 |
| 导出独立页 `/papers/:id/export` | ✅ 左参数 / 右预览 / 答案按钮 | `pnpm typecheck` / `build` / Edge 冒烟 |
| `P1-58b` 期望总分与配分校验 | ✅ 已接线 | `verify_paper_scoring.py` **39/39** |
| JSON ↔ PDF 一致性 | ✅ 文字基线 **0.000mm**、选项 x **0.000mm**、图尺寸 **0.00mm**、空白块 **0.00mm** | `diag_paper_layout_consistency.py` |
| 真机样张（A4 / 短长选项 / 判断 / 填空 / 简答 / 2 图 / 3 空白块） | ✅ 试卷 PDF + DOCX + 答案 PDF + 300dpi 截图 | `docs/poc/试卷导出v3/` |

**2026-10-18 v3.1 复核**：`verify_paper_layout` **98**、`verify_paper_render` **52**、
`verify_paper_export_api` **45**、`verify_paper_scoring` **39**、
`verify_api_contracts` **23** 全部通过；前端 `pnpm typecheck` / `pnpm build` exit 0。

---

## 一、契约 v3：选项排列（核心语义）

术语按「行 × 列」，`W` = 可用宽：

| 值 | 排列 | 列起点 |
| --- | --- | --- |
| `4` | 一行四列：A B C D | `0, 0.25W, 0.5W, 0.75W` |
| `2` | 两行两列：A B / C D | `0, 0.5W` |
| `1` | 四行一列：A / B / C / D | `0` |

★★ 按**等宽列网格**实现，不按文字宽度均分剩余空间。
→ 2×2 的 B/D 与 4 列的 C 必须同一 x（0.5W）。

**逐题自动判定**（判定对象是该题自己的选项文本）：

1. 每个 `A. ` + 内容宽 ≤ `W/4` → 4
2. 否则每个 ≤ `W/2` → 2
3. 否则每个 ≤ `W` → 1
4. 仍超宽 → 1 列内折行，记 `PAPER.OPTION_WRAPPED`（不截断、不省略号）

**逐题手动覆盖**：`option_cols_by_seq: {"题号": 4|2|1}`。
- 手动优先，**不再自动降级**；放不下按该排列折行 + 告警（老师的选择被执行）
- 不存在的题号忽略并告警
- 前端表格只列单选 / 多选（判断题没有选项）

**块结构**：每道题 = 题干块（全宽，以 `12.` 起排）+ 选项块（该题自己的排列）。
题与题不再横向拼行；选项首行紧接题干末行。选项块不拆，放不下整块下移；
题干允许按行跨页；单题超过一页可用高时告警。

---

## 二、v3 的其余硬规则

### 判断题
- `type == TRUE_FALSE` **只印题号 + 题干**，即使快照里有 A/B 选项也不印
- `answer_bracket` 打开时题号前照加 `（　　）`

### 选项字母
- 三处（PDF / Word / 预览）都只画 `A. 内容`（字母 + 半角点 + 空格）
- **不画方框**；`option_box_mm` / 框相关常量与绘制代码已删除

### 填空题
- `12. 题干正文……` 单一段落，序号与题干同段
- 去掉题干首部空行；自然折行从第二行开始

### 考生信息
- 字段固定顺序：**学号、座号、班级、姓名、得分**
- 默认选中：**学号、班级、姓名**
- 两端对齐：`seg = (W − (k−1)×gap)/k`，gap=6mm；首段 x=0，末段下划线右端 = W
- 下划线是**画出来的线**（PDF line / 预览 border-bottom / Word 带下划线制表位），
  **不再用 `___` 字符拼**
- 放不下时 gap 收到 2mm；仍不够折两行，每行各自两端对齐（不缩字号）
- header JSON：`student_items: [{label,x_mm,underline_x_mm,underline_w_mm,y_mm,...}]`

### 选项呈现
- 删除 `opt_style`；固定为「字母 + 内容」
- **数据兜底**：某题题干已含 `A. B. C. D.` 标记 → 该题只印字母不印内容，
  issues 提示「题干自带选项」；不出现在 UI，不占配置键

### 分值
- 删除逐题分值（选项行分值、主观题题号分值、`score_line_h`）
- `show_score` 语义 = 是否印**分组汇总**，默认开
- 标题同一行末尾：分值一致 `（本大题共 N 小题，每题 X 分，共 Y 分）`；
  不一致 `（本大题共 N 小题，共 Y 分）`；数字去尾零
- 一行放不下时标题后换行、缩进 2 字，不截断
- 题干里不再出现 `（3分）`

### 主观题
- 删除 `show_answer_space` / `answer_space_mm` 全链路
- 主观题只印题号 + 题干；需要留白用「空白区」插入
- 空白区功能不变（任意位置、高度可调、跨页整块下移）

### 答案
- 删除试卷导出的 `answers` / `with_answer_key` / 「带答案（教师版）」
- `POST /papers/{id}/export/answer/` 独立答案 PDF：
  - 标题 `《卷面标题》参考答案`
  - 按大题分组，组标题含汇总行
  - 组内 `12. A`；选择题/判断题一行 **5** 条等宽列（`uw/5`）
  - 判断题印 `正确 / 错误`；填空题多个空用 `；`；主观题印答案文本并折行
  - 不含题干、不含解析
- 答案取值：`answer_text` 优先；为空时用 `questionbank/answer_text.py::answer_text_of()`
  从 `answer` 派生（与 `QuestionService` 共用同一函数，不复制第二套）

### 下载文件名（v3.1）
- 试卷：`<paper.title>-试卷.pdf/.docx`；答案：`<paper.title>-答案.pdf`
- 一律取 `paper.title`（不是 `title_text`，不加《》）
- `\ / : * ? " < > |` 与控制字符 → `_`；连续空白压一个空格；去首尾；空标题回退「试卷」；
  标题最多 120 字（保留后缀）
- `Content-Disposition` ASCII 回退：`paper-{id}-paper.ext` / `paper-{id}-answer.ext`；
  `filename*` 对整个文件名 `quote(name, safe="")`

---

## 三、`layout_config` v3.1

保留：`page_size / orientation / margin_mm / line_spacing / base_font_pt /
title_font_pt / title_text / show_title / student_fields / student_line_extra /
notes_text / show_notes / show_page_number / page_number_align /
show_score / blank_blocks`

新增：
- `option_cols_by_seq: {"题号": 4|2|1}`
- `option_font_pt: number | null`，6–24，默认 `null`
  - `null` = 跟随正文（`base_font_pt − 1.5`，下限 6）
  - 数字夹到 6–24；未知类型 → `None`
  - **宽度测量、行高、4→2→1 判定、PDF/Word/预览都只走 `option_font_pt(cfg)`**

默认值变更：`answer_bracket` 默认 **true**（只改默认值，不做迁移；显式 `false` 保持 false）。

卷面副标题只保留 `满分：X 分`；**期望难度不印在卷面**（`Paper.difficulty_expect` 字段保留）。

**删除**（读到一律忽略，不迁移）：
`choice_layout` / `choice_layout_by_seq` / `opt_style` /
`show_answer_space` / `answer_space_mm` / `stem_mode`

归一化：
- 未知键丢弃；数值夹区间；文本截断
- `option_cols_by_seq` 值必须 4/2/1，键必须整数题号，否则 400 并给出收到的值
- `option_font_pt` 数字夹 6–24，`null`/未知类型 → None
- `page_size` / `orientation` / `page_number_align` 非法 → 400

---

## 四、删除清单（避免残留死代码/相反语义）

**配置键**
`choice_layout`、`choice_layout_by_seq`、`opt_style`、
`show_answer_space`、`answer_space_mm`、`stem_mode`。

**常量 / 函数**
`CHOICE_LAYOUTS`、`OPT_STYLES`、`STEM_MODES`、`option_box_mm`、
`OPTION_BOX_ASPECT`、`SEQ_COL_*`、`OPTION_GAP_MM`、
`GROUP_GAP_MM`、`ROW_EXTRA_GAP_MM`、`ANSWER_RULE_GAP_MM`、
`option_positions_mm`、`group_width_mm`、`seq_col_mm`、`measure_header_mm`、
`show_answer_space` 相关布局字段。

**块字段**
`groups`、`row_h_mm`、`score_line_mm`、`seq_labels`、`group_opts`、
`group_widths_mm`、`letter_base_w_mm`、`columns`、`per_group`、
`group_x_mm`、`seq_col_w_mm`、`seq_opts`、`seq_opt_positions`、
`score_texts`、`option_box_*`、`answer_h_mm`、`answer_y_mm`、
`figures`（主观题内嵌图改独立 `figure` 块）。

**接口 / UI**
`answers` 参数、`with_answer_key` 渲染分支、「带答案（教师版）」开关；
「全卷默认排列」「选项呈现」「主观题作答区」表单块。

---

## 五、诊断工具

| 脚本 | 用途 |
| --- | --- |
| `scripts/diag_pdf_text_pos.py` | 打印 PDF 每段文字的 x / bbox 顶 / 基线 / 宽（mm） |
| `scripts/diag_paper_layout_consistency.py` | JSON vs PDF：文字基线、**选项 x**、图尺寸、空白块 |
| `scripts/gen_poc_paper_export.py` | 生成 v3 POC 样张（临时卷/临时题 finally 删除） |

---

## 六、POC 样张（`docs/poc/试卷导出v3/`）

A4 纵向，**4 页**：20 道选择题（短/中/长选项，含手动 2 列折行）、
5 道判断题、3 道填空题、3 道简答题、2 张配图、3 个空白块、
5 项考生信息两端对齐、分组汇总。

| 文件 | 内容 |
| --- | --- |
| `样卷-A4.pdf` | 试卷 PDF |
| `样卷-A4.docx` | 可编辑 Word（段落/表格/inline 图/页码域） |
| `样卷-答案.pdf` | 独立答案 PDF（选择题 4 列、判断题正确/错误、填空 `；`、简答题） |
| `layout.json` | 预览 / PDF / Word 共用排版 JSON |
| `page-01.png` … `page-04.png` | 300dpi 试卷页面截图 |
| `answer-01.png` | 300dpi 答案 PDF 截图 |
| `文字坐标.txt` / `一致性核对.txt` | JSON vs PDF 实测坐标 |
| `Word校验.txt` | python-docx + Microsoft Word COM 真机打开/编辑验证 |
| `前端导出页-汇总.png` | Edge headless 页面截图 |
| `导出告警.txt` | 样张告警（手动 2 列长选项折行） |

一致性结果：文字基线 **0.000mm**、选项 x **0.000mm**、图尺寸 **0.00mm**、
空白块 **0.00mm**。

复现：

```powershell
$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "D:\tiku\.devdeps;D:\tiku\server"
$env:ZF_DB_PORT = "15433"; $env:ZF_DB_PASSWORD = "zhifou"
& $py scripts/gen_poc_paper_export.py

& $py scripts/verify_paper_layout.py
& $py scripts/verify_paper_render.py
& $py scripts/verify_paper_export_api.py
& $py scripts/verify_paper_scoring.py
& $py scripts/verify_api_contracts.py
```

★ 临时卷 `__POC_试卷导出v3__`、临时简答题、临时配图在 `finally` 里删除。
本次复核数据库：0 张 POC 卷、asset 总数回到基线。

---

## 七、已知限制

- Word 分页位置可能与 PDF 不同（流式排版，**可编辑优先**；POC Word 5 页 / PDF 4 页）。
- Word 页码是 `PAGE` / `NUMPAGES` 域；需 Word 打印或「更新域」后刷新。
- 选项块不拆：极端情况下整块下移，可能让当前页尾部留白。
- 手动指定列数时若选项超宽，按该列宽折行并告警，不会偷偷改回自动排列。
