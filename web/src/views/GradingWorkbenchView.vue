<script setup lang="ts">
/**
 * 批改工作台（`/assignments/:id/grading`）。
 *
 * ★★ 这个页面的核心设计是**把「待人工确认」与「已自动批改」分开**：
 *
 *     AUTO_GRADED    机器判好了，可以直接批量确认
 *     NEEDS_REVIEW   机器**不确定**，必须逐条看
 *
 *   后端 `confirm-class` **只确认 `AUTO_GRADED`**（`NEEDS_REVIEW`
 *   一律跳过）。这是刻意的：批量确认会把「机器不确定的」当成
 *   「已确认的」，等于把仲裁环节跳过。
 *   所以界面上也必须体现这个区分，不能让老师以为「确认全班」
 *   就万事大吉了。
 *
 * ★★ 第二个设计点：**自动批改是逐生的**。
 *   后端签名要 `student_ids`。一个学生的数据异常不该让全班失败。
 *   所以这里是「一键跑全班」＝ 循环调逐生接口，并把失败的学生
 *   单独列出来。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import * as G from '@/api/grading'
import * as O from '@/api/omr'
import { getAssetBlob } from '@/api/paper'
import ScanImageViewer from '@/components/ScanImageViewer.vue'
import { useAssignmentStore } from '@/stores/assignment'

const route = useRoute()
const router = useRouter()
const store = useAssignmentStore()

const id = computed(() => Number(route.params.id))

const progress = ref<G.GradingProgress | null>(null)
const scores = ref<G.ScoreRow[]>([])
const queue = ref<G.ReviewQueue | null>(null)
const loading = ref(false)
const grading = ref(false)
const confirming = ref(false)
const error = ref('')
const failMsg = ref('')

// ★ 学生名单（交卷情况 + 批改进度）——工作台的主视图
const roster = ref<G.GradingRoster | null>(null)
const rosterLoading = ref(false)
const gradingOne = ref<number | null>(null)
const detailRow = ref<G.GradingRosterRow | null>(null)
const detailVisible = ref(false)
const rosterThumbs = ref<Record<number, string>>({})
const detailItems = ref<G.GradingStudentItem[]>([])
const detailLoading = ref(false)
const detailImages = ref<Record<string, string>>({})
const activeScanIndex = ref(0)
const scanMode = ref<'annotated' | 'display' | 'raw'>('annotated')

// 名单分页 / 筛选
const rosterPage = ref(1)
const rosterPageSize = ref(20)
const rosterStateFilter = ref('')

// 筛选
const filterState = ref('')
const filterStudent = ref<number | undefined>(undefined)

const shown = computed(() =>
  scores.value.filter((s) => {
    if (filterState.value && s.state !== filterState.value) return false
    if (filterStudent.value && s.student_id !== filterStudent.value) return false
    return true
  }),
)

const reviewCount = computed(() => queue.value?.count ?? 0)
const autoGradedCount = computed(
  () => progress.value?.by_state?.AUTO_GRADED ?? 0,
)
const progressPct = computed(() =>
  Math.round((progress.value?.progress ?? 0) * 100),
)

async function refresh(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [p, sc, q] = await Promise.all([
      G.getProgress(id.value),
      G.listScores({ assignmentId: id.value, all: true, limit: 1000 }),
      G.getReviewQueue(id.value),
    ])
    progress.value = p
    scores.value = sc
    queue.value = q
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

// ══════════════════════════════════════════════════
//  ★ 学生名单（老师的心智：谁交了、谁批了）
// ══════════════════════════════════════════════════

const rosterRows = computed(() => roster.value?.items ?? [])
const filteredRosterRows = computed(() => {
  if (!rosterStateFilter.value) return rosterRows.value
  return rosterRows.value.filter(
    (r) => r.grading_state === rosterStateFilter.value,
  )
})
const pagedRosterRows = computed(() => {
  const start = (rosterPage.value - 1) * rosterPageSize.value
  return filteredRosterRows.value.slice(start, start + rosterPageSize.value)
})
const rosterFilterText = computed(() => {
  if (!rosterStateFilter.value) return ''
  return G.gradingStateLabel(rosterStateFilter.value)
})
const submittedCount = computed(() => roster.value?.counts.submitted ?? 0)
const notSubmittedCount = computed(
  () => roster.value?.counts.not_submitted ?? 0,
)
const pendingGradeCount = computed(
  () => roster.value?.counts.pending_grade ?? 0,
)
const gradedCount = computed(() => roster.value?.counts.graded ?? 0)
const rosterNeedsReview = computed(
  () => roster.value?.counts.needs_review ?? 0,
)

function clearRosterFilter(): void {
  rosterStateFilter.value = ''
  rosterPage.value = 1
}

function onRosterFilter(): void {
  rosterPage.value = 1
}

async function loadRoster(): Promise<void> {
  rosterLoading.value = true
  try {
    roster.value = await G.getRoster(id.value)
  } catch (e) {
    // 名单失败不该把整页打空：下面的进度/明细仍然可用
    roster.value = null
    console.warn('批改名单加载失败', e)
  } finally {
    rosterLoading.value = false
  }
}

/** 该生批改进度文案（规格 1.1：客观题 x/y · 待人工 n）。 */
function gradeProgressText(row: G.GradingRosterRow): string {
  if (!row.submitted) return '—'
  return `客观题 ${row.scored_auto}/${row.auto_item_count} · 待人工 ${row.remaining_manual}`
}

