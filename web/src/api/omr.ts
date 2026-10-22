/** 扫描导入 / 识别 / 待处理 API。 */

import { fetchBlob, http } from './http'
import type { Paged } from './paper'

export type CardStatus =
  | 'PENDING' | 'AUTO_OK' | 'NEEDS_REVIEW' | 'REVIEWED' | 'VOID' | 'FAILED'

export interface ScanCard {
  id: number
  scan_batch_id: number
  assignment_id: number | null
  sheet_template_id: number
  template_version: number
  page_no: number | null
  page_count_expected: number
  group_key: string
  student_id: number | null
  student_name: string
  recognized_student_no: string
  student_no_confidence: string | null
  student_no_flags: string[]
  student_no_flag_labels?: string[]
  status: CardStatus
  status_label?: string
  overall_confidence: string | null
  anchor_quality: string | null
  anchor_deviation: string | null
  uncertain_items: Array<Record<string, unknown>>
  recognized_answers: Array<{
    seq: number
    labels: string[]
    confidence: number
    fill_ratios: Record<string, number>
  }>
  fail_reason: string
  raw_asset_id: number
  /** 展示用整页图（识别成功后生成；旧卡回退原图） */
  display_asset_id?: number
  rectified_asset_id?: number | null
  source_image_asset_id: number | null
  recognized_at: string | null
}

export interface ScanBatch {
  id: number
  assignment_id: number
  assignment_title: string
  sheet_template_id: number
  template_name: string
  capture_channel: string
  status: string
  total_cards: number
  recognized_cards: number
  failed_cards: number
  review_cards: number
  threshold_version: string
  quality_report: Record<string, unknown>
  created_at: string
  completed_at: string | null
}

export interface BatchProgress {
  batch_id: number
  status: string
  total: number
  by_status: Record<string, number>
  auto_ok: number
  needs_review: number
  failed: number
  void: number
  pending: number
  progress: number
}

export async function createBatch(assignmentId: number): Promise<ScanBatch> {
  return http.post<ScanBatch>('/api/v1/omr/batches/', {
    assignment_id: assignmentId,
    capture_channel: 'UPLOAD',
  })
}

export async function uploadBatch(
  batchId: number, files: File[],
): Promise<{
  batch_id: number
  pages: number
  recognition: string
  skipped_duplicates?: Array<{
    filename: string
    sha256: string
    reason: string
  }>
}> {
  const fd = new FormData()
  for (const f of files) fd.append('files', f, f.name)
  return http.upload(`/api/v1/omr/batches/${batchId}/upload/`, fd)
}

export async function getBatchProgress(
  batchId: number,
): Promise<BatchProgress> {
  return http.get<BatchProgress>(`/api/v1/omr/batches/${batchId}/progress/`)
}

/** 手动提交/重试识别；返回值只表示提交方式，不代表已经识别完成。 */
export async function recognizeBatch(
  batchId: number,
): Promise<{ batch_id: number; recognition: string }> {
  return http.post<{ batch_id: number; recognition: string }>(
    `/api/v1/omr/batches/${batchId}/recognize/`)
}

export async function listCards(params: {
  assignmentId?: number
  batchId?: number
  status?: string
  onlyPending?: boolean
  pageSize?: number
} = {}): Promise<ScanCard[]> {
  const d = await http.get<Paged<ScanCard>>('/api/v1/omr/cards/', {
    assignment_id: params.assignmentId,
    batch_id: params.batchId,
    status: params.status,
    only_pending: params.onlyPending ? 'true' : undefined,
    page_size: params.pageSize ?? 200,
  })
  return d.items ?? []
}

export async function reviewCard(
  cardId: number,
  body: {
    student_id?: number
    status?: 'REVIEWED' | 'AUTO_OK' | 'VOID'
    uncertain_items?: Array<Record<string, unknown>>
    write_responses?: boolean
  },
): Promise<unknown> {
  return http.post(`/api/v1/omr/cards/${cardId}/review/`, body)
}

export async function resolveStudent(
  cardId: number, studentId: number,
): Promise<ScanCard> {
  return http.post<ScanCard>(
    `/api/v1/omr/cards/${cardId}/resolve-student/`,
    { student_id: studentId })
}

export async function voidCard(
  cardId: number, reason: string,
): Promise<ScanCard> {
  return http.post<ScanCard>(
    `/api/v1/omr/cards/${cardId}/void/`, { reason })
}

export async function rescanCard(cardId: number): Promise<unknown> {
  return http.post(`/api/v1/omr/cards/${cardId}/rescan/`)
}

/** 取展示用整页校正图；旧卡会由后端从原图重新校正并落库。 */
export async function getCardDisplayBlob(cardId: number): Promise<Blob> {
  return fetchBlob(`/api/v1/omr/cards/${cardId}/display/`, {
    query: { v: String(Date.now()) },
  })
}

