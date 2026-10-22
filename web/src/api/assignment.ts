/**
 * 作业（任务发布）API。
 *
 * ★★ 字段名照后端序列化器抄，不凭 DRF 默认约定猜。
 *    `AssignmentDetailSerializer` 与 `AssignmentListSerializer`
 *    的字段**不一样**——详情多了 `publish_token`、`sheet_template_id`。
 *
 * ★★ 三个容易踩的点：
 *
 *   1. **所有作业统一「线上 + 线下」**（2026-10-18 删除 channel）。
 *      线下用答题卡扫描，线上用 H5；两者是同一次作业的两个入口。
 *      没有答题卡也能建草稿，但**发布前必须绑定且通过逐题校验**。
 *
 *   2. **时间窗是左闭右开**，`start_at >= end_at` 会被 400 拒。
 *      而且后端按 `Asia/Shanghai` 解释（README 第八节），
 *      所以前端必须发**带时区**的 ISO 8601，不能发本地无时区的串。
 *
 *   3. **`publish_token` 是学生入口的唯一凭证**，
 *      它=「任务级口令」，所以教师端详情才返回它，
 *      而**绝不能**出现在学生端接口里。
 */

import { http } from './http'
import type { Paged } from './paper'
import type { AssignmentScanCards } from './omr'

export type AssignmentStatus =
  | 'DRAFT'
  | 'PUBLISHED'
  | 'CLOSED'
  | 'ARCHIVED'
export type AnswerVisibility = 'AFTER_SUBMIT' | 'AFTER_GRADE' | 'NEVER'
export type ScoreVisibility = 'TOTAL' | 'DETAIL' | 'NONE'

export interface SheetValidationItem {
  seq: number | null
  code: string
  message: string
  expected?: unknown
  actual?: unknown
}

export interface SheetValidation {
  ok: boolean
  blocking: SheetValidationItem[]
  warnings: SheetValidationItem[]
  paper_total_score?: number
  sheet_total_score?: number
}

/** 列表项（对齐 `AssignmentListSerializer`）。 */
export interface AssignmentBrief {
  id: number
  uuid: string
  title: string
  status: AssignmentStatus
  status_label?: string
  classroom_id: number
  classroom_name: string
  paper_id: number
  paper_title: string
  sheet_template_id: number | null
  sheet_template_name?: string
  sheet_template_version?: number
  start_at: string
  end_at: string
  published_at: string | null
  shuffle_questions: boolean
  shuffle_options: boolean
  answer_visibility: AnswerVisibility
  score_visibility: ScoreVisibility
  allow_late: boolean
  pass_ratio: number
  auto_practice: boolean
  /**
   * ★ 交卷自动批改（2026-10-17 新增）。
   *
   * 勾选后学生一交卷就自动批改**他这一份**（客观题与填空），
   * 老师仍可在批改工作台逐生重批。
   */
  auto_grade_on_submit: boolean
  created_at: string
}

export interface SubmissionStats {
  total: number
  by_status: Record<string, number>
  submitted: number
  absent: number
  pending: number
  /** ★ 已批改（grading_state=DONE） */
  graded: number
  /** ★ 待批改（已交卷且 grading_state != DONE） */
  pending_grade: number
  submitted_ratio: number
}

/** 详情（对齐 `AssignmentDetailSerializer`，★ 多 `publish_token`）。 */
export interface AssignmentDetail extends AssignmentBrief {
  sheet_template_name: string
  sheet_template_version: number
  /** 实时绑定校验结果（未绑卡为 null） */
  sheet_validation: SheetValidation | null
  /** ★ 学生扫码入口令牌。只有教师端详情才有 */
  publish_token: string
  updated_at: string
  /** ★ 只有 `GET /assignments/{id}/` 会带 `stats` */
  stats?: SubmissionStats
}

/** 名单行（对齐 `AssignmentStudentsView`）。 */
export interface AssignmentStudentRow {
  id: number
  /**
   * 该生的**身份行 id**（`role=STUDENT`）。
   *
   * ★ 2026-10-16 起发布任务会给每个学号建一行身份行，所以这里**永远非空**；
   *   它不等于「能不能登录」——没绑登录凭据的学生照样能答题。
   */
  student_id: number
  /** 学号（发布时冻结的身份快照） */
  student_no: string
  /** 姓名（发布时冻结的身份快照） */
  student_name: string
  /**
   * 是否已绑**登录凭据**（能不能自己登录看成绩）。
   *
   * ★ 一期全班通常都是 `false`：学生用学号 + 姓名答题，不需要账号。
   */
  student_login_bound: boolean
  status: string
  status_label?: string
  entered_at: string | null
  submitted_at: string | null
  has_mapping: boolean
}

export interface CreateAssignmentBody {
  paper_id: number
  classroom_id: number
  title: string
  start_at: string
  end_at: string
  /** ★ 新建表单已不再发送；保留给旧调用/服务层测试 */
  sheet_template_id?: number | null
  shuffle_questions?: boolean
  shuffle_options?: boolean
  answer_visibility?: AnswerVisibility
  score_visibility?: ScoreVisibility
  allow_late?: boolean
  pass_ratio?: number
  auto_practice?: boolean
  /** ★ 学生一交卷就自动批改他这一份（2026-10-17 新增） */
  auto_grade_on_submit?: boolean
}

