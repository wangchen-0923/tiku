/**
 * 组卷 API。
 *
 * ★★ 字段名全部**照后端序列化器抄**，不凭 DRF 默认约定猜。
 *    上一轮我按 DRF 默认写了 `results`/`count`，实际本项目
 *    `ZfJSONRenderer` 把它改写成 `items`/`total`——首页直接 KeyError。
 *
 * ★★ 三处容易写错的类型：
 *    1. `score` / `total_score` / `difficulty_expect` 是**字符串**
 *       （后端 Decimal 序列化），要先 `Number(...)` 再算
 *    2. `PaperItemSerializer.difficulty` 是**数字**（不算 Decimal）
 *    3. `PaperListSerializer` 有 `item_count`，`PaperItemSerializer`
 *       没有
 */

import { http } from './http'
import { fetchBlob } from './http'
import { downloadBlob } from './sheet'

/** 分页信封（本项目是 `items`/`total`，不是 DRF 默认的 `results`/`count`）。 */
export interface Paged<T> {
  items: T[]
  total: number
  next: string | null
  previous: string | null
}

export type PaperStatus = 'DRAFT' | 'FINALIZED' | 'ARCHIVED'
export type ComposeMode = 'MANUAL' | 'RULE' | 'DIAGNOSIS'
export type PaperPurpose = 'EXAM' | 'QUIZ' | 'HOMEWORK' | 'PRACTICE'

/** 列表项（对齐 `PaperListSerializer`）。 */
export interface PaperBrief {
  id: number
  uuid: string
  title: string
  purpose: PaperPurpose
  status: PaperStatus
  subject: string
  grade_band: string
  course_id: number
  course_name: string
  /** ★ 字符串（Decimal） */
  total_score: string
  item_count: number
  difficulty_expect: string
  compose_mode: ComposeMode
  is_personalized: boolean
  owner_student_id: number | null
  variant_label: string
  paper_group_id: number | null
  finalized_at: string | null
  created_at: string
  updated_at: string
}

/** 卷面题（对齐 `PaperItemSerializer`）。 */
export interface PaperItem {
  id: number
  seq: number
  section_title: string
  /** ★ 字符串 */
  score: string
  group_key: string
  question_id: number
  question_type: string
  stem_preview: string
  /** ★ 数字（不是字符串） */
  difficulty: number | null
  option_order: string[]
  layout_flags: string[]
  figure_display_scale: Record<string, number>
}

export interface ComposeSection {
  type: string
  count: number
  score_each: number
  difficulty_range: [number, number]
  title?: string
}

export interface ComposeConfig {
  sections: ComposeSection[]
  knowledge_nodes?: number[]
  weak_quota?: number
  exclude_recent_days?: number
}

export interface KnowledgeCoverage {
  covered_nodes?: number
  total_nodes?: number
  jaccard?: number
}

/** 详情（对齐 `PaperDetailSerializer`）。 */
export interface PaperDetail extends PaperBrief {
  compose_config: ComposeConfig
  compose_rationale: Array<Record<string, unknown>>
  knowledge_coverage: KnowledgeCoverage
  layout_config: Record<string, unknown>
  random_seed: number
  source_paper_id: number | null
  items: PaperItem[]
}

export interface FinalizeIssue {
  code: string
  detail: string
  question_id: number | null
  blocking: boolean
}

export interface FinalizeCheck {
  can_finalize: boolean
  summary: string
  /** ★ 阻断项与告警**分开**返回，前端不要合并 */
  blocking: FinalizeIssue[]
  warnings: FinalizeIssue[]
}

export interface CandidateItem {
  id: number
  uuid: string
  type: string
  subject: string
  grade_band: string
  stem_preview: string
  status: string
  source_type: string
  /** ★ 字符串 */
  difficulty_effective: string
  difficulty_band: string
  difficulty_source: string
  has_figure: boolean
  figure_count: number
  answer_count: number
  correct_rate: number | null
  task_id: number | null
  task_name: string
  created_at: string
}

export interface ComposeResult {
  paper_id: number
  seed: number
  item_count: number
  total_score: number
  difficulty_mean: number
  sections: Array<{
    type: string
    title: string
    need: number
    got: number
    score_each: number
    degrade_level: number
    effective_difficulty_range: [number, number]
  }>
  warnings: Array<{
    type: string
    section_title: string
    degrade_level: number
    effective_difficulty_range: [number, number]
    message: string
  }>
  gaps: Array<{
    type: string
    section_title: string
    need: number
    got: number
    short: number
    degrade_level: number
  }>
  candidate_truncated: boolean
  paper?: PaperDetail
}

