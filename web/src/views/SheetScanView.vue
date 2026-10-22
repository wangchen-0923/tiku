<script setup lang="ts">
/**
 * 扫描导入与待处理队列。
 *
 * 流程：选作业（自动带出绑定答题卡）→ 上传 PDF/JPG/PNG（可多选）
 *      → 逐页识别（异步，轮询进度）→ 成功/待处理/失败统计
 *      → 待处理可「指派学生 / 作废 / 重扫」
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import * as A from '@/api/assignment'
import * as O from '@/api/omr'
import { getAssetBlob } from '@/api/paper'

const route = useRoute()
const router = useRouter()

const assignments = ref<A.AssignmentBrief[]>([])
const assignmentId = ref<number | undefined>(undefined)
const students = ref<A.AssignmentStudentRow[]>([])
const current = computed(() =>
  assignments.value.find((a) => a.id === assignmentId.value))

const files = ref<File[]>([])
const batchId = ref<number | null>(null)
const progress = ref<O.BatchProgress | null>(null)
const cards = ref<O.ScanCard[]>([])
const busy = ref(false)
const uploading = ref(false)
const recognizing = ref(false)
const recognition = ref('')
const stalled = ref(false)
const error = ref('')
const thumbs = ref<Record<number, string>>({})
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewDisplay = ref('')
const previewRaw = ref('')
let poll: ReturnType<typeof setInterval> | null = null
let pollStartedAt = 0

function goBack(): void {
  if (assignmentId.value) {
    router.push({
      name: 'AssignmentDetail',
      params: { id: assignmentId.value },
    })
  } else {
    router.push({ name: 'SheetList' })
  }
}

function recognitionText(mode: string): string {
  if (mode === 'THREAD') return '已开始识别'
  if (mode === 'DONE') return '识别已完成'
  if (mode === 'QUEUED') return '已提交识别队列'
  if (mode === 'SKIPPED') return '批次正在识别'
  if (mode === 'FAILED') return '识别提交失败，请重试'
  return ''
}

async function loadAssignments(): Promise<void> {
  try {
    const d = await A.listAssignments({ pageSize: 100 })
    assignments.value = d.items
    const q = Number(route.query.assignment_id)
    if (q && d.items.some((a) => a.id === q)) assignmentId.value = q
  } catch (e) {
    error.value = e instanceof Error ? e.message : '作业列表加载失败'
  }
}

async function loadStudents(): Promise<void> {
  if (!assignmentId.value) { students.value = []; return }
  try {
    // ★ api 层已自动翻页，这里拿到的是**全班**（原来只有前 20 人，
    //   第 21 个学生永远分不到扫描页）。
    const d = await A.listAssignmentStudents(assignmentId.value)
    students.value = d.items
  } catch {
    students.value = []
  }
}

async function loadCards(): Promise<void> {
  if (!assignmentId.value) { cards.value = []; return }
  try {
    cards.value = await O.listCards({
      assignmentId: assignmentId.value, onlyPending: true,
    })
    await loadThumbs()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '待处理加载失败'
  }
}

async function loadThumbs(): Promise<void> {
  const todo = cards.value
    .flatMap((c) => [c.display_asset_id ?? c.raw_asset_id, c.raw_asset_id])
    .filter((id): id is number => !!id && !thumbs.value[id])
  for (const id of Array.from(new Set(todo)).slice(0, 80)) {
    try {
      const blob = await getAssetBlob(id)
      thumbs.value[id] = URL.createObjectURL(blob)
    } catch { /* 缩略图失败不阻断 */ }
  }
}

async function openCard(card: O.ScanCard): Promise<void> {
  previewVisible.value = true
  previewLoading.value = true
  previewDisplay.value = ''
  previewRaw.value = ''
  try {
    const displayId = card.display_asset_id ?? card.raw_asset_id
    const [d, r] = await Promise.all([
      getAssetBlob(displayId),
      getAssetBlob(card.raw_asset_id),
    ])
    previewDisplay.value = URL.createObjectURL(d)
    previewRaw.value = URL.createObjectURL(r)
  } catch {
    ElMessage.error('加载扫描图失败')
  } finally {
    previewLoading.value = false
  }
}

onMounted(async () => {
  await loadAssignments()
  if (assignmentId.value) {
    await Promise.all([loadStudents(), loadCards()])
  }
})

