<script setup lang="ts">
/**
 * 试卷导入向导（`ingest`）。
 *
 * ★★ 三句话说明这页为什么长这样：
 *
 *   1. **必须先选任务**。`task` 是组卷取数的最小粒度，导入的题没有任务
 *      就再也抽不到 —— 那种失效是静默的，比报错更糟。
 *   2. **逐题确认后才入库**。后端只处理 `CONFIRMED`/`EDITED`，
 *      未确认的题会以「未确认」被跳过并列出来。
 *   3. **结果分三桶**：入库 / 跳过（重复或未确认）/ 失败（结构不合法）。
 *      把跳过混进入库，老师会以为导进来了，其实没有。
 *
 * ★ 校对页只让改**会影响判分与组卷**的字段：题干、选项、答案、解析、题型、
 *   知识点。导入批次的原始识别结果（页码、告警）保留展示，便于回看原卷。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as I from '@/api/ingest'
import type { IngestBatch, IngestItem, IngestApplyResult } from '@/api/ingest'
import * as KM from '@/api/knowledgeMark'
import type { KnowledgeNodeRow } from '@/api/knowledge'

const file = ref<File | null>(null)
const uploading = ref(false)
const applying = ref(false)
const batch = ref<IngestBatch | null>(null)
const items = ref<IngestItem[]>([])
const result = ref<IngestApplyResult | null>(null)
const explainMissing = ref(true)

/**
 * AI 介入范围（2026-10-17 需求方口径）：
 * ★ 默认只勾「辅助识别」；其余要老师显式勾选，AI 产物一律标「AI 建议」。
 */
const aiScope = ref<I.AiScope[]>(['assist'])
const AI_SCOPE_OPTIONS: Array<{ value: I.AiScope; label: string; hint: string }> = [
  { value: 'assist', label: '辅助识别', hint: '切题失败/题数异常时用 AI 兜底' },
  { value: 'explain', label: '生成解析', hint: '缺解析的题生成解析（需人工确认）' },
  { value: 'answer', label: '生成答案', hint: '给无答案的客观题建议答案（风险最高）' },
  { value: 'node', label: '推荐知识点', hint: '按题目内容对照知识点给候选' },
  { value: 'difficulty', label: '推荐难度', hint: '按题目内容建议 1–5 难度' },
]

/** 全部知识点（含所属任务名）—— 审核时逐题选，不预先选任务 */
const nodes = ref<KnowledgeNodeRow[]>([])

/** 选项行（每题一组，A/B/C/D 各一行） */
const optRows = ref<Record<number, Array<{ label: string; text: string }>>>({})
/** 每题保存中标记 */
const saving = ref<Record<number, boolean>>({})
/** 配图预览的 object URL：key = `${itemId}:${index}` */
const figUrls = ref<Record<string, string>>({})
/** 拖拽中的配图（拖到另一题卡片上即改归属） */
const draggingFigure = ref<{ itemId: number; index: number } | null>(null)
/** 配图「指定题目」下拉的目标题号（key = `${itemId}:${index}`） */
const figTarget = ref<Record<string, number>>({})
/** 配图挂载点（题干/材料/选项 A–D） */
const figAttach = ref<Record<string, string>>({})
const movingFigure = ref<Record<string, boolean>>({})

// ── 确认页布局状态（只影响展示，不改数据协议）──
const uploadOpen = ref(false)
const selectedId = ref<number | null>(null)
/** 窄屏（≤900px）时在「列表 / 详情」之间切换 */
const mobileDetail = ref(false)
const filterMode = ref<'all' | 'abnormal' | 'pending' | 'confirmed'>('all')
const jumpSeq = ref<number | null>(null)
const bulkOpen = ref(false)
const bulkQtype = ref('')
const bulkResult = ref('')
const autoSaveTimers: Record<number, number> = {}
const editVersion: Record<number, number> = {}
const savedFlash = ref<Record<number, boolean>>({})

/** 筛选后的题目列表（仅影响展示） */
const visibleItems = computed(() => {
  const list = items.value
  if (filterMode.value === 'abnormal') return list.filter(isAbnormal)
  if (filterMode.value === 'pending') {
    return list.filter((x) => x.status === 'PENDING')
  }
  if (filterMode.value === 'confirmed') {
    return list.filter(
      (x) => x.status === 'CONFIRMED' || x.status === 'EDITED')
  }
  return list
})

const selectedItem = computed<IngestItem | null>(() => {
  const id = selectedId.value
  if (id != null) {
    const hit = items.value.find((x) => x.id === id)
    if (hit) return hit
  }
  return items.value.find((x) => x.kind !== 'group') ?? items.value[0] ?? null
})

const selectedIndex = computed(() =>
  selectedItem.value
    ? visibleItems.value.findIndex((x) => x.id === selectedItem.value!.id)
    : -1,
)

/** 模板里的非空别名：外层 `v-if="selectedItem"` 保证运行时非空 */
const detailItem = computed<IngestItem>(() => selectedItem.value as IngestItem)

/** 详情页「就近校验」列表 */
const detailNotes = computed(() =>
  selectedItem.value ? validationNotes(selectedItem.value) : [])

/** 小题 → 所属题组 */
const detailParent = computed(() =>
  selectedItem.value ? parentGroup(selectedItem.value) : null)

function statusType(s: string): 'success' | 'info' | 'danger' | 'warning' {
  if (s === 'CONFIRMED' || s === 'EDITED') return 'success'
  if (s === 'DISCARDED') return 'danger'
  if (s === 'PENDING') return 'info'
  return 'warning'
}

function aiScopeLabel(s: string): string {
  return AI_SCOPE_OPTIONS.find((o) => o.value === s)?.label ?? s
}

function typeLabel(it: IngestItem): string {
  if (it.kind === 'group') {
    return it.group_kind === 'CALCULATION' ? '计算题组·材料'
      : it.group_kind === 'DRAWING' ? '识图题组·材料' : '分析题组·材料'
  }
  const base = I.QTYPE_LABELS[it.qtype] ?? (it.qtype || '未识别')
  return it.kind === 'child' ? `${base} · 小题${it.group_seq || ''}` : base
}

/** 判断题/客观题/主观题的答案摘要 */
function answerSummary(it: IngestItem): string {
  if (it.kind === 'group') return '题组材料，不作为题目入库'
  const a = it.answer as
    | { value?: string | string[] | boolean; blanks?: unknown[]; text?: string }
    | null
  if (it.qtype === 'TRUE_FALSE') {
    const v = a?.value
    return typeof v === 'boolean' ? `答案：${v ? '正确' : '错误'}` : '答案：未设置'
  }
  if (it.qtype === 'FILL_BLANK') {
    const n = a?.blanks?.length ?? 0
    return n ? `答案：${n} 个空` : '答案：未设置'
  }
  if (SUBJECTIVE_QTYPES.has(it.qtype)) {
    return (a?.text || '').trim() ? '参考答案：已填写' : '参考答案：未填写'
  }
  const v = a?.value
  if (Array.isArray(v)) return v.length ? `答案：${v.join('')}` : '答案：未设置'
  return v ? `答案：${String(v)}` : '答案：未设置'
}

/** 选项摘要：图片型选项单独标出 */
function optionSummary(it: IngestItem): string {
  if (it.kind === 'group') return '材料/引子'
  if (SUBJECTIVE_QTYPES.has(it.qtype)) return '无 ABCD 选项'
  if (it.qtype === 'FILL_BLANK') return '无 ABCD 选项'
  const rows = optRows.value[it.id] ?? []
  const filled = rows.filter((r) => r.text.trim()).length
  const imgOpts = (it.figures ?? [])
    .filter((f) => (f.attach_point || '').startsWith('OPTION_'))
    .map((f) => (f.attach_point || '').replace('OPTION_', ''))
  if (imgOpts.length) {
    return `图片选项：${[...new Set(imgOpts)].sort().join('/')}`
  }
  return `选项：${filled} 个`
}

function hasFigure(it: IngestItem): boolean {
  return Boolean(it.figures && it.figures.length)
}

function stemPreview(it: IngestItem): string {
  const s = (it.stem_text || '').replace(/\s+/g, ' ').trim()
  if (!s) return it.kind === 'group' ? '（题组材料，无题干）' : '（未识别到题干）'
  return s.length > 92 ? `${s.slice(0, 92)}…` : s
}

function noteTone(note: string): 'ok' | 'warn' | 'danger' {
  if (note.includes('选项图为图片') || note.includes('已人工调整')) return 'ok'
  if (note.includes('AI') && note.includes('不可用')) return 'danger'
  if (note.includes('未') || note.includes('待确认') || note.includes('缺失')
    || note.includes('低') || note.includes('不可用')) return 'warn'
  return 'warn'
}

function groupChildren(it: IngestItem): IngestItem[] {
  if (it.kind !== 'group') return []
  return items.value.filter((x) => x.parent_group_no === it.group_no)
}

function parentGroup(it: IngestItem): IngestItem | null {
  if (it.kind !== 'child') return null
  return items.value.find(
    (x) => x.kind === 'group' && x.group_no === it.parent_group_no) ?? null
}

/** 异常判定：只影响「仅看异常项」筛选与标记，不改数据 */
function isAbnormal(it: IngestItem): boolean {
  if (it.kind === 'group') return false
  if (!it.qtype) return true
  if (it.figures?.some(
    (f) => f.binding_method === 'SEQUENCE'
      || (f.binding_confidence != null && f.binding_confidence < 0.8),
  )) return true
  return it.warnings.some((w) =>
    w.startsWith('OPTIONS_MISSING')
    || w.startsWith('ANSWER_MISSING')
    || w.startsWith('NO_ANSWER')
    || w.startsWith('TYPE_INFERRED')
    || w.startsWith('SEGMENT_UNCERTAIN')
    || w.startsWith('FIGURE_')
    || /^AI_.*UNAVAILABLE/.test(w))
}

