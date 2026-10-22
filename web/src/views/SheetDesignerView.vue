<script setup lang="ts">
/**
 * 答题卡设计器：页面设置 / 题目区域 / 定位与打印。
 *
 * ★ 右侧预览完全由后端 `preview/` 返回的 layout_json 驱动，
 *   与打印 PDF 同源；前端不自己算坐标。
 * ★ 「保存草稿」只存 sections；「发布」才生成坐标 + PDF + 版本号。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import SheetPreview from '@/components/SheetPreview.vue'
import { ApiError } from '@/api/http'
import * as S from '@/api/sheet'

interface DesignerStyle {
  name: string
  page_size: string
  orientation: 'PORTRAIT' | 'LANDSCAPE'
  columns: 1 | 2
  margin_mm: number
  box_scale: number
  row_gap_mm: number
  col_gap_mm: number
  band_gap_mm: number
  section_title_gap_mm: number
  base_font_pt: number
  title_text: string
  title_font_pt: number
  fill_line_spacing: number
  fill_item_gap_mm: number
  info_config: S.InfoConfig
}

interface FieldError {
  index: number
  key?: string
  code?: string
  message: string
}

const route = useRoute()
const router = useRouter()

const templateId = computed(() => {
  const v = Number(route.params.id)
  return Number.isFinite(v) && v > 0 ? v : null
})

const name = ref('未命名答题卡')
const style = reactive<DesignerStyle>({
  name: '默认页面设置',
  page_size: 'A4',
  orientation: 'PORTRAIT',
  columns: 1,
  margin_mm: 12,
  box_scale: 1.0,
  row_gap_mm: 1.2,
  col_gap_mm: 6,
  band_gap_mm: 10,
  section_title_gap_mm: 2.5,
  base_font_pt: 10,
  title_text: '答题卡',
  title_font_pt: 16,
  fill_line_spacing: 2.3,
  fill_item_gap_mm: 0,
  info_config: {
    handwrite_fields: [
      { key: 'name', label: '姓名' },
      { key: 'class', label: '班级' },
      { key: 'school', label: '学校' },
    ],
    student_no: { enabled: true, digits: 8, per_row: 8, group_size: 4 },
    notice_text: '1. 请用 2B 铅笔将所选选项涂满、涂黑。\n2. 保持答题卡清洁。',
    show_notice: true,
  },
})

const sections = ref<S.TemplateSection[]>([S.newSection('SINGLE_CHOICE', 1)])
const status = ref<S.TemplateStatus>('DRAFT')
const version = ref(1)
const layout = ref<S.LayoutJson | null>(null)
const issues = ref<S.LayoutIssue[]>([])
const pageNo = ref(1)
const activeTab = ref('page')
const loading = ref(false)
const saving = ref(false)
const publishing = ref(false)
const previewing = ref(false)
const error = ref('')
const fieldErrors = ref<FieldError[]>([])

const readonly = computed(() =>
  Boolean(templateId.value) && status.value !== 'DRAFT')
const blocking = computed(() => issues.value.filter((i) => i.blocking))
const pageCount = computed(() => layout.value?.page_count ?? 1)
const totalScore = computed(() => layout.value?.total_score ?? 0)

// ── 填空题行高换算（与后端同一公式，避免两套夹紧规则）──────────
// 行高(mm) = 字号(mm) × 倍数 + 1.6；切图侧按 8.0mm 切行、留 2.0mm 白带，
// 故一行至少 10.0mm，倍数下限 = (10.0 - 1.6) / 字号(mm)。
const PT_TO_MM = 0.3528
const FILL_PADDING_MM = 1.6
const FILL_SAFE_LINE_MM = 10.0

function fillFontMm(): number {
  return Number(((Number(style.base_font_pt) || 10) * PT_TO_MM).toFixed(4))
}

const fillRatioMin = computed(() => {
  const fm = fillFontMm()
  if (fm <= 0) return 1.6
  const raw = (FILL_SAFE_LINE_MM - FILL_PADDING_MM) / fm
  // ★ 向上取整到两位，与后端 fill_line_ratio_min 同一口径：
  //   向下取整会让 10pt 的 2.38 差 0.003mm 够不到 10mm。
  return Math.min(4, Math.max(1.6, Math.ceil(raw * 100 - 1e-9) / 100))
})

const fillLineMm = computed(() => Math.round(
  (fillFontMm() * (Number(style.fill_line_spacing) || 2.3)
    + FILL_PADDING_MM) * 100,
) / 100)

const fillLineTooTight = computed(() => fillLineMm.value < FILL_SAFE_LINE_MM)

/**
 * 老师主动改行距时才夹紧。
 *
 * ★ 不能用 `:min="fillRatioMin"` 硬绑。Element Plus 的 InputNumber 在
 *   **挂载时**就会按 min 修正一次值：存量模板 2.3 倍 @10pt 会被静默改成
 *   2.39，所有填空行 y_mm 平移，等于老师什么都没做版面就变了。
 *   所以 min 只绑绝对下限 1.6，语义上的安全下限放在这里拦。
 */
