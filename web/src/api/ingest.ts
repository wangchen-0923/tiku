/**
 * 试卷导入 API（`ingest`）。
 *
 * ★★ 上传走 **multipart**，不能用 `http.post`（那个会 `JSON.stringify`）。
 *   这里单独一个 `upload`，但仍复用同一套信封解析与错误语义 ——
 *   否则「上传失败」会变成一句 `Unexpected token`。
 */

import { ApiError, BusinessBranch, tokenStore } from './http'

export interface IngestItem {
  id: number
  seq: number
  status: 'PENDING' | 'CONFIRMED' | 'EDITED' | 'DISCARDED'
  stem_text: string
  qtype: string
  options: Array<{ label: string; content_text: string }>
  answer_raw: string
  answer: Record<string, unknown> | null
  explanation: string
  group_title: string
  node_id: number | null
  /** 难度 1–5（★ 审核时逐题确认，2026-10-17 口径） */
  difficulty: number | null
  /** `single` / `group`（题组头）/ `child`（题组小题） */
  kind: 'single' | 'group' | 'child'
  /** 小题在组内的序号（1 起） */
  group_seq: number
  /** 从 PDF 里抽出的配图（asset_id + 位置 + 归属） */
  figures: Array<{
    asset_id: number
    page: number
    bbox?: number[]
    width?: number
    height?: number
    /** STEM / MATERIAL / OPTION_A..D */
    attach_point?: string
    /** CAPTION / OPTION_POS / SEQUENCE / MANUAL */
    binding_method?: string
    binding_confidence?: number
    manual?: boolean
    /** 图注（如「第 6 题图」）；后端没有时页面按题目补默认文案 */
    caption?: string
  }>
  /** AI 建议（答案解析/知识点/难度），必须人工确认 */
  ai_suggestions: Record<string, {
    value: unknown
    confidence: number
    method?: string
    model?: string
    call_log_id?: number | null
  }>
  /** 题组类型：MATERIAL / ANALYSIS / CALCULATION / DRAWING */
  group_kind?: string
  section_title?: string
  /** 题组头序号（apply 时按它找组） */
  group_no?: number | null
  /** 小题所属题组头序号 */
  parent_group_no?: number | null
  warnings: string[]
  review_note: string
  question_id: number | null
  discard_reason: string
  page: number | null
}

export interface IngestBatch {
  id: number
  source_type: string
  status: string
  detected_count: number
  confirmed_count: number
  warnings: string[]
  counts: Record<string, number>
  items: IngestItem[]
  /** 解析日志：`ai_scope` / `ai` / `figures` / `quality` 等 */
  parse_log?: {
    ai_scope?: AiScope[]
    ai?: {
      assist?: {
        checked?: boolean
        level3_attempted?: boolean
        level3_count?: number
      }
      answer?: { attempted?: number; written?: number; skipped?: number }
      node?: { attempted?: number; written?: number; skipped?: number }
      difficulty?: { attempted?: number; written?: number; skipped?: number }
      errors?: string[]
      [k: string]: unknown
    }
    figures?: { count?: number; issues?: string[] }
    [k: string]: unknown
  }
}

export interface IngestUploadResult {
  batch_id: number
  detected: number
  strategy: string
  warnings: string[]
  items: Array<Record<string, unknown>>
}

export interface IngestApplyResult {
  batch_id: number
  requested: number
  created: number
  skipped: number
  failed: number
  explained: number
  created_items: Array<{ seq: number; question_id: number }>
  skipped_items: Array<{ seq: number; reason: string; detail?: string }>
  failed_items: Array<{ seq: number; reason: string }>
}

async function envelope<T>(res: Response): Promise<T> {
  const text = await res.text()
  let payload: { code?: string; message?: string; data?: T } | null = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    payload = null
  }
  if (!res.ok) {
    throw new ApiError(
      res.status,
      payload?.code ?? `HTTP.${res.status}`,
      payload?.message || `请求失败（HTTP ${res.status}）`,
      payload?.data,
    )
  }
  if (!payload) {
    throw new ApiError(res.status, 'RESPONSE.NOT_ENVELOPE',
      '后端返回的不是约定的 JSON 信封')
  }
  if (payload.code && payload.code !== 'OK') {
    throw new BusinessBranch(payload.code, payload.message ?? '', payload.data)
  }
  return payload.data as T
}

/** AI 介入范围（默认只勾辅助识别） */
export type AiScope = 'assist' | 'explain' | 'answer' | 'node' | 'difficulty'

