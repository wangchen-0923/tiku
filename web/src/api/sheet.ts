/**
 * 答题卡 API（v2：页面设置 + 题目区域 + 发布 + 绑定）。
 *
 * ★ 设计器只吃后端 `preview/` 返回的 `layout_json`，前端**不算坐标**。
 */

import { fetchBlob, http } from './http'
import type { Paged } from './paper'

// ══════════════════════════════════════════════════
//  页面设置
// ══════════════════════════════════════════════════

export interface HandwriteField {
  key: string
  label: string
}

export interface StudentNoConfig {
  enabled: boolean
  digits: number
  per_row: number
  group_size: number
}

export interface InfoConfig {
  handwrite_fields: HandwriteField[]
  student_no: StudentNoConfig
  notice_text: string
  show_notice: boolean
}

export interface SheetStyle {
  id: number
  name: string
  description: string
  is_preset: boolean
  is_editable: boolean
  page_size: 'A3' | 'A4' | 'A5' | string
  orientation: 'PORTRAIT' | 'LANDSCAPE' | string
  columns?: number
  margin_mm: string
  box_scale?: string
  row_gap_mm?: string
  col_gap_mm?: string
  band_gap_mm?: string
  section_title_gap_mm?: string
  base_font_pt: string
  title_text: string
  title_font_pt: string
  /** 填空行距倍数；实际行高 = 字号(mm) × 本值 + 1.6mm */
  fill_line_spacing?: string
  /** 相邻两个填空题之间的垂直间隙（mm）；0 = 旧卡几何 */
  fill_item_gap_mm?: string
  info_config: InfoConfig
  print_safe_margin_mm: string
  created_at: string
  updated_at: string
}

export type StylePayload = {
  name: string
  page_size?: string
  orientation?: 'PORTRAIT' | 'LANDSCAPE'
  columns?: 1 | 2
  margin_mm?: number
  box_scale?: number
  row_gap_mm?: number
  col_gap_mm?: number
  band_gap_mm?: number
  section_title_gap_mm?: number
  base_font_pt?: number
  title_text?: string
  title_font_pt?: number
  fill_line_spacing?: number
  fill_item_gap_mm?: number
  info_config?: InfoConfig
}

export async function listStyles(): Promise<SheetStyle[]> {
  const d = await http.get<SheetStyle[] | Paged<SheetStyle>>(
    '/api/v1/sheet/styles/')
  return Array.isArray(d) ? d : (d.items ?? [])
}

export async function createStyle(body: StylePayload): Promise<SheetStyle> {
  return http.post<SheetStyle>('/api/v1/sheet/styles/', body)
}

export async function updateStyle(
  id: number, body: Partial<StylePayload>,
): Promise<SheetStyle> {
  return http.patch<SheetStyle>(`/api/v1/sheet/styles/${id}/`, body)
}

export async function deleteStyle(id: number): Promise<void> {
  await http.del(`/api/v1/sheet/styles/${id}/`)
}

export async function duplicateStyle(id: number, name?: string):
Promise<SheetStyle> {
  return http.post<SheetStyle>(`/api/v1/sheet/styles/${id}/duplicate/`, { name })
}

// ══════════════════════════════════════════════════
//  题目区域
// ══════════════════════════════════════════════════

export type SectionType =
  | 'SINGLE_CHOICE'
  | 'MULTIPLE_CHOICE'
  | 'TRUE_FALSE'
  | 'FILL_BLANK'
  | 'ANSWER'
  | 'ESSAY'

export interface FillBlankItem {
  seq?: number
  blanks: number
  chars: number
  separator: string
  per_line: number
  /** 该小题是否另起一行（默认 false） */
  new_line?: boolean
  blank_scores: number[]
  score?: number
}

export interface AnswerItem {
  seq?: number
  lines: number
  style: 'SOLID' | 'DASHED' | 'BLANK' | 'BOX'
  /** 行距：行高 = 字号 × line_spacing */
  line_spacing: number
  sub_count: number
  score: number
}

export interface EssayItem {
  seq?: number
  total_chars: number
  per_line: number
  min_chars: number
  /** 方格间距（mm） */
  cell_gap_mm: number
  score: number
}

export interface TemplateSection {
  order: number
  title: string
  type: SectionType
  start_seq?: number
  count: number
  score: number
  options: number
  per_row: number
  items: Array<FillBlankItem | AnswerItem | EssayItem | Record<string, never>>
}

