/**
 * 机构 / 班级 / 名册 API。
 *
 * ★ 班级管理页（`P1-13b`）所需的完整 CRUD。
 *   后端原有缺口（本轮补齐）：
 *     · `ClassroomDetailView` 只有 `get` → 班级改不了、删不掉
 *     · 名册只有整表导入 → 单个学生加不了、改不了、转不出
 *   现在两端都补齐了。
 */

import { http } from './http'
import type { Paged } from './paper'

export interface Classroom {
  id: number
  name: string
  grade: string
  academic_year: string
  grade_band: string
  major_id: number | null
  major_name?: string
  /** ★ 归属教师。班级归个人（2026-10-21），教师端只会看到自己的 */
  owner_id?: number | null
  owner_name?: string
  head_teacher_id: number | null
  status?: string
  student_count?: number
  created_at?: string
  updated_at?: string
  /** ★ 详情接口额外返回：名册摘要（含**推断学号位数**） */
  roster_brief?: RosterBrief
}

/** 名册摘要。★ 字段名以 `RosterBrief` 为准，不要凭直觉加字段。 */
export interface RosterBrief {
  classroom_id: number | null
  classroom_name: string
  student_count: number
  /** 出现最多的学号位数 —— 答题卡排版的依据 */
  inferred_digits: number | null
  /** 形如 `{4: 2, 8: 43}`：让老师看到「位数不统一」的实情 */
  digit_distribution: Record<string, number>
}

export interface Major {
  id: number
  name: string
  code: string
  short_name?: string
  level?: string
}

export interface ClassroomInput {
  name: string
  grade: string
  academic_year: string
  grade_band?: string
  major_id?: number | null
  head_teacher_id?: number | null
  /** ★ 只有学校账户可传（指派历史班 / 离职转接），教师传会被 403 */
  owner_id?: number | null
}

export interface RosterRow {
  id: number
  classroom_id: number
  student_no: string
  name: string
  display_name?: string
  seat_no?: string
  /** ★ 未绑定系统账号时为空 */
  student_id: number | null
  /** ★ 是否已绑定系统账号（决策 8 相关：按渠道区分后不再强制） */
  is_bound?: boolean
  status?: string
  student_name?: string
}

/** 名册导入的预检问题。 */
export interface RosterIssue {
  row: number
  code: string
  detail: string
  student_no?: string
  name?: string
}

export interface RosterValidation {
  /** 有阻断项就整体拒绝导入 */
  can_import: boolean
  blocking: RosterIssue[]
  warnings: RosterIssue[]
  summary: string
  inferred_digits?: number | null
  digit_distribution?: Record<string, number>
}

export interface RosterImportResult {
  created: number
  updated: number
  left: number
  warnings: RosterIssue[]
  inferred_digits: number | null
}

// ══════════════════════════════════════════════════════════════
//  分页：★ 必须**翻完全部页**，不能只信第一次响应
// ══════════════════════════════════════════════════════════════

/**
 * 取全量（自动翻页）。
 *
 * ★★ 为什么必须翻页：后端 `StandardPagination.max_page_size = 200`。
 *   我第一版写 `page_size: 500` 期望一次拿全 —— 后端**静默截断到 200**，
 *   不报错。名册 >200 人的班级会**少人**，而发布前正是要核对
 *   「谁没绑系统账号」，漏掉的人会被误判为已绑定。
 *
 * ★ 兜底：`next` 为空即停；同时用 `total` 与页数设上限防死循环
 *   （`next` 若因后端 bug 永远非空，这里不会转不出来）。
 */
async function fetchAll<T>(
  path: string,
  query: Record<string, unknown> = {},
  pageSize = 200,
): Promise<T[]> {
  const out: T[] = []
  let page = 1
  // 上限：200 页 × 200 条 = 4 万条，远超任何真实名册
  for (let guard = 0; guard < 200; guard += 1) {
    const d = await http.get<Paged<T>>(path, {
      ...query, page, page_size: pageSize,
    })
    const items = d.items ?? []
    out.push(...items)
    // 页数判断：拿满了说明可能还有，没拿满就一定到底了
    if (!d.next || items.length < pageSize) break
    if (typeof d.total === 'number' && out.length >= d.total) break
    page += 1
  }
  return out
}

export async function listClassrooms(params: {
  academicYear?: string
  keyword?: string
} = {}): Promise<Classroom[]> {
  return fetchAll<Classroom>('/api/v1/org/classrooms/', {
    academic_year: params.academicYear,
    keyword: params.keyword,
  })
}

export async function getClassroom(id: number): Promise<Classroom> {
  return http.get<Classroom>(`/api/v1/org/classrooms/${id}/`)
}

