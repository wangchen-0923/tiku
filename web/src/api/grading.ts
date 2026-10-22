/**
 * 批改 API。
 *
 * ★★ 一个刻意的区分：`state` 与 `is_correct` 不是一回事。
 *
 *     `state`       评分记录的**处理状态**（AUTO_GRADED / NEEDS_REVIEW /
 *                   TEACHER_CONFIRMED / TEACHER_MODIFIED / VOID）
 *     `is_correct`  这道题**答对没有**
 *
 *   所以「已批改」不等于「已确认」：自动批改完是 `AUTO_GRADED`，
 *   而「机器不确定」的是 `NEEDS_REVIEW`——**必须逐条看**，
 *   不能批量确认（那等于把仲裁环节跳过）。
 */

import { fetchBlob, http } from './http'

/**
 * 题型中文名。
 *
 * ★ 从 `paper.ts` 复用，**不在这里重复定义**——
 *   两张映射表迟早会分叉，而分叉的表现是
 *   「同一个题型在试卷页和批改页叫不同名字」。
 */
export { QUESTION_TYPE_LABEL, typeLabel as questionTypeLabel } from './paper'

export type ScoreState =
  | 'PENDING'
  | 'AUTO_GRADED'
  | 'NEEDS_REVIEW'
  | 'MANUAL_ENTERED'
  | 'TEACHER_CONFIRMED'
  | 'TEACHER_MODIFIED'
  | 'VOID'

export type GradingState =
  | 'NOT_SUBMITTED'
  | 'PENDING_AUTO'
  | 'PENDING_MANUAL'
  | 'DONE'

export interface ScoreRow {
  id: number
  assignment_id: number
  student_id: number
  student_name: string
  paper_item_id: number
  /** 卷面题号 */
  seq: number
  section_title: string
  question_type: string
  response_id: number | null
  /** ★ Decimal 以字符串传输（避免浮点误差） */
  score: string
  max_score: string
  score_ratio: number
  is_correct: boolean | null
  method: string
  confidence: string
  state: ScoreState
  state_label?: string
  is_final: boolean
  score_points: Record<string, unknown>
  deduction_reasons: Array<{ detail?: string; reason?: string }>
  /** ★ 学生答了什么（已还原到原题字母；填空是逐空 JSON 串） */
  student_answer?: string
  /** ★ 标准答案文本（取自卷面快照） */
  correct_answer?: string
  graded_at: string | null
  confirmed_at: string | null
}

/**
 * 批改工作台的**学生名单**行（2026-10-17 新增）。
 *
 * ★ 老师的心智是「这次作业，谁交了、谁批了」——
 *   所以工作台的主视图是这张表，而不是一长串逐题评分。
 */
export interface GradingRosterRow {
  /** ★ 学生身份行 id —— 所有「按学生」的调用都用它 */
  student_id: number | null
  /** 名单行 id（单生详情接口用它） */
  row_id: number
  student_no: string
  student_name: string
  /** NOT_STARTED / IN_PROGRESS / SUBMITTED / SCANNED / ABSENT / PARTIAL */
  status: string
  status_label?: string
  submitted: boolean
  entered_at: string | null
  submitted_at: string | null
  item_count: number
  /** 该生来源下「机器能批」的题数（扫描不含填空） */
  auto_item_count: number
  /** 需要人工录分的题数（扫描含填空） */
  subjective_item_count: number
  manual_item_count?: number
  scored: number
  scored_auto: number
  scored_manual: number
  remaining_auto: number
  remaining_manual: number
  auto_graded: number
  needs_review: number
  confirmed: number
  /** 是否已批改完成（grading_state=DONE） */
  graded: boolean
  has_scores?: boolean
  /** 机器能批的部分是否批完 */
  auto_done: boolean
  grading_state: GradingState
  grading_state_label?: string
  total_score: string | null
  /** ★ 该生的扫描页（需求 7：批改页左侧原图对照） */
  scan_pages?: Array<{
    card_id: number
    page_no: number | null
    status: string
    status_label?: string
    raw_asset_id: number
    display_asset_id?: number
    source_image_asset_id: number | null
    group_key: string
  }>
  /** ★ 作答来源：扫描 / 线上 */
  answer_source?: 'SCAN' | 'ONLINE'
  scan_status?: string
  scan_status_label?: string
}

export interface GradingRoster {
  assignment_id: number
  title?: string
  item_count: number
  auto_item_count: number
  subjective_item_count: number
  counts: {
    total: number
    submitted: number
    not_submitted: number
    graded: number
    pending_grade: number
    pending_auto: number
    pending_manual: number
    needs_review: number
  }
  scan_auto_item_count?: number
  scan_manual_item_count?: number
  items: GradingRosterRow[]
}