export const SECTION_TYPE_LABEL: Record<string, string> = {
  SINGLE_CHOICE: '选择题',
  MULTIPLE_CHOICE: '多选题',
  TRUE_FALSE: '判断题',
  FILL_BLANK: '填空题',
  ANSWER: '解答题',
  ESSAY: '作文题',
}

/** 新建一个大题的默认参数。 */
export function newSection(type: SectionType, order: number): TemplateSection {
  const base: TemplateSection = {
    order,
    title: `${order}、${SECTION_TYPE_LABEL[type] ?? '题目'}`,
    type,
    count: 5,
    score: 2,
    options: 4,
    per_row: 5,
    items: [],
  }
  if (type === 'TRUE_FALSE') {
    base.options = 2
    base.count = 5
  } else if (type === 'FILL_BLANK') {
    base.count = 1
    base.score = 4
    base.per_row = 1
    base.items = [{
      blanks: 1, chars: 6, separator: '　　', per_line: 1,
      blank_scores: [4], new_line: false,
    }]
  } else if (type === 'ANSWER') {
    base.count = 1
    base.score = 10
    base.per_row = 0
    base.options = 0
    base.items = [{
      lines: 8, style: 'SOLID', line_spacing: 1.8, sub_count: 1, score: 10,
    }]
  } else if (type === 'ESSAY') {
    base.count = 1
    base.score = 30
    base.per_row = 0
    base.options = 0
    base.items = [{
      total_chars: 800, per_line: 20, min_chars: 500,
      cell_gap_mm: 0.6, score: 30,
    }]
  }
  return base
}

/** 题量变化后补齐/裁剪逐题配置（保持长度 = count）。 */
export function syncSectionItems(sec: TemplateSection): void {
  if (sec.type === 'FILL_BLANK') {
    const items = sec.items as FillBlankItem[]
    while (items.length < sec.count) {
      const last = items[items.length - 1]
      items.push(last
        ? {
            ...last,
            blank_scores: last.blank_scores.slice(0, last.blanks),
          }
        : { blanks: 1, chars: 6, separator: '　　', per_line: 1,
            blank_scores: [2], new_line: false })
    }
    items.length = sec.count
    for (const it of items) {
      if (it.blank_scores.length !== it.blanks) {
        it.blank_scores = Array.from({ length: it.blanks },
          (_, i) => it.blank_scores[i] ?? 2)
      }
      it.per_line = Math.min(it.per_line || 1, it.blanks)
    }
  } else if (sec.type === 'ANSWER') {
    const items = sec.items as AnswerItem[]
    while (items.length < sec.count) {
      const last = items[items.length - 1]
      items.push(last ? { ...last } : {
        lines: 8, style: 'SOLID', line_spacing: 1.8, sub_count: 1, score: 10,
      })
    }
    items.length = sec.count
    for (const it of items) {
      if (it.line_spacing === undefined || it.line_spacing === null) {
        it.line_spacing = 1.8
      }
    }
  } else if (sec.type === 'ESSAY') {
    const items = sec.items as EssayItem[]
    while (items.length < sec.count) {
      const last = items[items.length - 1]
      items.push(last ? { ...last } : {
        total_chars: 800, per_line: 20, min_chars: 500,
        cell_gap_mm: 0.6, score: 30,
      })
    }
    items.length = sec.count
    for (const it of items) {
      if (it.cell_gap_mm === undefined || it.cell_gap_mm === null) {
        it.cell_gap_mm = 0.6
      }
    }
  } else {
    sec.items = []
  }
}

/** 答题卡填空题空数上限。★ 后端 `BLANKS_MIN/BLANKS_MAX = 1/8`
 *  （server/apps/sheet/constants.py:183）；这里曾是 10，属历史遗留，
 *  超出的值会被后端夹回 8，前端必须与后端同一口径。 */
export const BLANKS_MAX = 8

/** 每行空格数上限。★ 后端 `BLANK_PER_LINE_MIN/MAX = 1/4`
 *  （constants.py:186-187），超出会被静默夹到 4。 */
export const BLANK_PER_LINE_MAX = 4

