/**
 * 学情 API（掌握度、热力图、错题、错因）。
 *
 * ★★ 这一层最容易犯的错：**把 `mastery` 直接当颜色用**。
 *
 *     `mastery` 低**不代表**这个知识点没掌握——可能只是**样本不足**。
 *     所以每个格子/条目都同时带 `confidence`，前端必须：
 *
 *        confidence 够  →  按 mastery 上色（这是结论）
 *        confidence 不足 →  显示灰色「数据不足」（这不是结论）
 *
 *     只按 mastery 上色会让「只考过 1 次答错」的知识点显示成深红色，
 *     而老师会以为全班这个知识点都很差 —— 设计文档 11.2 明确警告过。
 *
 *   `weak-nodes` 端点已经把这个判断做成了 `verdict`：
 *     `CRITICAL`  → 置信度足够且掌握度低 → **要补练**
 *     `SUSPECTED` → 掌握度低但置信度不足 → **要再测**
 */

import { fetchBlob, http } from './http'

// ══════════════════════════════════════════════════
//  掌握度
// ══════════════════════════════════════════════════

export interface MasteryItem {
  node_id: number
  node_name: string
  node_code: string
  mastery: number
  confidence: number
  effective_samples: number
  answered_count: number
  correct_rate: number | null
}

export interface MasteryResult {
  student_id: number
  /**
   * ★ 掌握度算法版本。
   *   换算法后新旧值**不可比**，前端要显示版本号，
   *   否则老师会拿两个口径的数据做对比。
   */
  calc_version: string | null
  count: number
  items: MasteryItem[]
}

export type WeakVerdict = 'CRITICAL' | 'SUSPECTED' | 'NONE'

export interface WeakNode {
  node_id: number
  node_name: string
  node_code: string
  mastery: number
  confidence: number
  effective_samples: number
  answered_count: number
  correct_rate: number | null
  verdict: WeakVerdict
  calculated_at: string
}

export interface WeakNodesResult {
  student_id: number
  count: number
  critical: number
  suspected: number
  items: WeakNode[]
}

export interface TrendPoint {
  snapshot_at: string
  trigger: string
  weak_nodes: unknown[]
  total_score: string | null
  class_avg: string | null
  mastery_map: Record<string, number>
}

export interface TrendResult {
  student_id: number
  count: number
  points: TrendPoint[]
}

// ══════════════════════════════════════════════════
//  热力图
// ══════════════════════════════════════════════════

export interface HeatmapNode {
  id: number
  name: string
  code: string
  exam_weight: number
}

export interface HeatmapCell {
  node_id: number
  student_id: number
  mastery: number
  /** ★ 必须与 mastery 一起用：低置信度的格子不该上色 */
  confidence: number
  answered_count: number
}

export interface HeatmapNodeSummary {
  node_id: number
  name: string
  code: string
  /** ★ 置信度达标的样本均值；样本为空时是 null（不是 0） */
  avg_mastery: number | null
  sample_students: number
  confident: boolean
}

export interface HeatmapStudent {
  student_id: number
  student_no: string
  student_name: string
}

export interface HeatmapResult {
  /**
   * ⚠ 遗留：只有一串学生 id。
   *
   * ★ 用 `student_list`（带学号与姓名）——按班级查热力图时，
   *   名单里可能有**不在本次任务名单**里的学生，前端拿任务名单
   *   映射姓名会显示成 `#123`（2026-10-17 修）。
   */
  students: number[]
  /** ★ 学生身份：`[{student_id, student_no, student_name}]`（顺序与 `students` 一致） */
  student_list?: HeatmapStudent[]
  nodes: HeatmapNode[]
  cells: HeatmapCell[]
  node_summary: HeatmapNodeSummary[]
  min_confidence: number
}

// ══════════════════════════════════════════════════
//  试卷测量指标（难度 / 区分度 / 信度 / 内容效度）
// ══════════════════════════════════════════════════

export interface MeasurementItem {
  paper_item_id: number
  seq: number
  question_type: string
  max_score: number
  /** 难度 = 平均得分率（0–1，**越大越易**） */
  difficulty: number | null
  difficulty_band: string | null
  /** ★ 中文档位（易/较易/中等/较难/难）——后端给，前端不重算 */
  difficulty_label: string | null
  /** 区分度 = 高分组 − 低分组（≥0.4 很好 … <0.2 差；负值提示题目或答案有问题） */
  discrimination: number | null
  discrimination_band: string | null
  discrimination_label: string | null
  discrimination_high: number | null
  discrimination_low: number | null
  /** 样本不足时说明原因（此时 `discrimination` 为 null） */
  discrimination_reason: string
}

