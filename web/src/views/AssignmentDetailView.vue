<script setup lang="ts">
/**
 * 作业详情 / 发布。
 *
 * ★★ 这个页面的核心是**让发布这件事可完成**。
 *   发布会被三种原因拦下，每种都要给出**能照做的下一步**：
 *
 *     未绑定系统账号（400）→ 列出学号，引导去名册绑定
 *     试卷未定稿（409）    → 引导回组卷页定稿
 *     缺答题卡模板（409）  → 引导去答题卡页生成
 *
 *   ★ 只给一句「发布失败」等于把问题推回给老师。
 *
 * ★★ 发布后**必须**给出学生入口（二维码 + 可复制链接）：
 *   那是「发布」这个动作的**唯一产出物**——没有它，
 *   发布成功了学生也进不来。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/http'
import QRCode from 'qrcode.vue'

import * as A from '@/api/assignment'
import * as G from '@/api/grading'
import * as O from '@/api/omr'
import type { AssignmentScanCards } from '@/api/omr'
import * as S from '@/api/sheet'
import { getAssetBlob } from '@/api/paper'
import ScanImageViewer from '@/components/ScanImageViewer.vue'
import { useAssignmentStore } from '@/stores/assignment'

const route = useRoute()
const router = useRouter()
const store = useAssignmentStore()

const id = computed(() => Number(route.params.id))
const selected = ref<A.AssignmentStudentRow[]>([])
const statusFilter = ref('')
const report = ref<S.BindingReport | null>(null)
const bindTemplateId = ref<number | undefined>(undefined)
const bindLoading = ref(false)
const bindErrors = ref<string[]>([])
const scan = ref<AssignmentScanCards | null>(null)
const scanThumbs = ref<Record<number, string>>({})
const annotatedThumbs = ref<Record<number, string>>({})
const roster = ref<G.GradingRoster | null>(null)
const quickFilter = ref<
  '' | 'submitted' | 'not_submitted' | 'graded' | 'pending_grade'
>('')
const listPage = ref(1)
const listPageSize = ref(20)
const scanDialogVisible = ref(false)
const scanDialogRow = ref<A.AssignmentStudentRow | null>(null)
const scanDialogMode = ref<'annotated' | 'display' | 'raw'>('annotated')
const scanDialogImages = ref<Record<string, string>>({})
const scanDialogIndex = ref(0)

const entryUrl = computed(() => store.entryUrl)
const stats = computed(() => store.stats)
/** ★ 当前出口地址是否只有本机能访问（决定要不要警告老师） */
const loopback = computed(() => A.isLoopbackOrigin())
const studentBase = computed(() => A.studentBaseUrl())

/** 学生 → 批改状态（来自 grading roster）。 */
const gradingByStudent = computed(() => {
  const m = new Map<number, G.GradingRosterRow>()
  for (const r of roster.value?.items ?? []) {
    if (r.student_id) m.set(r.student_id, r)
  }
  return m
})

function gradingOf(row: A.AssignmentStudentRow): G.GradingRosterRow | null {
  return gradingByStudent.value.get(row.student_id) ?? null
}

function isSubmitted(row: A.AssignmentStudentRow): boolean {
  return gradingOf(row)?.submitted
    ?? ['SUBMITTED', 'SCANNED', 'GRADED'].includes(row.status)
}

function gradingState(row: A.AssignmentStudentRow): string {
  return gradingOf(row)?.grading_state
    ?? (isSubmitted(row) ? 'PENDING_AUTO' : 'NOT_SUBMITTED')
}

/** 名单筛选：状态下拉 + 交卷情况四数字。 */
const shownStudents = computed(() => {
  let rows = store.students
  if (statusFilter.value) {
    rows = rows.filter((s) => s.status === statusFilter.value)
  }
  if (quickFilter.value === 'submitted') {
    rows = rows.filter(isSubmitted)
  } else if (quickFilter.value === 'not_submitted') {
    rows = rows.filter((s) => !isSubmitted(s))
  } else if (quickFilter.value === 'graded') {
    rows = rows.filter((s) => gradingState(s) === 'DONE')
  } else if (quickFilter.value === 'pending_grade') {
    rows = rows.filter(
      (s) => isSubmitted(s) && gradingState(s) !== 'DONE')
  }
  return rows
})

const pagedStudents = computed(() => {
  const start = (listPage.value - 1) * listPageSize.value
  return shownStudents.value.slice(start, start + listPageSize.value)
})

const quickFilterLabel = computed(() => {
  const map: Record<string, string> = {
    submitted: '已交',
    not_submitted: '未交',
    graded: '已批改',
    pending_grade: '待批改',
  }
  return map[quickFilter.value] ?? ''
})

const gradedCount = computed(
  () => roster.value?.counts.graded ?? store.stats?.graded ?? 0)
const pendingGradeCount = computed(
  () => roster.value?.counts.pending_grade ?? store.stats?.pending_grade ?? 0)

function toggleQuickFilter(
  key: 'submitted' | 'not_submitted' | 'graded' | 'pending_grade',
): void {
  quickFilter.value = quickFilter.value === key ? '' : key
  listPage.value = 1
}

