/**
 * 学生答题 API（H5）。
 *
 * ★★ 学生端与教师端是**两套完全不同的安全模型**，所以单独一个模块：
 *
 *     教师端   JWT（`Authorization: Bearer`）+ 角色权限
 *     学生端   URL 里的 `publish_token`（不可枚举）+ 绑定后签发的
 *              `X-Session-Token`
 *
 *   学生**没有账号**，他的身份由「学号 + 姓名」匹配名册后签发的
 *   会话令牌承载。所以这里所有请求都**不带 JWT**。
 *
 * ★★ `session_token` 绑定到 `(任务, 学生)`，**不能跨任务**。
 *   存 `sessionStorage` 而不是 `localStorage`：
 *   学生用同一台手机给两个人答题的情况很常见（同学的手机），
 *   `localStorage` 会让第二个人直接顶掉第一个人的会话。
 */

import { request } from './http'

const SESSION_PREFIX = 'zf.student.session.'

/** 按任务隔离地存会话令牌（跨任务不能复用）。 */
export const studentSession = {
  key(publishToken: string): string {
    return SESSION_PREFIX + publishToken
  },
  get(publishToken: string): string {
    return sessionStorage.getItem(this.key(publishToken)) ?? ''
  },
  set(publishToken: string, token: string): void {
    sessionStorage.setItem(this.key(publishToken), token)
  },
  clear(publishToken: string): void {
    sessionStorage.removeItem(this.key(publishToken))
  },
}

// ══════════════════════════════════════════════════
//  类型（**照后端 `_student_questions` 显式列举的字段**）
// ══════════════════════════════════════════════════

export interface StudentEntry {
  assignment_id: number
  title: string
  start_at: string
  end_at: string
  is_open: boolean
  allow_late: boolean
  need_bind: boolean
}

export interface BindResult {
  session_token: string
  student_id: number
  status: string
  has_submitted: boolean
}

export interface StudentOption {
  /**
   * **显示用**标签：与印在答题卡上的一致（判断题是 `T`/`F`，其余 `A`/`B`/…）。
   * ★ 只用于显示，**不要拿它作作答值**——`T` 不是位置字母，判分侧会判越界。
   */
  sheet_label: string
  /**
   * **作答回传用**标签：卷面位置字母（`A`/`B`/`C`…，第 i 个框）。
   * ★ 提交时传这个；判分侧按「卷面第几个框」还原成原题选项。
   */
  position_label: string
  content_html: string
  content_text: string
  image_asset_id: number | null
}

export interface StudentQuestion {
  /** ★ 卷面题号（1 起）。提交时传这个，**不要自己换算** */
  sheet_seq: number
  score: string
  type: string
  stem_html: string
  stem_text: string
  options: StudentOption[]
  figures: Array<Record<string, unknown>>
}

export interface StudentPaper {
  assignment_id: number
  title: string
  end_at: string
  status: string
  shuffled_questions: boolean
  shuffled_options: boolean
  total_items: number
  items: StudentQuestion[]
}

export interface SubmitResult {
  saved: number
  failed: number
  errors: Array<{ sheet_seq: number | null; error: string }>
  submitted: boolean
}

/** 单题作答：选项题给 `labels`，填空题给 `blanks`。 */
export interface AnswerPayload {
  sheet_seq: number
  labels?: string[]
  blanks?: string[]
}

// ══════════════════════════════════════════════════
//  请求
// ══════════════════════════════════════════════════

const base = (publishToken: string) => `/api/v1/s/${publishToken}`

/** 扫码入口信息（**免认证**，信息最小）。 */
export async function getEntry(publishToken: string): Promise<StudentEntry> {
  return request<StudentEntry>(`${base(publishToken)}/`, { anonymous: true })
}

/**
 * 绑定身份（学号 + 姓名）。
 *
 * ★ 失败时后端返回 **HTTP 200 + `code="SCAN.STUDENT_NO_UNMATCHED"`**
 *   （业务分支），会被 `request()` 抛成 `BusinessBranch`。
 *   调用方要按分支处理并显示后端给的 `message`——
 *   那是「学号不在名册」这类可自己纠正的提示，不是系统错误。
 */
export async function bind(
  publishToken: string,
  body: { student_no: string; name: string },
): Promise<BindResult> {
  const data = await request<BindResult>(`${base(publishToken)}/bind/`, {
    method: 'POST',
    body,
    anonymous: true,
  })
  studentSession.set(publishToken, data.session_token)
  return data
}

/** 取卷面（**不含答案**）。需要 `X-Session-Token`。 */
export async function getPaper(publishToken: string): Promise<StudentPaper> {
  const token = studentSession.get(publishToken)
  return request<StudentPaper>(`${base(publishToken)}/paper/`, {
    anonymous: true,
    headers: token ? { 'X-Session-Token': token } : undefined,
  })
}

/**
 * 交卷。
 *
 * ★★ 返回里可能 `failed > 0`：后端**逐题独立**，一道题的作答写不进去
 *   不该让整卷失败（否则学生会丢掉已答的全部内容）。
 *   所以调用方必须检查 `failed` 并提示，不能只看 HTTP 200。
 */
export async function submit(
  publishToken: string,
  answers: AnswerPayload[],
  final = true,
): Promise<SubmitResult> {
  const token = studentSession.get(publishToken)
  return request<SubmitResult>(`${base(publishToken)}/submit/`, {
    method: 'POST',
    body: { answers, final },
    anonymous: true,
    headers: token ? { 'X-Session-Token': token } : undefined,
  })
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

export const STUDENT_TYPE_LABEL: Record<string, string> = {
  SINGLE_CHOICE: '单项选择题',
  MULTI_CHOICE: '多项选择题',
  TRUE_FALSE: '判断题',
  FILL_BLANK: '填空题',
  SHORT_ANSWER: '简答题',
  CALCULATION: '计算题',
  DRAWING: '作图题',
  ESSAY: '论述题',
}

/** 有选项的题型（学生点选）；其余按填空题处理（学生打字）。 */
const OPTION_TYPES = new Set([
  'SINGLE_CHOICE', 'MULTI_CHOICE', 'TRUE_FALSE',
])

export function isOptionType(type: string): boolean {
  return OPTION_TYPES.has(type)
}

export function isMultiSelect(type: string): boolean {
  return type === 'MULTI_CHOICE'
}

/**
 * 数出填空题有几个空。
 *
 * ★ 题库里的空位标记是 **8 个半角下划线**（`________`）。
 *   实测确认过：题干如「销是标准件，主要用作________零件…」。
 *
 * ★ 数不出来时返回 1 —— 宁可给一个输入框让学打字，
 *   也不要给 0 个（那会让学生**无法作答**，而他明明看到有横线）。
 */
export function countBlanks(stem: string): number {
  const full = (stem.match(/_{3,}/g) ?? []).length
  const wide = (stem.match(/＿{3,}/g) ?? []).length
  return Math.max(1, full + wide)
}

export function typeLabel(t: string): string {
  return STUDENT_TYPE_LABEL[t] ?? t
}

/** 剩余时间（毫秒）。负数表示已超时。 */
export function remainMs(endAt: string, now = Date.now()): number {
  const end = new Date(endAt).getTime()
  if (Number.isNaN(end)) return 0
  return end - now
}

export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const p = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${p(h)}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`
}
