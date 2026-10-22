/**
 * 知识体系 API（组卷时选知识点范围用）。
 *
 * ★ 课程树是**三层嵌套的普通 dict**，不是模型实例：
 *
 *     { course, parts: [ { part, part_seq, chapters: [
 *         { id, name, code, children: [       // children = 任务（TASK）
 *             { id, name, code, children: [   // children = 知识点（NODE）
 *                 { id, name, code, seq_in_task, exam_weight, ... } ] } ] } ] } ] }
 *
 *   ★ `level` 字段标明层级（CHAPTER / TASK / NODE）——不要靠嵌套深度猜，
 *     因为「篇」是属性而不是一层（见 README）。
 */

import { http } from './http'
import type { Paged } from './paper'

export interface Course {
  id: number
  name: string
  code: string
  grade_band?: string
  level?: string
  total_hours?: number | null
  /** 展示排序（管理页可改） */
  sort_order?: number
  /** `ACTIVE` / `ARCHIVED`（归档 = 可逆下线，不是删除） */
  status?: string
  chapter_count?: number
  task_count?: number
  node_count?: number
  entry_count?: number
}

export interface TreeNode {
  id: number
  name: string
  code: string
  level: 'CHAPTER' | 'TASK' | 'NODE' | string
  seq?: number
  seq_in_task?: number
  part_seq?: number
  part_name?: string
  is_practical?: boolean
  exam_weight?: number
  question_count?: number
  entry_count?: number
  children?: TreeNode[]
}

export interface ChapterNode extends TreeNode {
  children: TreeNode[]
}

export interface PartNode {
  part: string
  part_seq: number
  chapters: ChapterNode[]
}

export interface CourseTree {
  course: Course
  parts: PartNode[]
  chapter_count: number
  task_count: number
  node_count: number
}

export async function listCourses(): Promise<Paged<Course>> {
  return http.get<Paged<Course>>('/api/v1/knowledge/courses/')
}

export async function getCourseTree(courseId: number): Promise<CourseTree> {
  return http.get<CourseTree>(`/api/v1/knowledge/courses/${courseId}/tree/`)
}

/**
 * 把课程树摊平成知识点列表。
 *
 * ★ 组卷的 `knowledge_nodes` 只要**知识点（NODE）**的 id，
 *   不要章/任务的 id——传错了后端筛不出任何题，
 *   而表现为「候选池为空」而不是报错。
 */
export function flattenNodes(tree: CourseTree | null): Array<
  TreeNode & { taskName: string; chapterName: string }
> {
  if (!tree) return []
  const out: Array<TreeNode & { taskName: string; chapterName: string }> = []
  for (const part of tree.parts) {
    for (const chapter of part.chapters) {
      for (const task of chapter.children ?? []) {
        for (const node of task.children ?? []) {
          if (node.level !== 'NODE') continue
          out.push({
            ...node,
            taskName: task.name,
            chapterName: chapter.name,
          })
        }
      }
    }
  }
  return out
}

// ══════════════════════════════════════════════════
//  知识点（扁平列表 —— 带题数/知识库充分性）
// ══════════════════════════════════════════════════

/**
 * 知识点列表项（对齐后端 `/knowledge/courses/{id}/nodes/`）。
 *
 * ★★ 这个端点比 `tree/` 有用得多：它带三个**运营指标** ——
 *
 *     `question_count`  该知识点有多少道可用题
 *     `entry_count`     有多少条知识库条目
 *     `kb_sufficient`   知识库是否够 AI 生成（`KB_MIN_ENTRIES` 门禁）
 *
 *   `tree/` 只有结构。所以「知识体系」页左树用 `tree/`、
 *   右侧指标用这个端点，**两者按 id 对齐**。
 */
export interface KnowledgeNodeRow {
  id: number
  name: string
  code: string
  task_id: number
  task_name: string
  chapter_seq: number
  chapter_name: string
  seq_in_task: number
  exam_weight: number
  /** ★ 该知识点的可用题数（0 表示讲练脱节） */
  question_count: number
  entry_count: number
  /** ★ 知识库是否达到 AI 生成的最低条数 */
  kb_sufficient: boolean
}

export async function listCourseNodes(
  courseId: number,
): Promise<KnowledgeNodeRow[]> {
  const d = await http.get<{ items?: KnowledgeNodeRow[] }>(
    `/api/v1/knowledge/courses/${courseId}/nodes/`,
  )
  return d.items ?? []
}

/** 知识点详情（含前置链）。★ `exam_weight` 是字符串（Decimal）。 */
export interface KnowledgeNodeDetail {
  id: number
  name: string
  code: string
  path: string
  level: number
  seq_in_task: number
  sort_order: number
  course_id: number
  task_id: number
  task_name: string
  exam_weight: string
  is_template: boolean
  status: string
  entry_count: number
  kb_sufficient: boolean
  question_count: number
  created_at: string
  updated_at: string
  prerequisites: Array<{
    id?: number
    node_id?: number
    name?: string
    code?: string
    [k: string]: unknown
  }>
}