// ══════════════════════════════════════════════════
//  试卷
// ══════════════════════════════════════════════════

export async function listPapers(params: {
  page?: number
  pageSize?: number
  status?: string
  purpose?: string
  courseId?: number
  onlyFinalized?: boolean
} = {}): Promise<Paged<PaperBrief>> {
  return http.get<Paged<PaperBrief>>('/api/v1/papers/', {
    page: params.page,
    page_size: params.pageSize,
    status: params.status,
    purpose: params.purpose,
    course_id: params.courseId,
    only_finalized: params.onlyFinalized ? 'true' : undefined,
  })
}

export async function getPaper(id: number): Promise<PaperDetail> {
  return http.get<PaperDetail>(`/api/v1/papers/${id}/`)
}

export async function createPaper(body: {
  course_id: number
  title: string
  purpose?: PaperPurpose
  compose_mode?: ComposeMode
  compose_config?: ComposeConfig
  difficulty_expect?: number | null
  /** ★ 可钉死：TC-PAP-107 要求同 seed 可复现 */
  random_seed?: number | null
  layout_config?: Record<string, unknown>
}): Promise<PaperDetail> {
  return http.post<PaperDetail>('/api/v1/papers/', body)
}

export async function updatePaper(
  id: number,
  body: Record<string, unknown>,
): Promise<PaperDetail> {
  return http.patch<PaperDetail>(`/api/v1/papers/${id}/`, body)
}

/** 删除（软删）。★ 只有 DRAFT 能删；定稿后返回 400。 */
export async function deletePaper(id: number): Promise<{ id: number }> {
  return http.del<{ id: number }>(`/api/v1/papers/${id}/`)
}

/**
 * 派生 AB 卷 / 补考卷。
 *
 * ★ 新卷是 DRAFT，且默认 `share_group=true`（与源卷同组），
 *   这样后续能做 AB 卷等值校验——等值的前提是「两份卷可比」，
 *   而可比性靠 `paper_group_id` 串起来。
 */
export async function duplicatePaper(
  id: number,
  body: { variant_label?: string; title?: string; share_group?: boolean } = {},
): Promise<PaperDetail> {
  return http.post<PaperDetail>(`/api/v1/papers/${id}/duplicate/`, body)
}

// ══════════════════════════════════════════════════
//  规则组卷
// ══════════════════════════════════════════════════

/**
 * 按规则自动组卷。
 *
 * ★★ 这个调用**可能返回「业务分支」而不是成功**：
 *    候选不足时后端返回 HTTP 200 + `code=PAPER.CANDIDATE_INSUFFICIENT`
 *    + 缺口清单，`http.request` 会把它抛成 `BusinessBranch`。
 *
 *    调用方**必须**用 `instanceof BusinessBranch` 分支处理，
 *    而不是当成失败弹红框——那是一条需要老师决策的正常路径
 *    （「少要几道还是换知识点」）。
 */
export async function composeByRule(
  paperId: number,
  body: { candidate_limit?: number; allow_partial?: boolean } = {},
): Promise<ComposeResult> {
  return http.post<ComposeResult>(`/api/v1/papers/${paperId}/compose/`, body)
}

// ══════════════════════════════════════════════════
//  卷面题增删
// ══════════════════════════════════════════════════

/** 批量加题。★ 返回的是**整张试卷详情**（含全部卷面题）。 */
export async function addItems(
  paperId: number,
  items: Array<{ question_id: number; score: number; section_title?: string }>,
  sectionTitle = '',
): Promise<PaperDetail> {
  return http.post<PaperDetail>(`/api/v1/papers/${paperId}/items/`, {
    items,
    section_title: sectionTitle,
  })
}

/** 删一道卷面题。★ 传的是 **paper_item id**，不是 question_id。 */
export async function removeItem(
  paperId: number,
  itemId: number,
): Promise<PaperDetail> {
  return http.del<PaperDetail>(`/api/v1/papers/${paperId}/items/`, {
    item_id: itemId,
  })
}

// ══════════════════════════════════════════════════
//  定稿
// ══════════════════════════════════════════════════

export async function finalizeCheck(paperId: number): Promise<FinalizeCheck> {
  return http.get<FinalizeCheck>(`/api/v1/papers/${paperId}/finalize-check/`)
}