/** 批改工作台的学生名单（交卷情况 + 批改进度）。 */
export async function getRoster(assignmentId: number): Promise<GradingRoster> {
  return http.get<GradingRoster>(
    `/api/v1/grading/assignments/${assignmentId}/roster/`,
  )
}

export interface GradingProgress {
  total: number
  by_state: Record<string, number>
  done: number
  needs_review: number
  /** 0–1 小数 */
  progress: number
}

export interface AutoGradeResult {
  graded: number
  skipped_subjective: number
  no_response: number
  needs_review: number
  failed: Array<{
    paper_item_id?: number
    seq?: number
    student_id?: number
    error: string
  }>
  failed_count?: number
  /**
   * ★★ 学情重算成功的人数。
   *
   *   「批改成功」不等于「学情算出来了」：重算是**派生数据**，
   *   它失败不影响分数，但会让学情页空白。
   *   所以必须单独报出来，否则老师只会以为「还没算」。
   */
  recompute_ok?: number
  recompute_failed?: Array<{ student_id: number; error: string }>
}

/** 批改 / 查询参数。 */
export interface ScoreQuery {
  assignmentId?: number
  studentId?: number
  paperItemId?: number
  state?: string
  limit?: number
  all?: boolean
}

export async function listScores(
  q: ScoreQuery = {},
): Promise<ScoreRow[]> {
  const d = await http.get<{ items: ScoreRow[] }>('/api/v1/grading/scores/', {
    assignment_id: q.assignmentId,
    student_id: q.studentId,
    paper_item_id: q.paperItemId,
    state: q.state,
    limit: q.limit ?? 500,
    all: q.all ? '1' : undefined,
  })
  return d.items ?? []
}

/** 单生全量逐题（含未批的主观题/填空题）。 */
export interface GradingStudentItem {
  paper_item_id: number
  seq: number
  section_title: string
  question_type: string
  question_type_label: string
  max_score: string
  score: string | null
  is_correct: boolean | null
  state: ScoreState | 'PENDING'
  state_label: string
  method: string
  confidence: string | null
  is_final: boolean
  student_answer: string
  correct_answer: string
  deduction_reasons: Array<{ detail?: string; reason?: string }>
  needs_manual: boolean
}

export async function getStudentItems(
  assignmentId: number, studentId: number,
): Promise<GradingStudentItem[]> {
  const d = await http.get<{ items: GradingStudentItem[] }>(
    `/api/v1/grading/assignments/${assignmentId}/students/${studentId}/items/`,
  )
  return d.items ?? []
}

export async function getProgress(
  assignmentId: number,
): Promise<GradingProgress> {
  return http.get<GradingProgress>(
    `/api/v1/grading/assignments/${assignmentId}/progress/`,
  )
}

/**
 * 逐生自动批改（**不是整班一次**）。
 *
 * ★ 后端签名要求 `student_ids`；不传则服务层决定范围。
 *   `allow_partial` + `partial_factor` 用于「部分给分」的判断题。
 */
export async function autoGrade(
  assignmentId: number,
  body: {
    student_ids?: number[]
    allow_partial?: boolean
    partial_factor?: number
    subject?: string
  } = {},
): Promise<AutoGradeResult> {
  return http.post<AutoGradeResult>(
    `/api/v1/grading/assignments/${assignmentId}/auto-grade/`,
    body,
  )
}

export interface ReviewQueue {
  assignment_id: number
  count: number
  needs_review_total: number
  truncated: boolean
  items: ScoreRow[]
}

/** 待人工确认队列。★ `NEEDS_REVIEW` 必须逐条看，不能批量确认。 */
export async function getReviewQueue(
  assignmentId: number,
  limit = 200,
): Promise<ReviewQueue> {
  return http.get<ReviewQueue>(
    `/api/v1/grading/assignments/${assignmentId}/review-queue/`,
    { limit },
  )
}

/** 确认单条评分。★ `modified=true` 表示「改分后确认」，会触发学情重算。 */
export async function confirmScore(
  scoreId: number,
  body: { modified?: boolean; reason?: string } = {},
): Promise<ScoreRow> {
  return http.post<ScoreRow>(`/api/v1/grading/scores/${scoreId}/confirm/`, body)
}

export async function voidScore(
  scoreId: number,
  reason = '',
): Promise<ScoreRow> {
  return http.post<ScoreRow>(`/api/v1/grading/scores/${scoreId}/void/`, {
    reason,
  })
}