function clearListFilters(): void {
  quickFilter.value = ''
  statusFilter.value = ''
  listPage.value = 1
}

/** 学生 → 扫描页（用于来源、缩略图、批改入口）。 */
const scanByStudent = computed(() => {
  const m = new Map<number, AssignmentScanCards['students'][number]>()
  for (const s of scan.value?.students ?? []) m.set(s.student_id, s)
  return m
})

function scanPageOf(row: A.AssignmentStudentRow) {
  return scanByStudent.value.get(row.student_id)?.pages?.[0] ?? null
}

function scanThumbOf(row: A.AssignmentStudentRow): string {
  const p = scanPageOf(row)
  if (!p) return ''
  return annotatedThumbs.value[p.card_id]
    || scanThumbs.value[p.display_asset_id ?? p.raw_asset_id] || ''
}

function sourceText(row: A.AssignmentStudentRow): string {
  const g = gradingOf(row)
  if (g?.answer_source) return g.answer_source === 'SCAN' ? '扫描' : '线上'
  if (row.status === 'SCANNED') return '扫描'
  if (row.status === 'SUBMITTED' || row.status === 'GRADED') return '线上'
  return '—'
}

async function openScan(row: A.AssignmentStudentRow): Promise<void> {
  scanDialogRow.value = row
  scanDialogVisible.value = true
  scanDialogMode.value = 'annotated'
  scanDialogIndex.value = 0
  await loadScanDialogImages(row)
}

async function loadScanDialogImages(
  row: A.AssignmentStudentRow,
): Promise<void> {
  for (const p of scanByStudent.value.get(row.student_id)?.pages ?? []) {
    const jobs: Promise<void>[] = []
    if (!scanDialogImages.value[`display:${p.card_id}`]) {
      // ★ 有 display_asset 直接取；旧卡走后端重新校正，不拿原图当展示图。
      jobs.push((p.display_asset_id
        ? getAssetBlob(p.display_asset_id)
        : O.getCardDisplayBlob(p.card_id)
      ).then((b) => {
        scanDialogImages.value[`display:${p.card_id}`] =
          URL.createObjectURL(b)
      }).catch(() => {
        ElMessage.warning('该卡无法生成校正图，请查看原图')
      }))
    }
    if (!scanDialogImages.value[`raw:${p.card_id}`]) {
      jobs.push(getAssetBlob(p.raw_asset_id).then((b) => {
        scanDialogImages.value[`raw:${p.card_id}`] = URL.createObjectURL(b)
      }).catch(() => undefined))
    }
    if (!scanDialogImages.value[`annotated:${p.card_id}`]) {
      jobs.push(O.getAnnotatedCardBlob(p.card_id).then((b) => {
        scanDialogImages.value[`annotated:${p.card_id}`] =
          URL.createObjectURL(b)
      }).catch(() => undefined))
    }
    await Promise.all(jobs)
  }
}

/** 名单当前页显示标注缩略图（只加载当前 20 人，避免 45 份大图）。 */
async function loadAnnotatedThumbs(
  rows: A.AssignmentStudentRow[],
): Promise<void> {
  for (const row of rows) {
    const page = scanPageOf(row)
    if (!page || annotatedThumbs.value[page.card_id]) continue
    try {
      const blob = await O.getAnnotatedCardBlob(page.card_id)
      annotatedThumbs.value[page.card_id] = URL.createObjectURL(blob)
    } catch { /* 标注图失败时回退校正图 */ }
  }
}

watch([pagedStudents, scan], () => {
  void loadAnnotatedThumbs(pagedStudents.value)
})

const scanDialogPages = computed(() =>
  scanDialogRow.value
    ? (scanByStudent.value.get(scanDialogRow.value.student_id)?.pages ?? [])
    : [],
)
function scanDialogUrl(cardId: number): string {
  return scanDialogImages.value[`${scanDialogMode.value}:${cardId}`] ?? ''
}

const scanDialogActiveUrl = computed(() => {
  const p = scanDialogPages.value[scanDialogIndex.value]
  return p ? (scanDialogUrl(p.card_id)
    || scanDialogImages.value[`display:${p.card_id}`] || '') : ''
})

const scanDialogPreview = computed(() =>
  scanDialogPages.value.map((p) => scanDialogUrl(p.card_id)
    || scanDialogImages.value[`display:${p.card_id}`] || '')
    .filter(Boolean),
)

onBeforeUnmount(() => {
  for (const u of Object.values(scanThumbs.value)) URL.revokeObjectURL(u)
  for (const u of Object.values(annotatedThumbs.value)) URL.revokeObjectURL(u)
  for (const u of Object.values(scanDialogImages.value)) URL.revokeObjectURL(u)
})

function gradeStudent(row: A.AssignmentStudentRow): void {
  if (!row.student_id) return
  router.push({
    name: 'GradingWorkbench',
    params: { id: id.value },
    query: { student_id: String(row.student_id) },
  })
}

onMounted(async () => {
  store.reset()
  await store.load(id.value)
  if (store.detail) {
    await loadBinding()
  }
  if (store.detail && store.detail.status !== 'DRAFT') {
    await Promise.all([store.loadStudents(), loadScan(), loadRoster()])
  }
})