export async function finalizePaper(
  paperId: number,
  body: { force?: boolean; reason?: string } = {},
): Promise<PaperDetail> {
  return http.post<PaperDetail>(`/api/v1/papers/${paperId}/finalize/`, body)
}

// ══════════════════════════════════════════════════
//  ★ 发布前覆盖体检（21 文档任务六）
// ══════════════════════════════════════════════════
//
//  ★★ 与 `finalizeCheck` 是**两套不同的门**，别合并成一个：
//
//      finalize-check  这张卷**本身完整吗**（配图、答案、配分、重复题）
//      coverage        这张卷**考点结构对不对**（覆盖、超纲、难度偏斜）
//
//  ★ 阻断项可填理由**强制发布**（`finalizePaper(force)` + `reason` 入审计）；
//    系统不替教研背「这张卷能不能考」的责任，但有义务讲清「为什么被拦」。

export interface CoverageIssue {
  /** 阻断：`B-` 开头；告警：`W-` 开头 */
  code: string
  message: string
  detail?: Record<string, unknown>
}

export interface CoverageNodeRow {
  node_id: number
  node_name: string
  /** 挂在该节点的题数 */
  count: number
  score: number
  /** 占全卷题数比例（0–1） */
  share: number
  /** 是否在 `compose_config.knowledge_nodes` 里（蓝图必需） */
  required: boolean
}

export interface CoverageReport {
  paper_id: number
  title: string
  item_count: number
  total_score: number
  target_total_score: number | null
  /** ★ 唯一判据：只看阻断项，告警不拦发布 */
  can_publish: boolean
  blocking: CoverageIssue[]
  warnings: CoverageIssue[]
  knowledge: {
    nodes: CoverageNodeRow[]
    node_count: number
    /** 一道考点都没挂的题号 */
    untagged_seqs: number[]
    untagged_count: number
    required_node_ids: number[]
    missing_required: number[]
    /** 超出蓝图配额的节点 */
    over_quota: Array<{ node_id: number; name: string; count: number
                        quota: number }>
    out_of_syllabus: Array<{ node_id: number; name: string }>
    note: string
  }
  difficulty: {
    buckets: Array<{ band: string; count: number; score: number
                     ratio: number; target_ratio: number; drift: number }>
    target: Record<string, number>
    max_drift: number
    tolerance: number
    skewed: boolean
  }
  structure: {
    rows: Array<{ type: string; count: number; score: number }>
    expected: Record<string, { count: number; score: number }>
    mismatched: Array<{ type: string; expected_count: number
                        actual_count: number; message: string }>
    has_config: boolean
  }
  /** `kind`: EXACT（题干哈希相同）/ NEAR（文本相似度 ≥ 0.90） */
  duplicates: Array<{ kind: 'EXACT' | 'NEAR'; seqs: number[]
                      similarity: number; stem_hash?: string }>
  /** 给发布页直接用的一句话 */
  summary: string
}

/** 发布前覆盖体检（只读，不改任何数据）。 */
export async function getPaperCoverage(
  paperId: number,
): Promise<CoverageReport> {
  return http.get<CoverageReport>(`/api/v1/papers/${paperId}/coverage/`)
}

// ══════════════════════════════════════════════════
//  排版与导出（`P1-60` / `P1-60b` / `P1-62b`）
// ══════════════════════════════════════════════════

// ══════════════════════════════════════════════════
//  配分建议与期望总分（`P1-58b`）
// ══════════════════════════════════════════════════
export interface AllocationRow {
  type: string
  count: number
  weight: number
  /** 建议的**单位分值**（每题多少分） */
  suggested_score: string
  /** 该题型合计 */
  subtotal: string
  /** 卷面现有单位分值（可能为空） */
  current_score: string | null
}

export interface SectionInfo {
  section_title: string
  type: string
  count: number
  current_score: number | null
  current_subtotal: number
}

export interface AllocationPlan {
  paper_id: number
  /** ★ `true` 表示还没填期望总分，填了才能反推配分 */
  needs_target: boolean
  target_total: string | null
  /** 卷面**实算**总分 */
  actual_total: string
  suggested_total?: string
  /** 与目标分的差额（正 = 还差；负 = 超了） */
  shortfall?: string
  granularity?: string
  ok?: boolean
  messages?: string[]
  sections: SectionInfo[]
  allocations?: AllocationRow[]
  /**
   * ★★ 逐题建议：`{卷面题号: 建议分值}`。
   *
   *   前端「一键按建议写入」直接拿它调 `applyAllocation()` ——
   *   **不用自己把题型映射到题号**（那样等于同一份映射算两遍，
   *   两边迟早不一致）。
   */
  expanded?: Record<string, string>
  /** 展开后逐题建议的合计（自检用：应与 `suggested_total` 一致） */
  suggested_total_from_expanded?: string
}