export interface MeasurementReport {
  assignment_id?: number
  student_count: number
  item_count: number
  full_score: number
  difficulty: {
    value: number | null
    band: string | null
    label: string | null
    note?: string
  }
  reliability: {
    value: number | null
    band: string | null
    label: string | null
    item_count?: number
    student_count?: number
    reason?: string
    note?: string
  }
  validity: {
    covered_node_count?: number
    total_node_count?: number
    /** 考点覆盖率（卷面知识点 / 课程知识点） */
    coverage: number | null
    /** 逐题挂点率（挂到知识点的题 / 总题数） */
    item_binding_rate: number | null
    items_with_nodes?: number
    item_count?: number
    uncovered_node_count?: number
    note?: string
  }
  discrimination_summary: {
    avg: number | null
    count: number
    /** ★ 「没有区分能力」（D < 0.2）的题数——这类题白占卷面 */
    poor_count: number
  }
  items: MeasurementItem[]
  reason?: string
}

/** 试卷测量指标（难度 / 区分度 / 信度 / 内容效度）。 */
export async function getMeasurement(
  assignmentId: number,
): Promise<MeasurementReport> {
  return http.get<MeasurementReport>(
    `/api/v1/analytics/assignments/${assignmentId}/measurement/`,
  )
}

// ══════════════════════════════════════════════════
//  错题 / 错因 / 分布
// ══════════════════════════════════════════════════

export interface QuestionError {
  question_id: number
  answered_count: number
  wrong_count: number
  blank_count: number
  correct_rate: number
  blank_rate: number
  avg_score: number
  /** ★ 选项分布是诊断干扰项的依据：`{"B": 18}` */
  option_distribution?: Record<string, number>
}

export interface ErrorTagRow {
  knowledge_node_id: number | null
  error_tag: string
  count: number
  ratio: number
}

export interface Distribution {
  count: number
  avg: number
  scores: number[]
}

// ══════════════════════════════════════════════════
//  请求
// ══════════════════════════════════════════════════

export async function getStudentMastery(
  studentId: number,
): Promise<MasteryResult> {
  return http.get<MasteryResult>(`/api/v1/analytics/students/${studentId}/mastery/`)
}

export async function getWeakNodes(
  studentId: number,
  limit?: number,
): Promise<WeakNodesResult> {
  return http.get<WeakNodesResult>(
    `/api/v1/analytics/students/${studentId}/weak-nodes/`,
    { limit },
  )
}

export async function getMasteryTrend(
  studentId: number,
  limit = 90,
): Promise<TrendResult> {
  return http.get<TrendResult>(
    `/api/v1/analytics/students/${studentId}/mastery-trend/`,
    { limit },
  )
}

/**
 * 班级热力图。
 *
 * ★ **必须**给 `studentIds` 或 `classroomId` 之一，否则后端返回空矩阵
 *   （消息是「未指定学生或班级」）。实测踩到过：传 `assignment_id`
 *   会得到一个不报错的空结果。
 */
export async function getHeatmap(q: {
  studentIds?: number[]
  classroomId?: number
}): Promise<HeatmapResult> {
  return http.get<HeatmapResult>('/api/v1/analytics/heatmap/', {
    student_ids: q.studentIds?.length ? q.studentIds.join(',') : undefined,
    classroom_id: q.classroomId,
  })
}

export async function getQuestionErrors(q: {
  assignmentId?: number
  minAnswered?: number
  limit?: number
} = {}): Promise<QuestionError[]> {
  const d = await http.get<{ items: QuestionError[] }>(
    '/api/v1/analytics/question-errors/',
    {
      assignment_id: q.assignmentId,
      // ★ 默认 1 而不是 0：`min_answered=0` 会把「没人答」的题也算进来，
      //   而「只有 1 个人做，他错了」也不是高频错题。前端默认给 5。
      min_answered: q.minAnswered ?? 5,
      limit: q.limit ?? 50,
    },
  )
  return d.items ?? []
}

export async function getErrorTags(q: {
  assignmentId?: number
} = {}): Promise<ErrorTagRow[]> {
  const d = await http.get<{ items: ErrorTagRow[] }>(
    '/api/v1/analytics/error-tags/',
    { assignment_id: q.assignmentId },
  )
  return d.items ?? []
}

export async function getDistribution(
  assignmentId: number,
): Promise<Distribution> {
  return http.get<Distribution>(
    `/api/v1/analytics/assignments/${assignmentId}/distribution/`,
  )
}

// ══════════════════════════════════════════════════
//  ★ 题目分析报表（21 文档任务四）
// ══════════════════════════════════════════════════
//
//  ★★ 与 `measurement` 的分工，别混用：
//
//      measurement  整卷视角：这张卷难不难、信不信、效度高不高
//      items        逐题视角：第 11 题 p=0.42 / D=0.31，选项 C 没人选
//
//  ★★ `sample_sufficient=false` 时**不要**给「好/差」判定：
//     3 个人做对的题算出来的区分度是噪声，标红会误导教研。

export interface ItemStatRow {
  paper_item_id: number
  seq: number
  question_type: string
  section_title: string
  max_score: number
  /** 有本题成绩的人数 */
  n: number
  mean: number
  /** 难度 = 平均得分率（0–1，**越大越易**） */
  p: number | null
  p_band?: string
  p_label: string
  /** 区分度 D（高分组 − 低分组，0–1，可为负） */
  d: number | null
  d_band?: string
  d_label: string
  d_high?: number | null
  d_low?: number | null
  /** 样本不足时说明「为什么没有 D」 */
  d_reason?: string
  /** 点二列相关（本题得分 vs 总分） */
  point_biserial: number | null
  /** 选择题选项分布 `{"A": 18, "B": 3}` */
  option_counts: Record<string, number>
  /** ★ 零选择干扰项：有选项却没人选 —— 该选项形同虚设 */
  zero_choice_options: string[]
  sample_sufficient: boolean
  /** 就地提示（太难/太易/区分度差/样本不足） */
  warnings: string[]
}