/** 就近展示的校验/提示文案（异常 + 正向识别结果） */
function validationNotes(it: IngestItem): string[] {
  const out: string[] = []
  if (it.kind === 'group') {
    out.push('题组材料：确认后随小题一并入库')
    return out
  }
  if (!it.qtype) out.push('题型未识别，请选择题型')
  const optionFigs = (it.figures ?? [])
    .filter((f) => (f.attach_point || '').startsWith('OPTION_'))
  if (optionFigs.length) {
    const labels = [...new Set(optionFigs.map(
      (f) => (f.attach_point || '').replace('OPTION_', '')))].sort()
    out.push(`第 ${it.seq} 题选项图为图片（${labels.join('/')}）`)
  }
  const uncertain = (it.figures ?? []).filter(
    (f) => f.binding_method === 'SEQUENCE'
      || (f.binding_confidence != null && f.binding_confidence < 0.8),
  )
  if (uncertain.length) out.push(`第 ${it.seq} 题图片归属待确认（空间位置推断）`)
  if (it.figures?.some((f) => f.manual)) out.push('图片已人工调整')
  for (const w of it.warnings) {
    if (w.startsWith('OPTIONS_MISSING')) out.push('选项缺失，请在右侧补填')
    else if (w.startsWith('ANSWER_MISSING') || w.startsWith('NO_ANSWER')) {
      out.push('答案未填写')
    } else if (w.startsWith('TYPE_INFERRED')) out.push(w.replace('TYPE_INFERRED: ', '题型推断：'))
    else if (w.startsWith('SEGMENT_UNCERTAIN')) out.push('切题置信度低，请核对题干')
    else if (w.startsWith('FIGURE_')) out.push(w)
    else if (/^AI_.*UNAVAILABLE/.test(w)) out.push(w.replace(/^AI_/, 'AI '))
    else if (w.startsWith('GROUP_CHILD')) continue
    else if (w.startsWith('AI_SUGGESTED')) out.push('AI 建议待人工确认')
    else out.push(w)
  }
  if (!it.node_id || it.difficulty == null) out.push('确认前需选择知识点与难度')
  return [...new Set(out)]
}

/** 概览：总题数 / 各题型 / 图片 / AI / 待确认 */
const overview = computed(() => {
  const real = items.value.filter((x) => x.kind !== 'group')
  const typeMap = new Map<string, number>()
  for (const it of real) {
    const key = typeLabel(it)
    typeMap.set(key, (typeMap.get(key) ?? 0) + 1)
  }
  const allFigs = items.value.flatMap((x) => x.figures ?? [])
  const autoFigs = allFigs.filter(
    (f) => f.manual || f.binding_method === 'CAPTION'
      || f.binding_method === 'OPTION_POS',
  ).length
  const pendingFigs = allFigs.filter(
    (f) => f.binding_method === 'SEQUENCE'
      || (f.binding_confidence != null && f.binding_confidence < 0.8),
  ).length
  const unbound = (batch.value?.warnings ?? []).filter(
    (w) => w.startsWith('FIGURE_') && w.includes('UNBOUND')).length
  const ai = batch.value?.parse_log?.ai ?? {}
  const aiWritten = (ai.answer?.written ?? 0) + (ai.node?.written ?? 0)
    + (ai.difficulty?.written ?? 0)
  const aiUnavailable = items.value.filter((x) =>
    x.warnings.some((w) => /^AI_.*UNAVAILABLE/.test(w))).length
  const aiScopes = batch.value?.parse_log?.ai_scope ?? []
  return {
    total: real.length,
    groups: items.value.filter((x) => x.kind === 'group').length,
    typeMap: [...typeMap.entries()].sort((a, b) => b[1] - a[1]),
    figures: allFigs.length,
    autoFigures: autoFigs,
    pendingFigures: pendingFigs + unbound,
    aiScopes,
    aiWritten,
    aiUnavailable,
    aiAssist: Boolean(ai.assist?.level3_attempted),
    pending: real.filter((x) => x.status === 'PENDING').length,
    abnormal: real.filter(isAbnormal).length,
    confirmed: real.filter(
      (x) => x.status === 'CONFIRMED' || x.status === 'EDITED').length,
  }
})

const firstAbnormal = computed(() =>
  items.value.find((x) => isAbnormal(x)) ?? null)

const bulkConfirmable = computed(() =>
  items.value.filter(
    (x) => x.kind !== 'group' && x.status === 'PENDING' && ready(x)),
)

function selectItem(it: IngestItem): void {
  selectedId.value = it.id
  mobileDetail.value = true
  // 详情打开后把本题配图都取回来（选项图要显示在选项行里）
  for (let i = 0; i < (it.figures?.length ?? 0); i += 1) {
    void loadFigure(it, i, true)
  }
  void nextTick(() => {
    document.getElementById(`imp-list-${it.id}`)
      ?.scrollIntoView({ block: 'nearest' })
  })
}

function selectRelative(delta: number): void {
  if (!visibleItems.value.length) return
  const cur = selectedIndex.value
  const next = cur < 0 ? 0 : Math.min(
    visibleItems.value.length - 1, Math.max(0, cur + delta))
  selectItem(visibleItems.value[next])
}

function jumpToSeq(): void {
  const seq = jumpSeq.value
  if (seq == null) return
  const hit = items.value.find((x) => x.seq === seq)
  if (!hit) {
    ElMessage.warning(`没有第 ${seq} 题`)
    return
  }
  if (!visibleItems.value.some((x) => x.id === hit.id)) {
    filterMode.value = 'all'
  }
  selectItem(hit)
}

function applyFilter(mode: typeof filterMode.value): void {
  filterMode.value = mode
  if (!visibleItems.value.some((x) => x.id === selectedItem.value?.id)) {
    const first = visibleItems.value[0]
    if (first) selectedId.value = first.id
  }
  // 窄屏：筛选后先回到列表，避免停在已不在筛选内的详情
  if (window.matchMedia('(max-width: 900px)').matches) {
    mobileDetail.value = false
  }
}

function locateFirstAbnormal(): void {
  if (!firstAbnormal.value) {
    ElMessage.success('没有发现异常项')
    return
  }
  applyFilter('all')
  selectItem(firstAbnormal.value)
  ElMessage.info(`已定位到第 ${firstAbnormal.value.seq} 题`)
}

/** 从批次告警文本里找题号（如 `..._Q6`），一键定位 */
function locateWarning(w: string): void {
  const m = w.match(/(?:^|[^A-Za-z])Q?(\d{1,3})(?:[^\d]|$)/)
  const seq = m ? Number(m[1]) : null
  const hit = seq != null
    ? items.value.find((x) => x.seq === seq)
    : items.value.find((x) => x.warnings.some((xw) => xw.includes(w)))
  if (hit) selectItem(hit)
  else locateFirstAbnormal()
}

/** 列表缩略图：只预取每道题第一张，失败不打扰老师 */
async function preloadThumbnails(): Promise<void> {
  for (const it of items.value) {
    if (!it.figures?.length) continue
    const key = figureKey(it, 0)
    if (figUrls.value[key]) continue
    try {
      figUrls.value[key] = await I.fetchFigureBlob(it.id, 0)
    } catch {
      // 缩略图失败不提示，详情里点开仍会重试
    }
  }
}

function openUpload(): void {
  uploadOpen.value = true
}

/** 自动保存：点选项 / 改题型 / 删选项后 800ms 落库 */
function scheduleAutoSave(it: IngestItem): void {
  editVersion[it.id] = (editVersion[it.id] ?? 0) + 1
  const old = autoSaveTimers[it.id]
  if (old) window.clearTimeout(old)
  autoSaveTimers[it.id] = window.setTimeout(() => {
    delete autoSaveTimers[it.id]
    void save(it, undefined, { silent: true, auto: true })
  }, 800)
}

function openBulk(): void {
  bulkResult.value = ''
  bulkQtype.value = ''
  bulkOpen.value = true
}

async function bulkConfirm(): Promise<void> {
  const targets = bulkConfirmable.value
  if (!targets.length) {
    ElMessage.warning('没有可批量确认的题：请先为每道题选好知识点与难度')
    return
  }
  let ok = 0
  for (const it of targets) {
    if (await save(it, 'CONFIRMED', { silent: true })) ok += 1
  }
  bulkResult.value = `批量确认：成功 ${ok} 道，失败 ${targets.length - ok} 道`
  ElMessage.success(`已批量确认 ${ok} 道`)
}

async function bulkApplyQtype(): Promise<void> {
  if (!bulkQtype.value) {
    ElMessage.warning('先选择题型')
    return
  }
  const targets = visibleItems.value.filter(
    (x) => x.kind !== 'group' && x.status !== 'DISCARDED')
  if (!targets.length) {
    ElMessage.warning('当前筛选下没有可修改的题')
    return
  }
  let ok = 0
  for (const it of targets) {
    it.qtype = bulkQtype.value
    onQtypeChange(it)
    if (await save(it, undefined, { silent: true })) ok += 1
  }
  bulkResult.value = `批量修正题型：成功 ${ok}/${targets.length} 道`
  ElMessage.success(`已批量修正 ${ok} 道题的题型`)
}

/** 答案编辑状态（每题一份） */
interface AnsState {
  picked: string[]      // 选择题：选中的选项字母
  bool: boolean | null  // 判断题
  blanks: number        // 填空题的空数
  text: string          // 填空题：每行一个空的答案
}
const ans = ref<Record<number, AnsState>>({})

function toAns(it: IngestItem): AnsState {
  const a = it.answer as
    | { value?: string | string[] | boolean; blanks?: Array<{ accepted?: string[] }>; text?: string }
    | null
  if (it.qtype === 'TRUE_FALSE') {
    // ★ 判断题与选择题走同一套「选中项 = 答案」：
    //   true → A（正确），false → B（错误）
    return {
      picked: typeof a?.value === 'boolean' ? [a.value ? 'A' : 'B'] : [],
      bool: typeof a?.value === 'boolean' ? a.value : null,
      blanks: 1, text: '',
    }
  }
  if (it.qtype === 'FILL_BLANK') {
    const blanks = a?.blanks ?? []
    return {
      picked: [], bool: null,
      blanks: Math.max(1, blanks.length || (it.answer_raw ? 1 : 1)),
      text: blanks.map((b) => (b.accepted ?? []).join('/')).join('\n'),
    }
  }
  if (SUBJECTIVE_QTYPES.has(it.qtype)) {
    return { picked: [], bool: null, blanks: 1, text: String(a?.text ?? it.answer_raw ?? '') }
  }
  const v = a?.value
  return { picked: Array.isArray(v) ? [...v] : v ? [String(v)] : [],
    bool: null, blanks: 1, text: '' }
}

/** 无选项题型：答案是一段文字（分析/计算/简答/作图/论述） */
const SUBJECTIVE_QTYPES = new Set([
  'ANALYSIS', 'CALCULATION', 'SHORT_ANSWER', 'DRAWING', 'ESSAY',
])

/** 这道题是否用「选项行 + 点选」形态（判断题也含在内） */
function hasOptionRows(it: IngestItem): boolean {
  return !SUBJECTIVE_QTYPES.has(it.qtype) && it.qtype !== 'FILL_BLANK'
}

function isPicked(it: IngestItem, label: string): boolean {
  return ans.value[it.id]?.picked.includes(label) ?? false
}