export async function getNode(id: number): Promise<KnowledgeNodeDetail> {
  return http.get<KnowledgeNodeDetail>(`/api/v1/knowledge/nodes/${id}/`)
}

// ══════════════════════════════════════════════════
//  知识库条目
// ══════════════════════════════════════════════════

export interface KnowledgeEntry {
  id: number
  title: string
  content: string
  /** 8 种类型之一（见 `ENTRY_TYPE_LABEL`） */
  entry_type: string
  source_type: string
  source_ref: string
  course_id: number | null
  chapter_id: number | null
  node_ids: number[]
  status: string
  adopted_count: number
  created_at: string
  updated_at: string
}

export async function listEntries(q: {
  nodeId?: number
  chapterId?: number
  pageSize?: number
} = {}): Promise<KnowledgeEntry[]> {
  const d = await http.get<{ items?: KnowledgeEntry[] }>(
    '/api/v1/knowledge/entries/',
    {
      node_id: q.nodeId,
      chapter_id: q.chapterId,
      page_size: q.pageSize ?? 100,
    },
  )
  return d.items ?? []
}

// ══════════════════════════════════════════════════
//  健康度与缺口
// ══════════════════════════════════════════════════

export interface KbHealth {
  course_id: number | null
  total_nodes: number
  covered_nodes: number
  total_entries: number
  adopted_entries: number
  /** 0–1 小数：有知识库条目的知识点占比 */
  coverage: number
  /** 0–1 小数：条目被 AI 采纳的比例 */
  utilisation: number
  /** ★ 是否达到「AI 可生成」的门槛 */
  sufficient_for_generation: boolean
}

export async function getKbHealth(courseId?: number): Promise<KbHealth> {
  return http.get<KbHealth>('/api/v1/knowledge/health/', {
    course_id: courseId,
  })
}

/** 缺知识库的知识点（AI 生成会被 L1 闸门拦下）。 */
export async function listNodesLackingKb(
  courseId: number,
): Promise<Array<{ id: number; name: string; code: string
                  entry_count: number; question_count: number }>> {
  const d = await http.get<{
    items?: Array<{ id: number; name: string; code: string
                    entry_count: number; question_count: number }>
  }>('/api/v1/knowledge/nodes/lacking-kb/', { course_id: courseId })
  return d.items ?? []
}

// ══════════════════════════════════════════════════
//  ★ 知识点标注升级（21 文档任务五）
// ══════════════════════════════════════════════════
//
//  ★★ 覆盖率**只计 `CONFIRMED`**：AI 标的 `PENDING` 不算。
//     否则 AI 一跑覆盖率就「达标」，而没人看过那些标注 ——
//     「达标」变成了「跑过一次 AI」，报表就失去意义。

export interface KnowledgeAlias {
  id: number
  node_id: number
  node_name?: string
  alias: string
  /** 归一化后的形式（后端用它做唯一性判定） */
  alias_normalized?: string
  source?: string
  created_at?: string
}

export interface AliasResolveResult {
  matched: boolean
  node_id: number | null
  /** `alias` 命中别名 / `name` 命中节点名 / 空串表示没命中 */
  via: '' | 'alias' | 'name' | string
  /** ★ 命中多个节点时 `matched=false` + 候选，**系统不猜** */
  candidates: Array<{ node_id: number; name: string; via: string }>
}

export async function listAliases(
  nodeId?: number,
): Promise<KnowledgeAlias[]> {
  const d = await http.get<{ items?: KnowledgeAlias[] }>(
    '/api/v1/knowledge/aliases/',
    { node_id: nodeId },
  )
  return d.items ?? []
}

/** 新增别名。★ 已指向别的节点时 400（正确动作是先合并，不是再加一条）。 */
export async function addAlias(body: {
  node_id: number
  alias: string
}): Promise<{ id: number; node_id: number; alias: string }> {
  return http.post('/api/v1/knowledge/aliases/', body)
}

export async function resolveAlias(
  text: string,
  courseId?: number,
): Promise<AliasResolveResult> {
  return http.get<AliasResolveResult>('/api/v1/knowledge/aliases/resolve/', {
    text,
    course_id: courseId,
  })
}

/** 合并历史（可按节点过滤）。 */
export interface MergeLog {
  id: number
  source_id: number
  source_name: string
  target_id: number
  target_name: string
  counts: Record<string, number>
  reason: string
  operator_name?: string
  created_at: string
  rolled_back_at: string | null
  can_rollback: boolean
}

