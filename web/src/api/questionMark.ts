/**
 * 难度快速打标 API（`P1-100b`）。
 *
 * ★★ 这一页的**唯一性能指标是「每题几次操作」**：
 *    标 1900 道题如果每题要 3 次点击，是不可接受的。
 *    所以后端把「打标 + 取下一题」合成一个请求（键盘流下每道题
 *    省一次往返），前端只负责按 `1`–`5`。
 */

import { http } from './http'

/** 打标页的题目（**含完整题干与选项**，不是 80 字摘要）。 */
export interface MarkQuestion {
  id: number
  type: string
  subject: string
  status: string
  stem_text: string
  answer: string
  options: Array<{ label: string; content_text: string; is_correct: boolean }>
  difficulty_manual: number | null
  difficulty_effective: string
  difficulty_band: string
  difficulty_source: string
  task_id: number | null
  task_name: string
  chapter_name: string
  has_figure: boolean
}

export interface MarkProgress {
  total: number
  marked: number
  remaining: number
}

export interface MarkQueue {
  items: MarkQuestion[]
  progress: MarkProgress
  only_unmarked: boolean
  has_more: boolean
}

export interface MarkResult {
  question_id: number
  difficulty_manual: number
  difficulty_effective: string
  difficulty_band: string
  difficulty_source: string
  /** ★★ 下一题（由打标接口**顺带返回**，前端不用再发一次请求）。 */
  next: MarkQuestion | null
  progress: MarkProgress
}

export interface MarkQueueQuery {
  onlyUnmarked?: boolean
  courseId?: number
  types?: string[]
  /** 从某题**之后**继续 —— 刷新后可续的实现方式 */
  afterId?: number
  limit?: number
}

/** 取待打标队列（含进度）。 */
export async function getMarkQueue(
  q: MarkQueueQuery = {},
): Promise<MarkQueue> {
  const query: Record<string, string | number> = {
    only_unmarked: q.onlyUnmarked === false ? '0' : '1',
    limit: q.limit ?? 20,
  }
  if (q.courseId) query.course_id = q.courseId
  if (q.types?.length) query.types = q.types.join(',')
  if (q.afterId) query.after_id = q.afterId
  return http.get<MarkQueue>('/api/v1/questions/mark-queue/', query)
}

/**
 * 打标。**返回里带下一题**。
 *
 * ★ 传 `afterId`（= 当前题 id）保证「下一题」不会又把刚标过的题给回来。
 */
export async function markDifficulty(
  questionId: number,
  difficulty: number,
  q: MarkQueueQuery = {},
): Promise<MarkResult> {
  const query: Record<string, string | number> = {
    only_unmarked: q.onlyUnmarked === false ? '0' : '1',
  }
  if (q.courseId) query.course_id = q.courseId
  return http.post<MarkResult>(
    `/api/v1/questions/${questionId}/mark-difficulty/`,
    { difficulty }, query,
  )
}

/** 难度等级的中文标签。 */
export const DIFFICULTY_LABELS: Record<number, string> = {
  1: '很易',
  2: '较易',
  3: '中等',
  4: '较难',
  5: '很难',
}

/** 难度等级的色调（1 绿 → 5 红）。 */
export const DIFFICULTY_TONE: Record<number, string> = {
  1: 'success',
  2: 'success',
  3: 'info',
  4: 'warning',
  5: 'danger',
}