/**
 * ★★ 选中态 ⇄ 答案双向同步（2026-10-18 需求）。
 *
 * 选中项就是答案：点选后立刻写回 `it.answer` / `it.answer_raw`，
 * 保存时原样提交；后端返回后 `toAns()` 再按答案回填选中态。
 */
function syncAnswerFromPick(it: IngestItem): void {
  const st = ans.value[it.id]
  if (!st) return
  if (it.qtype === 'TRUE_FALSE') {
    const v = st.picked[0] === 'A' ? true : st.picked[0] === 'B' ? false : null
    st.bool = v
    it.answer = v == null ? null : { value: v }
    it.answer_raw = v == null ? '' : (v ? '对' : '错')
    return
  }
  if (!st.picked.length) {
    it.answer = null
    it.answer_raw = ''
    return
  }
  it.answer = { value: st.picked.length === 1 ? st.picked[0] : [...st.picked].sort() }
  it.answer_raw = st.picked.join('')
}

function pick(it: IngestItem, label: string): void {
  const st = ans.value[it.id]
  if (!st) return
  if (it.qtype === 'MULTIPLE_CHOICE') {
    st.picked = st.picked.includes(label)
      ? st.picked.filter((x) => x !== label)
      : [...st.picked, label].sort()
  } else {
    // 单选/判断：再点一次已选项可取消
    st.picked = st.picked.includes(label) ? [] : [label]
  }
  syncAnswerFromPick(it)
  scheduleAutoSave(it)
}

/** 从编辑状态生成后端要的 answer + answer_raw */
function buildAnswer(it: IngestItem): {
  answer: Record<string, unknown> | null
  raw: string
} {
  const st = ans.value[it.id]
  if (!st) return { answer: null, raw: it.answer_raw }
  if (it.qtype === 'TRUE_FALSE') {
    const v = st.picked[0] === 'A'
      ? true : st.picked[0] === 'B' ? false : null
    return v == null
      ? { answer: null, raw: '' }
      : { answer: { value: v }, raw: v ? '对' : '错' }
  }
  if (it.qtype === 'FILL_BLANK') {
    const lines = st.text.split('\n').map((x) => x.trim()).filter(Boolean)
    if (!lines.length) return { answer: null, raw: '' }
    return {
      answer: {
        blanks: lines.map((x, i) => ({
          index: i + 1, accepted: x.split('/').map((y) => y.trim()).filter(Boolean),
        })),
      },
      raw: lines.join('、'),
    }
  }
  if (SUBJECTIVE_QTYPES.has(it.qtype)) {
    const text = st.text.trim()
    return text
      ? { answer: { text }, raw: text }
      : { answer: null, raw: '' }
  }
  if (!st.picked.length) return { answer: null, raw: '' }
  return {
    answer: { value: st.picked.length === 1 ? st.picked[0] : st.picked },
    raw: st.picked.join(''),
  }
}

/** 难度标签（与难度打标页同一套口径） */
const DIFFICULTY_LABELS: Record<number, string> = {
  1: '很易', 2: '较易', 3: '中等', 4: '较难', 5: '很难',
}

/** 逐题确认进度 */
const progress = computed(() => {
  const total = items.value.length
  const done = items.value.filter(
    (x) => x.status === 'CONFIRMED' || x.status === 'EDITED').length
  const discarded = items.value.filter((x) => x.status === 'DISCARDED').length
  return { total, done, discarded, pending: total - done - discarded }
})

const canApply = computed(() =>
  !!batch.value && progress.value.done > 0
  && batch.value.status !== 'COMPLETED')

/** 这道题能不能确认：知识点与难度都必须选（入库的前置条件） */
function ready(it: IngestItem): boolean {
  return it.node_id != null && it.difficulty != null
}

/**
 * 选项改为**独立输入框**（A/B/C/D 各一行），与题库编辑页一致。
 *
 * ★ 2026-10-17 需求方口径：识别后选项就应分开，不要让老师在
 *   一个文本框里改 `A.xxx B.yyy` —— 那种形态既容易改坏，也没法逐项挂图。
 */
interface OptRow { label: string; text: string }

function toRows(it: IngestItem): OptRow[] {
  // ★ 判断题固定 A=正确 / B=错误：选中项就是答案，不再单独给「对/错」按钮
  if (it.qtype === 'TRUE_FALSE') {
    return [
      { label: 'A', text: '正确' },
      { label: 'B', text: '错误' },
    ]
  }
  if (SUBJECTIVE_QTYPES.has(it.qtype) || it.qtype === 'FILL_BLANK') return []
  const rows: OptRow[] = (it.options ?? []).map((o) => ({
    label: o.label, text: o.content_text,
  }))
  // 选择题默认给满 4 行（缺的留空，老师可直接补）
  while (rows.length < 4) {
    rows.push({ label: String.fromCharCode(65 + rows.length), text: '' })
  }
  return rows
}

function optionsOf(it: IngestItem): Array<{ label: string; content_text: string }> {
  return (optRows.value[it.id] ?? [])
    .filter((r) => r.text.trim())
    .map((r) => ({ label: r.label, content_text: r.text.trim() }))
}

/**
 * 题型切换：选项行与答案形态必须跟着变。
 * ★ 选择题 → 判断题/主观题时清空旧答案，避免「A/B 选项答案」被当成
 *   判断题的布尔值或主观题文本提交（类型不兼容，入库会失败）。
 */
function onQtypeChange(it: IngestItem): void {
  it.answer = null
  it.answer_raw = ''
  optRows.value[it.id] = toRows(it)
  ans.value[it.id] = toAns(it)
  scheduleAutoSave(it)
}

function addOpt(it: IngestItem): void {
  const rows = optRows.value[it.id] ?? []
  const next = String.fromCharCode(65 + rows.length)
  rows.push({ label: next, text: '' })
  optRows.value[it.id] = rows
  scheduleAutoSave(it)
}

function removeOpt(it: IngestItem, index: number): void {
  const rows = optRows.value[it.id] ?? []
  const st = ans.value[it.id]
  // ★ 删除选项会重排字母：按「选项内容」把已选答案映射到新 label，
  //   否则选中态会指向另一个选项（选项与答案就不同步了）。
  const selectedTexts = new Set(
    (st?.picked ?? [])
      .map((label) => rows.find((r) => r.label === label)?.text ?? '')
      .filter(Boolean),
  )
  const kept = rows.filter((_, i) => i !== index)
  optRows.value[it.id] = kept.map((r, i) => ({
    label: String.fromCharCode(65 + i), text: r.text,
  }))
  if (st) {
    st.picked = (optRows.value[it.id] ?? [])
      .filter((r) => selectedTexts.has(r.text))
      .map((r) => r.label)
    syncAnswerFromPick(it)
  }
  scheduleAutoSave(it)
}

async function loadOptions(): Promise<void> {
  try {
    // ★ 全部知识点：审核时逐题选，上传前不选任务/知识点（2026-10-17 口径）
    nodes.value = await KM.listKpNodes(1)
  } catch (e) {
    ElMessage.error(`知识点加载失败：${(e as Error).message}`)
  }
}

async function upload(): Promise<void> {
  if (!file.value) {
    ElMessage.warning('先选一个文件（Word / 电子版 PDF / Excel）')
    return
  }
  uploading.value = true
  result.value = null
  try {
    const r = await I.uploadPaper(file.value, aiScope.value)
    await openBatch(r.batch_id)
    uploadOpen.value = false
    ElMessage.success(`已切出 ${r.detected} 道题，请逐题定难度与知识点后确认`)
  } catch (e) {
    ElMessage.error(`上传/切题失败：${(e as Error).message}`)
  } finally {
    uploading.value = false
  }
}

async function openBatch(id: number): Promise<void> {
  const b = await I.getBatch(id)
  batch.value = b
  items.value = b.items ?? []
  optRows.value = {}
  ans.value = {}
  for (const it of items.value) {
    optRows.value[it.id] = toRows(it)
    ans.value[it.id] = toAns(it)
  }
  selectedId.value = (items.value.find((x) => x.kind !== 'group')
    ?? items.value[0])?.id ?? null
  mobileDetail.value = false
  filterMode.value = 'all'
  jumpSeq.value = null
  void preloadThumbnails()
}

async function save(
  it: IngestItem,
  status?: 'CONFIRMED' | 'DISCARDED',
  opts: { silent?: boolean; auto?: boolean } = {},
): Promise<boolean> {
  saving.value = { ...saving.value, [it.id]: true }
  const version = editVersion[it.id] ?? 0
  try {
    const built = buildAnswer(it)
    const patch: I.ItemPatch = {
      stem_text: it.stem_text,
      qtype: it.qtype,
      answer: built.answer,
      answer_raw: built.raw,
      explanation: it.explanation,
      node_id: it.node_id,
      difficulty: it.difficulty,
      // ★ 判断题提交规范两选项；主观题不提交选项
      options: hasOptionRows(it) ? optionsOf(it) : [],
    }
    if (status) patch.status = status
    if (status === 'CONFIRMED' && !ready(it)) {
      if (!opts.silent) {
        ElMessage.warning('确认前请先选好知识点与难度（入库的前置条件）')
      }
      return false
    }
    // ★ 选择题应有 4 个选项、判断题答案必须选「对/错」：
    //   这两条不拦保存，但确认时必须让老师看到问题（否则入库才失败）
    if (status === 'CONFIRMED') {
      if (it.qtype === 'SINGLE_CHOICE' && (patch.options ?? []).length !== 4) {
        if (!opts.silent) {
          ElMessage.warning(`这题有 ${(patch.options ?? []).length} 个选项，选择题应为 4 个`)
        }
        return false
      }
      if (it.qtype === 'TRUE_FALSE' && built.answer == null) {
        if (!opts.silent) {
          ElMessage.warning('判断题请先点选「A. 正确」或「B. 错误」')
        }
        return false
      }
      if (!built.answer) {
        if (!opts.silent) {
          ElMessage.warning(SUBJECTIVE_QTYPES.has(it.qtype)
            ? '请先在下方填写参考答案（主观题）' : '请先设置答案')
        }
        return false
      }
    }
    if (status === 'DISCARDED') patch.discard_reason = '校对时丢弃'
    const updated = await I.patchItem(it.id, patch)
    // ★ 自动保存竞态保护：请求期间老师又改了，就不用旧响应覆盖新草稿
    if ((editVersion[it.id] ?? 0) === version) {
      const i = items.value.findIndex((x) => x.id === it.id)
      if (i >= 0) items.value[i] = updated
      optRows.value[it.id] = toRows(updated)
      ans.value[it.id] = toAns(updated)
    }
    savedFlash.value = { ...savedFlash.value, [it.id]: true }
    window.setTimeout(() => {
      savedFlash.value = { ...savedFlash.value, [it.id]: false }
    }, 1600)
    if (!opts.silent) {
      ElMessage.success(status === 'CONFIRMED' ? '已确认' : '已保存')
    }
    return true
  } catch (e) {
    if (!opts.silent) {
      ElMessage.error(`保存失败：${(e as Error).message}`)
    }
    return false
  } finally {
    saving.value = { ...saving.value, [it.id]: false }
  }
}