/** 取配分建议（只读，不改卷面）。 */
export async function getScoreAllocation(
  paperId: number, targetTotal?: number | string,
): Promise<AllocationPlan> {
  return http.get<AllocationPlan>(
    `/api/v1/papers/${paperId}/score-allocation/`,
    targetTotal === undefined || targetTotal === ''
      ? {} : { target_total: targetTotal },
  )
}

/** 设/清「期望总分」。★ 传 `null` 表示清空（取消校验）。 */
export async function setTargetTotal(
  paperId: number, target: number | string | null,
): Promise<{ target_total_score: string | null; total_score: string }> {
  return http.put(`/api/v1/papers/${paperId}/score-allocation/`, {
    target_total_score: target,
  })
}

/** 按建议写入配分。`scores` 是 `{卷面题号: 分值}`，键用 `seq`。 */
export async function applyAllocation(
  paperId: number, scores: Record<number | string, number | string>,
): Promise<{ changed: number; total_score: string }> {
  return http.post(`/api/v1/papers/${paperId}/score-allocation/apply/`, {
    scores,
  })
}

/**
 * ★ 从建议里取出「逐题分值」并直接写入。
 *
 *   ★★ 用后端给的 `expanded`（已按题号展开），**不自己按题型推题号**：
 *     同一份「题型 → 题号」映射算两遍，两边迟早不一致。
 */
export async function applyAllocationFromPlan(
  paperId: number, plan: AllocationPlan,
): Promise<{ changed: number; total_score: string }> {
  const expanded = plan.expanded ?? {}
  if (!Object.keys(expanded).length) {
    throw new Error('没有可写入的逐题建议（请先设置期望总分）')
  }
  return applyAllocation(paperId, expanded)
}



/** 空白手写区（`layout_config.blank_blocks[]`）。 */
export interface BlankBlock {
  id: string
  /** 0 = 卷首；N = 第 N 题之后 */
  after_seq: number
  height_mm: number
}

/** 排版参数（`Paper.layout_config` 契约 v3，见 `apps/paper/layout.py`）。 */
export interface PaperLayoutConfig {
  page_size?: 'A4' | 'A3' | 'A5' | 'B5'
  orientation?: 'PORTRAIT' | 'LANDSCAPE'
  margin_mm?: number
  line_spacing?: number
  base_font_pt?: number
  title_font_pt?: number
  /** 卷面标题；空则用 `paper.title`（★ 只改卷面标题，不改试卷名称） */
  title_text?: string
  show_title?: boolean
  /** 子集 `{学号,座号,班级,姓名,得分}`，默认 学号/班级/姓名 */
  student_fields?: string[]
  /** 接在考生信息下方（≤60 字，单独一行） */
  student_line_extra?: string
  /** 注意事项，`\n` 分段（≤1000 字） */
  notes_text?: string
  show_notes?: boolean
  show_page_number?: boolean
  page_number_align?: 'center' | 'right'
  /** 选择题/判断题题号前加 `（　　）` */
  answer_bracket?: boolean
  /** 印分组汇总（题数与总分） */
  show_score?: boolean
  /**
   * 选项字号（pt）。`null` = 跟随正文（`base_font_pt − 1.5`，下限 6）。
   * 6–24，步长 0.5。
   */
  option_font_pt?: number | null
  /**
   * ★ 逐题选项排列：`{卷面题号: 4|2|1}`。
   *
   *   4 = 一行四列：A B C D
   *   2 = 两行两列：A B / C D
   *   1 = 四行一列：A / B / C / D
   *
   * ★ 这是「一道题的四个选项怎么摆」，不是「一行放几道题」。
   */
  option_cols_by_seq?: Record<string, 4 | 2 | 1>
  blank_blocks?: BlankBlock[]
}

/** 一块配图（`kind === 'figure'`，或主观题块内嵌的图）。 */
export interface LayoutFigure {
  image_id?: number | null
  asset_id?: number | null
  attach_point?: string
  x_mm?: number
  y_mm?: number
  width_mm?: number
  image_h_mm?: number
  height_mm?: number
  caption?: string
  caption_lines?: string[]
  caption_line_h_mm?: number
  missing?: boolean
}