function onFillLineSpacingChange(v: number | undefined): void {
  if (readonly.value) return
  const min = fillRatioMin.value
  if (Number(v) < min) {
    style.fill_line_spacing = min
    ElMessage.warning(
      `行高低于切图安全线 ${FILL_SAFE_LINE_MM}mm，`
      + `${style.base_font_pt}pt 下最小 ${min} 倍（≈${fillLineMm.value}mm）`,
    )
  }
}

// 字号变小 → 安全下限抬高。不主动改老师已存的值（存量卡 2.3@10pt 本就
// 低于安全线，改了会平移所有填空行），只在老师动字号时才跟上。
watch(() => style.base_font_pt, () => {
  if (readonly.value) return
  const min = fillRatioMin.value
  if (Number(style.fill_line_spacing) < min) {
    style.fill_line_spacing = min
    ElMessage.warning(
      `字号 ${style.base_font_pt}pt 下填空行距下限为 ${min} 倍`
      + `（≈${fillLineMm.value}mm），已自动上调`,
    )
  }
})

function emptyLabelIndexes(): number[] {
  return style.info_config.handwrite_fields
    .map((f, i) => (String(f.label ?? '').trim() ? -1 : i + 1))
    .filter((i) => i > 0)
}

function hasFieldError(index: number): boolean {
  return fieldErrors.value.some((e) => e.index === index)
}

function syncFieldErrorsAfterEdit(): void {
  const empties = emptyLabelIndexes()
  if (!empties.length) {
    fieldErrors.value = []
    return
  }
  // 只有已经出现过错误时才重建序号；编辑过程中不主动弹错。
  if (fieldErrors.value.length) {
    fieldErrors.value = empties.map((index) => ({
      index,
      code: 'EMPTY_LABEL',
      message: `第 ${index} 个手写字段没有名称`,
    }))
  }
}

let timer: ReturnType<typeof setTimeout> | null = null

function schedulePreview(): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { void runPreview() }, 400)
}

async function runPreview(): Promise<void> {
  previewing.value = true
  try {
    const r = await S.previewTemplate(templateId.value, {
      name: name.value,
      style: payloadStyle(),
      sections: sections.value,
    })
    layout.value = r.layout_json
    issues.value = r.issues
    // ★ 预览成功即清掉上一次的旧 error；空 label 的错误在填好后自动消失。
    error.value = ''
    syncFieldErrorsAfterEdit()
    if (pageNo.value > (r.layout_json.page_count || 1)) pageNo.value = 1
  } catch (e) {
    // 预览失败保留上一次结果，避免右侧闪空
    error.value = e instanceof Error ? e.message : '预览失败'
  } finally {
    previewing.value = false
  }
}

function payloadStyle(): S.StylePayload {
  return {
    name: `${name.value}-页面设置`.slice(0, 100),
    page_size: style.page_size,
    orientation: style.orientation,
    columns: style.columns,
    margin_mm: style.margin_mm,
    box_scale: style.box_scale,
    row_gap_mm: style.row_gap_mm,
    col_gap_mm: style.col_gap_mm,
    band_gap_mm: style.band_gap_mm,
    section_title_gap_mm: style.section_title_gap_mm,
    base_font_pt: style.base_font_pt,
    title_text: style.title_text,
    title_font_pt: style.title_font_pt,
    fill_line_spacing: style.fill_line_spacing,
    fill_item_gap_mm: style.fill_item_gap_mm,
    info_config: style.info_config,
  }
}