async function discard(it: IngestItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `丢弃第 ${it.seq} 题？丢弃的题不会入库。`, '确认丢弃',
      { confirmButtonText: '丢弃', cancelButtonText: '取消', type: 'warning' })
  } catch {
    return
  }
  await save(it, 'DISCARDED')
}

async function apply(): Promise<void> {
  if (!batch.value) return
  const done = progress.value.done
  const pend = progress.value.pending
  // ★ 只有上传时勾了「生成解析」才会真的调 AI；未勾选时后端也拒绝调用
  const useExplain = explainMissing.value && aiScope.value.includes('explain')
  try {
    await ElMessageBox.confirm(
      `将入库 **${done}** 道已确认的题${pend ? `；还有 ${pend} 道未确认，会被跳过（不会入库）` : ''}。` +
      (useExplain ? '\n缺解析的题会调用 AI 生成解析（每道约 0.002 元）。' : ''),
      '确认入库',
      { confirmButtonText: '确认入库', cancelButtonText: '再看看', type: 'warning' })
  } catch {
    return
  }
  applying.value = true
  try {
    result.value = await I.applyBatch(batch.value.id, {
      explain_missing: useExplain,
    })
    const r = result.value
    if (r.failed) {
      ElMessage.warning(`入库 ${r.created} 道；失败 ${r.failed} 道（见下方清单）`)
    } else {
      ElMessage.success(`入库 ${r.created} 道（跳过 ${r.skipped} 道）`)
    }
    await openBatch(batch.value.id)
  } catch (e) {
    ElMessage.error(`入库失败：${(e as Error).message}`)
  } finally {
    applying.value = false
  }
}

async function loadFigure(
  it: IngestItem, index: number, silent = false,
): Promise<void> {
  const key = `${it.id}:${index}`
  if (figUrls.value[key]) return
  try {
    figUrls.value[key] = await I.fetchFigureBlob(it.id, index)
  } catch (e) {
    if (!silent) {
      ElMessage.error(`配图读取失败：${(e as Error).message}`)
    }
  }
}

/** 配图当前挂载点的中文说明 */
function attachLabel(f: { attach_point?: string }): string {
  const p = f.attach_point || 'STEM'
  if (p === 'STEM') return '题干'
  if (p === 'MATERIAL') return '材料'
  return `选项 ${p.replace('OPTION_', '')}`
}

function figureKey(it: IngestItem, index: number): string {
  return `${it.id}:${index}`
}

/** 选项图在本题 figures 里的下标（没有则 -1） */
function optionFigureIndex(it: IngestItem, label: string): number {
  return (it.figures ?? []).findIndex(
    (f) => f.attach_point === `OPTION_${label}`)
}

function optionFigureUrl(it: IngestItem, label: string): string {
  const i = optionFigureIndex(it, label)
  return i >= 0 ? (figUrls.value[figureKey(it, i)] ?? '') : ''
}

/** 应用一次归属调整（拖拽与「指定题目」共用） */
async function applyFigureMove(
  source: IngestItem, index: number, targetSeq: number,
): Promise<void> {
  if (!targetSeq || targetSeq === source.seq) return
  const target = items.value.find((x) => x.seq === targetSeq)
  if (!target) return
  const key = figureKey(source, index)
  movingFigure.value = { ...movingFigure.value, [key]: true }
  try {
    const body: { target_seq: number; attach_point?: string } = {
      target_seq: targetSeq,
    }
    const att = figAttach.value[key]
    if (att) body.attach_point = att
    const r = await I.moveFigure(source.id, index, body)
    const si = items.value.findIndex((x) => x.id === r.source.id)
    const ti = items.value.findIndex((x) => x.id === r.target.id)
    if (si >= 0) items.value[si] = r.source
    if (ti >= 0) items.value[ti] = r.target
    // 两张题的配图 index 都变了 → 清掉预览缓存，避免显示错图
    for (const k of Object.keys(figUrls.value)) {
      if (k.startsWith(`${r.source.id}:`) || k.startsWith(`${r.target.id}:`)) {
        delete figUrls.value[k]
      }
    }
    savedFlash.value = {
      ...savedFlash.value, [r.source.id]: true, [r.target.id]: true,
    }
    window.setTimeout(() => {
      savedFlash.value = {
        ...savedFlash.value, [r.source.id]: false, [r.target.id]: false,
      }
    }, 1600)
    ElMessage.success(`配图已调整到第 ${targetSeq} 题`)
  } catch (e) {
    ElMessage.error(`调整配图失败：${(e as Error).message}`)
  } finally {
    movingFigure.value = { ...movingFigure.value, [key]: false }
  }
}

function onFigureDragStart(it: IngestItem, index: number): void {
  draggingFigure.value = { itemId: it.id, index }
}

async function onItemDrop(target: IngestItem): Promise<void> {
  const drag = draggingFigure.value
  draggingFigure.value = null
  if (!drag || drag.itemId === target.id) return
  const source = items.value.find((x) => x.id === drag.itemId)
  if (!source) return
  await applyFigureMove(source, drag.index, target.seq)
}

/** 指定题目调整（下拉框 + 按钮） */
async function moveFigureTo(it: IngestItem, index: number): Promise<void> {
  const key = figureKey(it, index)
  const seq = figTarget.value[key]
  if (!seq) {
    ElMessage.warning('先选目标题目')
    return
  }
  await applyFigureMove(it, index, seq)
}

function statusTag(s: string): string {
  return ({ PENDING: '待确认', CONFIRMED: '已确认', EDITED: '改后确认',
    DISCARDED: '已丢弃' } as Record<string, string>)[s] ?? s
}

onMounted(loadOptions)

onBeforeUnmount(() => {
  for (const t of Object.values(autoSaveTimers)) window.clearTimeout(t)
  for (const url of Object.values(figUrls.value)) URL.revokeObjectURL(url)
})
</script>