export async function listMerges(nodeId?: number): Promise<MergeLog[]> {
  const d = await http.get<{ items?: MergeLog[] }>(
    '/api/v1/knowledge/merges/',
    { node_id: nodeId },
  )
  return d.items ?? []
}

/** 把 `sourceId` 并进 `targetId`（写可回滚日志）。 */
export async function mergeNodes(body: {
  source_id: number
  target_id: number
  reason?: string
}): Promise<{ log_id: number; counts: Record<string, number> }> {
  return http.post(`/api/v1/knowledge/nodes/${body.target_id}/merge/`, body)
}

/** 按日志反向恢复。★ 已回滚过的会被拒（不能重复回滚）。 */
export async function rollbackMerge(
  logId: number,
): Promise<{ question_links_reverted: number
             question_links_restored: number
             edges_restored: number }> {
  return http.post(`/api/v1/knowledge/merges/${logId}/rollback/`)
}

/** 标注覆盖率报表（目标：覆盖率 ≥ 90%、主标签率 ≥ 85%）。 */
export interface CoverageNodeRow {
  node_id: number
  code: string
  name: string
  task_id: number | null
  task_name: string
  chapter_id: number | null
  chapter_name: string
  question_count: number
  /** 该节点题数 / 课程总题数 */
  share: number
}

export interface CoverageReport {
  course_id: number | null
  total_questions: number
  tagged_questions: number
  /** 0–1 小数（只有 CONFIRMED 计入） */
  coverage_ratio: number
  primary_tagged_questions: number
  primary_ratio: number
  /** 有标注但没有主标签：组卷时不知道该抽哪个考点 */
  conflict_questions: number
  conflict_ratio: number
  /** AI 标了但没人确认的题数 */
  pending_ai_questions: number
  target: { coverage: number; primary: number }
  meets_target: boolean
  nodes: CoverageNodeRow[]
  chapters: Array<{ chapter_name: string; node_count: number
                    covered_node_count: number; question_count: number }>
  untagged_node_count: number
}

export async function getCoverage(
  courseId?: number,
): Promise<CoverageReport> {
  return http.get<CoverageReport>('/api/v1/knowledge/coverage/', {
    course_id: courseId,
  })
}

/** 待人工确认的标注（★ 按 `confidence` 升序：最不可信的排在最前）。 */
export interface TagReviewRow {
  /** 标注关联行 id —— 确认/驳回都传它 */
  link_id: number
  question_id: number
  question_type: string
  stem_preview: string
  node_id: number
  node_name: string
  is_primary: boolean
  /** `AI` / `TEACHER` / `IMPORT` */
  source: string
  confidence: number | null
  review_state: 'PENDING' | 'CONFIRMED' | 'REJECTED'
}

export async function listTagReviewQueue(q: {
  state?: 'PENDING' | 'CONFIRMED' | 'REJECTED'
  limit?: number
} = {}): Promise<{ total: number; count: number; items: TagReviewRow[] }> {
  return http.get('/api/v1/knowledge/tag-review/', {
    state: q.state ?? 'PENDING',
    limit: q.limit ?? 100,
  })
}

export async function reviewTag(
  linkId: number,
  body: { state: 'CONFIRMED' | 'REJECTED'; reason?: string },
): Promise<{ link_id: number; review_state: string }> {
  return http.post(`/api/v1/knowledge/tag-review/${linkId}/`, body)
}

// ══════════════════════════════════════════════════
//  展示辅助
// ══════════════════════════════════════════════════

/** 知识库条目的 8 种类型（设计文档 2.5）。 */
export const ENTRY_TYPE_LABEL: Record<string, string> = {
  CONCEPT: '概念定义',
  PRINCIPLE: '原理规律',
  PROCEDURE: '操作步骤',
  PARAMETER: '参数标准',
  FORMULA: '公式',
  EXAMPLE: '例题',
  SAFETY: '安全规程',
  COMMON_ERROR: '常见错误',
}

export const ENTRY_SOURCE_LABEL: Record<string, string> = {
  TEACHER_INPUT: '教师录入',
  AI_GENERATED: 'AI 生成',
  IMPORTED: '外部导入',
  ADOPTED: '采纳',
}

/** 知识点是否「讲练脱节」：有知识库但没题。 */
export function isLectureWithoutPractice(n: KnowledgeNodeRow): boolean {
  return n.entry_count > 0 && n.question_count === 0
}

/** 知识点是否「有题但没知识库」（AI 生成会被闸门拦下）。 */
export function isPracticeWithoutLecture(n: KnowledgeNodeRow): boolean {
  return n.question_count > 0 && !n.kb_sufficient
}