export interface PublishResult {
  assignment_id: number
  status: string
  student_count: number
  students_created: number
  students_preserved: number
  /** ★ 本次为「没绑账号的学生」补建的身份行数（2026-10-17 起有值） */
  identity_created?: number
  identity_linked?: number
  students_identity_repaired?: number
  item_count: number
  publish_token: string
  republished: boolean
  /** ★ 未绑答题卡的纯线上作业（逐生乱序仍生效） */
  online_only?: boolean
}

// ══════════════════════════════════════════════════
//  请求
// ══════════════════════════════════════════════════

export async function listAssignments(params: {
  page?: number
  pageSize?: number
  status?: string
  classroomId?: number
  paperId?: number
} = {}): Promise<Paged<AssignmentBrief>> {
  return http.get<Paged<AssignmentBrief>>('/api/v1/assignments/', {
    page: params.page,
    page_size: params.pageSize,
    status: params.status,
    classroom_id: params.classroomId,
    paper_id: params.paperId,
  })
}

export async function getAssignment(id: number): Promise<AssignmentDetail> {
  return http.get<AssignmentDetail>(`/api/v1/assignments/${id}/`)
}

export async function createAssignment(
  body: CreateAssignmentBody,
): Promise<AssignmentDetail> {
  return http.post<AssignmentDetail>('/api/v1/assignments/', body)
}

/**
 * 改作业设置（`PATCH /assignments/{id}/`，2026-10-17 新增）。
 *
 * ★★ 后端是**白名单**：只接受
 *
 *     auto_grade_on_submit / answer_visibility / score_visibility /
 *     allow_late / pass_ratio / auto_practice / start_at / end_at /
 *     sheet_template_id
 *
 *   `shuffle_*` 只在**草稿且未绑卡**时可改（2026-10-19）；
 *   发布/绑卡后逐生顺序已冻结，再改会让卡上题号与系统题号对不上。
 */
export async function updateAssignment(
  id: number,
  body: AssignmentSettings,
): Promise<AssignmentDetail> {
  return http.patch<AssignmentDetail>(`/api/v1/assignments/${id}/`, body)
}

/**
 * 作业详情页唯一绑定入口。
 * 服务端一次写 `PaperSheetBinding` + `assignment.sheet_template`，
 * 不再要求试卷事先已有绑定。
 */
export async function bindAssignmentSheet(
  id: number,
  templateId: number,
): Promise<AssignmentDetail> {
  return http.post<AssignmentDetail>(
    `/api/v1/assignments/${id}/bind-sheet/`,
    { template_id: templateId },
  )
}

/** 可在创建后修改的作业设置（与后端白名单一致）。 */
export interface AssignmentSettings {
  start_at?: string
  end_at?: string
  answer_visibility?: AnswerVisibility
  score_visibility?: ScoreVisibility
  allow_late?: boolean
  pass_ratio?: number
  auto_practice?: boolean
  /** ★ 学生一交卷就自动批改他这一份 */
  auto_grade_on_submit?: boolean
  /** ★ 仅草稿且未绑卡时可改 */
  shuffle_questions?: boolean
  shuffle_options?: boolean
  /** ★ 发布前可改绑答题卡 */
  sheet_template_id?: number | null
}

/**
 * 发布（**幂等**）。
 *
 * ★ 重复发布不会重新乱序（服务层保证），所以「点了发布但网络断了」
 *   可以直接重试。返回值里 `republished` 区分「首次发布」与
 *   「补齐名单后重新发布」。
 *
 * ★ 发布时会**为每个学号建学生身份行**（`role=STUDENT`，无登录凭据），
 *   所以「有学生没绑账号」**不再**是拒绝发布的理由
 *   （2026-10-17 起，见开发进度记录【★ 修复记录】）。
 *   `publishHint` 那条 `unbound_student_nos` 分支已是历史行为。
 */
export async function publishAssignment(
  id: number,
  body: { student_ids?: number[] } = {},
): Promise<PublishResult> {
  return http.post<PublishResult>(`/api/v1/assignments/${id}/publish/`, body)
}

export async function closeAssignment(id: number): Promise<AssignmentDetail> {
  return http.post<AssignmentDetail>(`/api/v1/assignments/${id}/close/`)
}

export async function archiveAssignment(id: number): Promise<AssignmentDetail> {
  return http.post<AssignmentDetail>(`/api/v1/assignments/${id}/archive/`)
}

/** 名单一次拉取的页大小。后端 `max_page_size = 200`，超了会被静默截断。 */
const STUDENT_PAGE_SIZE = 200

export interface AssignmentStudentsResult {
  items: AssignmentStudentRow[]
  /** 后端报告的总人数（用于核对是否拉全）。 */
  total: number
}