/** 批量确认某任务下 `AUTO_GRADED` 的评分（**不含** `NEEDS_REVIEW`）。 */
export async function confirmClass(
  assignmentId: number,
  body: { reason?: string; max_count?: number } = {},
): Promise<{ confirmed: number; needs_review_remaining: number }> {
  return http.post(
    `/api/v1/grading/assignments/${assignmentId}/confirm-class/`,
    body,
  )
}

/** 人工录分（主观题：教师线下批改后录入）。 */
export async function manualScore(
  assignmentId: number,
  body: {
    student_id: number
    paper_item_id: number
    score: number
    reason?: string
  },
): Promise<ScoreRow> {
  return http.post<ScoreRow>(
    `/api/v1/grading/assignments/${assignmentId}/manual-score/`,
    body,
  )
}

// ══════════════════════════════════════════════════
//  ★ 主观题阅卷（21 文档任务一）
// ══════════════════════════════════════════════════
//
//  ★★ 「按题」与「按人」是**两条轴**，数据源不同：
//
//      roster/  → 按学生看（谁交了、谁批完了）      —— 工作台
//      review/  → 按题看（第 11 题批了几份、下一位是谁）—— 阅卷页
//
//  老师的真实动线是**按题**的：一次只批「第 11 题」，45 份连着看。
//  按人批则每题都要重新找位置，切图要重看 45 次。

/** 阅卷页左栏：一道主观题的批改进度。 */
export interface ReviewItemProgress {
  paper_item_id: number
  /** 卷面题号 */
  seq: number
  type: string
  type_label: string
  section_title: string
  stem_text: string
  /** Decimal 字符串 */
  max_score: string
  /** 作答需求提示（写几行 / 多少字 / 几空） */
  part_hint: Record<string, number>
  /** ★ 分母只含**已交卷**的人：未交卷不该让进度到不了 100% */
  total_students: number
  graded: number
  pending: number
}

export interface ReviewItemsResult {
  assignment_id: number
  items: ReviewItemProgress[]
  overall: { graded: number; total: number }
  pending: number
}

/** 某题某位学生的答题位置（切图矩形，单位 mm）。 */
export interface CropRect {
  x_mm?: number
  y_mm?: number
  w_mm?: number
  h_mm?: number
}

export interface ReviewStudentRow {
  student_id: number
  student_no: string
  student_name: string
  submitted_at: string | null
  /** ★ 第一段切图的资产 id；为 null 时会回退整页图 */
  crop_asset_id: number | null
  /** 切图在原页上的位置（mm）——「看整页」时按它高亮 */
  rect: CropRect
  page_no: number | null
  /** ★ 页面尺寸（mm）：把 `rect` 换成整页高亮百分比要用它，别假设 A4 */
  page_width_mm?: number
  page_height_mm?: number
  part_index: number
  /** 跨页作文的总段数；0 表示单段 */
  part_count: number
  /** ★ 多段（跨页）全部切图，前端按 part_index 竖向拼接 */
  crops: Array<{
    asset_id: number
    part_index: number
    page_no: number | null
    rect: CropRect
  }>
  has_crop: boolean
  score: string | null
  score_state: ScoreState | ''
  /** ★ 评语：后端存在 `deduction_reasons[0].reason`，不新增字段 */
  comment: string
  graded: boolean
}

export interface ReviewStudentsResult {
  items: ReviewStudentRow[]
  total: number
  page: number
  page_size: number
  paper_item_id: number
  seq: number
  max_score: string
  stem_text: string
  /** ★ 参考答案（快照优先，服务端已派生为纯文本；试题库未录入时为空串） */
  answer_text: string
  /** ★ 答案解析（同上；部分老卷子没有解析，为空串） */
  explanation_text: string
  graded: number
}

export interface ReviewScoreResult {
  score_id: number
  state: ScoreState
  score: string
  updated_at: string
  /** 该题最新进度（不是整卷） */
  item_progress: { graded: number; total: number }
  /**
   * ★ 下一位待批学生。
   *
   *   `null` 表示**这道题批完了** —— 阅卷页据此跳汇总页，
   *   而不是弹一个「没有下一位」的提示让老师自己找。
   */
  next_student: {
    student_id: number
    student_no: string
    student_name: string
  } | null
}

export interface ReviewSummary {
  assignment_id: number
  title: string
  items: ReviewItemProgress[]
  overall: { graded: number; total: number }
  /** 0–1 小数 */
  progress: number
  finished: boolean
  students: number
}

/** 阅卷页左栏：主观题清单 + 逐题进度。★ 只含主观题。 */
export async function getReviewItems(
  assignmentId: number,
): Promise<ReviewItemsResult> {
  return http.get<ReviewItemsResult>(
    `/api/v1/grading/assignments/${assignmentId}/review/items/`,
  )
}