<template>
  <div class="imp">
    <!-- ══════════════════════════════════════════════════════════
         ① 未导入：上传页
         ══════════════════════════════════════════════════════════ -->
    <section v-if="!batch" class="imp__landing">
      <div class="imp__landing-head">
        <div>
          <div class="imp__eyebrow">导入题目</div>
          <h1 class="imp__title">把试卷导入题库</h1>
          <p class="imp__sub">
            支持 Word(.docx) / 电子版 PDF / Excel 题目表，以及
            <b>扫描版 PDF 与拍照（自动 OCR）</b>。导入只进题库，不生成试卷。
          </p>
        </div>
        <div class="imp__steps" aria-label="导入步骤">
          <span class="is-active">① 上传切题</span>
          <span>② 逐题确认</span>
          <span>③ 入库</span>
        </div>
      </div>

      <el-card shadow="never" class="imp__uploadcard">
        <div class="imp__uploadrow">
          <div class="imp__filestate">
            <span class="imp__label">选择文件</span>
            <input type="file"
              accept=".docx,.pdf,.xlsx,.xls,.png,.jpg,.jpeg,.webp"
              @change="(e) => { const f = (e.target as HTMLInputElement).files?.[0]; if (f) file = f }" />
            <span v-if="file" class="imp__hint">已选：{{ file.name }}</span>
            <span v-else class="imp__hint">Word / PDF / Excel / 图片</span>
          </div>
          <el-button type="primary" size="large" :loading="uploading"
            @click="upload">
            上传并切题
          </el-button>
        </div>

        <div class="imp__hintblock">
          ★ 上传前不用选知识点/任务 —— 切题后逐题确认<b>难度</b>与<b>知识点</b>，
          知识点会自动决定题目归属的任务。
          <br />★ OCR 一页约 20 秒；若提示「未识别到选项」，请在校对页补填
          （`A.xxx B.yyy`）—— 试卷 PDF 的选项字母常印在方框里，OCR 读不到。
        </div>

        <div class="imp__ai">
          <span class="imp__ai-label">AI 介入</span>
          <el-checkbox-group v-model="aiScope" size="small">
            <el-checkbox v-for="o in AI_SCOPE_OPTIONS" :key="o.value"
              :value="o.value">
              <el-tooltip :content="o.hint" placement="top">
                <span>{{ o.label }}</span>
              </el-tooltip>
            </el-checkbox>
          </el-checkbox-group>
          <span class="imp__hint">AI 产出都标「AI 建议」，确认后才入库</span>
        </div>
      </el-card>
    </section>

    <!-- ══════════════════════════════════════════════════════════
         ② 已导入：确认工作台（顶部概览/操作栏 + 左列表 + 右详情）
         ══════════════════════════════════════════════════════════ -->
    <template v-else>
      <header class="imp__topbar">
        <div class="imp__topbar-row">
          <div class="imp__topbar-title">
            <div class="imp__eyebrow">导入题目 / 逐题确认</div>
            <div class="imp__titleline">
              <h1 class="imp__title">批次 #{{ batch.id }}</h1>
              <el-tag size="small" :type="statusType(batch.status)" effect="plain">
                {{ statusTag(batch.status) }}
              </el-tag>
              <el-tag size="small" type="info" effect="plain">
                {{ batch.source_type }}
              </el-tag>
            </div>
            <div class="imp__sub">
              共 <b>{{ overview.total }}</b> 道题
              <span v-if="overview.groups"> · {{ overview.groups }} 个题组材料</span>
              · 已确认 {{ progress.done }} · 待确认 {{ progress.pending }}
              · 已丢弃 {{ progress.discarded }}
            </div>
          </div>

          <div class="imp__topbar-actions">
            <el-button @click="openUpload">返回修改</el-button>
            <el-button @click="openBulk">批量操作</el-button>
            <el-button type="primary" size="large" :loading="applying"
              :disabled="!canApply" @click="apply">
              确认导入（{{ progress.done }}）
            </el-button>
          </div>
        </div>

        <!-- 概览指标 -->
        <div class="imp__metrics">
          <div class="imp__metric">
            <span class="imp__metric-value">{{ overview.total }}</span>
            <span class="imp__metric-label">总题数</span>
          </div>
          <div class="imp__metric">
            <span class="imp__metric-value">{{ overview.pending }}</span>
            <span class="imp__metric-label">待确认</span>
          </div>
          <div class="imp__metric" :class="{ 'is-danger': overview.abnormal > 0 }">
            <span class="imp__metric-value">{{ overview.abnormal }}</span>
            <span class="imp__metric-label">异常项</span>
          </div>
          <div class="imp__metric">
            <span class="imp__metric-value">{{ overview.figures }}</span>
            <span class="imp__metric-label">
              配图 · 自动 {{ overview.autoFigures }} / 待确认 {{ overview.pendingFigures }}
            </span>
          </div>
          <div class="imp__metric"
            :title="overview.aiScopes.map(aiScopeLabel).join('、') || '未勾选 AI'">
            <span class="imp__metric-value">{{ overview.aiScopes.length }}</span>
            <span class="imp__metric-label">
              AI 项 · 已写入 {{ overview.aiWritten }} / 未生效 {{ overview.aiUnavailable }}
            </span>
          </div>
          <div class="imp__metric">
            <span class="imp__metric-value">{{ overview.confirmed }}</span>
            <span class="imp__metric-label">已确认</span>
          </div>
        </div>

        <!-- 题型分布 -->
        <div class="imp__typebar">
          <span class="imp__typebar-label">题型分布</span>
          <span v-for="[label, count] in overview.typeMap" :key="label"
            class="imp__typechip">
            {{ label }} <b>{{ count }}</b>
          </span>
          <span v-if="!overview.typeMap.length" class="imp__hint">暂无题目</span>
        </div>

        <!-- 筛选 / 定位 -->
        <div class="imp__toolbar">
          <div class="imp__filters" role="tablist" aria-label="题目筛选">
            <button type="button" class="imp__filter"
              :class="{ 'is-active': filterMode === 'all' }"
              @click="applyFilter('all')">
              全部 {{ items.length }}
            </button>
            <button type="button" class="imp__filter is-abnormal"
              :class="{ 'is-active': filterMode === 'abnormal' }"
              @click="applyFilter('abnormal')">
              仅看异常 {{ overview.abnormal }}
            </button>
            <button type="button" class="imp__filter"
              :class="{ 'is-active': filterMode === 'pending' }"
              @click="applyFilter('pending')">
              待确认 {{ overview.pending }}
            </button>
            <button type="button" class="imp__filter"
              :class="{ 'is-active': filterMode === 'confirmed' }"
              @click="applyFilter('confirmed')">
              已确认 {{ overview.confirmed }}
            </button>
          </div>

          <div class="imp__tools">
            <el-select v-model="jumpSeq" filterable clearable size="small"
              placeholder="定位题号" style="width: 150px" @change="jumpToSeq">
              <el-option v-for="it in items" :key="it.id"
                :label="`第 ${it.seq} 题 · ${typeLabel(it)}`" :value="it.seq" />
            </el-select>
            <el-button size="small" :disabled="selectedIndex <= 0"
              @click="selectRelative(-1)">上一题</el-button>
            <el-button size="small"
              :disabled="selectedIndex < 0 || selectedIndex >= visibleItems.length - 1"
              @click="selectRelative(1)">下一题</el-button>
            <el-button v-if="overview.abnormal" type="warning" plain size="small"
              @click="locateFirstAbnormal">
              定位异常（{{ overview.abnormal }}）
            </el-button>
          </div>
        </div>

        <div v-if="batch.warnings?.length" class="imp__batchwarn">
          <span class="imp__batchwarn-title">切题 / 配图告警</span>
          <button v-for="(w, i) in batch.warnings.slice(0, 4)" :key="i"
            type="button" class="imp__warnchip" @click="locateWarning(w)">
            {{ w.length > 64 ? `${w.slice(0, 64)}…` : w }}
          </button>
          <span v-if="batch.warnings.length > 4" class="imp__hint">
            +{{ batch.warnings.length - 4 }} 条
          </span>
        </div>
      </header>

      <!-- 入库结果（顶部就近展示，不堆到页尾） -->
      <div v-if="result" class="imp__resultcard">
        <div class="imp__result-head">
          <el-tag type="success" effect="dark">入库 {{ result.created }}</el-tag>
          <el-tag type="info" effect="plain">跳过 {{ result.skipped }}</el-tag>
          <el-tag :type="result.failed ? 'danger' : 'info'" effect="plain">
            失败 {{ result.failed }}
          </el-tag>
          <span class="imp__hint">其中 AI 补解析 {{ result.explained }} 道</span>
          <el-button size="small" text @click="result = null">收起</el-button>
        </div>
        <ul v-if="result.skipped_items.length" class="imp__resultlist">
          <li v-for="s in result.skipped_items" :key="`s${s.seq}`">
            <button type="button" class="imp__linkbtn"
              @click="jumpSeq = s.seq; jumpToSeq()">第 {{ s.seq }} 题</button>
            ：{{ s.reason }}<span v-if="s.detail">（{{ s.detail }}）</span>
          </li>
        </ul>
        <ul v-if="result.failed_items.length"
          class="imp__resultlist imp__resultlist--bad">
          <li v-for="s in result.failed_items" :key="`f${s.seq}`">
            <button type="button" class="imp__linkbtn"
              @click="jumpSeq = s.seq; jumpToSeq()">第 {{ s.seq }} 题</button>
            ：{{ s.reason }}
          </li>
        </ul>
      </div>

      <!-- 主工作区：左列表 / 右详情 -->
      <div class="imp__workspace"
        :class="{ 'is-mobile-detail': mobileDetail }">
        <!-- ── 左：题目列表 ── -->
        <aside class="imp__listpane" aria-label="题目列表">
          <div class="imp__pane-head">
            <span>题目列表</span>
            <span class="imp__hint">
              {{ visibleItems.length }} / {{ items.length }}
            </span>
          </div>
          <div class="imp__list" role="listbox" aria-label="逐题列表">
            <button v-for="it in visibleItems" :id="`imp-list-${it.id}`"
              :key="it.id" type="button" class="imp__row"
              :class="{
                'is-selected': selectedItem?.id === it.id,
                'is-abnormal': isAbnormal(it),
                'is-done': it.status === 'CONFIRMED' || it.status === 'EDITED',
                'is-discarded': it.status === 'DISCARDED',
                'is-dragover': Boolean(draggingFigure),
              }"
              role="option" :aria-selected="selectedItem?.id === it.id"
              @click="selectItem(it)"
              @keydown.up.prevent="selectRelative(-1)"
              @keydown.down.prevent="selectRelative(1)"
              @dragover.prevent @drop.stop="onItemDrop(it)">
              <span class="imp__row-seq">#{{ it.seq }}</span>
              <span class="imp__row-body">
                <span class="imp__row-top">
                  <el-tag size="small" effect="plain">{{ typeLabel(it) }}</el-tag>
                  <el-tag size="small" :type="statusType(it.status)" effect="plain">
                    {{ statusTag(it.status) }}
                  </el-tag>
                  <span v-if="isAbnormal(it)" class="imp__badge">异常</span>
                  <span v-if="saving[it.id]" class="imp__badge is-saving">
                    保存中
                  </span>
                </span>
                <span class="imp__row-stem">{{ stemPreview(it) }}</span>
                <span class="imp__row-meta">
                  <span>{{ answerSummary(it) }}</span>
                  <span>·</span>
                  <span>{{ optionSummary(it) }}</span>
                  <span v-if="hasFigure(it)">· 图 {{ it.figures?.length }}</span>
                </span>
                <span v-if="validationNotes(it).length" class="imp__row-note"
                  :class="`is-${noteTone(validationNotes(it)[0])}`">
                  {{ validationNotes(it)[0] }}
                </span>
              </span>
              <span v-if="figUrls[figureKey(it, 0)]" class="imp__row-thumb">
                <el-image :src="figUrls[figureKey(it, 0)]" fit="cover" />
              </span>
              <span v-else-if="hasFigure(it)" class="imp__row-thumb is-empty">
                图 {{ it.figures?.length }}
              </span>
            </button>

            <el-empty v-if="!visibleItems.length" :image-size="72"
              description="当前筛选没有题目" />
          </div>
        </aside>

        <!-- ── 右：题目详情 ── -->
        <section v-if="selectedItem" class="imp__detailpane"
          :aria-label="`第 ${detailItem.seq} 题详情`"
          @dragover.prevent @drop="onItemDrop(detailItem)">
          <div class="imp__detail-head">
            <div class="imp__detail-headleft">
              <el-button class="imp__back" text size="small"
                @click="mobileDetail = false">← 返回列表</el-button>
              <span class="imp__detail-seq">第 {{ detailItem.seq }} 题</span>
              <el-tag size="small" effect="plain">{{ typeLabel(detailItem) }}</el-tag>
              <el-tag size="small" :type="statusType(detailItem.status)"
                effect="plain">{{ statusTag(detailItem.status) }}</el-tag>
              <span v-if="detailParent" class="imp__hint">
                属于
                <button type="button" class="imp__linkbtn"
                  @click="selectItem(detailParent)">第 {{ detailParent.seq }} 题题组</button>
              </span>
              <span v-if="saving[detailItem.id]" class="imp__saveflash">
                保存中…
              </span>
              <span v-else-if="savedFlash[detailItem.id]" class="imp__saveflash is-ok">
                ✓ 已自动保存
              </span>
            </div>
            <div class="imp__detail-actions">
              <el-button size="small" :disabled="selectedIndex <= 0"
                @click="selectRelative(-1)">上一题</el-button>
              <el-button size="small"
                :disabled="selectedIndex < 0 || selectedIndex >= visibleItems.length - 1"
                @click="selectRelative(1)">下一题</el-button>
              <el-button size="small" :loading="saving[detailItem.id]"
                @click="save(detailItem)">保存</el-button>
              <el-button v-if="detailItem.kind !== 'group'" size="small"
                type="primary" :loading="saving[detailItem.id]"
                :disabled="detailItem.status === 'DISCARDED' || !ready(detailItem)"
                @click="save(detailItem, 'CONFIRMED')">确认本题</el-button>
              <el-button size="small" type="danger" plain
                :disabled="detailItem.status === 'DISCARDED'"
                @click="discard(detailItem)">丢弃</el-button>
            </div>
          </div>

          <div class="imp__detail-scroll">
            <div class="imp__detail-grid">
              <!-- 详情主列 -->
              <div class="imp__detail-main">
                <div v-if="detailNotes.length" class="imp__inline-notes">
                  <span v-for="n in detailNotes" :key="n" class="imp__note"
                    :class="`is-${noteTone(n)}`">{{ n }}</span>
                </div>

                <div class="imp__field">
                  <label class="imp__label" :for="`stem-${detailItem.id}`">
                    题干
                  </label>
                  <el-input :id="`stem-${detailItem.id}`"
                    v-model="detailItem.stem_text" type="textarea" :rows="4"
                    placeholder="题干"
                    @blur="scheduleAutoSave(detailItem)" />
                </div>

                <div class="imp__field-grid">
                  <div class="imp__field">
                    <label class="imp__label">题型</label>
                    <el-select v-model="detailItem.qtype" size="small"
                      style="width: 100%"
                      @change="onQtypeChange(detailItem)">
                      <el-option v-for="(lab, k) in I.QTYPE_LABELS" :key="k"
                        :label="lab" :value="k" />
                    </el-select>
                  </div>
                  <div class="imp__field imp__field--wide">
                    <label class="imp__label">
                      知识点 <em class="imp__required">必选</em>
                    </label>
                    <el-select v-model="detailItem.node_id" size="small"
                      filterable clearable placeholder="知识点（必选）"
                      style="width: 100%"
                      @change="scheduleAutoSave(detailItem)">
                      <el-option v-for="n in nodes" :key="n.id"
                        :label="`${n.code} ${n.name} · ${n.task_name}`"
                        :value="n.id" />
                    </el-select>
                  </div>
                  <div class="imp__field">
                    <label class="imp__label">
                      难度 <em class="imp__required">必选</em>
                    </label>
                    <el-select v-model="detailItem.difficulty" size="small"
                      clearable placeholder="难度（必选）" style="width: 100%"
                      @change="scheduleAutoSave(detailItem)">
                      <el-option v-for="(lab, k) in DIFFICULTY_LABELS" :key="k"
                        :label="`${k} ${lab}`" :value="Number(k)" />
                    </el-select>
                  </div>
                </div>

                <div v-if="!ready(detailItem)" class="imp__needbar">
                  确认前请先选好<b>知识点</b>与<b>难度</b>（入库的前置条件）
                </div>

                <!-- 题组材料：没有选项/答案 -->
                <div v-if="detailItem.kind === 'group'" class="imp__group-note">
                  这是题组（材料/分析/计算题）的<b>引子</b>，不作为题目入库；
                  右侧可查看并逐题确认本组小题。
                </div>

                <!-- 普通题：选项/答案 + 解析 -->
                <template v-else>
                  <section class="imp__section">
                    <div class="imp__section-head">
                      <span>{{ hasOptionRows(detailItem)
                        ? '选项与答案'
                        : detailItem.qtype === 'FILL_BLANK'
                          ? '填空答案' : '作答与答案' }}</span>
                      <span class="imp__hint">{{ hasOptionRows(detailItem)
                        ? '点选即答案，自动保存' : '主观题填写参考答案' }}</span>
                    </div>

                    <div v-if="hasOptionRows(detailItem)" class="imp__opts">
                      <div class="imp__opt-hint">
                        点选即答案
                        <span v-if="detailItem.qtype === 'TRUE_FALSE'">
                          （判断题：A. 正确 / B. 错误）
                        </span>
                        <span v-else-if="detailItem.qtype === 'MULTIPLE_CHOICE'">
                          （可多选）
                        </span>
                      </div>
                      <div v-for="(r, ri) in (optRows[detailItem.id] ?? [])"
                        :key="ri" class="imp__opt-row"
                        :class="{ 'is-picked': isPicked(detailItem, r.label) }"
                        @click="pick(detailItem, r.label)">
                        <el-checkbox v-if="detailItem.qtype === 'MULTIPLE_CHOICE'"
                          :model-value="isPicked(detailItem, r.label)"
                          @click.stop="pick(detailItem, r.label)" />
                        <el-radio v-else
                          :model-value="ans[detailItem.id]?.picked[0] ?? ''"
                          :value="r.label"
                          @click.stop="pick(detailItem, r.label)" />
                        <span class="imp__opt-label"
                          @click.stop="pick(detailItem, r.label)">{{ r.label }}</span>
                        <el-input v-model="r.text" size="small"
                          :placeholder="optionFigureIndex(detailItem, r.label) >= 0
                            ? '图片选项（可补充文字）' : '选项内容'"
                          @click.stop
                          @blur="scheduleAutoSave(detailItem)" />
                        <span v-if="optionFigureIndex(detailItem, r.label) >= 0"
                          class="imp__opt-thumb" title="该选项是图片">
                          <el-image v-if="optionFigureUrl(detailItem, r.label)"
                            :src="optionFigureUrl(detailItem, r.label)"
                            fit="cover" />
                          <span v-else>图</span>
                        </span>
                        <el-button v-if="detailItem.qtype !== 'TRUE_FALSE'"
                          size="small" text type="danger"
                          @click.stop="removeOpt(detailItem, ri)">删</el-button>
                      </div>
                      <div class="imp__opt-acts">
                        <el-button v-if="detailItem.qtype !== 'TRUE_FALSE'"
                          size="small" text type="primary"
                          @click="addOpt(detailItem)">+ 添加选项</el-button>
                        <span class="imp__picked">
                          本题答案：
                          <b>{{ ans[detailItem.id]?.picked.join('') || '未选' }}</b>
                        </span>
                        <span v-if="detailItem.qtype === 'SINGLE_CHOICE'
                          && optionsOf(detailItem).length !== 4"
                          class="imp__warn">
                          选项数 {{ optionsOf(detailItem).length }}（选择题应为 4）
                        </span>
                      </div>
                    </div>

                    <div v-else class="imp__ans">
                      <template v-if="detailItem.qtype === 'FILL_BLANK'">
                        <div class="imp__ans-row">
                          <span class="imp__label">空数</span>
                          <el-input-number v-model="ans[detailItem.id].blanks"
                            :min="1" :max="10" size="small"
                            controls-position="right" style="width: 90px"
                            @change="scheduleAutoSave(detailItem)" />
                        </div>
                        <el-input v-model="ans[detailItem.id].text"
                          type="textarea" :rows="3"
                          placeholder="每行一个空的答案；同一空多个可接受答案用 / 分隔"
                          @blur="scheduleAutoSave(detailItem)" />
                      </template>
                      <template v-else>
                        <label class="imp__label">参考答案 / 作答要点</label>
                        <el-input v-model="ans[detailItem.id].text"
                          type="textarea" :rows="5"
                          placeholder="主观题无 ABCD 选项；这里填写参考答案或评分要点（入库前必须填）"
                          @blur="scheduleAutoSave(detailItem)" />
                        <span class="imp__hint">
                          题型：{{ I.QTYPE_LABELS[detailItem.qtype] ?? detailItem.qtype }}；
                          本题不生成选项
                        </span>
                      </template>
                    </div>
                  </section>

                  <section class="imp__section">
                    <div class="imp__section-head">
                      <span>解析</span>
                      <span class="imp__hint">留空则入库时按 AI 设置决定是否生成</span>
                    </div>
                    <el-input v-model="detailItem.explanation" type="textarea"
                      :rows="3"
                      placeholder="解析（可选；勾选「生成解析」时缺解析会调用 AI）"
                      @blur="scheduleAutoSave(detailItem)" />
                  </section>
                </template>

                <div class="imp__meta">
                  识别页码：{{ detailItem.page ?? '-' }}
                  <span v-if="detailItem.question_id">
                    · 已入库题目 #{{ detailItem.question_id }}
                  </span>
                  <span v-if="detailItem.discard_reason">
                    · 丢弃原因：{{ detailItem.discard_reason }}
                  </span>
                  <span v-if="detailItem.section_title">
                    · 大题：{{ detailItem.section_title }}
                  </span>
                </div>
              </div>

              <!-- 详情侧栏：校验 / 配图 / AI / 题组 -->
              <aside class="imp__detail-aside">
                <section class="imp__panel">
                  <div class="imp__panel-head">
                    <h3>校验与提示</h3>
                    <el-tag v-if="isAbnormal(detailItem)" size="small"
                      type="warning" effect="plain">异常</el-tag>
                    <el-tag v-else size="small" type="success"
                      effect="plain">正常</el-tag>
                  </div>
                  <ul v-if="detailNotes.length" class="imp__notes">
                    <li v-for="n in detailNotes" :key="n"
                      :class="`is-${noteTone(n)}`">{{ n }}</li>
                  </ul>
                  <p v-else class="imp__hint">未发现需要处理的问题</p>
                </section>

                <section class="imp__panel">
                  <div class="imp__panel-head">
                    <h3>配图与素材</h3>
                    <span class="imp__hint">
                      {{ detailItem.figures?.length || 0 }} 张
                    </span>
                  </div>
                  <p v-if="!hasFigure(detailItem)" class="imp__hint">
                    本题没有配图
                  </p>
                  <div v-else class="imp__figlist">
                    <div v-for="(f, fi) in detailItem.figures" :key="fi"
                      class="imp__figcard" draggable="true"
                      @dragstart="onFigureDragStart(detailItem, fi)"
                      @dragend="draggingFigure = null">
                      <div class="imp__figthumb">
                        <el-image v-if="figUrls[figureKey(detailItem, fi)]"
                          :src="figUrls[figureKey(detailItem, fi)]"
                          fit="contain" class="imp__fig" />
                        <el-button v-else size="small" text type="primary"
                          @click="loadFigure(detailItem, fi)">
                          加载第 {{ fi + 1 }} 张（第 {{ f.page }} 页）
                        </el-button>
                      </div>
                      <div class="imp__figcap">
                        <el-tag size="small" effect="plain">
                          {{ f.caption || `第 ${detailItem.seq} 题图` }}
                        </el-tag>
                        <el-tag size="small" type="info" effect="plain">
                          {{ attachLabel(f) }}
                        </el-tag>
                        <span class="imp__hint">
                          {{ f.binding_method || 'SEQUENCE' }}
                          <template v-if="f.manual"> · 手工</template>
                        </span>
                      </div>
                      <div class="imp__figmove">
                        <el-select v-model="figTarget[figureKey(detailItem, fi)]"
                          size="small" filterable placeholder="归属题目"
                          style="width: 100%">
                          <el-option v-for="o in items" :key="o.id"
                            :disabled="o.id === detailItem.id || o.kind === 'group'"
                            :label="`第 ${o.seq} 题${o.stem_text ? '：' + o.stem_text.slice(0, 10) : ''}`"
                            :value="o.seq" />
                        </el-select>
                        <div class="imp__figmove-row">
                          <el-select v-model="figAttach[figureKey(detailItem, fi)]"
                            size="small" placeholder="挂载点" clearable
                            style="flex: 1">
                            <el-option label="题干" value="STEM" />
                            <el-option label="选项 A" value="OPTION_A" />
                            <el-option label="选项 B" value="OPTION_B" />
                            <el-option label="选项 C" value="OPTION_C" />
                            <el-option label="选项 D" value="OPTION_D" />
                          </el-select>
                          <el-button size="small" type="primary" plain
                            :loading="movingFigure[figureKey(detailItem, fi)]"
                            @click="moveFigureTo(detailItem, fi)">移动</el-button>
                        </div>
                      </div>
                    </div>
                  </div>
                  <p class="imp__hint">
                    可拖到左侧题目卡片，或选「归属题目 → 移动」；调整后立即保存
                  </p>
                </section>

                <section v-if="detailItem.ai_suggestions
                  && Object.keys(detailItem.ai_suggestions).length"
                  class="imp__panel">
                  <div class="imp__panel-head">
                    <h3>AI 建议</h3>
                    <el-tag size="small" type="warning" effect="plain">
                      需人工确认
                    </el-tag>
                  </div>
                  <ul class="imp__notes">
                    <li v-if="detailItem.ai_suggestions.answer">
                      答案：{{ String(detailItem.ai_suggestions.answer.value) }}
                      （{{ Math.round((detailItem.ai_suggestions.answer.confidence || 0) * 100) }}%）
                    </li>
                    <li v-if="detailItem.ai_suggestions.node">
                      知识点 #{{ detailItem.ai_suggestions.node.value }}
                      （{{ Math.round((detailItem.ai_suggestions.node.confidence || 0) * 100) }}%）
                    </li>
                    <li v-if="detailItem.ai_suggestions.difficulty">
                      难度：{{ detailItem.ai_suggestions.difficulty.value }}
                      （{{ Math.round((detailItem.ai_suggestions.difficulty.confidence || 0) * 100) }}%）
                    </li>
                  </ul>
                </section>

                <section v-if="detailItem.kind === 'group'" class="imp__panel">
                  <div class="imp__panel-head">
                    <h3>本组小题</h3>
                    <span class="imp__hint">{{ groupChildren(detailItem).length }} 道</span>
                  </div>
                  <div class="imp__childlist">
                    <button v-for="ch in groupChildren(detailItem)" :key="ch.id"
                      type="button" class="imp__childbtn"
                      @click="selectItem(ch)">
                      <span>第 {{ ch.seq }} 题 · {{ typeLabel(ch) }}</span>
                      <el-tag size="small" :type="statusType(ch.status)"
                        effect="plain">{{ statusTag(ch.status) }}</el-tag>
                    </button>
                    <p v-if="!groupChildren(detailItem).length" class="imp__hint">
                      没有识别到小题
                    </p>
                  </div>
                </section>

                <section class="imp__panel">
                  <div class="imp__panel-head"><h3>识别信息</h3></div>
                  <div class="imp__kv">
                    <span>批次</span><b>#{{ batch.id }}</b>
                    <span>来源</span><b>{{ batch.source_type }}</b>
                    <span>页码</span><b>{{ detailItem.page ?? '-' }}</b>
                    <span>状态</span><b>{{ statusTag(detailItem.status) }}</b>
                  </div>
                </section>
              </aside>
            </div>
          </div>
        </section>
      </div>
    </template>

    <!-- ══════════════════════════════════════════════════════════
         ③ 重新导入 / 批量操作
         ══════════════════════════════════════════════════════════ -->
    <el-dialog v-model="uploadOpen" title="返回修改 / 重新导入"
      width="640px" append-to-body>
      <div class="imp__dialog">
        <p class="imp__hint">
          重新上传会新建批次；当前批次的未入库修改不会丢失。
        </p>
        <div class="imp__uploadrow">
          <div class="imp__filestate">
            <span class="imp__label">选择文件</span>
            <input type="file"
              accept=".docx,.pdf,.xlsx,.xls,.png,.jpg,.jpeg,.webp"
              @change="(e) => { const f = (e.target as HTMLInputElement).files?.[0]; if (f) file = f }" />
            <span v-if="file" class="imp__hint">已选：{{ file.name }}</span>
          </div>
        </div>
        <div class="imp__ai">
          <span class="imp__ai-label">AI 介入</span>
          <el-checkbox-group v-model="aiScope" size="small">
            <el-checkbox v-for="o in AI_SCOPE_OPTIONS" :key="o.value"
              :value="o.value">{{ o.label }}</el-checkbox>
          </el-checkbox-group>
        </div>
      </div>
      <template #footer>
        <el-button @click="uploadOpen = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="upload">
          上传并切题
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="bulkOpen" title="批量操作" width="600px"
      append-to-body>
      <div class="imp__bulk">
        <section class="imp__bulk-section">
          <h4>批量确认</h4>
          <p class="imp__hint">
            对<b>已选好知识点与难度</b>的待确认题执行确认；已丢弃的题不会处理。
          </p>
          <el-button type="primary" :disabled="!bulkConfirmable.length"
            @click="bulkConfirm">
            批量确认可确认项（{{ bulkConfirmable.length }}）
          </el-button>
        </section>

        <el-divider />

        <section class="imp__bulk-section">
          <h4>批量修正题型</h4>
          <p class="imp__hint">
            仅修改当前筛选下的题型，并清空该题旧答案形态；题干/选项文字不变。
            当前筛选：{{ visibleItems.length }} 道。
          </p>
          <div class="imp__bulk-row">
            <el-select v-model="bulkQtype" placeholder="选择题型"
              style="width: 180px">
              <el-option v-for="(lab, k) in I.QTYPE_LABELS" :key="k"
                :label="lab" :value="k" />
            </el-select>
            <el-button :disabled="!bulkQtype" @click="bulkApplyQtype">
              应用到当前筛选
            </el-button>
          </div>
        </section>

        <el-alert v-if="bulkResult" type="success" :closable="false"
          :title="bulkResult" class="imp__bulkalert" />
      </div>
      <template #footer>
        <el-button @click="bulkOpen = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>