function gradingStateLabel(row: G.GradingRosterRow): string {
  return row.grading_state_label ?? G.gradingStateLabel(row.grading_state)
}

function canAutoGrade(row: G.GradingRosterRow): boolean {
  return !!row.student_id && row.grading_state === 'PENDING_AUTO'
}

/** 单生自动批改。★ 后端接口就是单生的（一个学生数据异常不该拖垮全班）。 */
async function handleGradeOne(row: G.GradingRosterRow): Promise<void> {
  if (!row.student_id) {
    ElMessage.warning('该生还没有身份行，无法批改')
    return
  }
  if (!canAutoGrade(row)) {
    ElMessage.warning(row.grading_state === 'DONE'
      ? '该生已批改完成'
      : '该生当前没有待自动批改的客观题')
    return
  }
  gradingOne.value = row.row_id
  try {
    const r = await G.autoGrade(id.value, { student_ids: [row.student_id] })
    // ★ 手动批改后必须重新拉 roster：状态/统计立即更新
    await Promise.all([loadRoster(), refresh()])
    ElMessage.success(
      `已批改 ${row.student_name || row.student_no}：${r.graded} 题`,
    )
    if (r.needs_review) {
      ElMessage.warning(`其中 ${r.needs_review} 条机器不确定，需人工确认`)
    }
    if (r.recompute_failed?.length) {
      ElMessage.warning('学情重算未全部成功，掌握度可能暂缺')
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '批改失败')
  } finally {
    gradingOne.value = null
  }
}

/** 名单当前页显示标注缩略图（只加载 20 人）。 */
async function loadRosterThumbs(
  rows: G.GradingRosterRow[],
): Promise<void> {
  for (const row of rows) {
    const page = row.scan_pages?.[0]
    if (!page || rosterThumbs.value[page.card_id]) continue
    try {
      const blob = await O.getAnnotatedCardBlob(page.card_id)
      rosterThumbs.value[page.card_id] = URL.createObjectURL(blob)
    } catch { /* 无扫描图/标注失败时显示 — */ }
  }
}

watch(pagedRosterRows, (rows) => {
  void loadRosterThumbs(rows)
}, { immediate: true })

function rosterThumb(row: G.GradingRosterRow): string {
  const page = row.scan_pages?.[0]
  return page ? (rosterThumbs.value[page.card_id] ?? '') : ''
}

onBeforeUnmount(() => {
  for (const u of Object.values(rosterThumbs.value)) URL.revokeObjectURL(u)
})

/** 查看详情：该生**全部题**（含未批的主观题）+ 扫描图/标注图。 */
async function openDetail(row: G.GradingRosterRow): Promise<void> {
  if (!row.student_id) return
  detailRow.value = row
  detailVisible.value = true
  detailLoading.value = true
  detailItems.value = []
  activeScanIndex.value = 0
  scanMode.value = 'annotated'
  try {
    detailItems.value = await G.getStudentItems(id.value, row.student_id)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载逐题详情失败')
  } finally {
    detailLoading.value = false
  }
  await loadDetailImages(row)
}

async function loadDetailImages(
  row: G.GradingRosterRow, forceAnnotated = false,
): Promise<void> {
  const pages = row.scan_pages ?? []
  if (forceAnnotated) {
    for (const p of pages) {
      const key = `annotated:${p.card_id}`
      const old = detailImages.value[key]
      if (old) URL.revokeObjectURL(old)
      delete detailImages.value[key]
      const thumbOld = rosterThumbs.value[p.card_id]
      if (thumbOld) URL.revokeObjectURL(thumbOld)
      delete rosterThumbs.value[p.card_id]
    }
  }
  await Promise.all(pages.flatMap((p) => {
    const jobs: Promise<void>[] = []
    if (!detailImages.value[`display:${p.card_id}`]) {
      jobs.push((p.display_asset_id
        ? getAssetBlob(p.display_asset_id)
        : O.getCardDisplayBlob(p.card_id)
      ).then((b) => {
        detailImages.value[`display:${p.card_id}`] =
          URL.createObjectURL(b)
      }).catch(() => undefined))
    }
    if (!detailImages.value[`raw:${p.card_id}`]) {
      jobs.push(getAssetBlob(p.raw_asset_id).then((b) => {
        detailImages.value[`raw:${p.card_id}`] = URL.createObjectURL(b)
      }).catch(() => undefined))
    }
    if (!detailImages.value[`annotated:${p.card_id}`]) {
      jobs.push(O.getAnnotatedCardBlob(p.card_id).then((b) => {
        detailImages.value[`annotated:${p.card_id}`] =
          URL.createObjectURL(b)
      }).catch(() => undefined))
    }
    return jobs
  }))
}

