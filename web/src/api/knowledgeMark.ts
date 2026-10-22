/**
 * 知识点批量补标 API（2026-10-16 需求方口径）。
 *
 * ★★ 这一页与难度打标页**形态不同**，因为两者的活儿不同：
 *
 *   难度打标：**每道题都要标**（1944 道），所以是「一屏一题 + 键盘流」。
 *   知识点补标：**落点只有 44 个**，而缺口是「21 个知识点一道题都没有」
 *              —— 所以是「**先选知识点，再批量勾题**」。
 *              一屏一题在这里反而更慢：老师心里想的是
 *              「这几道都该挂到 K02」，而不是「这道题该挂哪」。
 *
 * ★★ 覆盖语义（后端 `set_primary_knowledge`）：
 *   旧的主知识点**不删**，降为次要关联（`is_primary=False`、权重 0.30），
 *   所以「原来是哪个」查得到。
 *
 * ★★ 批量不做整批回滚：一道题被拒（例如跨任务），另外的照常写入，
 *   被拒的带原因返回 —— 前端必须把三桶（updated / skipped / failed）
 *   **分开显示**，把 skipped 混进 updated 会让老师以为改了 50 道。
 */

import { http } from './http'
import type { KnowledgeNodeRow } from './knowledge'

/** 候选题（结构同补标队列的一行）。 */
export interface KpCandidate {
  id: number
  type: string
  subject: string
  status: string
  stem_text: string
  options: Array<{
    label: string
    content_text: string
    content_html?: string
    is_correct?: boolean
  }>
  task_id: number | null
  task_name: string
  task_code: string
  /** 当前主知识点（**覆盖前要看清楚**） */
  current_primary: {
    id: number
    code: string
    name: string
    source: string
  } | null
  has_figure: boolean
}

export interface KpCandidates {
  node: {
    id: number
    code: string
    name: string
    task_id: number | null
    task_name: string
  }
  items: KpCandidate[]
  total: number
  has_more: boolean
  only_unmarked: boolean
}

export interface KpBatchResult {
  knowledge_node_id: number
  requested: number
  updated: number
  skipped: number
  failed: number
  updated_items: Array<{
    question_id: number
    primary_node_code: string
    before: Array<Record<string, unknown>> | null
  }>
  skipped_items: Array<{ question_id: number; reason?: string }>
  failed_items: Array<{
    question_id: number
    code: string
    detail: string
  }>
}

export interface KpProgress {
  total: number
  marked: number
  remaining: number
}

export interface KpGap {
  total_nodes: number
  zero_nodes: number
  zero_list: string[]
}

/** 知识点清单（带题量）。★ 与「知识体系」页同一个端点，口径一致。 */
export async function listKpNodes(
  courseId: number,
): Promise<KnowledgeNodeRow[]> {
  const d = await http.get<{ items?: KnowledgeNodeRow[] }>(
    `/api/v1/knowledge/courses/${courseId}/nodes/`,
  )
  return d.items ?? []
}

/** 补标进度与缺口（用队列端点顺带取，避免再加一个统计端点）。 */
export async function getKpSummary(): Promise<{
  progress: KpProgress
  gap: KpGap
}> {
  const d = await http.get<{ progress: KpProgress; gap: KpGap }>(
    '/api/v1/questions/kp-queue/',
    { limit: 1 },
  )
  return {
    progress: d.progress,
    gap: d.gap,
  }
}

/** 某知识点所属任务下的候选题。 */
export async function getKpCandidates(
  nodeId: number,
  q: { search?: string; onlyUnmarked?: boolean; limit?: number } = {},
): Promise<KpCandidates> {
  const query: Record<string, string | number> = {
    knowledge_node_id: nodeId,
    limit: q.limit ?? 100,
  }
  if (q.search) query.search = q.search
  if (q.onlyUnmarked) query.only_unmarked = 1
  return http.get<KpCandidates>('/api/v1/questions/kp-candidates/', query)
}

/** 批量把题挂到某个知识点（**覆盖**每题原来的主知识点）。 */
export async function setKnowledgeBatch(
  nodeId: number,
  questionIds: number[],
): Promise<KpBatchResult> {
  return http.post<KpBatchResult>(
    '/api/v1/questions/set-knowledge-batch/',
    { knowledge_node_id: nodeId, question_ids: questionIds },
  )
}

/** 标注来源的中文标签（与后端 `KnowledgeLinkSource` 一致）。 */
export const KP_SOURCE_LABELS: Record<string, string> = {
  IMPORT: '导入占位',
  AI: 'AI 预标',
  TEACHER: '人工确认',
  STAT: '统计',
  DEFAULT: '默认',
}