onBeforeUnmount(() => {
  if (poll) clearInterval(poll)
  for (const u of Object.values(thumbs.value)) URL.revokeObjectURL(u)
  if (previewDisplay.value) URL.revokeObjectURL(previewDisplay.value)
  if (previewRaw.value) URL.revokeObjectURL(previewRaw.value)
})

watch(assignmentId, async () => {
  batchId.value = null
  progress.value = null
  cards.value = []
  recognition.value = ''
  stalled.value = false
  if (poll) clearInterval(poll)
  poll = null
  await Promise.all([loadStudents(), loadCards()])
})

function onFileChange(_file: UploadFile, list: UploadFile[]): void {
  files.value = list.flatMap((f) => (f.raw ? [f.raw as File] : []))
}

function onFileRemove(_f: UploadFile, list: UploadFile[]): void {
  files.value = list.flatMap((f) => (f.raw ? [f.raw as File] : []))
}

async function startUpload(): Promise<void> {
  if (!assignmentId.value) {
    ElMessage.warning('请先选择作业')
    return
  }
  if (!current.value?.sheet_template_id) {
    ElMessage.warning('该作业还没有绑定答题卡，请到作业详情页绑定')
    return
  }
  if (!files.value.length) {
    ElMessage.warning('请选择扫描件（PDF / JPG / PNG）')
    return
  }
  uploading.value = true
  error.value = ''
  try {
    if (!batchId.value) {
      const b = await O.createBatch(assignmentId.value)
      batchId.value = b.id
    }
    const r = await O.uploadBatch(batchId.value, files.value)
    const skipped = r.skipped_duplicates ?? []
    if (r.pages === 0) {
      const why = skipped.length
        ? `：${skipped.map((x) => `${x.filename}（${x.reason}）`).join('；')}`
        : ''
      error.value = `没有新增扫描页${why}`
      ElMessage.error(error.value)
      files.value = []
      await loadCards()
      return
    }
    recognition.value = r.recognition
    const tip = recognitionText(r.recognition)
    if (r.recognition === 'FAILED') {
      ElMessage.error(`已导入 ${r.pages} 页，但识别提交失败`)
    } else {
      const extra = skipped.length ? `；跳过 ${skipped.length} 个文件` : ''
      ElMessage.success(`已导入 ${r.pages} 页${tip ? `，${tip}` : ''}${extra}`)
    }
    files.value = []
    await pollProgress()
    await loadCards()
  } catch (e) {
    const msg = e instanceof Error ? e.message : '上传失败'
    error.value = msg
    ElMessage.error(msg)
  } finally {
    uploading.value = false
  }
}

async function pollProgress(): Promise<void> {
  if (!batchId.value) return
  if (poll) clearInterval(poll)
  stalled.value = false
  pollStartedAt = Date.now()
  const tick = async (): Promise<void> => {
    if (!batchId.value) return
    try {
      progress.value = await O.getBatchProgress(batchId.value)
      const settling = progress.value.status === 'PROCESSING'
        || progress.value.status === 'UPLOADING'
      // ★ pending 归零时批次可能还在写 Response / 标记 SCANNED /
      //   自动批改；必须等状态离开 PROCESSING/UPLOADING 再停表。
      if (progress.value.pending === 0 && !settling && poll) {
        clearInterval(poll)
        poll = null
        stalled.value = false
        await loadCards()
        return
      }
      const processing = progress.value.status === 'PROCESSING'
      if (progress.value.status === 'FAILED') {
        stalled.value = true
        if (poll) {
          clearInterval(poll)
          poll = null
        }
        return
      }
      if (!processing && Date.now() - pollStartedAt >= 30000) {
        stalled.value = true
      }
    } catch { /* 轮询失败下次再试 */ }
  }
  // 先挂 interval 再 tick：首次 pending 已为 0 时也能正确停表。
  poll = setInterval(() => { void tick() }, 1500)
  await tick()
}

async function retryRecognize(): Promise<void> {
  if (!batchId.value || recognizing.value) return
  recognizing.value = true
  stalled.value = false
  try {
    const r = await O.recognizeBatch(batchId.value)
    recognition.value = r.recognition
    const tip = recognitionText(r.recognition)
    ElMessage.success(tip || '已重新提交识别')
    await pollProgress()
    await loadCards()
  } catch (e) {
    const msg = e instanceof Error ? e.message : '重新识别失败'
    error.value = msg
    ElMessage.error(msg)
  } finally {
    recognizing.value = false
  }
}