const scanPages = computed(() => detailRow.value?.scan_pages ?? [])

function scanUrl(prefix: 'annotated' | 'display' | 'raw',
                  cardId: number): string {
  return detailImages.value[`${prefix}:${cardId}`] ?? ''
}

const activeScanUrl = computed(() => {
  const p = scanPages.value[activeScanIndex.value]
  if (!p) return ''
  return scanUrl(scanMode.value, p.card_id) || scanUrl('display', p.card_id)
})

const scanPreviewList = computed(() =>
  scanPages.value
    .map((p) => scanUrl(scanMode.value, p.card_id)
      || scanUrl('display', p.card_id))
    .filter(Boolean),
)

onBeforeUnmount(() => {
  for (const u of Object.values(detailImages.value)) URL.revokeObjectURL(u)
})

const STATUS_LABEL: Record<string, string> = {
  NOT_STARTED: '未开始',
  IN_PROGRESS: '作答中',
  SUBMITTED: '已交卷',
  SCANNED: '已扫描',
  ABSENT: '缺考',
  PARTIAL: '部分完成',
  GRADED: '已批改',
}
const STATUS_TONE: Record<string, 'info' | 'warning' | 'success' | 'danger'> = {
  NOT_STARTED: 'info',
  IN_PROGRESS: 'warning',
  SUBMITTED: 'success',
  SCANNED: 'success',
  ABSENT: 'danger',
  PARTIAL: 'warning',
  GRADED: 'success',
}

function statusLabel(row: G.GradingRosterRow): string {
  return row.status_label ?? STATUS_LABEL[row.status]
    ?? `未知状态（${row.status}）`
}

/** 未交卷的行压低对比度（Element Plus 的行 class 钩子）。 */
function rosterRowClass({ row }: { row: G.GradingRosterRow }): string {
  return row.submitted ? '' : 'roster-row--idle'
}

/** 交卷时间显示（空则 —）。 */
function timeText(v: string | null): string {
  if (!v) return '—'
  return new Date(v).toLocaleString('zh-CN', { hour12: false })
}

onMounted(async () => {
  const sid = Number(route.query.student_id)
  if (sid) filterStudent.value = sid
  store.reset()
  await store.load(id.value)
  await Promise.all([store.loadStudents(), loadRoster()])
  await refresh()
  if (sid) {
    const row = rosterRows.value.find((r) => r.student_id === sid)
    if (row) await openDetail(row)
  }
})

/**
 * 批改全班（**只批已交卷的**）。
 *
 * ★ 循环调后端（后端接口是**单生**的）。把失败的学生收集起来，
 *   不因为一个人失败就中断——那正是后端「逐题独立」的同一思路。
 *
 * ★★ 范围为什么是「已交卷」而不是「名单里所有人」：
 *   给未交卷的学生也批一遍，会给他们建出一批 0 分记录，
 *   成绩单上就出现大量**假的 0 分** —— 而老师需要的是
 *   「谁还没交」这个信息（名单视图里已经单独列了）。
 */