<style scoped>
/* ══════════════════════════════════════════════════════════════
   页面骨架
   ══════════════════════════════════════════════════════════════ */
.imp {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-4);
  min-width: 0;
}
.imp__eyebrow {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
  letter-spacing: .04em;
}
.imp__title {
  margin: 2px 0 0;
  font-size: var(--zf-text-2xl);
  line-height: var(--zf-leading-tight);
  color: var(--zf-text-primary);
}
.imp__sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
.imp__hint {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
  line-height: var(--zf-leading-normal);
}
.imp__label {
  display: block;
  margin-bottom: var(--zf-space-1);
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-medium);
  color: var(--zf-text-secondary);
}
.imp__required { color: var(--zf-danger); font-style: normal; }
.imp__linkbtn {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--zf-brand);
  font: inherit;
  cursor: pointer;
}
.imp__linkbtn:hover { text-decoration: underline; }

/* ══════════════════════════════════════════════════════════════
   ① 上传页
   ══════════════════════════════════════════════════════════════ */
.imp__landing {
  max-width: 920px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-4);
  padding-top: var(--zf-space-6);
}
.imp__landing-head {
  display: flex;
  justify-content: space-between;
  gap: var(--zf-space-6);
  align-items: flex-start;
}
.imp__steps {
  display: flex;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}