async function assign(card: O.ScanCard): Promise<void> {
  if (!students.value.length) {
    ElMessage.warning('该作业还没有学生名单')
    return
  }
  try {
    const { value } = await ElMessageBox.prompt(
      '输入学生学号（或从列表里复制）', '指派学生',
      { confirmButtonText: '指派', cancelButtonText: '取消' },
    )
    const row = students.value.find((s) => s.student_no === value.trim())
    if (!row) {
      ElMessage.warning('名单里没有这个学号')
      return
    }
    busy.value = true
    await O.resolveStudent(card.id, row.student_id)
    if (hasWritableAnswers(card)) {
      await O.reviewCard(card.id, {
        status: 'REVIEWED', write_responses: true,
      })
      ElMessage.success('已指派并写回作答')
    } else {
      ElMessage.warning('已指派学生；该卡没有确定涂点，请重扫后复核')
    }
    await loadCards()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(e instanceof Error ? e.message : '指派失败')
    }
  } finally {
    busy.value = false
  }
}

function hasWritableAnswers(card: O.ScanCard): boolean {
  return (card.recognized_answers ?? []).some(
    (a) => (a.labels ?? []).length > 0,
  )
}

async function reviewOnly(card: O.ScanCard): Promise<void> {
  if (!hasWritableAnswers(card)) {
    ElMessage.warning('这张卡没有确定涂点，不能复核写回；请先重扫')
    return
  }
  busy.value = true
  try {
    await O.reviewCard(card.id, {
      status: 'REVIEWED', write_responses: true,
    })
    ElMessage.success('已复核并写回作答')
    await loadCards()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '复核失败')
  } finally {
    busy.value = false
  }
}

async function voidOne(card: O.ScanCard): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      '作废原因（必填）', '作废扫描卡',
      { confirmButtonText: '作废', cancelButtonText: '取消' },
    )
    if (!value.trim()) {
      ElMessage.warning('必须填写作废原因')
      return
    }
    busy.value = true
    await O.voidCard(card.id, value.trim())
    ElMessage.success('已作废')
    await loadCards()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(e instanceof Error ? e.message : '作废失败')
    }
  } finally {
    busy.value = false
  }
}

async function rescan(card: O.ScanCard): Promise<void> {
  busy.value = true
  try {
    await O.rescanCard(card.id)
    ElMessage.success('已重新提交识别')
    await pollProgress()
    await loadCards()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '重扫失败')
  } finally {
    busy.value = false
  }
}

/** 定位失败：四角定位块没找全。 */
function isLocateFailure(card: O.ScanCard): boolean {
  return (card.fail_reason || '').startsWith('定位失败')
}

/** 打印裁切/拉伸：定位成功但比例不对。 */
function isPrintDistorted(card: O.ScanCard): boolean {
  const reason = card.fail_reason || ''
  return reason.includes('裁切或拉伸') || reason.includes('打印比例异常')
}

/**
 * 待处理原因。
 *
 * ★ 定位失败时只显示定位原因，绝不显示「未识别到学号」——
 *   那张卡根本没校正成功，学号区都没读到。
 * ★ 只有定位成功、学号区确实有问题时，才出现学号提示。
 */
function reasonText(card: O.ScanCard): string {
  const reason = O.localizeReason(card.fail_reason)
  if (reason) return reason
  const flags = card.student_no_flags ?? []
  if (!flags.length) return '—'
  if (flags.some((f) => f.startsWith('NO_MARK') || f === 'STUDENT_NO_EMPTY')) {
    return '未识别到学号'
  }
  if (flags.some(
    (f) => f.startsWith('UNCERTAIN') || f.startsWith('MULTI_MARK'),
  )) {
    return '学号不完整'
  }
  return flags.map((f) => O.flagLabel(f)).join('、')
}

const LOCATE_HINTS = [
  '整张答题卡入镜，四角不要出画',
  '避免手指、阴影遮挡四角定位块',
  '图片长边不低于 1000px（建议 300dpi 扫描或提高拍照像素）',
]
</script>