async function handleAutoGrade(): Promise<void> {
  const targets = rosterRows.value.filter(
    (r) => r.submitted && r.student_id
      && r.grading_state === 'PENDING_AUTO')
  if (!targets.length) {
    const tip = roster.value?.counts.pending_manual
      ? `没有待自动批改的客观题；还有 ${roster.value.counts.pending_manual} 人待人工录分`
      : '没有需要自动批改的学生'
    ElMessage.warning(tip)
    return
  }
  try {
    await ElMessageBox.confirm(
      `对 ${targets.length} 名**已交卷**的学生执行自动批改？\n` +
        '只批选择题与判断题；扫描卷的填空题和主观题需人工录分。\n' +
        '未交卷的学生不会产生记录。',
      '批改全班',
      { confirmButtonText: '开始批改', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  grading.value = true
  failMsg.value = ''
  const failed: Array<{ no: string; err: string }> = []
  let done = 0
  try {
    for (const s of targets) {
      try {
        await G.autoGrade(id.value, { student_ids: [s.student_id as number] })
        done += 1
      } catch (e) {
        failed.push({
          no: s.student_no,
          err: e instanceof Error ? e.message : '未知错误',
        })
      }
    }
    await Promise.all([loadRoster(), refresh()])
    if (failed.length) {
      failMsg.value =
        `${done} 人批改成功，${failed.length} 人失败：` +
        failed.map((f) => `${f.no}（${f.err}）`).join('；')
      ElMessage.warning(`部分失败：${failed.length} 人`)
    } else {
      ElMessage.success(`已完成 ${done} 人的批改`)
    }
  } finally {
    grading.value = false
  }
}

/** 批量确认 `AUTO_GRADED`（★ 不含 `NEEDS_REVIEW`）。 */
async function handleConfirmClass(): Promise<void> {
  if (!autoGradedCount.value) {
    ElMessage.warning('没有可批量确认的评分')
    return
  }
  try {
    await ElMessageBox.confirm(
      `批量确认 ${autoGradedCount.value} 条自动批改结果？\n` +
        `另有 ${reviewCount.value} 条「待人工确认」，` +
        '它们**不会**被批量确认，必须逐条看。',
      '批量确认',
      { confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  confirming.value = true
  try {
    const r = await G.confirmClass(id.value)
    await refresh()
    ElMessage.success(
      `已确认 ${r.confirmed} 条，剩余待人工确认 ${r.needs_review_remaining} 条`,
    )
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '确认失败')
  } finally {
    confirming.value = false
  }
}

async function handleConfirmOne(row: G.ScoreRow): Promise<void> {
  try {
    await G.confirmScore(row.id, { modified: false, reason: '教师确认' })
    await refresh()
    ElMessage.success('已确认')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '确认失败')
  }
}

/** 改分：先填新分，再以 `modified=true` 确认（会触发学情重算）。 */
async function handleModify(row: G.ScoreRow): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      `第 ${row.seq} 题（满分 ${Number(row.max_score).toFixed(0)}）改分为：`,
      '教师改分',
      {
        inputValue: row.score,
        inputPattern: /^\d+(\.\d+)?$/,
        inputErrorMessage: '请输入数字',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
      },
    )
    const v = Number(value)
    if (v < 0 || v > Number(row.max_score)) {
      ElMessage.warning(`分数需在 0 – ${Number(row.max_score).toFixed(0)} 之间`)
      return
    }
    // ★ 人工录分接口，然后用 modified=true 确认（触发重算）
    await G.manualScore(id.value, {
      student_id: row.student_id,
      paper_item_id: row.paper_item_id,
      score: v,
      reason: '教师改分',
    })
    await refresh()
    ElMessage.success('已改分（学情已重算）')
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e instanceof Error ? e.message : '改分失败')
  }
}

/** 详情表里的录分/改分（主观题、填空题、客观题都可用）。 */
async function handleManualItem(item: G.GradingStudentItem): Promise<void> {
  const row = detailRow.value
  if (!row?.student_id) return
  try {
    const { value } = await ElMessageBox.prompt(
      `第 ${item.seq} 题（满分 ${Number(item.max_score).toFixed(0)}）录分：`,
      item.needs_manual ? '主观题录分' : '教师改分',
      {
        inputValue: item.score ?? '0',
        inputPattern: /^\d+(\.\d+)?$/,
        inputErrorMessage: '请输入数字',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
      },
    )
    const v = Number(value)
    if (v < 0 || v > Number(item.max_score)) {
      ElMessage.warning(`分数需在 0 – ${Number(item.max_score).toFixed(0)} 之间`)
      return
    }
    await G.manualScore(id.value, {
      student_id: row.student_id,
      paper_item_id: item.paper_item_id,
      score: v,
      reason: item.needs_manual ? '主观题录分' : '教师改分',
    })
    await Promise.all([loadRoster(), refresh()])
    detailItems.value = await G.getStudentItems(id.value, row.student_id)
    await loadDetailImages(row, true)
    await loadRosterThumbs([row])
    ElMessage.success('已保存，批改状态已更新')
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e instanceof Error ? e.message : '录分失败')
  }
}