// ══════════════════════════════════════════════════
//  ★ 课程与知识点的写操作（计划 U08 / U09，2026-10-21）
// ══════════════════════════════════════════════════
//
// ★★ 后端拒绝**未声明字段**（400），不是静默忽略。
//   所以这里的 payload 类型要严格：多传一个键就会被拒，
//   而那条报错会完整列出「可用字段」——这是刻意的。

/**
 * 一条「引用」。
 *
 * 删除课程/知识点被拒（409 `KNOWLEDGE.IN_USE`）时，后端列出的就是它：
 *
 *     { kind: 'question', label: '题目', count: 12,
 *       samples: ['联轴器的主要作用…'], hint: '先在题库里把这些题改挂…' }
 *
 * ★ `blocking === false` 的项（如前置关系）**不阻止删除**，
 *   只是告知「删的时候会顺带清掉」。
 */
export interface EntityReference {
  kind: string
  label: string
  count: number
  samples: string[]
  hint?: string
  blocking?: boolean
}

export interface CourseDetail extends Course {
  /** 下级引用清点。为空数组 = 可以改编码 / 可以删除 */
  references?: EntityReference[]
}

export interface CourseWriteBody {
  name?: string
  code?: string
  major_id?: number | null
  grade_band?: string
  level?: string
  total_hours?: number | null
  sort_order?: number
  status?: string
}

export async function getCourse(id: number): Promise<CourseDetail> {
  return http.get<CourseDetail>(`/api/v1/knowledge/courses/${id}/`)
}

export async function createCourse(body: CourseWriteBody): Promise<Course> {
  return http.post<Course>('/api/v1/knowledge/courses/', body)
}

export async function updateCourse(
  id: number,
  body: CourseWriteBody,
): Promise<Course> {
  return http.patch<Course>(`/api/v1/knowledge/courses/${id}/`, body)
}

export async function deleteCourse(id: number): Promise<void> {
  await http.del(`/api/v1/knowledge/courses/${id}/`)
}

// ── 章 / 任务（只有「建」，改删还没做）──

export interface ChapterRow {
  id: number
  name: string
  code: string
  seq: number
  part_seq: number | null
  part_name: string
  task_count?: number
}

export async function listChapters(courseId: number): Promise<ChapterRow[]> {
  const d = await http.get<{ items?: ChapterRow[] } | ChapterRow[]>(
    '/api/v1/knowledge/chapters/',
    { course_id: courseId },
  )
  return Array.isArray(d) ? d : (d.items ?? [])
}

export async function createChapter(body: {
  course_id: number
  name: string
  seq: number
  part_seq?: number | null
  part_name?: string
}): Promise<ChapterRow> {
  return http.post<ChapterRow>('/api/v1/knowledge/chapters/', body)
}

export interface TaskRow {
  id: number
  name: string
  code: string
  seq: number
  chapter_id: number
  is_practical?: boolean
  node_count?: number
}

export async function createTask(body: {
  chapter_id: number
  name: string
  seq: number
  is_practical?: boolean
}): Promise<TaskRow> {
  return http.post<TaskRow>('/api/v1/knowledge/tasks/', body)
}

// ── 知识点 ──

export interface NodeWriteBody {
  name?: string
  code?: string
  seq_in_task?: number | null
  sort_order?: number
  exam_weight?: number
  status?: string
  /** ★ 就是「移动」：换任务 = 换章（甚至换课程），`code` 不重算 */
  task_id?: number
  deprecated?: boolean
}

export async function updateNode(
  id: number,
  body: NodeWriteBody,
): Promise<KnowledgeNodeDetail> {
  return http.patch<KnowledgeNodeDetail>(
    `/api/v1/knowledge/nodes/${id}/`, body)
}

/**
 * 新建知识点（既有端点 `POST /knowledge/nodes/`）。
 *
 * ★ `seq_in_task` 必填：它决定编码里的 `K` 序号。让后端自动分配，
 *   等于「插一个知识点把后面所有知识点的编码都改掉」。
 */
export async function createNode(body: {
  task_id: number
  name: string
  seq_in_task: number
  exam_weight?: number
}): Promise<KnowledgeNodeDetail> {
  return http.post<KnowledgeNodeDetail>('/api/v1/knowledge/nodes/', body)
}

export async function deleteNode(id: number): Promise<void> {
  await http.del(`/api/v1/knowledge/nodes/${id}/`)
}

/** 知识点详情里的引用清点（`getNode` 返回值上多一个 `references`）。 */
export interface KnowledgeNodeDetailWithRefs extends KnowledgeNodeDetail {
  references?: EntityReference[]
}

export async function getNodeWithRefs(
  id: number,
): Promise<KnowledgeNodeDetailWithRefs> {
  return http.get<KnowledgeNodeDetailWithRefs>(
    `/api/v1/knowledge/nodes/${id}/`)
}