.imp__steps span {
  padding: var(--zf-space-1) var(--zf-space-2);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-pill);
  background: var(--zf-bg-surface);
  white-space: nowrap;
}
.imp__steps span.is-active {
  color: var(--zf-brand);
  border-color: var(--zf-brand-border);
  background: var(--zf-brand-subtle);
}
.imp__uploadcard { padding: var(--zf-space-5); }
.imp__uploadrow {
  display: flex;
  align-items: center;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.imp__filestate {
  flex: 1;
  min-width: 260px;
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
}
.imp__filestate input[type="file"] {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
.imp__hintblock {
  margin-top: var(--zf-space-3);
  padding: var(--zf-space-3);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-subtle);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  line-height: var(--zf-leading-loose);
}
.imp__ai {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  margin-top: var(--zf-space-3);
  padding-top: var(--zf-space-3);
  border-top: 1px dashed var(--zf-border);
}
.imp__ai-label {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}

/* ══════════════════════════════════════════════════════════════
   ② 顶部概览与操作栏（sticky）
   ══════════════════════════════════════════════════════════════ */
.imp__topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
  padding: var(--zf-space-4);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-lg);
  background: color-mix(in srgb, var(--zf-bg-surface) 96%, transparent);
  backdrop-filter: blur(6px);
  box-shadow: var(--zf-shadow-xs);
}
.imp__topbar-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.imp__topbar-title { min-width: 240px; }
.imp__titleline {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__topbar-actions {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  margin-left: auto;
  flex-wrap: wrap;
}
.imp__metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--zf-space-2);
}
.imp__metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: var(--zf-space-2) var(--zf-space-3);
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-subtle);
}
.imp__metric.is-danger {
  border-color: color-mix(in srgb, var(--zf-danger) 35%, var(--zf-border));
  background: var(--zf-danger-bg);
}
.imp__metric-value {
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
  line-height: var(--zf-leading-tight);
}
.imp__metric.is-danger .imp__metric-value { color: var(--zf-danger); }
.imp__metric-label {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
  line-height: var(--zf-leading-tight);
}
.imp__typebar {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  min-width: 0;
}
.imp__typebar-label {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}
.imp__typechip {
  padding: 2px var(--zf-space-2);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-pill);
  background: var(--zf-bg-surface);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  white-space: nowrap;
}
.imp__typechip b { color: var(--zf-text-primary); }
.imp__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
}
.imp__filters {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-subtle);
}
.imp__filter {
  border: 0;
  border-radius: var(--zf-radius-sm);
  background: transparent;
  padding: 5px var(--zf-space-3);
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  cursor: pointer;
  white-space: nowrap;
}
.imp__filter:hover { background: var(--zf-bg-hover); }
.imp__filter.is-active {
  background: var(--zf-bg-surface);
  color: var(--zf-brand);
  font-weight: var(--zf-weight-semibold);
  box-shadow: var(--zf-shadow-xs);
}
.imp__filter.is-abnormal.is-active { color: var(--zf-danger); }
.imp__tools {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__batchwarn {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  padding-top: var(--zf-space-2);
  border-top: 1px dashed var(--zf-border);
}
.imp__batchwarn-title {
  font-size: var(--zf-text-xs);
  color: var(--zf-warning);
  font-weight: var(--zf-weight-semibold);
}
.imp__warnchip {
  max-width: 100%;
  border: 1px solid color-mix(in srgb, var(--zf-warning) 30%, var(--zf-border));
  border-radius: var(--zf-radius-pill);
  background: var(--zf-warning-bg);
  color: var(--zf-text-secondary);
  font-size: var(--zf-text-xs);
  padding: 2px var(--zf-space-2);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.imp__warnchip:hover { border-color: var(--zf-warning); }

/* 入库结果 */
.imp__resultcard {
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-lg);
  background: var(--zf-bg-surface);
  padding: var(--zf-space-3) var(--zf-space-4);
}
.imp__result-head {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__resultlist {
  margin: var(--zf-space-2) 0 0;
  padding-left: var(--zf-space-5);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  max-height: 120px;
  overflow: auto;
}
.imp__resultlist--bad { color: var(--zf-danger); }

/* ══════════════════════════════════════════════════════════════
   ③ 主工作区：左列表 / 右详情
   ══════════════════════════════════════════════════════════════ */
.imp__workspace {
  display: grid;
  grid-template-columns: 380px minmax(0, 1fr);
  gap: var(--zf-space-4);
  height: calc(100vh - 360px);
  min-height: 480px;
  align-items: stretch;
}
.imp__listpane,
.imp__detailpane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-lg);
  background: var(--zf-bg-surface);
  overflow: hidden;
}
.imp__pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
  padding: var(--zf-space-3) var(--zf-space-4);
  border-bottom: 1px solid var(--zf-border);
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
  flex: none;
}
.imp__list {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--zf-space-2);
}