async function handleVoid(row: G.ScoreRow): Promise<void> {
  try {
    await ElMessageBox.confirm(`作废第 ${row.seq} 题的评分？`, '作废', {
      confirmButtonText: '作废',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await G.voidScore(row.id, '教师作废')
    await refresh()
    ElMessage.success('已作废')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '作废失败')
  }
}

const studentName = (sid: number): string =>
  store.students.find((s) => s.student_id === sid)?.student_name ?? `#${sid}`
</script>

<template>
  <div v-loading="loading" class="zf-stack">
    <div class="head">
      <div>
        <h1 class="page-title">批改 · {{ store.detail?.title ?? '' }}</h1>
        <p class="zf-muted page-sub">
          {{ store.detail?.classroom_name }} ·
          {{ store.detail?.paper_title }}
        </p>
      </div>
      <div class="zf-row">
        <el-button @click="router.push({ name: 'AssignmentDetail', params: { id } })">
          返回详情
        </el-button>
        <el-button @click="refresh">刷新</el-button>
        <el-button
          type="primary"
          :loading="grading"
          :disabled="!submittedCount"
          @click="handleAutoGrade"
        >
          批改全班（已交卷 {{ submittedCount }} 人）
        </el-button>
        <el-button :loading="confirming" @click="handleConfirmClass">
          批量确认（{{ autoGradedCount }}）
        </el-button>
        <!-- ★ 主观题「按题批」的唯一入口（需求 4）。
             这一页是按学生看，主观题连着批 45 份要一直重新找位置，
             所以把「按题」视角放在这里而不是作业详情页。 -->
        <el-button
          @click="router.push({ name: 'AssignmentReview', params: { id } })"
        >
          主观题阅卷
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
    <el-alert
      v-if="failMsg"
      type="warning"
      :closable="false"
      show-icon
      title="部分学生批改失败"
      :description="failMsg"
    />

    <!-- ── ★ 学生名单（交卷情况 + 批改进度） ── -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">学生名单</h2>
        <div class="zf-row">
          <el-tag size="small" type="success">
            已交卷 {{ submittedCount }}
          </el-tag>
          <el-tag v-if="notSubmittedCount" size="small" type="info">
            未交 {{ notSubmittedCount }}
          </el-tag>
          <el-tag v-if="gradedCount" size="small" type="success">
            已批改 {{ gradedCount }}
          </el-tag>
          <el-tag v-if="pendingGradeCount" size="small" type="warning">
            待批改 {{ pendingGradeCount }}
          </el-tag>
          <el-tag v-if="rosterNeedsReview" size="small" type="danger">
            待人工确认 {{ rosterNeedsReview }}
          </el-tag>
        </div>
      </div>
      <div class="zf-card__body">
        <p
          class="zf-dim"
          style="margin: 0 0 12px; font-size: var(--zf-text-xs)"
        >
          已交卷的学生可以单独点「批改」（只批他这一份，不影响别人），
          批完点「查看详情」逐题核对；也可以一次<b>批改全班</b>。
          扫描来源的填空题与所有主观题机器不判，需点「录分」录入分数。
        </p>
        <div class="zf-row" style="margin-bottom: 10px; gap: 8px; flex-wrap: wrap">
          <el-select
            v-model="rosterStateFilter"
            clearable
            size="small"
            placeholder="按批改状态筛选"
            style="width: 180px"
            @change="onRosterFilter"
          >
            <el-option
              v-for="(l, v) in G.GRADING_STATE_LABEL"
              :key="v"
              :label="l"
              :value="v"
            />
          </el-select>
          <el-tag v-if="rosterFilterText" closable size="small" type="warning"
                  @close="clearRosterFilter">
            已按「{{ rosterFilterText }}」筛选
          </el-tag>
          <el-button v-if="rosterFilterText" size="small" text
                     @click="clearRosterFilter">
            清除筛选
          </el-button>
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            共 {{ filteredRosterRows.length }} 人 · 每页 {{ rosterPageSize }} 人
          </span>
        </div>
        <el-table
          v-loading="rosterLoading"
          :data="pagedRosterRows"
          size="small"
          height="60vh"
          :row-class-name="rosterRowClass"
        >
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="student_name" label="姓名" width="110" />
          <el-table-column label="扫描图" width="78">
            <template #default="{ row }">
              <img
                v-if="rosterThumb(row)"
                :src="rosterThumb(row)"
                class="roster-thumb"
                alt="标注图"
                @click="openDetail(row)"
              />
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <el-tag size="small" :type="STATUS_TONE[row.status] ?? 'info'">
                {{ statusLabel(row) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="批改状态" width="120">
            <template #default="{ row }">
              <el-tag size="small"
                      :type="G.GRADING_STATE_TONE[row.grading_state] ?? 'info'">
                {{ gradingStateLabel(row) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="90">
            <template #default="{ row }">
              <el-tag
                v-if="row.answer_source"
                size="small"
                :type="row.answer_source === 'SCAN' ? 'success' : 'info'"
              >
                {{ row.answer_source === 'SCAN' ? '扫描' : '线上' }}
              </el-tag>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="交卷时间" width="170">
            <template #default="{ row }">{{ timeText(row.submitted_at) }}</template>
          </el-table-column>
          <el-table-column label="批改进度" min-width="210">
            <template #default="{ row }">
              <span class="zf-dim" style="font-size: var(--zf-text-xs)">
                {{ gradeProgressText(row) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="得分" width="96">
            <template #default="{ row }">
              <b v-if="row.total_score !== null">{{ row.total_score }}</b>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="190" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.grading_state === 'PENDING_AUTO'"
                size="small"
                text
                type="primary"
                :disabled="!row.submitted || !row.student_id"
                :loading="gradingOne === row.row_id"
                @click="handleGradeOne(row)"
              >
                批改
              </el-button>
              <el-button
                v-else-if="row.grading_state === 'PENDING_MANUAL'"
                size="small"
                text
                type="warning"
                :disabled="!row.student_id"
                @click="openDetail(row)"
              >
                录分
              </el-button>
              <el-button
                v-else
                size="small"
                text
                disabled
              >
                {{ row.grading_state === 'DONE' ? '已批改' : '批改' }}
              </el-button>
              <el-button
                size="small"
                text
                :disabled="!row.student_id"
                @click="openDetail(row)"
              >
                查看详情
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">
              {{ rosterLoading ? '读取中…' : '名单里还没有学生' }}
            </div>
          </template>
        </el-table>
        <div class="roster-pager">
          <el-pagination
            v-model:current-page="rosterPage"
            v-model:page-size="rosterPageSize"
            :total="filteredRosterRows.length"
            :page-sizes="[20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            background
            small
          />
        </div>
      </div>
    </section>

    <!-- ── 进度 ── -->
    <section v-if="progress" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">批改进度</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          {{ progress.done }} / {{ progress.total }}
        </span>
      </div>
      <div class="zf-card__body">
        <el-progress
          :percentage="progressPct"
          :stroke-width="10"
          :show-text="false"
        />
        <div class="statgrid">
          <div>
            <div class="n">{{ progress.total }}</div>
            <div class="l">评分总数</div>
          </div>
          <div>
            <div class="n ok">{{ progress.done }}</div>
            <div class="l">已批改</div>
          </div>
          <div>
            <div class="n warn">{{ progress.needs_review }}</div>
            <div class="l">待人工确认</div>
          </div>
          <div>
            <div class="n">{{ autoGradedCount }}</div>
            <div class="l">可批量确认</div>
          </div>
        </div>
      </div>
    </section>

    <!-- ── 待人工确认队列 ── -->
    <section v-if="queue" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">待人工确认</h2>
        <el-tag v-if="queue.count" type="warning" size="small">
          {{ queue.count }} 条
        </el-tag>
        <el-tag v-else type="success" size="small">已清空</el-tag>
      </div>
      <div class="zf-card__body">
        <!-- ★ 这一段文案是必须的：否则老师会以为「批量确认」能解决全部 -->
        <p class="zf-dim" style="margin: 0 0 12px; font-size: var(--zf-text-xs)">
          这些是<b>机器不确定</b>的评分（置信度低于阈值）。
          「批量确认」会跳过它们——把不确定的当成已确认等于跳过仲裁环节。
          请逐条看后确认或改分。
        </p>
        <el-table v-if="queue.items.length" :data="queue.items" size="small">
          <el-table-column label="学生" width="110">
            <template #default="{ row }">
              {{ row.student_name || studentName(row.student_id) }}
            </template>
          </el-table-column>
          <el-table-column prop="seq" label="题号" width="64" />
          <el-table-column label="题型" width="120">
            <template #default="{ row }">
              {{ G.questionTypeLabel(row.question_type) }}
            </template>
          </el-table-column>
          <el-table-column label="得分" width="110">
            <template #default="{ row }">{{ G.scoreText(row) }}</template>
          </el-table-column>
          <el-table-column label="置信度" width="92">
            <template #default="{ row }">
              {{ Number(row.confidence).toFixed(3) }}
            </template>
          </el-table-column>
          <el-table-column label="判分说明" min-width="220">
            <template #default="{ row }">
              <span class="zf-dim" style="font-size: var(--zf-text-xs)">
                {{ (row.deduction_reasons || []).map(G.reasonText).join('；') || '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="handleConfirmOne(row)">
                确认
              </el-button>
              <el-button size="small" text @click="handleModify(row)">改分</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="zf-empty">没有需要人工确认的评分</div>
      </div>
    </section>

    <!-- ── 评分明细 ── -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">评分明细</h2>
        <div class="zf-row">
          <el-select
            v-model="filterState"
            clearable
            size="small"
            placeholder="全部状态"
            style="width: 150px"
          >
            <el-option
              v-for="(l, v) in G.SCORE_STATE_LABEL"
              :key="v"
              :label="l"
              :value="v"
            />
          </el-select>
          <el-select
            v-model="filterStudent"
            clearable
            filterable
            size="small"
            placeholder="全部学生"
            style="width: 150px"
          >
            <el-option
              v-for="s in store.students"
              :key="s.student_id"
              :label="`${s.student_no} ${s.student_name}`"
              :value="s.student_id"
            />
          </el-select>
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            显示 {{ shown.length }} / {{ scores.length }}
          </span>
        </div>
      </div>
      <div class="zf-card__body">
        <el-table :data="shown" size="small" max-height="560">
          <el-table-column label="学生" width="110">
            <template #default="{ row }">
              {{ row.student_name || studentName(row.student_id) }}
            </template>
          </el-table-column>
          <el-table-column prop="seq" label="题号" width="62" />
          <el-table-column label="题型" width="118">
            <template #default="{ row }">
              {{ G.questionTypeLabel(row.question_type) }}
            </template>
          </el-table-column>
          <el-table-column label="得分" width="104">
            <template #default="{ row }">{{ G.scoreText(row) }}</template>
          </el-table-column>
          <el-table-column label="对错" width="70">
            <template #default="{ row }">
              <el-tag
                v-if="row.is_correct === true"
                size="small"
                type="success"
              >
                对
              </el-tag>
              <el-tag
                v-else-if="row.is_correct === false"
                size="small"
                type="danger"
              >
                错
              </el-tag>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="126">
            <template #default="{ row }">
              <el-tag size="small" :type="G.SCORE_STATE_TONE[row.state]">
                {{ row.state_label || G.scoreStateLabel(row.state) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="判分说明" min-width="200">
            <template #default="{ row }">
              <span class="zf-dim" style="font-size: var(--zf-text-xs)">
                {{ (row.deduction_reasons || []).map(G.reasonText).join('；') || '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button
                size="small"
                text
                @click="handleModify(row)"
                :disabled="row.state === 'VOID'"
              >
                改分
              </el-button>
              <el-button
                size="small"
                text
                type="danger"
                @click="handleVoid(row)"
                :disabled="row.state === 'VOID'"
              >
                作废
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">
              {{ loading ? '读取中…' : '还没有评分记录。先点「批改全班」。' }}
            </div>
          </template>
        </el-table>
      </div>
    </section>

    <!-- ── ★ 单生详情（逐题全量 + 扫描图/标注图） ── -->
    <el-dialog
      v-model="detailVisible"
      :title="`批改详情 · ${detailRow?.student_no ?? ''} ${detailRow?.student_name ?? ''}`"
      width="min(1000px, 90vw)"
      top="5vh"
      class="grading-detail-dialog"
    >
      <div v-if="detailRow" class="detail-head">
        <span>状态：<b>{{ statusLabel(detailRow) }}</b></span>
        <span>批改状态：<b>{{ gradingStateLabel(detailRow) }}</b></span>
        <span>交卷：{{ timeText(detailRow.submitted_at) }}</span>
        <span>得分：<b>{{ detailRow.total_score ?? '—' }}</b></span>
        <span>
          客观题：{{ detailRow.scored_auto }} / {{ detailRow.auto_item_count }}
          · 待人工 {{ detailRow.remaining_manual }}
        </span>
      </div>
      <div class="detail-body">
        <aside class="scan-pane">
          <div class="scan-toolbar">
            <el-radio-group v-model="scanMode" size="small">
              <el-radio-button value="annotated">标注</el-radio-button>
              <el-radio-button value="display">校正</el-radio-button>
              <el-radio-button value="raw">原图</el-radio-button>
            </el-radio-group>
          </div>
          <div v-if="scanPages.length" class="scan-main">
            <ScanImageViewer
              v-if="activeScanUrl"
              :src="activeScanUrl"
              :preview-list="scanPreviewList"
              :initial-index="activeScanIndex"
              alt="扫描答题卡"
            />
            <div v-else class="zf-dim scan-empty">扫描图加载中…</div>
            <div class="scan-page-tabs">
              <el-button
                v-for="(p, i) in scanPages"
                :key="p.card_id"
                size="small"
                :type="i === activeScanIndex ? 'primary' : 'default'"
                @click="activeScanIndex = i"
              >
                第 {{ p.page_no ?? i + 1 }} 页
                <span v-if="p.status_label" class="zf-dim">
                  · {{ p.status_label }}
                </span>
              </el-button>
            </div>
            <p class="zf-dim scan-tip">
              点击大图可放大；「标注」绿=正确 红=错误，未作答不标。
            </p>
          </div>
          <el-empty v-else description="没有扫描图（线上作答）" :image-size="48" />
        </aside>
        <section class="score-pane">
          <el-table
            v-loading="detailLoading"
            :data="detailItems"
            size="small"
            height="55vh"
          >
            <el-table-column prop="seq" label="题号" width="58" fixed="left" />
            <el-table-column label="题型" width="96">
              <template #default="{ row }">
                {{ row.question_type_label || G.questionTypeLabel(row.question_type) }}
              </template>
            </el-table-column>
            <el-table-column label="学生答案" min-width="150">
              <template #default="{ row }">
                <!-- ★ 识别存疑题的作答记录可能根本写不出来（浅涂/漏涂），
                     此时若显示「未作答」，老师会以为学生没答——
                     这正是需求 1 里要分开的两种情形。 -->
                <span
                  class="detail-ans"
                  :class="{
                    'is-wrong': row.is_correct === false,
                    'is-correct': row.is_correct === true,
                    'is-uncertain': row.method === 'OMR_UNCERTAIN',
                  }"
                >
                  {{ row.student_answer
                    || (row.method === 'OMR_UNCERTAIN' ? '（涂点未识别清楚）' : '（未作答）') }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="正确答案" min-width="120">
              <template #default="{ row }">
                <span class="detail-ans detail-ans--std">
                  {{ row.correct_answer || '—' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="得分" width="92">
              <template #default="{ row }">
                <b v-if="row.score !== null">{{ row.score }}</b>
                <span v-else class="zf-dim">未录</span>
                <span class="zf-dim"> / {{ Number(row.max_score).toFixed(0) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="112">
              <template #default="{ row }">
                <el-tag
                  v-if="row.method === 'OMR_UNCERTAIN'"
                  size="small"
                  type="warning"
                  effect="plain"
                >
                  识别存疑
                </el-tag>
                <el-tag v-else size="small" :type="G.SCORE_STATE_TONE[row.state]">
                  {{ row.state_label || G.scoreStateLabel(row.state) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="判分说明" min-width="160">
              <template #default="{ row }">
                <span class="zf-dim" style="font-size: var(--zf-text-xs)">
                  {{ (row.deduction_reasons || []).map(G.reasonText).join('；') || '—' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  text
                  type="primary"
                  @click="handleManualItem(row)"
                >
                  {{ row.needs_manual || row.score === null ? '录分' : '改分' }}
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <div class="zf-empty">
                {{ detailLoading ? '读取中…' : '这名学生还没有可显示的题目' }}
              </div>
            </template>
          </el-table>
        </section>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
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
}
.page-sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}
.statgrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
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
.statgrid .l {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}

.roster-pager {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--zf-space-3);
}
.roster-thumb {
  width: 46px;
  height: 60px;
  object-fit: contain;
  border: 1px solid var(--zf-border-subtle);
  background: #fafafa;
  cursor: zoom-in;
}
/* 详情弹窗：1000px / top 5vh / 高度不超过 90vh */
:deep(.grading-detail-dialog) {
  width: 90vw !important;
  max-width: 1000px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  margin-bottom: 0;
}
:deep(.grading-detail-dialog .el-dialog__body) {
  flex: none;
  height: calc(90vh - 150px);
  min-height: 0;
  overflow: hidden;
  box-sizing: border-box;
}
.detail-body {
  display: flex;
  gap: var(--zf-space-4);
  align-items: stretch;
  height: 100%;
  min-height: 0;
}
.scan-pane {
  width: 320px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.scan-toolbar { flex: none; }
.scan-main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.scan-main :deep(.viewer-shell) {
  flex: 1;
  min-height: 0;
}
@media (max-width: 768px) {
  :deep(.grading-detail-dialog) { width: 96vw !important; }
  :deep(.grading-detail-dialog .el-dialog__body) { height: 75vh; }
  .detail-body { flex-direction: column; }
  .scan-pane { width: 100%; height: 40vh; }
}
.scan-page-tabs {
  flex: none;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.scan-tip {
  flex: none;
  margin: 0;
  font-size: var(--zf-text-xs);
}
.scan-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--zf-border-subtle);
}
.score-pane {
  flex: 1;
  min-width: 0;
  min-height: 0;
}
:deep(.roster-row--idle) {  color: var(--zf-text-secondary);
}
.detail-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--zf-space-4);
  margin-bottom: var(--zf-space-3);
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
.detail-ans {
  font-family: var(--zf-font-mono, ui-monospace, monospace);
  word-break: break-all;
}
/* 学生答案：错→红、对→绿（与标注图同一套语义色） */
.detail-ans.is-wrong {
  color: var(--zf-danger);
  font-weight: 600;
}
.detail-ans.is-correct {
  color: var(--zf-success);
}
/* 正确答案列：始终用成功色，与「学生答错的红」形成对照 */
.detail-ans--std {
  color: var(--zf-success);
}
/* 识别存疑：不是学生答错，也不是没答——用警示色，别用红 */
.detail-ans.is-uncertain {
  color: var(--zf-warning, #d98600);
  font-style: italic;
  font-weight: 500;
}
</style>