/** 空格数变化时重建每空分值；长度严格等于 blanks。 */
export function syncBlankScores(it: FillBlankItem): void {
  it.blanks = Math.max(1, Math.min(BLANKS_MAX, Number(it.blanks) || 1))
  it.per_line = Math.min(
    Math.max(1, Number(it.per_line) || 1), it.blanks, BLANK_PER_LINE_MAX,
  )
  it.blank_scores = Array.from({ length: it.blanks },
    (_, i) => Number(it.blank_scores[i] ?? 2))
  it.score = it.blank_scores.reduce((a, b) => a + Number(b || 0), 0)
}

// ══════════════════════════════════════════════════
//  坐标（后端产出，前端只读）
// ══════════════════════════════════════════════════

export interface Point {
  x_mm: number
  y_mm: number
  size_mm?: number
  index?: number
}

export interface Bubble extends Point {
  label?: string
  value?: string
  w_mm: number
  h_mm: number
}

export interface LayoutIssue {
  code: string
  message: string
  blocking: boolean
  section_order?: number
  seq?: number
}

export interface LayoutObjective {
  seq: number
  type: string
  section_order: number
  x_mm: number
  y_mm: number
  /** 新模板：列首块；旧模板只有 row_marker */
  column_marker?: Point
  row_marker?: Point
  options: Bubble[]
}

export interface LayoutPage {
  page_no: number
  anchors: Record<string, Point>
  markers: Point[]
  row_markers: Point[]
  column_markers?: Point[]
  columns?: number
  column_x_mm?: number[]
  column_widths_mm?: number[]
  divider_x_mm?: number | null
  divider_y1_mm?: number
  divider_y2_mm?: number
  section_boxes?: Array<{
    x_mm: number; y_mm: number; w_mm: number; h_mm: number
    section_order: number; continuation?: boolean
  }>
  section_titles?: Array<{
    text: string; legend?: string; legend_x_mm?: number
    x_mm: number; y_mm: number
    top_mm?: number; width_mm?: number; font_pt: number
    lines?: Array<{
      text: string; top_mm: number; baseline_mm: number; width_mm: number
    }>
    line_pitch_mm?: number
    section_order?: number; continuation?: boolean
  }>
  objectives: LayoutObjective[]
  fill_blanks: Array<{
    seq: number
    lines: Array<{
      y_mm: number
      show_qno: boolean
      qno_x_mm: number
      qno: string
      blanks: Array<{ x1_mm: number; x2_mm: number; y_mm: number }>
    }>
  }>
  subjective: Array<{
    seq: number
    style: string
    score: number
    x_mm: number
    y_mm: number
    w_mm: number
    h_mm: number
    sub_blocks: Array<{
      label: string
      x_mm: number
      y_mm: number
      w_mm: number
      h_mm: number
      line_ys: number[]
    }>
  }>
  essays: Array<{
    seq: number
    cell_mm: number
    cell_gap_mm?: number
    cols: number
    x_mm: number
    y_mm: number
    start_row: number
    end_row: number
    total_chars: number
    min_marker: { text: string; x_mm: number; y_mm: number } | null
  }>
  section_headers: Array<{
    text: string
    legend: string
    x_mm: number
    y_mm: number
    font_pt: number
  }>
  header: {
    title: {
      text: string; x_mm: number; y_mm: number; font_pt: number;
      baseline_mm?: number; height_mm?: number
      lines?: Array<{
        text: string; top_mm: number; baseline_mm: number; width_mm: number
      }>
    } | null
    title_bottom_mm?: number
    info_top_mm?: number
    handwrite: Array<{
      label: string
      x_mm: number
      y_mm: number
      line_x1_mm: number
      line_x2_mm: number
      line_y_mm?: number
      font_pt: number
    }> | {
      rows: Array<{
        label: string
        x_mm: number
        y_mm: number
        line_x1_mm: number
        line_x2_mm: number
        line_y_mm?: number
        label_top_mm?: number
        font_pt: number
      }>
      height_mm: number
      top_mm: number
      row_count?: number
      field_w_mm?: number
      field_gap_mm?: number
    }
    notice: {
      x_mm: number; y_mm: number; w_mm: number; h_mm: number
      box?: { x_mm: number; y_mm: number; w_mm: number; h_mm: number }
      lines: Array<{ text: string; y_mm: number }>
      font_pt: number
    } | null
  } | null
  student_no: {
    enabled: boolean
    digits: number
    x_mm: number
    y_mm: number
    box?: { x_mm: number; y_mm: number; w_mm: number; h_mm: number }
    positions: Array<{
      digit_index: number
      x_mm: number
      y_mm: number
      bubbles: Bubble[]
    }>
    aux_anchor: Point
    label?: { text: string; x_mm: number; y_mm: number; font_pt: number }
    handwrite_box?: {
      x_mm: number; y_mm: number; w_mm: number; h_mm: number
      boxes: Array<{
        digit_index: number; x_mm: number; y_mm: number
        w_mm: number; h_mm: number
      }>
    }
    group_lines: Array<{ x_mm: number; y1_mm: number; y2_mm: number }>
  } | null
  footer: {
    left: string; right: string; y_mm: number; font_pt: number
    left_x_mm?: number; right_x_mm?: number; width_mm?: number
  }
  choice_bands?: Array<{
    band: number; top_mm: number; bottom_mm: number; rows: number
    columns: number; gap_after_mm: number
  }>
}