export async function createClassroom(
  input: ClassroomInput,
): Promise<Classroom> {
  return http.post<Classroom>('/api/v1/org/classrooms/', input)
}

/** 改班级。★ `PATCH` 语义：只传要改的字段。 */
export async function updateClassroom(
  id: number, input: Partial<ClassroomInput> & { status?: string },
): Promise<Classroom> {
  return http.patch<Classroom>(`/api/v1/org/classrooms/${id}/`, input)
}

/**
 * 删班级（后端软删）。
 *
 * ★ 后端有两道拒绝，都会返回**可照做的说明**：
 *   · 还有作业引用 → 提示先删/归档作业
 *   · 还有在册学生 → 提示先转出学生
 *   所以调用方要**把 message 原样展示**，不要吞掉换成「删除失败」。
 */
export async function deleteClassroom(
  id: number, reason = '',
): Promise<void> {
  await http.del(`/api/v1/org/classrooms/${id}/`, undefined, { reason })
}

export async function listMajors(): Promise<Major[]> {
  return http.get<Major[]>('/api/v1/org/majors/')
}

export async function listAcademicYears(): Promise<string[]> {
  return http.get<string[]>('/api/v1/org/classrooms/academic-years/')
}

// ══════════════════════════════════════════════════════════════
//  名册
// ══════════════════════════════════════════════════════════════

/**
 * 名册明细（发布前核对「谁没绑账号」用）。
 *
 * ★★ 路径容易写错，实测过：
 *     `GET  /classrooms/{id}/roster/`       → **405**（那是导入用的）
 *     `POST /classrooms/{id}/roster/`       → 导入名册
 *     `GET  /classrooms/{id}/roster/list/`  → 名册明细
 *   优先试 `list` 路径，失败再试 `roster/`，避免路由调整时整页报错。
 */
export async function listRoster(
  classroomId: number,
  opts: { status?: string } = {},
): Promise<RosterRow[]> {
  const paths = [
    `/api/v1/org/classrooms/${classroomId}/roster/list/`,
    `/api/v1/org/classrooms/${classroomId}/roster/`,
  ]
  let lastErr: unknown = null
  for (const p of paths) {
    try {
      return await fetchAll<RosterRow>(p, { status: opts.status })
    } catch (e) {
      lastErr = e
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error('名册加载失败')
}

/** 加一名学生。 */
export async function addRosterStudent(
  classroomId: number,
  input: { student_no: string; name: string; seat_no?: string },
): Promise<RosterRow> {
  return http.post<RosterRow>(
    `/api/v1/org/classrooms/${classroomId}/roster/students/`, input,
  )
}

/**
 * 改一名学生。
 *
 * ★★ **不接受 `student_no`** —— 学号是答题卡认人的唯一依据，
 *   改学号等于换一个人（历史成绩会错挂）。后端也刻意不接收该参数。
 *   要换学号请「转出旧的 + 加入新的」。
 */
export async function updateRosterStudent(
  classroomId: number,
  studentId: number,
  input: { name?: string; seat_no?: string; status?: string },
): Promise<RosterRow> {
  return http.patch<RosterRow>(
    `/api/v1/org/classrooms/${classroomId}/roster/students/${studentId}/`,
    input,
  )
}

/** 转出学生（置 `LEFT`，不物理删除，保留历史成绩）。 */
export async function removeRosterStudent(
  classroomId: number,
  studentId: number,
  reason = '',
): Promise<void> {
  await http.del(
    `/api/v1/org/classrooms/${classroomId}/roster/students/${studentId}/`,
    undefined,
    { reason },
  )
}

/** 导入预检（★ 不写库）。有阻断项时后端也会在导出里带 `blocking`。 */
export async function validateRoster(
  classroomId: number,
  rows: Array<{ student_no: string; name: string; seat_no?: string }>,
): Promise<RosterValidation> {
  return http.post<RosterValidation>(
    `/api/v1/org/classrooms/${classroomId}/roster/validate/`,
    { rows },
  )
}

/** 导入名册。`replace=true` 会把现有 ACTIVE 名册标记为 LEFT。 */
export async function importRoster(
  classroomId: number,
  rows: Array<{ student_no: string; name: string; seat_no?: string }>,
  replace = false,
): Promise<RosterImportResult> {
  return http.post<RosterImportResult>(
    `/api/v1/org/classrooms/${classroomId}/roster/`,
    { rows, replace },
  )
}

export async function listDigitMismatch(): Promise<{
  count: number
  items: Array<{
    classroom_id: number
    classroom_name: string
    inferred_digits: number | null
    digit_distribution: Record<string, number>
  }>
}> {
  return http.get('/api/v1/org/roster/digit-mismatch/')
}