async function load(): Promise<void> {
  if (!templateId.value) {
    schedulePreview()
    return
  }
  loading.value = true
  try {
    const t = await S.getTemplate(templateId.value)
    name.value = t.name
    status.value = t.status
    version.value = t.version
    style.name = t.style.name
    style.page_size = t.style.page_size
    style.orientation = (t.style.orientation ?? 'PORTRAIT') as
      'PORTRAIT' | 'LANDSCAPE'
    style.columns = (Number(t.style.columns ?? 1) === 2 ? 2 : 1)
    style.margin_mm = Number(t.style.margin_mm)
    style.box_scale = Number(t.style.box_scale ?? 1)
    style.row_gap_mm = Number(t.style.row_gap_mm ?? 1.2)
    style.col_gap_mm = Number(t.style.col_gap_mm ?? 6)
    style.band_gap_mm = Number(t.style.band_gap_mm ?? 10)
    style.section_title_gap_mm = Number(t.style.section_title_gap_mm ?? 2.5)
    style.base_font_pt = Number(t.style.base_font_pt)
    style.title_text = t.style.title_text
    style.title_font_pt = Number(t.style.title_font_pt)
    style.fill_line_spacing = Number(t.style.fill_line_spacing ?? 2.3)
    style.fill_item_gap_mm = Number(t.style.fill_item_gap_mm ?? 0)
    style.info_config = {
      ...t.style.info_config,
      student_no: { ...t.style.info_config.student_no },
      handwrite_fields: [...t.style.info_config.handwrite_fields],
    }
    sections.value = (t.sections?.length
      ? JSON.parse(JSON.stringify(t.sections))
      : [S.newSection('SINGLE_CHOICE', 1)]) as S.TemplateSection[]
    sections.value.forEach((sec) => S.syncSectionItems(sec))
    await runPreview()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

watch(
  [name, style, sections],
  () => { if (!readonly.value) schedulePreview() },
  { deep: true },
)

// ── 题目区域操作 ──

function orderSections(): void {
  sections.value.forEach((s, i) => { s.order = i + 1 })
}

function addSection(type: S.SectionType): void {
  sections.value.push(S.newSection(type, sections.value.length + 1))
  orderSections()
}

function move(index: number, delta: number): void {
  const to = index + delta
  if (to < 0 || to >= sections.value.length) return
  const arr = sections.value
  const [x] = arr.splice(index, 1)
  arr.splice(to, 0, x)
  orderSections()
}

function removeSection(index: number): void {
  sections.value.splice(index, 1)
  orderSections()
}

function onCountChange(sec: S.TemplateSection): void {
  sec.count = Math.max(1, Math.min(60, Number(sec.count) || 1))
  S.syncSectionItems(sec)
}

function fillItems(sec: S.TemplateSection): S.FillBlankItem[] {
  return sec.items as S.FillBlankItem[]
}

function answerItems(sec: S.TemplateSection): S.AnswerItem[] {
  return sec.items as S.AnswerItem[]
}

function essayItems(sec: S.TemplateSection): S.EssayItem[] {
  return sec.items as S.EssayItem[]
}

function onBlanksChange(it: S.FillBlankItem): void {
  S.syncBlankScores(it)
}

function fillTotal(it: S.FillBlankItem): number {
  return it.blank_scores.reduce((a, b) => a + Number(b || 0), 0)
}

// ── 填空题「统一设置」──────────────────────────────────────────
//
// ★ 只做批量写回，不改排版引擎、不改判分：写回的仍是每个 item 的
//   blanks / chars / per_line / blank_scores，与逐题配置完全同一份数据。
// ★ 状态按 `sec.order` 暂存：这批值在点「应用到全部小题」之前**不影响**
//   任何小题，避免边输入边刷预览。
interface FillBulk {
  blanks: number
  chars: number
  per_line: number
  score: number
}

const fillBulk = reactive<Record<string, FillBulk>>({})

function fillBulkOf(sec: S.TemplateSection): FillBulk {
  const key = String(sec.order)
  const cached = fillBulk[key]
  if (cached) return cached
  // 初值取第 1 题，避免用硬编码默认值覆盖老师已经配好的题
  const first = fillItems(sec)[0]
  const init: FillBulk = {
    blanks: Number(first?.blanks ?? 1),
    chars: Number(first?.chars ?? 6),
    per_line: Number(first?.per_line ?? 1),
    score: Number(first?.blank_scores?.[0] ?? 2),
  }
  fillBulk[key] = init
  return init
}

function applyFillBulk(sec: S.TemplateSection): void {
  const items = fillItems(sec)
  if (!items.length) return
  const b = fillBulkOf(sec)
  const blanks = Math.max(1, Math.min(S.BLANKS_MAX, Number(b.blanks) || 1))
  const chars = Math.max(2, Math.min(12, Number(b.chars) || 6))
  const perLine = Math.max(
    1, Math.min(S.BLANK_PER_LINE_MAX, Number(b.per_line) || 1),
  )
  const score = Number(b.score) > 0 ? Number(b.score) : 2
  const before = items.reduce((a, it) => a + Number(it.score || 0), 0)

  for (const it of items) {
    it.blanks = blanks
    it.chars = chars
    it.per_line = Math.min(perLine, blanks)
    // ★ 顺序不能反：syncBlankScores 内部是 `blank_scores[i] ?? 2`，
    //   先调会把上一轮的旧分值带出来。必须先把 blank_scores 铺成
    //   「N 份同值」，再交给它兜底夹紧与求和。
    it.blank_scores = Array.from({ length: blanks }, () => score)
    S.syncBlankScores(it)
  }

  const after = items.reduce((a, it) => a + Number(it.score || 0), 0)
  ElMessage.success(
    `已应用到 ${items.length} 道小题：空格数 ${blanks}、每空字符 ${chars}、`
    + `每行 ${Math.min(perLine, blanks)} 空、每空 ${score} 分；`
    + `本大题合计 ${before} → ${after} 分`,
  )
}

// ── 保存 / 发布 ──

async function save(): Promise<boolean> {
  if (readonly.value) {
    ElMessage.warning('已发布版本不能原地修改，请「另存为新版本」')
    return false
  }
  if (!name.value.trim()) {
    ElMessage.warning('请填写答题卡名')
    return false
  }
  saving.value = true
  error.value = ''
  fieldErrors.value = []
  try {
    if (!templateId.value) {
      const t = await S.createTemplate({
        name: name.value.trim(),
        style: payloadStyle(),
        sections: sections.value,
      })
      ElMessage.success('草稿已保存')
      await router.replace({
        name: 'SheetDesigner', params: { id: t.id },
      })
      status.value = t.status
      await load()
    } else {
      await S.updateTemplate(templateId.value, {
        name: name.value.trim(),
        style: payloadStyle(),
        sections: sections.value,
      })
      ElMessage.success('草稿已保存')
      await runPreview()
    }
    return true
  } catch (e) {
    const msg = e instanceof Error ? e.message : '保存失败'
    error.value = msg
    if (e instanceof ApiError) {
      const list = (e.data as { field_errors?: FieldError[] } | undefined)
        ?.field_errors
      if (Array.isArray(list)) fieldErrors.value = list
    }
    ElMessage.error(msg)
    return false
  } finally {
    saving.value = false
  }
}

async function publish(): Promise<void> {
  const empties = emptyLabelIndexes()
  if (empties.length) {
    const msg = `第 ${empties[0]} 个手写字段没有名称`
    error.value = msg
    fieldErrors.value = empties.map((index) => ({
      index,
      code: 'EMPTY_LABEL',
      message: `第 ${index} 个手写字段没有名称`,
    }))
    ElMessage.warning(msg)
    return
  }
  if (!templateId.value) {
    const ok = await save()
    if (!ok) return
  }
  if (!templateId.value) return
  if (blocking.value.length) {
    ElMessage.warning('还有阻断项，先按右侧提示修正')
    return
  }
  try {
    await ElMessageBox.confirm(
      '发布后生成 PDF 与版本号；已发布版本不能原地改。继续？',
      '发布答题卡',
      { confirmButtonText: '发布', cancelButtonText: '取消' },
    )
  } catch { return }
  publishing.value = true
  try {
    const t = await S.publishTemplate(templateId.value)
    ElMessage.success(`已发布 v${t.version}`)
    status.value = t.status
    version.value = t.version
    await load()
  } catch (e) {
    const msg = e instanceof Error ? e.message : '发布失败'
    error.value = msg
    if (e instanceof ApiError) {
      const list = (e.data as { field_errors?: FieldError[] } | undefined)
        ?.field_errors
      if (Array.isArray(list)) fieldErrors.value = list
    }
    ElMessage.error(msg)
  } finally {
    publishing.value = false
  }
}

async function saveAsNewVersion(): Promise<void> {
  if (!templateId.value) return
  try {
    const t = await S.duplicateTemplate(templateId.value, {
      as_new_version: true,
    })
    ElMessage.success(`已另存为草稿 v${t.version}`)
    await router.replace({ name: 'SheetDesigner', params: { id: t.id } })
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '另存失败')
  }
}

async function exportPdf(): Promise<void> {
  if (!templateId.value) return
  try {
    const blob = await S.getTemplatePdfBlob(templateId.value)
    S.downloadBlob(blob, `${name.value}-v${version.value}.pdf`)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'PDF 下载失败')
  }
}