export interface LayoutJson {
  version: number
  layout_version?: number
  page_size: string
  orientation?: string
  columns?: number
  box_scale?: number
  row_gap_mm?: number
  col_gap_mm?: number
  page_width_mm: number
  page_height_mm: number
  margin_mm: number
  anchor_size_mm: number
  band_gap_mm?: number
  column_gap_mm?: number
  section_title_gap_mm?: number
  title_text?: string
  content_start_mm: number
  content_width_mm?: number
  content_height_mm?: number
  base_font_pt: number
  header_gap_mm?: number
  anchors: Record<string, Point>
  page_count: number
  pages: LayoutPage[]
  issues: LayoutIssue[]
  question_count: number
  total_score: number
  signature: string
}

export interface PreviewResult {
  layout_json: LayoutJson
  issues: LayoutIssue[]
  page_count: number
  question_count: number
  total_score: number
}

// ══════════════════════════════════════════════════
//  答题卡模板
// ══════════════════════════════════════════════════

export type TemplateStatus = 'DRAFT' | 'ACTIVE' | 'DEPRECATED'

export interface SheetTemplateBrief {
  id: number
  name: string
  version: number
  status: TemplateStatus
  style_id: number
  style_name: string
  page_size: string
  margin_mm: string
  page_count: number
  question_count: number
  total_score: string
  asset_id: number | null
  preview_asset_id: number | null
  created_at: string
  updated_at: string
}

export interface SheetTemplateDetail extends SheetTemplateBrief {
  style: SheetStyle
  sections: TemplateSection[]
  question_types: Record<string, unknown>
  threshold_config: Record<string, unknown>
}

export async function listTemplates(params: {
  status?: string
  keyword?: string
  /** 分页条数。★ 不要与纸张筛选 `paperSize` 混用 */
  pageSize?: number
  /** 纸张筛选（A3/A4/A5），映射到查询参数 `paper_size` */
  paperSize?: string
} = {}): Promise<SheetTemplateBrief[]> {
  const d = await http.get<Paged<SheetTemplateBrief>>(
    '/api/v1/sheet/templates/',
    {
      status: params.status,
      keyword: params.keyword,
      paper_size: params.paperSize,
      page_size: params.pageSize ?? 200,
    },
  )
  return d.items ?? []
}

export async function getTemplate(id: number): Promise<SheetTemplateDetail> {
  return http.get<SheetTemplateDetail>(`/api/v1/sheet/templates/${id}/`)
}

export async function createTemplate(body: {
  name: string
  style: StylePayload
  sections: TemplateSection[]
}): Promise<SheetTemplateDetail> {
  return http.post<SheetTemplateDetail>('/api/v1/sheet/templates/', body)
}

export async function updateTemplate(
  id: number,
  body: {
    name?: string
    style?: Partial<StylePayload>
    sections?: TemplateSection[]
  },
): Promise<SheetTemplateDetail> {
  return http.patch<SheetTemplateDetail>(
    `/api/v1/sheet/templates/${id}/`, body)
}

export async function deleteTemplate(id: number): Promise<void> {
  await http.del(`/api/v1/sheet/templates/${id}/`)
}

export async function previewTemplate(
  id: number | null,
  body: { name?: string; style?: Partial<StylePayload>;
    sections?: TemplateSection[] },
): Promise<PreviewResult> {
  const path = id
    ? `/api/v1/sheet/templates/${id}/preview/`
    : '/api/v1/sheet/templates/preview/'
  return http.post<PreviewResult>(path, body)
}