async function loadRoster(): Promise<void> {
  try {
    roster.value = await G.getRoster(id.value)
  } catch {
    roster.value = null
  }
}

async function loadBinding(): Promise<void> {
  const paperId = store.detail?.paper_id
  if (!paperId) return
  try {
    report.value = await S.getBindingReport(paperId)
    if (!bindTemplateId.value && report.value.primary_template_id) {
      bindTemplateId.value = report.value.primary_template_id
    }
  } catch {
    report.value = null
  }
}

async function bindCard(): Promise<void> {
  const paperId = store.detail?.paper_id
  if (!paperId || !bindTemplateId.value) {
    ElMessage.warning('请选择要绑定的答题卡')
    return
  }
  bindLoading.value = true
  bindErrors.value = []
  try {
    await A.bindAssignmentSheet(id.value, bindTemplateId.value)
    ElMessage.success('绑定成功，校验通过')
    await store.load(id.value)
    await loadBinding()
  } catch (e) {
    const validation = e instanceof ApiError
      ? e.extra<{ blocking?: Array<{ message: string }> }>('validation')
      : undefined
    if (validation?.blocking?.length) {
      bindErrors.value = validation.blocking.map((b) => b.message)
    }
    ElMessage.error(e instanceof Error ? e.message : '绑定失败')
  } finally {
    bindLoading.value = false
  }
}

async function loadScan(): Promise<void> {
  if (!store.detail || store.detail.status === 'DRAFT') return
  try {
    scan.value = await A.getAssignmentScanCards(id.value)
    const ids = scan.value.students
      .flatMap((s) => s.pages.map((p) => p.display_asset_id))
      .filter((v): v is number => !!v && !scanThumbs.value[v])
    for (const aid of ids.slice(0, 80)) {
      try {
        const blob = await getAssetBlob(aid)
        scanThumbs.value[aid] = URL.createObjectURL(blob)
      } catch { /* 缩略图失败不阻断 */ }
    }
  } catch {
    scan.value = null
  }
}

async function openAsset(assetId: number): Promise<void> {
  try {
    const blob = await getAssetBlob(assetId)
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
  } catch {
    ElMessage.error('打开原图失败')
  }
}

/** ★ 已关闭/已归档的任务不能再改设置（成绩已定稿，后端也会拦）。 */
const canEditSettings = computed(
  () =>
    !!store.detail &&
    store.detail.status !== 'CLOSED' &&
    store.detail.status !== 'ARCHIVED',
)

/** 乱序只能在草稿且未绑答题卡时改（绑卡后逐生顺序已冻结）。 */
const canEditShuffle = computed(
  () =>
    !!store.detail &&
    store.detail.status === 'DRAFT' &&
    !store.detail.sheet_template_id,
)

/**
 * 答题卡是否已锁定（需求 3）。
 *
 * ★ 判据与后端一致：**已绑定 + 现算校验通过**。
 *   未通过校验的绑定不锁 —— 那种情况老师必须能换一张卡。
 *   前端只是把界面转成只读；真正的守卫在 `AssignmentService.bind_sheet`，
 *   绕过界面直接打接口同样会 409。
 */
const sheetLocked = computed(
  () =>
    !!store.detail?.sheet_template_id &&
    store.detail?.sheet_validation?.ok === true,
)

async function toggleShuffle(
  key: 'shuffle_questions' | 'shuffle_options',
  value: string | number | boolean,
): Promise<void> {
  const ok = await store.updateSettings({ [key]: Boolean(value) })
  if (!ok) ElMessage.error(store.error || '保存失败')
  else ElMessage.success('乱序设置已保存')
}

/**
 * 切换「交卷自动批改」。
 *
 * ★ 失败时 store 会重新拉一次详情 —— 开关**自动回滚**，
 *   不会留下「看起来改了」的假象。
 */
async function toggleAutoGrade(value: string | number | boolean): Promise<void> {
  const okDone = await store.updateSettings({
    auto_grade_on_submit: Boolean(value),
  })
  if (okDone) {
    ElMessage.success(
      value
        ? '已开启：学生一交卷就自动批改他这一份'
        : '已关闭交卷自动批改',
    )
  } else {
    ElMessage.error(store.error || '保存失败')
  }
}

async function handlePublish(): Promise<void> {
  const r = await store.publish()
  if (r === 'ok') {
    ElMessage.success('已发布，学生可以扫码进入答题')
    await Promise.all([loadBinding(), loadScan(), loadRoster()])
    return
  }
  if (r === 'need_bind') {
    // ★ 这是「还差一步」：不弹红错，把差异/学号列在下方的提示里
    ElMessage.warning('发布被阻断，请看下方提示')
    return
  }
  ElMessage.error(store.error || '发布失败')
}