function addHandwrite(): void {
  if (style.info_config.handwrite_fields.length >= 6) return
  style.info_config.handwrite_fields.push({
    key: `field${style.info_config.handwrite_fields.length + 1}`,
    label: '字段',
  })
}

function removeHandwrite(i: number): void {
  if (style.info_config.handwrite_fields.length <= 1) return
  style.info_config.handwrite_fields.splice(i, 1)
  // 删除空字段后剩余字段重新均分，错误区同步清空。
  syncFieldErrorsAfterEdit()
}
</script>

<template>
  <div v-loading="loading" class="designer">
    <div class="head">
      <div>
        <h1 class="page-title">
          {{ templateId ? '编辑答题卡' : '新建答题卡' }}
          <el-tag size="small" :type="S.TEMPLATE_STATUS_TONE[status]">
            {{ S.TEMPLATE_STATUS_LABEL[status] }}
          </el-tag>
        </h1>
        <p class="zf-muted page-sub">
          左侧参数 · 右侧实时预览（与打印同源）
        </p>
      </div>
      <div class="zf-row">
        <el-button @click="router.push({ name: 'SheetList' })">返回列表</el-button>
        <el-button v-if="templateId" :disabled="!layout" @click="exportPdf">
          导出 PDF
        </el-button>
        <el-button
          v-if="readonly"
          type="warning"
          @click="saveAsNewVersion"
        >
          另存为新版本
        </el-button>
        <el-button
          v-else
          :loading="saving"
          @click="save"
        >
          保存草稿
        </el-button>
        <el-button
          v-if="!readonly"
          type="primary"
          :loading="publishing"
          :disabled="!templateId || blocking.length > 0"
          @click="publish"
        >
          发布
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <div class="workspace">
      <section class="zf-card left">
        <div class="zf-card__head">
          <h2 class="zf-card__title">参数</h2>
        </div>
        <div class="zf-card__body">
          <el-tabs v-model="activeTab">
            <!-- ═════════ 页面设置 ═════════ -->
            <el-tab-pane label="页面设置" name="page">
              <el-form label-width="94px" size="small" :disabled="readonly">
                <el-form-item label="答题卡名">
                  <el-input v-model="name" maxlength="200" />
                </el-form-item>
                <el-form-item label="标题">
                  <el-input
                    v-model="style.title_text"
                    placeholder="留空时卷面显示「答题卡」"
                  />
                </el-form-item>
                <el-form-item label="纸张">
                  <el-select v-model="style.page_size" style="width: 100%">
                    <el-option label="A3（297×420）" value="A3" />
                    <el-option label="A4（210×297）" value="A4" />
                    <el-option label="A5（148×210）" value="A5" />
                  </el-select>
                </el-form-item>
                <el-form-item label="方向">
                  <el-radio-group v-model="style.orientation">
                    <el-radio-button label="PORTRAIT">纵向</el-radio-button>
                    <el-radio-button label="LANDSCAPE">横向</el-radio-button>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="分栏">
                  <el-radio-group v-model="style.columns">
                    <el-radio-button :label="1">单栏</el-radio-button>
                    <el-radio-button :label="2">双栏</el-radio-button>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="页边距">
                  <el-input-number
                    v-model="style.margin_mm"
                    :min="12" :max="30" :step="1"
                  />
                  <span class="unit">mm（纸边→定位块外沿）</span>
                </el-form-item>
                <el-form-item label="大题标题间距">
                  <el-input-number
                    v-model="style.section_title_gap_mm"
                    :min="0" :max="10" :step="0.5"
                  />
                  <span class="unit">mm</span>
                </el-form-item>
                <el-form-item label="正文字号">
                  <el-input-number
                    v-model="style.base_font_pt"
                    :min="8" :max="14" :step="0.5"
                  />
                  <span class="unit">pt</span>
                </el-form-item>
                <el-form-item label="标题字号">
                  <el-input-number
                    v-model="style.title_font_pt"
                    :min="12" :max="28" :step="1"
                  />
                  <span class="unit">pt</span>
                </el-form-item>

                <el-divider content-position="left">填空题排版</el-divider>
                <el-form-item label="填空行距">
                  <el-input-number
                    v-model="style.fill_line_spacing"
                    :min="1.6" :max="4" :step="0.1"
                    @change="onFillLineSpacingChange"
                  />
                  <span class="unit">倍 ≈ {{ fillLineMm }}mm</span>
                </el-form-item>
                <div
                  v-if="fillLineTooTight"
                  class="zf-dim" style="margin: -8px 0 8px 94px"
                >
                  行高 {{ fillLineMm }}mm 低于切图安全线 10mm，扫描略有倾斜时
                  相邻两行可能串行；本字号下建议不小于 {{ fillRatioMin }} 倍。
                </div>
                <el-form-item label="小题间距">
                  <el-input-number
                    v-model="style.fill_item_gap_mm"
                    :min="0" :max="20" :step="0.5"
                  />
                  <span class="unit">mm</span>
                </el-form-item>
                <div
                  class="zf-dim" style="margin: -8px 0 8px 94px"
                >
                  只加在「另起一行的小题」之间；想让每题独占一行，
                  在逐题配置里勾选「另起一行」。
                </div>
              </el-form>

              <el-divider content-position="left">手写区（左）</el-divider>
              <div
                v-for="(f, i) in style.info_config.handwrite_fields"
                :key="`hf-${i}`"
                class="field-row"
                :class="{ 'field-row--error': hasFieldError(i + 1) }"
              >
                <el-input
                  v-model="f.label"
                  size="small"
                  :disabled="readonly"
                  :placeholder="`第 ${i + 1} 个字段名称`"
                />
                <el-button
                  size="small" text type="danger"
                  :disabled="readonly"
                  @click="removeHandwrite(i)"
                >
                  删除
                </el-button>
              </div>
              <div
                v-for="fe in fieldErrors"
                :key="`fe-${fe.index}`"
                class="field-error"
              >
                {{ fe.message }}
              </div>
              <el-button
                size="small" text type="primary"
                :disabled="readonly"
                @click="addHandwrite"
              >
                + 加字段
              </el-button>

              <el-divider content-position="left">考号填涂区（右）</el-divider>
              <el-form label-width="94px" size="small" :disabled="readonly">
                <el-form-item label="启用考号">
                  <el-switch v-model="style.info_config.student_no.enabled" />
                </el-form-item>
                <el-form-item label="位数">
                  <el-input-number
                    v-model="style.info_config.student_no.digits"
                    :min="3" :max="8"
                  />
                </el-form-item>
                <el-form-item label="每行位数">
                  <el-input-number
                    v-model="style.info_config.student_no.per_row"
                    :min="1" :max="style.info_config.student_no.digits"
                  />
                </el-form-item>
                <el-form-item label="分组线">
                  <el-input-number
                    v-model="style.info_config.student_no.group_size"
                    :min="1" :max="style.info_config.student_no.digits"
                  />
                </el-form-item>
              </el-form>

              <el-divider content-position="left">注意事项（通栏底部）</el-divider>
              <el-switch
                v-model="style.info_config.show_notice"
                :disabled="readonly"
                active-text="显示"
              />
              <el-input
                v-model="style.info_config.notice_text"
                type="textarea"
                :rows="4"
                :disabled="readonly"
                style="margin-top: 8px"
              />
            </el-tab-pane>

            <!-- ═════════ 题目区域 ═════════ -->
            <el-tab-pane label="题目区域" name="sections">
              <el-form inline size="small" :disabled="readonly"
                       style="margin-bottom: 10px">
                <el-form-item label="框大小">
                  <el-input-number v-model="style.box_scale" :min="0.7"
                                   :max="1.5" :step="0.05" />
                </el-form-item>
                <el-form-item label="行间距">
                  <el-input-number v-model="style.row_gap_mm" :min="0"
                                   :max="6" :step="0.2" />
                </el-form-item>
                <el-form-item label="组横向间距">
                  <el-input-number v-model="style.col_gap_mm" :min="0"
                                   :max="20" :step="0.5" />
                </el-form-item>
                <el-form-item label="组纵向间距">
                  <el-input-number v-model="style.band_gap_mm" :min="0"
                                   :max="30" :step="0.5" />
                </el-form-item>
              </el-form>
              <div class="zf-row" style="margin-bottom: 10px; flex-wrap: wrap">
                <el-button
                  v-for="t in (['SINGLE_CHOICE', 'MULTIPLE_CHOICE',
                               'TRUE_FALSE', 'FILL_BLANK', 'ANSWER',
                               'ESSAY'] as S.SectionType[])"
                  :key="t"
                  size="small"
                  :disabled="readonly"
                  @click="addSection(t)"
                >
                  + {{ S.SECTION_TYPE_LABEL[t] }}
                </el-button>
              </div>

              <el-empty
                v-if="!sections.length"
                description="还没有题目区域"
              />

              <div
                v-for="(sec, i) in sections"
                :key="`sec-${i}`"
                class="section-card"
              >
                <div class="section-head">
                  <b>第 {{ i + 1 }} 大题</b>
                  <el-tag size="small">
                    {{ S.SECTION_TYPE_LABEL[sec.type] }}
                  </el-tag>
                  <span class="zf-dim">题号 {{ sec.start_seq ?? '自动' }}</span>
                  <div class="zf-row" style="margin-left: auto">
                    <el-button
                      size="small" text :disabled="readonly || i === 0"
                      @click="move(i, -1)"
                    >
                      上移
                    </el-button>
                    <el-button
                      size="small" text
                      :disabled="readonly || i === sections.length - 1"
                      @click="move(i, 1)"
                    >
                      下移
                    </el-button>
                    <el-button
                      size="small" text type="danger" :disabled="readonly"
                      @click="removeSection(i)"
                    >
                      删除
                    </el-button>
                  </div>
                </div>

                <el-form label-width="84px" size="small" :disabled="readonly">
                  <el-form-item label="大题标题">
                    <el-input v-model="sec.title" maxlength="60" />
                  </el-form-item>
                  <el-form-item label="题数">
                    <el-input-number
                      v-model="sec.count" :min="1" :max="60"
                      @change="onCountChange(sec)"
                    />
                  </el-form-item>

                  <template v-if="sec.type !== 'FILL_BLANK'
                    && sec.type !== 'ANSWER' && sec.type !== 'ESSAY'">
                    <el-form-item
                      v-if="sec.type !== 'TRUE_FALSE'"
                      label="选项数"
                    >
                      <el-input-number
                        v-model="sec.options" :min="4" :max="6"
                      />
                    </el-form-item>
                    <el-form-item label="每小题分">
                      <el-input-number
                        v-model="sec.score" :min="0.5" :step="0.5"
                      />
                    </el-form-item>
                  </template>

                  <!-- 填空统一设置：一次刷到本大题全部小题 -->
                  <template v-if="sec.type === 'FILL_BLANK'">
                    <el-divider content-position="left">统一设置</el-divider>
                    <div class="item-card">
                      <div class="item-grid">
                        <span>空格数</span>
                        <el-input-number
                          v-model="fillBulkOf(sec).blanks"
                          :min="1" :max="S.BLANKS_MAX"
                        />
                        <span>每空字符</span>
                        <el-input-number
                          v-model="fillBulkOf(sec).chars" :min="2" :max="12"
                        />
                        <span>每行空格</span>
                        <el-input-number
                          v-model="fillBulkOf(sec).per_line" :min="1" :max="4"
                        />
                        <span>每空分值</span>
                        <el-input-number
                          v-model="fillBulkOf(sec).score"
                          :min="0.5" :step="0.5"
                        />
                      </div>
                      <div class="zf-row" style="margin-top: 6px">
                        <el-button
                          size="small" type="primary" plain
                          @click="applyFillBulk(sec)"
                        >
                          应用到全部小题
                        </el-button>
                        <span class="zf-dim">
                          只影响本大题（共 {{ fillItems(sec).length }} 道小题），
                          之后仍可逐题微调
                        </span>
                      </div>
                      <el-alert
                        type="info" :closable="false" show-icon
                        style="margin-top: 8px"
                        title="答题卡上的每空分值仅用于卷面标注与总分核算，自动判分仍按每空等分。"
                      />

                      <el-divider content-position="left">
                        填空行距与间距
                      </el-divider>
                      <div class="item-grid">
                        <span>行距</span>
                        <el-input-number
                          v-model="style.fill_line_spacing"
                          :min="1.6" :max="4" :step="0.1"
                          @change="onFillLineSpacingChange"
                        />
                        <span class="zf-dim">倍 ≈ {{ fillLineMm }}mm</span>
                        <span>小题间距</span>
                        <el-input-number
                          v-model="style.fill_item_gap_mm"
                          :min="0" :max="20" :step="0.5"
                        />
                        <span class="zf-dim">mm</span>
                      </div>
                      <div
                        v-if="fillLineTooTight"
                        class="zf-dim"
                        style="margin: 4px 0 0"
                      >
                        行高 {{ fillLineMm }}mm 低于切图安全线 10mm，扫描略有
                        倾斜时相邻两行可能串行；本字号下建议不小于
                        {{ fillRatioMin }} 倍。
                      </div>
                      <div class="zf-dim" style="margin-top: 4px">
                        ★ 这两项是整张答题卡的填空题共用参数（不是逐题的），
                        保存模板后生效。小题间距只在该题「另起一行」时起作用
                        —— 同一行并排的两道小题之间是横向间距。
                      </div>
                    </div>

                    <el-divider content-position="left">逐题配置</el-divider>
                    <div
                      v-for="(it, qi) in fillItems(sec)"
                      :key="`fill-${i}-${qi}`"
                      class="item-card"
                    >
                      <b>第 {{ qi + 1 }} 题</b>
                      <div class="item-grid">
                        <span>空格数</span>
                        <el-input-number
                          v-model="it.blanks" :min="1" :max="8"
                          @change="onBlanksChange(it)"
                        />
                        <span>每空字符</span>
                        <el-input-number v-model="it.chars" :min="2" :max="12" />
                        <span>每行空格</span>
                        <el-input-number
                          v-model="it.per_line"
                          :min="1"
                          :max="Math.min(S.BLANK_PER_LINE_MAX, it.blanks)"
                          :disabled="it.blanks <= 1"
                        />
                        <span>另起一行</span>
                        <el-checkbox v-model="it.new_line" />
                      </div>
                      <div class="zf-row" style="margin-top: 6px">
                        <span class="zf-dim">每空分值</span>
                        <el-input-number
                          v-for="(_v, si) in it.blank_scores"
                          :key="`bs-${i}-${qi}-${si}`"
                          v-model="it.blank_scores[si]"
                          :min="0.5" :step="0.5" size="small"
                          style="width: 92px"
                        />
                        <span class="zf-dim">
                          本题合计 = {{ fillTotal(it) }}
                        </span>
                      </div>
                    </div>
                  </template>

                  <!-- 解答逐题 -->
                  <template v-if="sec.type === 'ANSWER'">
                    <el-divider content-position="left">逐题配置</el-divider>
                    <div
                      v-for="(it, qi) in answerItems(sec)"
                      :key="`ans-${i}-${qi}`"
                      class="item-card"
                    >
                      <b>第 {{ qi + 1 }} 题</b>
                      <div class="item-grid">
                        <span>行数</span>
                        <el-input-number v-model="it.lines" :min="2" :max="30" />
                        <span>样式</span>
                        <el-select v-model="it.style" style="width: 100px">
                          <el-option label="实线" value="SOLID" />
                          <el-option label="虚线" value="DASHED" />
                          <el-option label="空白" value="BLANK" />
                          <el-option label="方框" value="BOX" />
                        </el-select>
                        <span>小题数</span>
                        <el-input-number v-model="it.sub_count" :min="1" :max="4" />
                        <span>行距</span>
                        <el-input-number
                          v-model="it.line_spacing" :min="1.0" :max="3.0"
                          :step="0.1"
                        />
                        <span>分值</span>
                        <el-input-number v-model="it.score" :min="0.5" :step="0.5" />
                      </div>
                    </div>
                  </template>

                  <!-- 作文逐题 -->
                  <template v-if="sec.type === 'ESSAY'">
                    <el-divider content-position="left">逐题配置</el-divider>
                    <div
                      v-for="(it, qi) in essayItems(sec)"
                      :key="`essay-${i}-${qi}`"
                      class="item-card"
                    >
                      <b>第 {{ qi + 1 }} 题</b>
                      <div class="item-grid">
                        <span>总字数</span>
                        <el-input-number
                          v-model="it.total_chars" :min="100" :max="1200"
                          :step="50"
                        />
                        <span>每行格数</span>
                        <el-input-number v-model="it.per_line" :min="10" :max="30" />
                        <span>格间距</span>
                        <el-input-number
                          v-model="it.cell_gap_mm" :min="0.4" :max="1.2"
                          :step="0.1"
                        />
                        <span>最少字数</span>
                        <el-input-number
                          v-model="it.min_chars" :min="0" :max="it.total_chars"
                          :step="50"
                        />
                        <span>分值</span>
                        <el-input-number v-model="it.score" :min="0.5" :step="0.5" />
                      </div>
                    </div>
                  </template>
                </el-form>
              </div>
            </el-tab-pane>

            <!-- ═════════ 定位与打印 ═════════ -->
            <el-tab-pane label="定位与打印" name="print">
              <div class="kv">
                <span class="k">页数</span>
                <span class="v">{{ pageCount }} / 4</span>
              </div>
              <div class="kv">
                <span class="k">题量</span>
                <span class="v">{{ layout?.question_count ?? '—' }}</span>
              </div>
              <div class="kv">
                <span class="k">总分</span>
                <span class="v">{{ totalScore }}</span>
              </div>
              <div class="kv">
                <span class="k">主定位块</span>
                <span class="v">
                  {{ S.PAGE_SIZE_LABEL[style.page_size] }} ·
                  {{ layout?.anchor_size_mm ?? '—' }}mm
                </span>
              </div>
              <el-alert
                type="info"
                :closable="false"
                show-icon
                title="打印提示"
                style="margin-top: 12px"
              >
                请用 <b>100% 比例、不缩放、黑白</b> 打印；
                建议 300dpi 扫描。四角定位块与页定位块必须完整可见。
              </el-alert>

              <div style="margin-top: 12px">
                <b>校验结果</b>
                <el-empty
                  v-if="!issues.length"
                  description="没有发现问题"
                  :image-size="60"
                />
                <div
                  v-for="(it, i) in issues"
                  :key="`iss-${i}`"
                  class="issue"
                  :class="{ block: it.blocking }"
                >
                  <el-tag size="small" :type="it.blocking ? 'danger' : 'warning'">
                    {{ it.blocking ? '阻断' : '告警' }}
                  </el-tag>
                  <span>{{ it.message }}</span>
                </div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>
      </section>

      <section v-loading="previewing" class="zf-card right">
        <div class="zf-card__head">
          <h2 class="zf-card__title">预览</h2>
          <div class="zf-row">
            <el-pagination
              v-if="pageCount > 1"
              v-model:current-page="pageNo"
              layout="prev, pager, next"
              :total="pageCount"
              :page-size="1"
              small
            />
            <span class="zf-dim" style="font-size: var(--zf-text-xs)">
              第 {{ pageNo }} / {{ pageCount }} 页
            </span>
          </div>
        </div>
        <div class="zf-card__body preview-body">
          <SheetPreview :layout="layout" :page-no="pageNo" />
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.designer { display: flex; flex-direction: column; gap: var(--zf-space-4); }
.head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--zf-space-4); flex-wrap: wrap;
}
.page-title {
  margin: 0; font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  display: flex; align-items: center; gap: var(--zf-space-2);
}
.page-sub { margin: var(--zf-space-1) 0 0; font-size: var(--zf-text-sm); }
.workspace {
  display: grid; grid-template-columns: minmax(420px, 1fr) auto;
  gap: var(--zf-space-4); align-items: start;
}
@media (max-width: 1100px) {
  .workspace { grid-template-columns: 1fr; }
}
.fill { flex: 1; }
.unit { margin-left: 6px; color: var(--zf-text-secondary); font-size: 12px; }
.field-row { display: flex; gap: 8px; margin-bottom: 6px; }
.field-row--error :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}
.field-error {
  color: var(--el-color-danger);
  font-size: var(--zf-text-xs);
  margin: -2px 0 6px;
}
.section-card {
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-sm);
  padding: 10px; margin-bottom: 10px;
}
.section-head {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}
.item-card {
  border-top: 1px dashed var(--zf-border-subtle);
  padding: 8px 0; font-size: 12px;
}
.item-grid {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-top: 6px;
}
.kv {
  display: flex; justify-content: space-between; gap: 12px;
  padding: 6px 0; border-bottom: 1px dashed var(--zf-border-subtle);
}
.issue {
  display: flex; gap: 8px; align-items: flex-start;
  padding: 6px 0; font-size: 12px;
}
.preview-body {
  max-height: 78vh; overflow: auto;
  display: flex; justify-content: center;
}
</style>