/**
 * 拉名单（**自动翻页，返回全量**）。
 *
 * ★★ 为什么必须自己翻页（需求 2 的根因）：
 *
 *   后端 `GET /assignments/{id}/students/` 走 `StandardPagination`，
 *   `PAGE_SIZE = 20`。原来的实现不传 `page_size`，于是**只拿回 20 条**，
 *   而页面把 `students.length` 当总数 —— 45 人的班永远只显示 20 人，
 *   翻页也翻不出第 21 个。
 *
 *   前端的「状态」「已交/未交/已批改/待批改」筛选全是**本地算**的
 *   （`quickFilter` 依赖 roster，后端没有对应参数），
 *   所以这里必须拿全量，不能把分页下推给后端。
 *
 * ★ 兜底：`next` 为空即停；再用 `total` 与页数上限防死循环。
 */
export async function listAssignmentStudents(
  id: number,
  status?: string,
): Promise<AssignmentStudentsResult> {
  const out: AssignmentStudentRow[] = []
  let total = 0
  // 上限 20 页 × 200 条 = 4000 人，远超任何真实班级
  for (let guard = 0; guard < 20; guard += 1) {
    const d = await http.get<{
      items: AssignmentStudentRow[] | null
      total?: number
      next?: string | null
    }>(`/api/v1/assignments/${id}/students/`, {
      status,
      page: guard + 1,
      page_size: STUDENT_PAGE_SIZE,
    })
    const items = d.items ?? []
    if (typeof d.total === 'number') total = d.total
    out.push(...items)
    if (!d.next || items.length < STUDENT_PAGE_SIZE) break
    if (total && out.length >= total) break
  }
  return { items: out, total }
}

export async function getAssignmentScanCards(
  id: number,
): Promise<AssignmentScanCards> {
  return http.get<AssignmentScanCards>(
    `/api/v1/assignments/${id}/scan-cards/`)
}

export async function markAbsent(  id: number,
  studentIds: number[],
  reason = '',
): Promise<{ marked: number }> {
  return http.post<{ marked: number }>(
    `/api/v1/assignments/${id}/mark-absent/`,
    { student_ids: studentIds, reason },
  )
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

export const ASSIGNMENT_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  PUBLISHED: '已发布',
  CLOSED: '已关闭',
  ARCHIVED: '已归档',
}

export const ASSIGNMENT_STATUS_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  DRAFT: 'info',
  PUBLISHED: 'success',
  CLOSED: 'warning',
  ARCHIVED: 'info',
}

export const STUDENT_STATUS_LABEL: Record<string, string> = {
  NOT_STARTED: '未开始',
  IN_PROGRESS: '作答中',
  SUBMITTED: '已交卷',
  SCANNED: '已扫描',
  GRADED: '已批改',
  ABSENT: '缺考',
  PARTIAL: '部分完成',
}

/** 未知学生状态统一中文。 */
export function studentStatusLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return STUDENT_STATUS_LABEL[code] ?? `未知状态（${code}）`
}

export function assignmentStatusLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return ASSIGNMENT_STATUS_LABEL[code] ?? `未知状态（${code}）`
}

export const STUDENT_STATUS_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  NOT_STARTED: 'info',
  IN_PROGRESS: 'warning',
  SUBMITTED: 'success',
  SCANNED: 'success',
  GRADED: 'success',
  ABSENT: 'danger',
  PARTIAL: 'warning',
}

/** 学生扫码入口的完整 URL。
 *
 * ★★ 这里有个**真实会踩的坑**：默认取 `window.location.origin`，
 *    而老师通常在本机用 `http://localhost:5173` 打开系统。
 *    此时二维码里编的是 **`127.0.0.1`** —— 学生用手机扫它
 *    会指向**手机自己**，永远打不开。
 *
 *   所以：
 *     1. 支持用 `VITE_STUDENT_BASE_URL` 显式指定对外地址
 *        （如 `http://192.168.1.10:5173`）
 *     2. 没配置时回落到 `location.origin`，但调用方**应当提示老师**
 *        改用局域网 IP（见 `isLoopbackOrigin`）
 */
export function studentEntryUrl(publishToken: string): string {
  const base = studentBaseUrl()
  return `${base.replace(/\/+$/, '')}/s/${publishToken}/`
}

/** 对外可访问的前端地址（学生手机能访问的那个）。 */
export function studentBaseUrl(): string {
  const configured = import.meta.env.VITE_STUDENT_BASE_URL as
    | string
    | undefined
  if (configured && configured.trim()) return configured.trim()
  return window.location.origin
}

/**
 * 当前地址是否**只有本机能访问**（`localhost` / `127.0.0.1` / `::1`）。
 *
 * ★ 用来自动给老师一个警告：「这个二维码学生扫不了」。
 *   不警告的话，老师会以为二维码坏了，而实际是地址问题。
 */
export function isLoopbackOrigin(): boolean {
  try {
    const h = new URL(studentBaseUrl()).hostname
    return h === 'localhost' || h === '127.0.0.1' || h === '::1'
  } catch {
    return false
  }
}

/** 局域网 IP 形式的建议地址（供老师改用）。 */
export function suggestedLanUrl(): string {
  const u = new URL(studentBaseUrl())
  u.hostname = '<你电脑的局域网IP>'
  return u.toString()
}