export interface ItemAnalysisSummary {
  item_count: number
  graded_items?: number
  student_count?: number
  /** 样本门槛（默认 30） */
  min_samples: number
  sample_sufficient?: boolean
  /** 样本不足时的整卷提示（**空串**表示没问题） */
  warning?: string
  /** 读的是快照而不是现算（`refresh=1` 时为 false/缺省） */
  cached?: boolean
}

export interface ItemAnalysisResult {
  assignment_id: number
  title: string
  items: ItemStatRow[]
  summary: ItemAnalysisSummary
}

/**
 * 逐题指标（难度 / 区分度 / 点二列 / 选项分布）。
 *
 * @param refresh `true` 时**现算并落库**；默认读上一次的快照
 *   （每次打开学情页重算 25 题 × 45 人，老师会等到放弃）。
 *   ★ 首次访问（无快照）后端会自动算一次。
 */
export async function getItemAnalysis(
  assignmentId: number,
  refresh = false,
): Promise<ItemAnalysisResult> {
  return http.get<ItemAnalysisResult>(
    `/api/v1/analytics/assignments/${assignmentId}/items/`,
    { refresh: refresh ? 1 : undefined },
  )
}

/** 逐题指标导出 CSV（★ 带 UTF-8 BOM，Excel 双击不乱码）。 */
export async function exportItemAnalysis(
  assignmentId: number,
): Promise<Blob> {
  return fetchBlob(
    `/api/v1/analytics/assignments/${assignmentId}/items/export/`,
  )
}

/** 题目历史校准（同一道题考多次的 p / D 均值）。 */
export interface QuestionStat {
  question_id: number
  /** ★ 达到样本门槛的场次数；为 0 时所有指标为 null（不是 0） */
  exam_count: number
  total_n: number
  p_avg: number | null
  d_avg: number | null
  last_assignment_id: number | null
  note?: string
}

export async function getQuestionStat(
  questionId: number,
): Promise<QuestionStat> {
  return http.get<QuestionStat>(
    `/api/v1/analytics/questions/${questionId}/stats/`,
  )
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

/** `CRITICAL` 要补练，`SUSPECTED` 要再测——两者处置不同。 */
export const VERDICT_LABEL: Record<string, string> = {
  CRITICAL: '要补练',
  SUSPECTED: '要再测',
  NONE: '正常',
}

export const VERDICT_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  CRITICAL: 'danger',
  SUSPECTED: 'warning',
  NONE: 'success',
}

/**
 * 掌握度 → 颜色档位。
 *
 * ★★ `confidence` 不足时**必须**返回 `unknown`，不能返回一个颜色。
 *   这是这个函数存在的全部理由：把「数据不足」和「掌握度低」
 *   在视觉上区分开。
 */
export function masteryBand(
  mastery: number,
  confidence: number,
  minConfidence: number,
): 'unknown' | 'bad' | 'weak' | 'mid' | 'good' {
  if (confidence < minConfidence) return 'unknown'
  if (mastery < 0.4) return 'bad'
  if (mastery < 0.6) return 'weak'
  if (mastery < 0.8) return 'mid'
  return 'good'
}

export const BAND_LABEL: Record<string, string> = {
  unknown: '数据不足',
  bad: '较差',
  weak: '偏弱',
  mid: '一般',
  good: '较好',
}

/**
 * 把分布分数切成 10 档直方图。
 *
 * ★ 用「满分」定档宽，不用 `max(scores)`——
 *   后者会把「全班最高 12 分（满分 100）」放大成满格，
 *   看起来像考得不错。
 */
export function histogram(
  scores: number[],
  fullScore: number,
  bins = 10,
): Array<{ label: string; from: number; to: number; count: number }> {
  const top = fullScore > 0 ? fullScore : Math.max(...scores, 1)
  const w = top / bins
  const out = Array.from({ length: bins }, (_, i) => ({
    label: `${Math.round(i * w)}-${Math.round((i + 1) * w)}`,
    from: i * w,
    to: (i + 1) * w,
    count: 0,
  }))
  for (const s of scores) {
    let i = Math.floor(s / w)
    if (i >= bins) i = bins - 1
    if (i < 0) i = 0
    out[i].count += 1
  }
  return out
}

/** 错题里最强的干扰项（选的人最多、且不是正确答案的那个）。 */
export function topDistractor(
  dist: Record<string, number> | undefined,
): { label: string; count: number } | null {
  if (!dist) return null
  const entries = Object.entries(dist).sort((a, b) => b[1] - a[1])
  if (!entries.length) return null
  return { label: entries[0][0], count: entries[0][1] }
}