async function handleClose(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '关闭作答后学生不能再进入答题（已交的作答保留）。确定关闭作答吗？',
      '关闭作答',
      { confirmButtonText: '关闭作答', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const ok = await store.close()
  ElMessage[ok ? 'success' : 'error'](ok ? '已关闭作答' : store.error)
}

async function handleArchive(): Promise<void> {
  const ok = await store.archive()
  ElMessage[ok ? 'success' : 'error'](ok ? '已归档' : store.error)
}

async function handleMarkAbsent(): Promise<void> {
  if (!selected.value.length) {
    ElMessage.warning('请先勾选要标记为缺考的学生')
    return
  }
  // ★ 必须显式指定学生：后端**不提供**「全部未交都算缺考」——
  //   那会把「卡还没扫」的学生一起判缺考。
  try {
    await ElMessageBox.confirm(
      `将 ${selected.value.length} 名学生标记为缺考？\n` +
        '缺考与「未交」在成绩单上是两件事，请确认这些人确实没参加。',
      '标记缺考',
      { confirmButtonText: '标记', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const ok = await store.markAbsent(selected.value.map((s) => s.student_id))
  ElMessage[ok ? 'success' : 'error'](ok ? '已标记缺考' : store.error)
  selected.value = []
}

async function copyEntry(): Promise<void> {
  try {
    await navigator.clipboard.writeText(entryUrl.value)
    ElMessage.success('入口链接已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选中链接')
  }
}

function formatTime(s: string | null): string {
  return s ? new Date(s).toLocaleString() : '—'
}
</script>

<template>
  <div v-loading="store.loading" class="zf-stack">
    <!-- ── 头部 ── -->
    <div class="head">
      <div>
        <h1 class="page-title">
          {{ store.detail?.title ?? '作业' }}
          <el-tag
            v-if="store.detail"
            size="small"
            :type="A.ASSIGNMENT_STATUS_TONE[store.detail.status]"
          >
            {{ A.assignmentStatusLabel(store.detail.status) }}
          </el-tag>
        </h1>
        <p class="zf-muted page-sub">
          {{ store.detail?.classroom_name }} ·
          {{ store.detail?.paper_title }} ·
          答题卡：{{ store.detail?.sheet_template_name || '未绑定' }}
          <span v-if="store.detail?.sheet_template_version">
            v{{ store.detail.sheet_template_version }}
          </span>
        </p>
      </div>
      <div class="zf-row">
        <el-button @click="router.push({ name: 'AssignmentList' })">
          返回列表
        </el-button>
        <!-- ★ 批改 / 学情只在已发布后才有意义（草稿没有作答）。
             用 `v-if` 而不是禁用：禁用按钮会让人反复点。 -->
        <template v-if="store.detail && store.detail.status !== 'DRAFT'">
          <!-- ★ 主观题阅卷入口已移到「批改工作台」（需求 4）：
               改卷是「从批改进去、按题切过去」的一条线，
               在详情页再放一个入口会让老师分不清该点哪个。 -->
          <el-button
            @click="router.push({ name: 'GradingWorkbench', params: { id } })"
          >
            批改
          </el-button>
          <el-button
            @click="router.push({ name: 'AssignmentAnalytics', params: { id } })"
          >
            学情
          </el-button>
        </template>
        <el-button
          v-if="store.canPublish"
          type="primary"
          :loading="store.saving"
          @click="handlePublish"
        >
          发布
        </el-button>
        <el-button
          v-if="store.canClose"
          :loading="store.saving"
          @click="handleClose"
        >
          关闭作答
        </el-button>
        <el-button
          v-if="store.canArchive"
          :loading="store.saving"
          @click="handleArchive"
        >
          归档
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
    />

    <!-- ★★ 发布被拦时的「还差一步」提示（不是失败） -->
    <el-alert
      v-if="store.publishHint"
      type="warning"
      :closable="false"
      show-icon
      :title="store.publishHint.text"
    >
      <template #default>
        <p v-if="store.publishHint.items.length" style="margin: 4px 0 0">
          学号：{{ store.publishHint.items.join('、') }}
        </p>
        <p class="zf-dim" style="margin: 6px 0 0; font-size: var(--zf-text-xs)">
          按提示补齐后即可发布；答题卡绑定与校验在下方「答题卡绑定」完成。
        </p>
      </template>
    </el-alert>

    <!-- ══════════ 答题卡绑定（需求 6）══════════ -->
    <section v-if="store.detail" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">答题卡绑定</h2>
        <!-- ★ 需求 3：校验通过即锁定。这个标签要说明「为什么不能再改」，
             而不是只显示一个成功态。 -->
        <el-tag v-if="sheetLocked" size="small" type="success">
          校验通过 · 已锁定
        </el-tag>
        <el-tag
          v-else-if="store.detail.sheet_validation"
          size="small"
          :type="store.detail.sheet_validation.ok ? 'success' : 'danger'"
        >
          {{ store.detail.sheet_validation.ok ? '校验通过' : '校验未通过' }}
        </el-tag>
      </div>
      <div class="zf-card__body">
        <p v-if="store.detail.sheet_template_name" class="zf-muted">
          当前：{{ store.detail.sheet_template_name }}
          v{{ store.detail.sheet_template_version }}
          <template v-if="sheetLocked">
            <br />
            <span class="zf-dim" style="font-size: var(--zf-text-xs)">
              这张卡已校验通过并锁定，不能再更换（已打印的纸卡与系统
              题号、选项位置、考号位数都已按它固定）。如需换卡请新建任务。
            </span>
          </template>
        </p>
        <p v-else-if="store.detail.status === 'DRAFT'" class="zf-muted">
          尚未绑定答题卡；发布前必须绑定（纯线上作业请开启逐生乱序后发布）。
        </p>
        <p v-else class="zf-muted">
          纯线上作业：未绑定答题卡，不可扫描；逐生乱序仍按发布时冻结。
        </p>

        <el-alert
          v-if="store.detail.shuffle_questions || store.detail.shuffle_options"
          type="warning"
          :closable="false"
          show-icon
          title="线下扫描作业不支持逐生乱序，请先关闭乱序"
          style="margin-top: 8px"
        >
          <template #default>
            <p style="margin: 4px 0 0; font-size: var(--zf-text-xs)">
              逐生纸质试卷尚未实现：每生题序/选项序不同，纸卡无法与系统一一对应。
              请在上方「基本信息」关闭乱序后再绑定答题卡；纯线上作业可保留乱序。
            </p>
          </template>
        </el-alert>

        <div class="zf-row" style="margin: 10px 0">
          <el-select
            v-model="bindTemplateId"
            filterable
            clearable
            :disabled="sheetLocked"
            placeholder="选择要绑定的答题卡"
            style="width: 320px"
          >
            <el-option
              v-for="c in report?.candidates ?? []"
              :key="c.template_id"
              :label="`${c.name} v${c.version}（${c.bindable ? '可绑定' : '不匹配'}）`"
              :value="c.template_id"
            />
          </el-select>
          <el-button
            type="primary"
            :loading="bindLoading"
            :disabled="!bindTemplateId || sheetLocked"
            @click="bindCard"
          >
            绑定并校验
          </el-button>
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            试卷：{{ report?.paper_question_count ?? '—' }} 题 /
            {{ report?.paper_total_score ?? '—' }} 分
          </span>
        </div>

        <el-alert
          v-if="bindErrors.length"
          type="error"
          :closable="false"
          show-icon
          title="绑定被阻断，差异如下"
          style="margin-top: 8px"
        >
          <ul style="margin: 4px 0 0; padding-left: 18px">
            <li v-for="(msg, i) in bindErrors" :key="`be-${i}`">
              {{ msg }}
            </li>
          </ul>
        </el-alert>

        <el-alert
          v-if="store.detail.sheet_validation
            && !store.detail.sheet_validation.ok"
          type="error"
          :closable="false"
          show-icon
          title="答题卡与试卷不一致，发布会被阻断"
        >
          <ul style="margin: 4px 0 0; padding-left: 18px">
            <li
              v-for="(b, i) in store.detail.sheet_validation.blocking"
              :key="`blk-${i}`"
            >
              {{ b.message }}
            </li>
          </ul>
        </el-alert>
      </div>
    </section>

    <!-- ══════════ 学生入口（发布后才给）══════════ -->
    <section v-if="entryUrl" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">学生答题入口</h2>
        <el-tag size="small" type="success">已发布</el-tag>
      </div>
      <div class="zf-card__body entry">
        <div class="entry__qr">
          <QRCode :value="entryUrl" :size="168" level="M" />
        </div>
        <div class="entry__info">
          <p class="zf-muted" style="margin: 0 0 8px">
            学生用手机扫码进入；也可以把链接发到班级群。
          </p>

          <!-- ★★ 这个警告是必须的：老师在本机用 localhost 打开系统时，
               二维码里编的就是 127.0.0.1 —— 学生手机扫它只会指向
               手机自己。不提示的话老师会以为二维码坏了。 -->
          <el-alert
            v-if="loopback"
            type="warning"
            :closable="false"
            show-icon
            title="这个二维码学生扫不了"
            style="margin-bottom: 10px"
          >
            <template #default>
              <p style="margin: 0; font-size: var(--zf-text-xs)">
                当前地址是 <code>{{ studentBase }}</code>，
                <code>localhost</code> / <code>127.0.0.1</code>
                在手机上指向手机自己。
              </p>
              <p style="margin: 6px 0 0; font-size: var(--zf-text-xs)">
                改成你电脑的局域网 IP 再打开本页即可（形如
                <code>http://192.168.x.x:5173</code>）；
                或在 <code>web/.env.local</code> 里设置
                <code>VITE_STUDENT_BASE_URL</code>。
              </p>
            </template>
          </el-alert>

          <div class="entry__url">
            <code>{{ entryUrl }}</code>
          </div>
          <div class="zf-row" style="margin-top: 12px">
            <el-button size="small" @click="copyEntry">复制链接</el-button>
            <el-button
              size="small"
              tag="a"
              :href="entryUrl"
              target="_blank"
              rel="noopener"
            >
              在这台机器上打开
            </el-button>
          </div>
          <p class="zf-dim" style="margin: 12px 0 0; font-size: var(--zf-text-xs)">
            ★ 链接里的 <code>publish_token</code> 是 32 位随机串，
            等价于「任务级口令」。它只授权「进入这个任务」，
            不授权「以谁的身份」——身份由学生填学号+姓名后确定。
          </p>
        </div>
      </div>
    </section>

    <!-- ══════════ 交卷情况 ══════════ -->
    <section v-if="stats" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">交卷情况</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          {{ stats.submitted }} / {{ stats.total }} 已交（{{ store.submittedRatio }}%）
        </span>
      </div>
      <div class="zf-card__body">
        <el-progress
          :percentage="store.submittedRatio"
          :stroke-width="10"
          :show-text="false"
        />
        <div class="statgrid">
          <button
            class="stat stat--ok"
            :class="{ active: quickFilter === 'submitted' }"
            @click="toggleQuickFilter('submitted')"
          >
            <span class="n ok">{{ stats.submitted }}</span>
            <span class="l">已交</span>
          </button>
          <button
            class="stat"
            :class="{ active: quickFilter === 'not_submitted' }"
            @click="toggleQuickFilter('not_submitted')"
          >
            <span class="n warn">{{ stats.total - stats.submitted }}</span>
            <span class="l">未交</span>
          </button>
          <button
            class="stat"
            :class="{ active: quickFilter === 'graded' }"
            @click="toggleQuickFilter('graded')"
          >
            <span class="n ok">{{ gradedCount }}</span>
            <span class="l">已批改</span>
          </button>
          <button
            class="stat"
            :class="{ active: quickFilter === 'pending_grade' }"
            @click="toggleQuickFilter('pending_grade')"
          >
            <span class="n bad">{{ pendingGradeCount }}</span>
            <span class="l">待批改</span>
          </button>
        </div>
        <p class="zf-dim" style="margin: 8px 0 0; font-size: var(--zf-text-xs)">
          点击数字可筛选下方名单，再次点击取消。共 {{ stats.total }} 人应考，
          缺考 {{ stats.absent }} 人。
        </p>
      </div>
    </section>

    <!-- ══════════ 名单 ══════════ -->
    <section v-if="store.detail && store.detail.status !== 'DRAFT'" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">名单</h2>
        <div class="zf-row">
          <!-- ★ 显示后端报告的总人数：与已加载条数不一致时立刻能看出来
               （需求 2 的 bug 就是「只加载 20 条却当成全班」）。 -->
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            共 {{ store.studentsTotal }} 人<template
              v-if="shownStudents.length !== store.students.length"
            >，筛选后 {{ shownStudents.length }} 人</template>
          </span>
          <el-select
            v-model="statusFilter"
            clearable
            size="small"
            placeholder="全部状态"
            style="width: 140px"
            @change="listPage = 1"
          >
            <el-option
              v-for="(l, v) in A.STUDENT_STATUS_LABEL"
              :key="v"
              :label="l"
              :value="v"
            />
          </el-select>
          <el-button size="small" @click="store.loadStudents()">刷新</el-button>
          <el-button
            size="small"
            :disabled="!selected.length"
            @click="handleMarkAbsent"
          >
            标记缺考
          </el-button>
        </div>
      </div>
      <div class="zf-card__body">
        <div
          v-if="quickFilterLabel || statusFilter"
          class="zf-row"
          style="margin-bottom: 8px; gap: 8px; flex-wrap: wrap"
        >
          <el-tag v-if="quickFilterLabel" size="small" type="warning" closable
                  @close="clearListFilters">
            已按「{{ quickFilterLabel }}」筛选
          </el-tag>
          <el-tag v-if="statusFilter" size="small" type="info" closable
                  @close="statusFilter = ''">
            已按状态「{{ A.studentStatusLabel(statusFilter) }}」筛选
          </el-tag>
          <el-button size="small" text @click="clearListFilters">
            清除筛选
          </el-button>
        </div>
        <el-table
          v-loading="store.studentsLoading"
          :data="pagedStudents"
          size="small"
          height="60vh"
          @selection-change="(v: A.AssignmentStudentRow[]) => (selected = v)"
        >
          <el-table-column type="selection" width="42" />
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="student_name" label="姓名" width="120" />
          <el-table-column label="状态" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="A.STUDENT_STATUS_TONE[row.status]">
                {{ row.status_label || A.studentStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="80">
            <template #default="{ row }">
              <el-tag v-if="row.status === 'SCANNED'" size="small" type="success">
                扫描
              </el-tag>
              <span v-else class="zf-dim">{{ sourceText(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="扫描图" width="86">
            <template #default="{ row }">
              <img
                v-if="scanThumbOf(row)"
                :src="scanThumbOf(row)"
                class="scan-thumb"
                style="width: 54px; height: 70px"
                alt="扫描图"
                @click="openScan(row)"
              />
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="94">
            <template #default="{ row }">
              <el-button
                v-if="gradingState(row) === 'PENDING_AUTO'"
                size="small"
                type="primary"
                link
                @click="gradeStudent(row)"
              >
                批改
              </el-button>
              <el-button
                v-else-if="gradingState(row) === 'PENDING_MANUAL'"
                size="small"
                type="warning"
                link
                @click="gradeStudent(row)"
              >
                录分
              </el-button>
              <el-button
                v-else-if="gradingState(row) === 'DONE'"
                size="small"
                link
                @click="gradeStudent(row)"
              >
                已批改
              </el-button>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="进入时间" width="170">
            <template #default="{ row }">{{ formatTime(row.entered_at) }}</template>
          </el-table-column>
          <el-table-column label="交卷时间" width="170">
            <template #default="{ row }">{{ formatTime(row.submitted_at) }}</template>
          </el-table-column>
          <el-table-column label="乱序映射" width="116">
            <template #default="{ row }">
              <el-tooltip
                content="已生成＝发布时已为该生冻结顺序，扫描与线上作答都按它还原"
                placement="top"
              >
                <span class="mapping-help">?</span>
              </el-tooltip>
              <el-tag v-if="row.has_mapping" size="small" type="success">
                已生成
              </el-tag>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">
              {{ store.studentsLoading ? '读取中…' : '没有符合条件的学生' }}
            </div>
          </template>
        </el-table>
        <div class="list-pager">
          <!-- ★ total 用**本地筛选后**的长度，不用后端 total：
               四种快捷筛选（已交/未交/已批改/待批改）都在本地算，
               用后端 total 会让「筛出 8 人」也显示 5 页空白页。
               分页数据源是 `pagedStudents`（对 `shownStudents` 切片），
               两者必须同源。 -->
          <el-pagination
            v-model:current-page="listPage"
            v-model:page-size="listPageSize"
            :total="shownStudents.length"
            :page-sizes="[20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            background
            small
          />
        </div>
      </div>
    </section>

    <!-- ══════════ 扫描页（需求 6）══════════ -->
    <section v-if="scan" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">扫描页</h2>
        <div class="zf-row">
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            共 {{ scan.total_pages }} 页 · 未归属 {{ scan.unassigned.length }} 页
          </span>
          <el-button
            size="small"
            type="primary"
            @click="router.push({ name: 'SheetScan',
                                  query: { assignment_id: String(id) } })"
          >
            导入扫描件
          </el-button>
        </div>
      </div>
      <div class="zf-card__body">
        <el-table :data="scan.students" size="small">
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="student_name" label="姓名" width="110" />
          <el-table-column label="来源" width="90">
            <template #default="{ row }">
              <el-tag size="small"
                      :type="row.source === 'SCAN' ? 'success' : 'info'">
                {{ row.source === 'SCAN' ? '扫描' : '线上' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="识别状态" width="110">
            <template #default="{ row }">
              <el-tag size="small">
                {{ row.status_label || O.cardStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="扫描图" min-width="240">
            <template #default="{ row }">
              <div class="scan-thumbs">
                <template v-for="p in row.pages" :key="p.card_id">
                  <img
                    v-if="p.display_asset_id && scanThumbs[p.display_asset_id]"
                    :src="scanThumbs[p.display_asset_id]"
                    :title="`第 ${p.page_no ?? '?'} 页`"
                    class="scan-thumb"
                    @click="openAsset(p.display_asset_id)"
                  />
                </template>
                <span v-if="!row.pages.length" class="zf-dim">—</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="!scan.students.length" class="zf-dim">
          还没有扫描页。点右上角「导入扫描件」。
        </p>
      </div>
    </section>

    <!-- ══════════ 基本信息 ══════════ -->
    <section v-if="store.detail" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">基本信息</h2>
      </div>
      <div class="zf-card__body">
        <div class="kv"><span class="k">时间窗</span>
          <span class="v">
            {{ formatTime(store.detail.start_at) }} –
            {{ formatTime(store.detail.end_at) }}
          </span>
        </div>
        <div class="kv"><span class="k">题目乱序</span>
          <span class="v">
            <el-switch
              v-if="canEditShuffle"
              :model-value="store.detail.shuffle_questions"
              :loading="store.saving"
              @change="(v: string | number | boolean) => toggleShuffle('shuffle_questions', v)"
            />
            <template v-else>{{ store.detail.shuffle_questions ? '开' : '关' }}</template>
            <span v-if="canEditShuffle" class="zf-dim shuffle-tip">
              线下扫描作业必须关闭；纯线上作业可开启逐生乱序
            </span>
          </span>
        </div>
        <div class="kv"><span class="k">选项乱序</span>
          <span class="v">
            <el-switch
              v-if="canEditShuffle"
              :model-value="store.detail.shuffle_options"
              :loading="store.saving"
              @change="(v: string | number | boolean) => toggleShuffle('shuffle_options', v)"
            />
            <template v-else>{{ store.detail.shuffle_options ? '开' : '关' }}</template>
          </span>
        </div>
        <div class="kv"><span class="k">允许迟交</span>
          <span class="v">{{ store.detail.allow_late ? '是' : '否' }}</span>
        </div>
        <div class="kv"><span class="k">及格线</span>
          <span class="v">{{ Math.round(store.detail.pass_ratio * 100) }}%</span>
        </div>
        <!-- ★ 交卷自动批改（2026-10-17 新增）：发布后也能改，即时生效 -->
        <div class="kv">
          <span class="k">交卷自动批改</span>
          <span class="v">
            <el-switch
              :model-value="store.detail.auto_grade_on_submit"
              :loading="store.saving"
              :disabled="!canEditSettings"
              @change="toggleAutoGrade"
            />
            <span class="zf-dim" style="margin-left: 8px; font-size: var(--zf-text-xs)">
              勾选后，学生一交卷就自动批改他这一份（选择/判断；线上填空题也可）
            </span>
          </span>
        </div>
        <div class="kv"><span class="k">首次发布</span>
          <span class="v">{{ formatTime(store.detail.published_at) }}</span>
        </div>
      </div>
    </section>

    <!-- ══════════ 扫描图对照：标注 / 校正 / 原图 ══════════ -->
    <el-dialog
      v-model="scanDialogVisible"
      :title="`扫描图 · ${scanDialogRow?.student_no ?? ''} ${scanDialogRow?.student_name ?? ''}`"
      width="min(1000px, 90vw)"
      top="5vh"
      class="scan-image-dialog"
    >
      <div class="scan-dialog-body">
        <div class="scan-dialog-toolbar">
          <el-radio-group v-model="scanDialogMode" size="small">
            <el-radio-button value="annotated">标注</el-radio-button>
            <el-radio-button value="display">校正</el-radio-button>
            <el-radio-button value="raw">原图</el-radio-button>
          </el-radio-group>
          <el-button
            v-for="(p, i) in scanDialogPages"
            :key="p.card_id"
            size="small"
            :type="i === scanDialogIndex ? 'primary' : 'default'"
            @click="scanDialogIndex = i"
          >
            第 {{ p.page_no ?? i + 1 }} 页
          </el-button>
        </div>
        <div class="scan-dialog-viewer">
          <ScanImageViewer
            v-if="scanDialogActiveUrl"
            :src="scanDialogActiveUrl"
            :preview-list="scanDialogPreview"
            :initial-index="scanDialogIndex"
            alt="扫描答题卡"
          />
          <el-empty v-else description="图片加载中…" :image-size="48" />
        </div>
      </div>
      <template #footer>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          「标注」绿=正确 红=错误，未作答不标；「校正」是整页展示图；「原图」仅用于追溯。
        </span>
        <el-button @click="scanDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.page-title {
  margin: 0;
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
}
.page-sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}
.entry {
  display: flex;
  gap: var(--zf-space-6);
  flex-wrap: wrap;
}
.entry__qr {
  flex: none;
  padding: var(--zf-space-3);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
}
.entry__info {
  flex: 1;
  min-width: 260px;
}
.entry__url {
  padding: var(--zf-space-2) var(--zf-space-3);
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-subtle);
  word-break: break-all;
  font-size: var(--zf-text-xs);
}
.statgrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(88px, 1fr));
  gap: var(--zf-space-3);
  margin-top: var(--zf-space-4);
}
.statgrid .n {
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.statgrid .n.ok {
  color: var(--zf-success);
}
.statgrid .n.warn {
  color: var(--zf-warning);
}
.statgrid .n.bad {
  color: var(--zf-danger);
}
.statgrid .l {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
/* 四个可点击数字：交卷/批改筛选 */
.stat {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  cursor: pointer;
  text-align: left;
}
.stat:hover { border-color: var(--zf-primary); }
.stat.active {
  border-color: var(--zf-primary);
  box-shadow: 0 0 0 1px var(--zf-primary) inset;
}
.list-pager {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--zf-space-3);
}
.mapping-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-right: 4px;
  border-radius: 50%;
  border: 1px solid var(--zf-border);
  color: var(--zf-text-secondary);
  font-size: 11px;
  cursor: help;
}
.shuffle-tip {
  margin-left: 8px;
  font-size: var(--zf-text-xs);
}

/* 扫描图弹窗：90vw / 最大 1000px，body 固定高度，图片容器完整 contain */
:deep(.scan-image-dialog) {
  width: 90vw !important;
  max-width: 1000px;
}
:deep(.scan-image-dialog .el-dialog__body) {
  height: calc(90vh - 150px);
  overflow: hidden;
  padding-top: 8px;
  padding-bottom: 8px;
  box-sizing: border-box;
}
.scan-dialog-body {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
}
.scan-dialog-toolbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 8px;
}
.scan-dialog-viewer {
  flex: 1;
  min-height: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #f5f5f5;
}
@media (max-width: 768px) {
  :deep(.scan-image-dialog) { width: 96vw !important; }
  :deep(.scan-image-dialog .el-dialog__body) { height: 75vh; }
}
.kv {
  display: flex;
  justify-content: space-between;
  gap: var(--zf-space-4);
  padding: 7px 0;
  border-bottom: 1px dashed var(--zf-border-subtle);
}
.kv:last-child {
  border-bottom: none;
}
.kv .k {
  color: var(--zf-text-secondary);
}
.kv .v {
  text-align: right;
}
.scan-thumbs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.scan-thumb {
  width: 46px;
  height: 62px;
  object-fit: contain;
  border: 1px solid var(--zf-border-subtle);
  cursor: zoom-in;
  background: #fafafa;
}</style>