<template>
  <div class="zf-stack">
    <div class="head">
      <div>
        <h1 class="page-title">扫描识别</h1>
        <p class="zf-muted page-sub">
          选作业 → 上传 → 逐页识别 → 待处理队列
        </p>
      </div>
      <el-button @click="goBack">
        返回
      </el-button>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon
              :title="error" />

    <div v-if="stalled && (progress?.pending ?? 0) > 0" class="zf-row">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="识别尚未开始或未完成：可能没有后台 worker。可点「重新识别」再提交一次。"
        style="flex: 1"
      />
      <el-button type="warning" :loading="recognizing" @click="retryRecognize">
        重新识别
      </el-button>
    </div>

    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">1 · 选择作业</h2>
      </div>
      <div class="zf-card__body">
        <el-select
          v-model="assignmentId"
          filterable
          placeholder="选择要扫描的作业"
          style="width: 100%; max-width: 520px"
        >
          <el-option
            v-for="a in assignments"
            :key="a.id"
            :label="`${a.title}（${a.classroom_name}）`"
            :value="a.id"
          />
        </el-select>
        <p v-if="current" class="zf-dim" style="margin-top: 8px">
          答题卡：{{ current.sheet_template_id ? '已绑定' : '未绑定（不能扫描）' }}
        </p>
      </div>
    </section>

    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">2 · 上传扫描件</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          单次最多 200 页；PDF 自动按页拆分
        </span>
      </div>
      <div class="zf-card__body">
        <el-upload
          drag
          multiple
          :auto-upload="false"
          accept=".pdf,.jpg,.jpeg,.png"
          :on-change="onFileChange"
          :on-remove="onFileRemove"
        >
          <div class="upload-hint">
            把 PDF / JPG / PNG 拖到这里，或点击选择
          </div>
        </el-upload>
        <div class="zf-row" style="margin-top: 12px">
          <el-button
            type="primary"
            :loading="uploading"
            :disabled="!assignmentId || !files.length"
            @click="startUpload"
          >
            开始识别
          </el-button>
          <span class="zf-dim">{{ files.length }} 个文件待上传</span>
        </div>
      </div>
    </section>

    <section v-if="progress" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">识别进度</h2>
        <span class="zf-dim">{{ O.batchStatusLabel(progress.status) }}</span>
      </div>
      <div class="zf-card__body">
        <el-progress
          :percentage="Math.round((progress.progress || 0) * 100)"
          :stroke-width="10"
        />
        <div class="statgrid">
          <div><div class="n">{{ progress.total }}</div><div class="l">总页数</div></div>
          <div><div class="n ok">{{ progress.auto_ok }}</div><div class="l">成功</div></div>
          <div><div class="n warn">{{ progress.needs_review }}</div><div class="l">待处理</div></div>
          <div><div class="n bad">{{ progress.failed }}</div><div class="l">失败</div></div>
        </div>
      </div>
    </section>

    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">待处理队列</h2>
        <div class="zf-row">
          <el-button
            v-if="(progress?.pending ?? 0) > 0"
            size="small"
            type="primary"
            :loading="recognizing"
            @click="retryRecognize"
          >
            识别待处理页
          </el-button>
          <el-button size="small" @click="loadCards">刷新</el-button>
        </div>
      </div>
      <div class="zf-card__body">
        <el-empty
          v-if="!cards.length"
          description="没有待处理的扫描页"
          :image-size="60"
        />
        <div v-for="c in cards" :key="c.id" class="card-row">
          <div class="thumb" @click="openCard(c)">
            <img
              v-if="thumbs[c.display_asset_id ?? c.raw_asset_id]"
              :src="thumbs[c.display_asset_id ?? c.raw_asset_id]"
              alt="扫描页"
            />
            <span v-else class="zf-dim">无图</span>
          </div>
          <div class="meta">
            <div class="line">
              <b>卡 #{{ c.id }}</b>
              <el-tag size="small" :type="O.CARD_STATUS_TONE[c.status]">
                {{ c.status_label || O.cardStatusLabel(c.status) }}
              </el-tag>
              <span v-if="c.page_no" class="zf-dim">
                第 {{ c.page_no }} 页
              </span>
              <!-- ★ 只有第 1 页且定位成功（page_no 已识别）才显示学号，
                   避免定位失败/后页被误读成「未识别到学号」 -->
              <span v-if="c.page_no === 1" class="zf-dim">
                学号：{{ c.recognized_student_no || '未识别到学号' }}
              </span>
              <span v-else-if="c.page_no" class="zf-dim">学号：本页无</span>
            </div>
            <div class="reason">{{ reasonText(c) }}</div>
            <ul v-if="isLocateFailure(c)" class="locate-hints">
              <li v-for="h in LOCATE_HINTS" :key="h">{{ h }}</li>
            </ul>
            <div v-else-if="isPrintDistorted(c)" class="reason-sub">
              请检查打印设置：必须整页等比缩放，不能裁切或非等比拉伸。
            </div>
          </div>
          <div class="actions">
            <el-button
              v-if="!c.student_id"
              size="small"
              :loading="busy"
              @click="assign(c)"
            >
              指派学生
            </el-button>
            <el-button
              v-else
              size="small"
              type="primary"
              :loading="busy"
              @click="reviewOnly(c)"
            >
              复核写回
            </el-button>
            <el-button size="small" :loading="busy" @click="rescan(c)">
              重扫
            </el-button>
            <el-button size="small" type="danger" plain @click="voidOne(c)">
              作废
            </el-button>
          </div>
        </div>
      </div>
    </section>

    <!-- 扫描图对照：校正整页 / 原图 -->
    <el-dialog
      v-model="previewVisible"
      title="扫描图对照"
      width="900px"
      top="5vh"
    >
      <div v-loading="previewLoading" class="preview-grid">
        <div>
          <p class="zf-dim" style="margin: 0 0 6px">校正整页（四角完整）</p>
          <el-image
            v-if="previewDisplay"
            :src="previewDisplay"
            :preview-src-list="[previewDisplay]"
            fit="contain"
            preview-teleported
            style="width: 100%; max-height: 70vh; background: #fafafa"
          />
          <el-empty v-else description="加载中…" :image-size="40" />
        </div>
        <div>
          <p class="zf-dim" style="margin: 0 0 6px">原始照片</p>
          <el-image
            v-if="previewRaw"
            :src="previewRaw"
            :preview-src-list="[previewRaw]"
            fit="contain"
            preview-teleported
            style="width: 100%; max-height: 70vh; background: #fafafa"
          />
          <el-empty v-else description="加载中…" :image-size="40" />
        </div>
      </div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--zf-space-4); flex-wrap: wrap;
}
.page-title { margin: 0; font-size: var(--zf-text-2xl); font-weight: 600; }
.page-sub { margin: var(--zf-space-1) 0 0; font-size: var(--zf-text-sm); }
.upload-hint { padding: 22px; color: var(--zf-text-secondary); }
.statgrid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
  gap: 12px; margin-top: 14px;
}
.statgrid .n { font-size: var(--zf-text-xl); font-weight: 600; }
.statgrid .n.ok { color: var(--zf-success); }
.statgrid .n.warn { color: var(--zf-warning); }
.statgrid .n.bad { color: var(--zf-danger); }
.statgrid .l { font-size: var(--zf-text-xs); color: var(--zf-text-secondary); }
.card-row {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 10px 0; border-bottom: 1px dashed var(--zf-border-subtle);
}
.card-row:last-child { border-bottom: none; }
.thumb {
  width: 92px; height: 120px; flex: none;
  border: 1px solid var(--zf-border-subtle);
  display: flex; align-items: center; justify-content: center;
  background: #fafafa; overflow: hidden; cursor: zoom-in;
}
.preview-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--zf-space-4);
}
@media (max-width: 720px) {
  .preview-grid { grid-template-columns: 1fr; }
}
.thumb img { width: 100%; height: 100%; object-fit: contain; }
.meta { flex: 1; min-width: 200px; }
.line { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.reason {
  margin-top: 6px; font-size: var(--zf-text-xs);
  color: var(--zf-danger); word-break: break-all;
}
.reason-sub {
  margin-top: 4px; font-size: var(--zf-text-xs);
  color: var(--zf-warning);
}
.locate-hints {
  margin: 6px 0 0; padding-left: 18px;
  font-size: var(--zf-text-xs); color: var(--zf-text-secondary);
}
.locate-hints li { line-height: 1.6; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
</style>