/** 上传并切题（同步）。`aiScope` 决定这批题允许 AI 介入哪些环节。 */
export async function uploadPaper(
  file: File, aiScope: AiScope[] = ['assist'],
): Promise<IngestUploadResult> {
  const fd = new FormData()
  fd.append('file', file)
  // ★ 数组字段用重复 key（DRF ListField 认这种 multipart 写法）
  for (const s of aiScope) fd.append('ai_scope', s)
  const headers: Record<string, string> = { Accept: 'application/json' }
  const t = tokenStore.access
  if (t) headers.Authorization = `Bearer ${t}`
  let res: Response
  try {
    res = await fetch('/api/v1/ingest/batches/', {
      method: 'POST', headers, body: fd,
    })
  } catch (e) {
    throw new ApiError(0, 'NETWORK.UNREACHABLE',
      `连接不上后端（${String(e)}）。请确认后端已在 127.0.0.1:8001 运行。`)
  }
  return envelope<IngestUploadResult>(res)
}

export async function getBatch(id: number): Promise<IngestBatch> {
  const r = await fetch(`/api/v1/ingest/batches/${id}/`, {
    headers: {
      Accept: 'application/json',
      ...(tokenStore.access
        ? { Authorization: `Bearer ${tokenStore.access}` } : {}),
    },
  })
  return envelope<IngestBatch>(r)
}

export interface ItemPatch {
  stem_text?: string
  qtype?: string
  options?: Array<{ label: string; content_text: string }>
  /** 结构化答案（选择题 {value}、判断题 {value:bool}、填空 {blanks}） */
  answer?: Record<string, unknown> | null
  answer_raw?: string
  explanation?: string
  node_id?: number | null
  difficulty?: number | null
  status?: 'CONFIRMED' | 'DISCARDED'
  discard_reason?: string
}

export async function patchItem(
  itemId: number, patch: ItemPatch,
): Promise<IngestItem> {
  const headers: Record<string, string> = {
    Accept: 'application/json', 'Content-Type': 'application/json',
  }
  if (tokenStore.access) headers.Authorization = `Bearer ${tokenStore.access}`
  const r = await fetch(`/api/v1/ingest/items/${itemId}/`, {
    method: 'PATCH', headers, body: JSON.stringify(patch),
  })
  return envelope<IngestItem>(r)
}

/**
 * 入库：只处理**已确认**的题。
 *
 * ★ 不再传 `task_id`（2026-10-17 口径）：任务是每道题选的**知识点**反推的。
 *   知识点与难度都是入库前置条件，缺一道就会被列进 `failed`。
 */
export async function applyBatch(
  batchId: number,
  body: { explain_missing?: boolean } = {},
): Promise<IngestApplyResult> {
  const headers: Record<string, string> = {
    Accept: 'application/json', 'Content-Type': 'application/json',
  }
  if (tokenStore.access) headers.Authorization = `Bearer ${tokenStore.access}`
  const r = await fetch(`/api/v1/ingest/batches/${batchId}/apply/`, {
    method: 'POST', headers, body: JSON.stringify(body),
  })
  return envelope<IngestApplyResult>(r)
}

/** 题型标签 */
export const QTYPE_LABELS: Record<string, string> = {
  SINGLE_CHOICE: '单选',
  MULTIPLE_CHOICE: '多选',
  TRUE_FALSE: '判断',
  FILL_BLANK: '填空',
  SHORT_ANSWER: '简答',
  ANALYSIS: '分析',
  CALCULATION: '计算',
  DRAWING: '作图',
  ESSAY: '论述',
}

/** 人工调整配图归属（拖拽/指定题目） */
export async function moveFigure(
  itemId: number,
  index: number,
  body: { target_item_id?: number; target_seq?: number; attach_point?: string },
): Promise<{ source: IngestItem; target: IngestItem }> {
  const headers: Record<string, string> = {
    Accept: 'application/json', 'Content-Type': 'application/json',
  }
  if (tokenStore.access) headers.Authorization = `Bearer ${tokenStore.access}`
  const r = await fetch(
    `/api/v1/ingest/items/${itemId}/figures/${index}/move/`, {
      method: 'POST', headers, body: JSON.stringify(body),
    })
  return envelope<{ source: IngestItem; target: IngestItem }>(r)
}

/**
 * 取校对页配图的 object URL（调用方负责 `URL.revokeObjectURL`）。
 *
 * ★ 为什么用 fetch 再转 blob：鉴权是 JWT 在 localStorage，
 *   `<img src>` 不会带 `Authorization` 头。
 */
export async function fetchFigureBlob(
  itemId: number, index: number,
): Promise<string> {
  const headers: Record<string, string> = {}
  if (tokenStore.access) headers.Authorization = `Bearer ${tokenStore.access}`
  const r = await fetch(`/api/v1/ingest/items/${itemId}/figures/${index}/`, {
    headers,
  })
  if (!r.ok) {
    throw new ApiError(r.status, `HTTP.${r.status}`,
      `配图读取失败（HTTP ${r.status}）`)
  }
  return URL.createObjectURL(await r.blob())
}