export async function publishTemplate(
  id: number,
): Promise<SheetTemplateDetail> {
  return http.post<SheetTemplateDetail>(
    `/api/v1/sheet/templates/${id}/publish/`)
}

export async function duplicateTemplate(
  id: number,
  body: { name?: string; as_new_version?: boolean } = {},
): Promise<SheetTemplateDetail> {
  return http.post<SheetTemplateDetail>(
    `/api/v1/sheet/templates/${id}/duplicate/`, body)
}

export function templatePdfUrl(templateId: number): string {
  return `/api/v1/sheet/templates/${templateId}/pdf/`
}

export async function getTemplatePdfBlob(
  templateId: number, signal?: AbortSignal,
): Promise<Blob> {
  return fetchBlob(templatePdfUrl(templateId), { signal })
}

/** 触发浏览器下载。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** 新标签页打开 PDF（Chrome 的 iframe + blob 有已知空白问题）。 */
export function openBlobInNewTab(blob: Blob): boolean {
  const url = URL.createObjectURL(blob)
  const w = window.open(url, '_blank')
  if (!w) {
    URL.revokeObjectURL(url)
    return false
  }
  return true
}

// ══════════════════════════════════════════════════
//  卷 ↔ 卡 绑定
// ══════════════════════════════════════════════════

export interface PaperSheetBinding {
  id: number
  paper_id: number
  sheet_template_id: number
  template_name: string
  template_version: number
  page_size: string
  page_count: number
  is_primary: boolean
  bound_at: string
  bind_warnings: unknown[]
  validation_result: ValidationResult
}

export interface ValidationItem {
  seq: number | null
  code: string
  message: string
  expected?: unknown
  actual?: unknown
}

export interface ValidationResult {
  ok: boolean
  blocking: ValidationItem[]
  warnings: ValidationItem[]
  paper_total_score: number
  sheet_total_score: number
  paper_question_count: number
  sheet_question_count: number
}

export interface BindingReport {
  paper_id: number
  paper_status: string
  paper_question_count: number
  paper_total_score: number
  bound_template_ids: number[]
  primary_template_id: number | null
  candidates: Array<{
    template_id: number
    name: string
    version: number
    page_count: number
    question_count: number
    page_size: string
    bindable: boolean
    result: string
    messages: string[]
    validation: ValidationResult
    usable: boolean
  }>
  bindings?: PaperSheetBinding[]
  has_primary?: boolean
}

export async function getBindingReport(
  paperId: number,
): Promise<BindingReport> {
  return http.get<BindingReport>(
    `/api/v1/sheet/papers/${paperId}/binding-report/`)
}

export async function listBindings(paperId: number): Promise<{
  items: PaperSheetBinding[]
  count: number
  report: BindingReport
}> {
  return http.get(`/api/v1/sheet/papers/${paperId}/binding/`)
}

export async function bindSheet(
  paperId: number,
  body: { template_id: number; is_primary?: boolean },
): Promise<PaperSheetBinding> {
  return http.post<PaperSheetBinding>(
    `/api/v1/sheet/papers/${paperId}/binding/`, body)
}

export async function unbindSheet(
  paperId: number,
  body: { binding_id?: number; unbind_all?: boolean },
): Promise<{ removed: number; remaining: number }> {
  return http.del(`/api/v1/sheet/papers/${paperId}/binding/`, {}, body)
}

export async function setPrimaryBinding(
  paperId: number, bindingId: number,
): Promise<PaperSheetBinding> {
  return http.post<PaperSheetBinding>(
    `/api/v1/sheet/papers/${paperId}/binding/set-primary/`,
    { binding_id: bindingId })
}

export const PAGE_SIZE_LABEL: Record<string, string> = {
  A3: 'A3（297×420）',
  A4: 'A4（210×297）',
  A5: 'A5（148×210）',
}

export const TEMPLATE_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  ACTIVE: '已发布',
  DEPRECATED: '已弃用',
}

export const TEMPLATE_STATUS_TONE: Record<
  string, 'info' | 'success' | 'warning'
> = {
  DRAFT: 'info',
  ACTIVE: 'success',
  DEPRECATED: 'warning',
}

export function hasBlockingIssue(issues: LayoutIssue[] | null): boolean {
  return Boolean(issues?.some((i) => i.blocking))
}