/**
 * 某题的逐份学生清单。
 *
 * @param filter `pending` 只看未批（连着批时用），`all` 全部
 * @param ensureCrops ★ 默认 true：缺切图的会**现生成**（历史卡也能直接批）
 */
export async function getReviewStudents(
  assignmentId: number,
  paperItemId: number,
  q: { filter?: 'pending' | 'all'; page?: number; pageSize?: number
       ensureCrops?: boolean } = {},
): Promise<ReviewStudentsResult> {
  return http.get<ReviewStudentsResult>(
    `/api/v1/grading/assignments/${assignmentId}/review/items/`
    + `${paperItemId}/students/`,
    {
      filter: q.filter ?? 'all',
      page: q.page ?? 1,
      page_size: q.pageSize ?? 50,
      ensure_crops: q.ensureCrops === false ? 0 : 1,
    },
  )
}

/**
 * 保存一道题的分并返回下一位。
 *
 * ★ 越界（> 配分）抛 `ApiError`，`code = GRADING.SCORE_EXCEED_MAX`；
 *   重复给分视为**修改**（`TEACHER_MODIFIED`），不新增记录。
 */
export async function reviewScore(
  assignmentId: number,
  body: { student_id: number; paper_item_id: number; score: number
          reason?: string },
): Promise<ReviewScoreResult> {
  return http.post<ReviewScoreResult>(
    `/api/v1/grading/assignments/${assignmentId}/review/score/`,
    body,
  )
}

/** 整卷主观题完成情况（批完后的汇总页）。 */
export async function getReviewSummary(
  assignmentId: number,
): Promise<ReviewSummary> {
  return http.get<ReviewSummary>(
    `/api/v1/grading/assignments/${assignmentId}/review/summary/`,
  )
}

/**
 * 取切图**字节**（不是 URL）。
 *
 * ★★ 为什么不直接给 `<img src="/api/v1/...">`：
 *   资产接口要 `Authorization: Bearer`，而 `<img>` **带不上自定义头**。
 *   所以统一走后端中转 + `fetchBlob`。
 *
 * ★ 调用方**必须**在切换学生 / 离开页面时 `URL.revokeObjectURL`，
 *   否则 45 张切图会一直占着内存（实测切图 200–400KB/张）。
 *
 * 后端顺序：该题切图 → 该生整页校正图 → 原图。
 * 回退整页图时响应头带 `X-Zf-Fallback: PAGE`，前端据此决定
 * 「显示整页 + 高亮 rect」而不是「裁剪图」。
 */
export async function getReviewCrop(
  assignmentId: number,
  paperItemId: number,
  studentId: number,
  signal?: AbortSignal,
): Promise<Blob> {
  return fetchBlob(
    `/api/v1/grading/assignments/${assignmentId}/review/items/`
    + `${paperItemId}/crop/`,
    { query: { student_id: studentId }, signal },
  )
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

export const SCORE_STATE_LABEL: Record<string, string> = {
  PENDING: '待批改',
  AUTO_GRADED: '已自动批改',
  NEEDS_REVIEW: '待人工确认',
  MANUAL_ENTERED: '已人工录分',
  TEACHER_CONFIRMED: '教师已确认',
  TEACHER_MODIFIED: '教师已改分',
  VOID: '已作废',
}

/** 未知评分状态统一中文。 */
export function scoreStateLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return SCORE_STATE_LABEL[code] ?? `未知状态（${code}）`
}

/** 规格 1.1：批改状态。 */
export const GRADING_STATE_LABEL: Record<string, string> = {
  NOT_SUBMITTED: '未交卷',
  PENDING_AUTO: '待自动批改',
  PENDING_MANUAL: '待人工录分',
  DONE: '已批改',
}

export const GRADING_STATE_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  NOT_SUBMITTED: 'info',
  PENDING_AUTO: 'warning',
  PENDING_MANUAL: 'warning',
  DONE: 'success',
}

export function gradingStateLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return GRADING_STATE_LABEL[code] ?? `未知状态（${code}）`
}

export const SCORE_STATE_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  AUTO_GRADED: 'info',
  NEEDS_REVIEW: 'warning',
  TEACHER_CONFIRMED: 'success',
  TEACHER_MODIFIED: 'success',
  VOID: 'danger',
}

/** 一个 0–1 小数 → 百分数（★ 全站比例都是 0–1 小数）。 */
export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

export function scoreText(row: ScoreRow): string {
  return `${Number(row.score).toFixed(1)} / ${Number(row.max_score).toFixed(0)}`
}

/** 扣分理由的文字（后端两种键名都出现过）。 */
export function reasonText(r: { detail?: string; reason?: string }): string {
  return r.detail || r.reason || ''
}