/* 列表行 */
.imp__row {
  width: 100%;
  display: flex;
  gap: var(--zf-space-2);
  align-items: flex-start;
  text-align: left;
  padding: var(--zf-space-2) var(--zf-space-2);
  border: 1px solid transparent;
  border-bottom-color: var(--zf-border-subtle);
  border-radius: var(--zf-radius-md);
  background: transparent;
  cursor: pointer;
  color: inherit;
  font: inherit;
}
.imp__row:hover { background: var(--zf-bg-hover); }
.imp__row:focus-visible {
  outline: 2px solid var(--zf-brand);
  outline-offset: 1px;
}
.imp__row.is-selected {
  border-color: var(--zf-brand-border);
  background: var(--zf-brand-subtle);
}
.imp__row.is-abnormal { border-left: 3px solid var(--zf-danger); }
.imp__row.is-done { border-left: 3px solid var(--zf-success); }
.imp__row.is-discarded { opacity: .5; }
.imp__row.is-dragover {
  outline: 1px dashed color-mix(in srgb, var(--zf-brand) 40%, transparent);
  outline-offset: -2px;
}
.imp__row-seq {
  flex: none;
  min-width: 34px;
  padding-top: 1px;
  font-family: var(--zf-font-mono);
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
.imp__row-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.imp__row-top {
  display: flex;
  align-items: center;
  gap: var(--zf-space-1);
  flex-wrap: wrap;
}
.imp__badge {
  padding: 0 6px;
  border-radius: var(--zf-radius-pill);
  background: var(--zf-danger-bg);
  color: var(--zf-danger);
  font-size: 11px;
  line-height: 18px;
}
.imp__badge.is-saving {
  background: var(--zf-warning-bg);
  color: var(--zf-warning);
}
.imp__row-stem {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-primary);
  line-height: var(--zf-leading-normal);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.imp__row-meta {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}
.imp__row-note {
  font-size: 11px;
  color: var(--zf-warning);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.imp__row-note.is-ok { color: var(--zf-success); }
.imp__row-note.is-danger { color: var(--zf-danger); }
.imp__row-thumb {
  flex: none;
  width: 52px;
  height: 42px;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  overflow: hidden;
  background: var(--zf-bg-subtle);
}
.imp__row-thumb .el-image { width: 100%; height: 100%; }
.imp__row-thumb.is-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--zf-text-tertiary);
}

/* 详情头 */
.imp__detail-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
  padding: var(--zf-space-2) var(--zf-space-4);
  border-bottom: 1px solid var(--zf-border);
  background: var(--zf-bg-surface);
}
.imp__detail-headleft,
.imp__detail-actions {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__detail-seq {
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.imp__back { display: none; }
.imp__saveflash { font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.imp__saveflash.is-ok { color: var(--zf-success); }
.imp__detail-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--zf-space-4);
}
.imp__detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: var(--zf-space-4);
  align-items: start;
}
.imp__detail-main {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-4);
  min-width: 0;
}
.imp__detail-aside {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
  min-width: 0;
}
.imp__field { min-width: 0; }
.imp__field-grid {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr) 150px;
  gap: var(--zf-space-3);
}
.imp__field--wide { min-width: 0; }
.imp__needbar {
  padding: var(--zf-space-2) var(--zf-space-3);
  border: 1px solid color-mix(in srgb, var(--zf-danger) 30%, var(--zf-border));
  border-radius: var(--zf-radius-md);
  background: var(--zf-danger-bg);
  color: var(--zf-danger);
  font-size: var(--zf-text-xs);
}
.imp__inline-notes {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
  padding: var(--zf-space-2) var(--zf-space-3);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-subtle);
}
.imp__note {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.imp__note.is-ok { color: var(--zf-success); }
.imp__note.is-warn { color: var(--zf-warning); }
.imp__note.is-danger { color: var(--zf-danger); }
.imp__section {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-2);
  padding-top: var(--zf-space-3);
  border-top: 1px solid var(--zf-border-subtle);
}
.imp__section-head,
.imp__panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__section-head span:first-child {
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.imp__group-note {
  padding: var(--zf-space-3);
  border-radius: var(--zf-radius-md);
  background: var(--zf-warning-bg);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  line-height: var(--zf-leading-loose);
}
.imp__meta {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}

/* 侧栏面板 */
.imp__panel {
  padding: var(--zf-space-3);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
}
.imp__panel-head h3 {
  margin: 0;
  font-size: var(--zf-text-sm);
  color: var(--zf-text-primary);
}
.imp__notes {
  margin: var(--zf-space-2) 0 0;
  padding-left: var(--zf-space-4);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  line-height: var(--zf-leading-loose);
}
.imp__notes li.is-ok { color: var(--zf-success); }
.imp__notes li.is-warn { color: var(--zf-warning); }
.imp__notes li.is-danger { color: var(--zf-danger); }
.imp__kv {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 4px var(--zf-space-2);
  margin-top: var(--zf-space-2);
  font-size: var(--zf-text-xs);
}
.imp__kv span { color: var(--zf-text-tertiary); }
.imp__kv b { color: var(--zf-text-secondary); font-weight: var(--zf-weight-normal); }
.imp__childlist {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
  margin-top: var(--zf-space-2);
}
.imp__childbtn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
  width: 100%;
  padding: var(--zf-space-1) var(--zf-space-2);
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-subtle);
  color: var(--zf-text-secondary);
  font: inherit;
  font-size: var(--zf-text-xs);
  text-align: left;
  cursor: pointer;
}
.imp__childbtn:hover { border-color: var(--zf-brand-border); color: var(--zf-brand); }

/* ══════════════════════════════════════════════════════════════
   选项 / 答案 / 配图（沿用原交互，只换排版）
   ══════════════════════════════════════════════════════════════ */
.imp__opts { display: flex; flex-direction: column; gap: var(--zf-space-1); }
.imp__opt-hint { font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.imp__opt-row {
  display: grid;
  grid-template-columns: 24px 24px minmax(0, 1fr) auto 40px;
  gap: var(--zf-space-1);
  align-items: center;
  padding: 4px var(--zf-space-2);
  border: 1px solid transparent;
  border-radius: var(--zf-radius-md);
  cursor: pointer;
}
.imp__opt-row:hover { background: var(--zf-bg-hover); }
.imp__opt-row.is-picked {
  border-color: var(--zf-success);
  background: var(--zf-success-bg);
}
.imp__opt-label {
  font-family: var(--zf-font-mono);
  color: var(--zf-text-secondary);
}
.imp__opt-thumb {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 28px;
  overflow: hidden;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-subtle);
  font-size: 11px;
  color: var(--zf-text-tertiary);
}
.imp__opt-thumb .el-image { width: 100%; height: 100%; }
.imp__opt-acts {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.imp__picked {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.imp__warn { font-size: var(--zf-text-xs); color: var(--zf-warning); }
.imp__ans { display: flex; flex-direction: column; gap: var(--zf-space-2); }
.imp__ans-row { display: flex; align-items: center; gap: var(--zf-space-2); }

.imp__figlist {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-2);
  margin-top: var(--zf-space-2);
}
.imp__figcard {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
  padding: var(--zf-space-2);
  border: 1px dashed var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-subtle);
  cursor: grab;
}
.imp__figcard:active { cursor: grabbing; }
.imp__figthumb {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 84px;
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  overflow: hidden;
}
.imp__fig {
  width: 100%;
  max-height: 160px;
  border-radius: var(--zf-radius-sm);
  background: #fff;
}
.imp__figcap {
  display: flex;
  align-items: center;
  gap: var(--zf-space-1);
  flex-wrap: wrap;
}
.imp__figmove {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
}
.imp__figmove-row {
  display: flex;
  align-items: center;
  gap: var(--zf-space-1);
}

/* ══════════════════════════════════════════════════════════════
   弹窗 / 批量
   ══════════════════════════════════════════════════════════════ */
.imp__dialog { display: flex; flex-direction: column; gap: var(--zf-space-3); }
.imp__bulk { display: flex; flex-direction: column; gap: var(--zf-space-2); }
.imp__bulk-section h4 {
  margin: 0 0 var(--zf-space-1);
  font-size: var(--zf-text-base);
  color: var(--zf-text-primary);
}
.imp__bulk-row {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  margin-top: var(--zf-space-2);
}
.imp__bulkalert { margin-top: var(--zf-space-2); }

/* ══════════════════════════════════════════════════════════════
   响应式
   ══════════════════════════════════════════════════════════════ */
@media (max-width: 1280px) {
  .imp__workspace { grid-template-columns: 340px minmax(0, 1fr); }
  .imp__detail-grid { grid-template-columns: minmax(0, 1fr); }
  .imp__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 900px) {
  .imp__workspace {
    grid-template-columns: minmax(0, 1fr);
    height: auto;
    min-height: 0;
  }
  .imp__listpane,
  .imp__detailpane { max-height: none; }
  .imp__list { max-height: 52vh; }
  .imp__detail-scroll { overflow: visible; }
  .imp__back { display: inline-flex; }
  .imp__workspace.is-mobile-detail .imp__listpane { display: none; }
  .imp__workspace:not(.is-mobile-detail) .imp__detailpane { display: none; }
  .imp__topbar { position: static; }
  .imp__topbar-actions { width: 100%; }
  .imp__topbar-actions .el-button:last-child { flex: 1; }
}

@media (max-width: 720px) {
  .imp__topbar { padding: var(--zf-space-3); }
  .imp__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .imp__field-grid { grid-template-columns: minmax(0, 1fr); }
  .imp__toolbar { align-items: stretch; }
  .imp__filters { overflow-x: auto; }
  .imp__tools { width: 100%; }
  .imp__landing-head { flex-direction: column; }
}
</style>