/** 选项单元（`option_cells[]`）。 */
export interface LayoutOptionCell {
  label: string
  text: string
  /** 已按列宽折好行，预览直接画 */
  lines: string[]
  row: number
  col: number
  x_mm: number
  y_mm: number
  width_mm: number
}

/** 排版结果里的一块。★ 前端**照这个画**，不自己算几何。 */
export interface LayoutBlock {
  kind: 'stem' | 'q_stems' | 'choice_row' | 'subjective' | 'blank' | 'figure'
  y_mm: number
  height_mm: number
  seq: number | null
  /** `kind === 'stem'` 时的分节标题原文 */
  text: string
  /** `kind === 'stem'` 时折好行的标题 + 汇总 */
  title_lines: string[]
  /** 题干/主观题正文：已折好行 */
  stem_lines: string[]
  line_h_mm: number
  /** 主观题题号（单独一行） */
  seq_label: string
  /** 选项块：4 / 2 / 1 */
  option_cols: number
  option_w_mm: number
  option_font_pt: number
  option_row_heights: number[]
  option_cells: LayoutOptionCell[]
  force_letter: boolean
  // figure
  asset_id?: number | null
  image_id?: number | null
  attach_point?: string
  x_mm: number
  width_mm: number
  image_h_mm: number
  caption: string
  caption_lines: string[]
  caption_line_h_mm: number
  missing: boolean
  // blank
  blank_id: string
  after_seq: number
}

export interface LayoutPageNumber {
  text: string
  y_mm: number
  align: 'center' | 'right'
}

export interface LayoutHeaderLine {
  text: string
  lines: string[]
  y_mm: number
  font_pt: number
  line_h_mm: number
}

export interface LayoutStudentItem {
  label: string
  name: string
  line_no: number
  x_mm: number
  label_w_mm: number
  underline_x_mm: number
  underline_w_mm: number
  y_mm: number
  seg_w_mm: number
}

export interface LayoutHeader {
  title: LayoutHeaderLine
  subtitle: { text: string; y_mm: number; font_pt: number }
  /** 考生信息：首项 x=0，末项下划线右端=可用宽 */
  student_items: LayoutStudentItem[]
  student_lines: number
  student_extra: { text: string; y_mm: number; font_pt: number } | null
  notes: {
    lines: string[]
    y_mm: number
    font_pt: number
    line_h_mm: number
  }
  rules: number[]
  height_mm: number
}

export interface LayoutPage {
  page_no: number
  used_mm: number
  page_number: LayoutPageNumber | null
  blocks: LayoutBlock[]
}

export interface LayoutIssue {
  code: string
  message: string
  blocking: boolean
}

export interface PaperLayoutResult {
  config: PaperLayoutConfig
  page_width_mm: number
  page_height_mm: number
  margin_mm: number
  usable_width_mm: number
  usable_height_mm: number
  header_height_mm: number
  header: LayoutHeader
  page_count: number
  figure_gap_mm: number
  issues: LayoutIssue[]
  pages: LayoutPage[]
  title?: string
  subtitle?: string
}

/**
 * 取**排版结果 JSON**（POST；预览 / PDF / Word 共用同一份）。
 *
 * ★★ 前端预览**照它画** —— 不自己算几何、不估算分页/行高，
 *   否则「预览」与「导出」会各算一套，两边迟早不一致。
 */
export async function previewPaperLayout(
  paperId: number,
  layoutConfig: PaperLayoutConfig = {},
  opts: { signal?: AbortSignal } = {},
): Promise<PaperLayoutResult> {
  return http.post<PaperLayoutResult>(
    `/api/v1/papers/${paperId}/export/`,
    {
      format: 'layout',
      layout_config: layoutConfig,
    },
    { signal: opts.signal },
  )
}

/**
 * 保存版式参数（PATCH）。★ 后端会先 `normalize_config`：
 * 非法枚举 400，数值夹区间，未知键丢弃；定稿卷也允许保存。
 */
export async function saveLayoutConfig(
  paperId: number,
  cfg: PaperLayoutConfig,
): Promise<PaperDetail> {
  return http.patch<PaperDetail>(`/api/v1/papers/${paperId}/`, {
    layout_config: cfg,
  })
}