/** 取批改标注图（绿=正确 红=错误）；ETag 随评分更新时间变化。 */
export async function getAnnotatedCardBlob(cardId: number): Promise<Blob> {
  return fetchBlob(`/api/v1/omr/cards/${cardId}/annotated/`, {
    query: { v: String(Date.now()) },
  })
}

export interface AssignmentScanCards {
  assignment_id: number
  students: Array<{
    student_id: number
    student_no: string
    student_name: string
    source: 'SCAN' | 'ONLINE'
    status: string
    pages: Array<{
      card_id: number
      page_no: number | null
      page_count_expected: number
      group_key: string
      status: string
      status_label?: string
      confidence: number | null
      fail_reason: string
      raw_asset_id: number
      display_asset_id?: number
      source_image_asset_id: number | null
      recognized_student_no: string
    }>
    card_ids: number[]
  }>
  unassigned: Array<{
    card_id: number
    recognized_student_no: string
    page_no: number | null
    status: string
    status_label?: string
    fail_reason: string
    raw_asset_id: number
    display_asset_id?: number
  }>
  total_pages: number
}

export async function getAssignmentScanCards(
  assignmentId: number,
): Promise<AssignmentScanCards> {
  return http.get<AssignmentScanCards>(
    `/api/v1/assignments/${assignmentId}/scan-cards/`)
}

export const CARD_STATUS_LABEL: Record<string, string> = {
  PENDING: '待识别',
  NEEDS_REVIEW: '待复核',
  AUTO_OK: '已识别',
  REVIEWED: '已复核',
  FAILED: '识别失败',
  VOID: '已作废',
}

/** 未知状态统一中文，绝不裸露英文码。 */
export function cardStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return CARD_STATUS_LABEL[status] ?? `未知状态（${status}）`
}

export const CARD_STATUS_TONE: Record<
  string, 'info' | 'success' | 'warning' | 'danger'
> = {
  PENDING: 'info',
  AUTO_OK: 'success',
  NEEDS_REVIEW: 'warning',
  REVIEWED: 'success',
  VOID: 'info',
  FAILED: 'danger',
}

/** 扫描批次状态中文。 */
export const BATCH_STATUS_LABEL: Record<string, string> = {
  UPLOADING: '上传中',
  PROCESSING: '识别中',
  COMPLETED: '已完成',
  WAITING_REVIEW: '待复核',
  FAILED: '识别失败',
}

export function batchStatusLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return BATCH_STATUS_LABEL[code] ?? `未知状态（${code}）`
}

export const FLAG_LABEL: Record<string, string> = {
  NOT_IN_ROSTER: '学号不在名册',
  STUDENT_NO_MISSING: '未识别到学号',
  STUDENT_NO_INCOMPLETE: '学号不完整',
  STUDENT_NO_EMPTY: '学号区全部未涂',
  STUDENT_NO_UNMATCHED: '学号不在名册',
  STUDENT_UNRESOLVED: '未匹配到学生',
  CHECKSUM_FAILED: '学号校验位错误',
  DUPLICATE: '重复卡',
  SHORT_NO: '学号位数不足',
  DUPLICATE_STUDENT_NO: '学号重复',
  DUP_STUDENT: '同一学生有多张卡',
  DUP_STUDENT_NO: '同一学号出现多次',
  ORPHAN_PAGE: '后页缺少第 1 页',
  DUPLICATE_PAGE: '重复页',
  MISSING_PAGE: '缺页',
}

/** 把原因文本里残留的英文标记码替换成中文（旧数据/后端拼接）。 */
export function localizeReason(text: string | null | undefined): string {
  const raw = (text || '').trim()
  if (!raw) return ''
  return raw.replace(
    /\b[A-Z][A-Z0-9_]*(?:_POS\d+|_Q\d+)?\b/g,
    (code) => (FLAG_LABEL[code] || /_POS\d+|_Q\d+/.test(code)
      ? flagLabel(code) : code),
  )
}

/** 识别标记 → 中文；动态位置标记也翻。 */
export function flagLabel(code: string): string {
  if (FLAG_LABEL[code]) return FLAG_LABEL[code]
  let m = /^(MULTI_MARK|NO_MARK|UNCERTAIN|NO_BUBBLES)_POS(\d+)$/.exec(code)
  if (m) {
    const base: Record<string, string> = {
      MULTI_MARK: '多涂',
      NO_MARK: '未涂',
      UNCERTAIN: '不确定',
      NO_BUBBLES: '没有涂点框',
    }
    return `第 ${m[2]} 位${base[m[1]]}`
  }
  m = /^(MULTI_MARK|NO_MARK|UNCERTAIN)_Q(\d+)$/.exec(code)
  if (m) {
    const base: Record<string, string> = {
      MULTI_MARK: '多涂',
      NO_MARK: '未作答',
      UNCERTAIN: '涂点不确定',
    }
    return `${base[m[1]]}（第 ${m[2]} 题）`
  }
  if (code.startsWith('STUDENT_NO_LOW_CONFIDENCE')) return '学号置信度不足'
  return `未知状态（${code}）`
}