/**
 * 导出试卷文件（PDF / Word）并触发下载。
 *
 * ★★ 必须走 `fetchBlob`（带 Authorization 头）**再**造 `blob:` 链接。
 * ★ 现在是 **POST**：结构化参数（`option_cols_by_seq` / `blank_blocks`）
 *   无法放进 query，只能走 body。
 */
export async function exportPaper(
  paperId: number,
  fmt: 'pdf' | 'docx',
  opts: { layout?: PaperLayoutConfig } = {},
): Promise<void> {
  const blob = await fetchBlob(`/api/v1/papers/${paperId}/export/`, {
    method: 'POST',
    body: {
      format: fmt,
      layout_config: opts.layout ?? {},
    },
  })
  downloadBlob(blob, `试卷-${paperId}.${fmt}`)
}

/**
 * 导出**独立答案 PDF**（只印答案，不含题干/解析）。
 *
 * ★ 与试卷 PDF 分开：不是「带答案版」，而是单独的答案页。
 */
export async function exportAnswerPdf(
  paperId: number,
  layout: PaperLayoutConfig = {},
): Promise<void> {
  const blob = await fetchBlob(`/api/v1/papers/${paperId}/export/answer/`, {
    method: 'POST',
    body: { layout_config: layout },
  })
  downloadBlob(blob, `试卷-${paperId}-答案.pdf`)
}

/** 取配图字节（认证 + 本机构校验）。前端再转 `URL.createObjectURL`。 */
export async function getAssetBlob(assetId: number): Promise<Blob> {
  return fetchBlob(`/api/v1/media/assets/${assetId}/content/`)
}

// ══════════════════════════════════════════════════
//  候选池
// ══════════════════════════════════════════════════

export interface CandidateQuery {
  paperId?: number
  courseId?: number
  taskIds?: number[]
  nodeIds?: number[]
  types?: string[]
  difficultyBands?: string[]
  page?: number
  pageSize?: number
}

export async function listCandidates(
  q: CandidateQuery = {},
): Promise<Paged<CandidateItem>> {
  return http.get<Paged<CandidateItem>>('/api/v1/papers/candidates/', {
    paper_id: q.paperId,
    course_id: q.courseId,
    task_id: q.taskIds?.length ? q.taskIds.join(',') : undefined,
    node_id: q.nodeIds?.length ? q.nodeIds.join(',') : undefined,
    type: q.types?.length ? q.types.join(',') : undefined,
    difficulty_band: q.difficultyBands?.length
      ? q.difficultyBands.join(',')
      : undefined,
    page: q.page,
    page_size: q.pageSize,
  })
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

export const QUESTION_TYPE_LABEL: Record<string, string> = {
  SINGLE_CHOICE: '单项选择',
  MULTI_CHOICE: '多项选择',
  TRUE_FALSE: '判断题',
  FILL_BLANK: '填空题',
  SHORT_ANSWER: '简答题',
  CALCULATION: '计算题',
  DRAWING: '作图题',
  ESSAY: '论述题',
}

export const PAPER_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  FINALIZED: '已定稿',
  ARCHIVED: '已归档',
}

export const PAPER_STATUS_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  DRAFT: 'info',
  FINALIZED: 'success',
  ARCHIVED: 'warning',
}

export const DIFFICULTY_BAND_LABEL: Record<string, string> = {
  EASY: '易',
  MEDIUM: '中',
  HARD: '难',
}

export function typeLabel(t: string): string {
  return QUESTION_TYPE_LABEL[t] ?? t
}

/**
 * 阻断项/告警码 → 中文说明。
 *
 * ★ 后端**刻意不返回 `message`**（见 `_finalize_payload` 的注释）：
 *   展示文案交给前端，这样调整文案不用改后端。
 *   所以这里必须把码表补全，否则老师看到的是 `B-03` 这种天书。
 */
export const FINALIZE_CODE_LABEL: Record<string, string> = {
  'B-01': '试卷必须有至少一道题',
  'B-02': '存在未审核（非 APPROVED）的题目',
  'B-03': '存在配图未确认的题目',
  'B-04': '题干提到「如图」但实际无配图',
  'B-05': '同一道题在卷内重复出现',
  'B-06': '卷面配分合计与期望不符',
  'W-01': '存在难度分布偏斜',
  'W-02': '存在知识点覆盖缺口',
  'W-03': '知识点覆盖率偏低',
  'W-04': '卷面题量偏少',
}

export function finalizeCodeLabel(code: string): string {
  return FINALIZE_CODE_LABEL[code] ?? code
}
