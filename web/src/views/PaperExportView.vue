<script setup lang="ts">
/**
 * 试卷导出页（`P1-62b` / 契约 v2）。
 *
 * ★★ 本页是**唯一**的导出界面（抽屉已删除，不允许两套并存）：
 *
 *     左：版式参数（`PaperLayoutForm`）      右：预览（`PaperPreviewCanvas`）
 *
 * ★★ 预览与 PDF / Word 用**同一份排版 JSON**：
 *   参数一改 → 防抖后 POST `/export/ {format:"layout"}` → 照毫米坐标画。
 *   前端**不估算行数、不分页、不算选项位置**。
 *
 * ★ 竞态：每次重排带自增序号，只采纳最后一次响应。
 * ★ 输入框防抖 400ms、滑块 200ms（由表单的 `interact` 事件区分）。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'

import * as P from '@/api/paper'
import PaperLayoutForm from '@/components/paper/PaperLayoutForm.vue'
import PaperPreviewCanvas from '@/components/paper/PaperPreviewCanvas.vue'

const route = useRoute()
const router = useRouter()
const paperId = computed(() => Number(route.params.id))

/** 与后端 `DEFAULT_LAYOUT` 对齐的客户端初值（首屏不至于全是 undefined）。 */
const DEFAULT_CONFIG: P.PaperLayoutConfig = {
  page_size: 'A4',
  orientation: 'PORTRAIT',
  margin_mm: 20,
  line_spacing: 1.5,
  base_font_pt: 10.5,
  title_font_pt: 16,
  title_text: '',
  show_title: true,
  student_fields: ['学号', '班级', '姓名'],
  student_line_extra: '',
  notes_text: '',
  show_notes: false,
  show_page_number: true,
  page_number_align: 'center',
  answer_bracket: true,
  show_score: true,
  option_font_pt: null,
  option_cols_by_seq: {},
  blank_blocks: [],
}

const paper = ref<P.PaperDetail | null>(null)
const cfg = ref<P.PaperLayoutConfig>({ ...DEFAULT_CONFIG })
const layout = ref<P.PaperLayoutResult | null>(null)
const loading = ref(true)
const previewing = ref(false)
const exporting = ref<'pdf' | 'docx' | 'answer' | ''>('')
const saving = ref(false)
const error = ref('')
const dirty = ref(false)
const zoomPercent = ref<number | null>(null)

/** 首次回填期间不把赋值当成「用户改动」 */
let syncing = true
/** 竞态：只采纳最后一次重排 */
let reqSeq = 0
let timer: ReturnType<typeof setTimeout> | null = null
/** 下一次防抖间隔：输入 400ms / 滑块 200ms */
let nextDelay = 400

const items = computed(() => paper.value?.items ?? [])
const questionOptions = computed(() =>
  items.value.map((it) => ({
    seq: it.seq,
    label: it.stem_preview || '',
    type: it.question_type,
  })),
)
// ★ 逐题选项覆盖只列**有选项的题**（单选/多选）；判断题没有选项
const CHOICE_TYPES = new Set([
  'SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'MULTI_CHOICE',
])
const choiceQuestions = computed(() =>
  items.value
    .filter((it) => CHOICE_TYPES.has(it.question_type))
    .map((it) => ({
      seq: it.seq,
      preview: (it.stem_preview || '').slice(0, 20),
      type: it.question_type,
    })),
)

const issues = computed(() => layout.value?.issues ?? [])
const blockingLocal = computed<P.LayoutIssue[]>(() =>
  items.value.length
    ? []
    : [{
        code: 'PAPER.NO_QUESTIONS',
        message: '这份试卷还没有题，无法导出。请先回组卷页加题。',
        blocking: true,
      }],
)
const blocking = computed(() => [
  ...blockingLocal.value,
  ...issues.value.filter((i) => i.blocking),
])
const warnings = computed(() => issues.value.filter((i) => !i.blocking))
const pageCount = computed(() => layout.value?.page_count ?? 0)

function mergeSaved(raw: Record<string, unknown> | undefined):
P.PaperLayoutConfig {
  return { ...DEFAULT_CONFIG, ...(raw as P.PaperLayoutConfig ?? {}) }
}

watch(
  cfg,
  () => {
    if (syncing) return
    dirty.value = true
    if (timer) clearTimeout(timer)
    const delay = nextDelay
    nextDelay = 400
    timer = setTimeout(() => {
      timer = null
      void refreshPreview()
    }, delay)
  },
  { deep: true },
)

function onInteract(kind: 'input' | 'slider'): void {
  nextDelay = kind === 'slider' ? 200 : 400
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    paper.value = await P.getPaper(paperId.value)
    syncing = true
    cfg.value = mergeSaved(paper.value.layout_config)
    await refreshPreview({ initial: true })
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function refreshPreview(
  opts: { initial?: boolean } = {},
): Promise<void> {
  const seq = ++reqSeq
  previewing.value = true
  error.value = ''
  try {
    const res = await P.previewPaperLayout(
      paperId.value, cfg.value,
    )
    if (seq !== reqSeq) return          // ★ 只采纳最后一次响应
    layout.value = res
    if (opts.initial) {
      syncing = true
      cfg.value = mergeSaved({ ...res.config })
      await new Promise((r) => setTimeout(r, 0))
      syncing = false
      dirty.value = false
    }
    await loadImages(res)
  } catch (e) {
    if (seq !== reqSeq) return
    error.value = e instanceof Error ? e.message : '排版计算失败'
  } finally {
    if (seq === reqSeq) previewing.value = false
  }
}

// ══════════════════════════════════════════════════
//  预览图片：fetchBlob + createObjectURL，按 asset_id 缓存
// ══════════════════════════════════════════════════

const imageUrls = ref<Record<number, string>>({})
const objectUrls: string[] = []
const pendingImages = new Set<number>()
let alive = true

async function loadImages(lay: P.PaperLayoutResult): Promise<void> {
  const ids = new Set<number>()
  for (const p of lay.pages) {
    for (const b of p.blocks) {
      if (b.asset_id) ids.add(b.asset_id)
    }
  }
  for (const id of ids) {
    if (imageUrls.value[id] || pendingImages.has(id)) continue
    pendingImages.add(id)
    try {
      const blob = await P.getAssetBlob(id)
      if (!alive) return
      const url = URL.createObjectURL(blob)
      objectUrls.push(url)
      imageUrls.value = { ...imageUrls.value, [id]: url }
    } catch {
      // ★ 缺图不阻断：预览里印「［配图缺失］」占位
    } finally {
      pendingImages.delete(id)
    }
  }
}

onBeforeUnmount(() => {
  alive = false
  if (timer) clearTimeout(timer)
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.length = 0
  imageUrls.value = {}
})

// ══════════════════════════════════════════════════
//  保存 / 导出
// ══════════════════════════════════════════════════

async function save(): Promise<boolean> {
  saving.value = true
  try {
    const updated = await P.saveLayoutConfig(paperId.value, cfg.value)
    paper.value = updated
    syncing = true
    cfg.value = mergeSaved(updated.layout_config)
    await new Promise((r) => setTimeout(r, 0))
    syncing = false
    dirty.value = false
    ElMessage.success('版式已保存')
    return true
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
    return false
  } finally {
    saving.value = false
  }
}

async function doExport(fmt: 'pdf' | 'docx' | 'answer'): Promise<void> {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(
        '当前面板参数尚未保存。导出会使用**当前参数**；' +
        '可以选择先保存到试卷，或不保存直接导出。',
        '有未保存的版式改动',
        {
          confirmButtonText: '保存并导出',
          cancelButtonText: '不保存，直接导出',
          distinguishCancelAndClose: true,
          type: 'warning',
        },
      )
      const ok = await save()
      if (!ok) return
    } catch (action) {
      // cancel = 直接导出；close = 取消
      if (action !== 'cancel') return
    }
  }
  exporting.value = fmt
  try {
    if (fmt === 'answer') {
      await P.exportAnswerPdf(paperId.value, cfg.value)
      ElMessage.success('已开始下载答案 PDF')
    } else {
      await P.exportPaper(paperId.value, fmt, { layout: cfg.value })
      ElMessage.success(`已开始下载 ${fmt.toUpperCase()}`)
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '导出失败')
  } finally {
    exporting.value = ''
  }
}

async function handleSave(): Promise<void> {
  await save()
}

// ══════════════════════════════════════════════════
//  未保存离开拦截
// ══════════════════════════════════════════════════

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(
      '有未保存的版式改动，离开后会丢失。确定离开？',
      '离开页面',
      { confirmButtonText: '离开', cancelButtonText: '留在本页' },
    )
    return true
  } catch {
    return false
  }
})

function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}

onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
  void load()
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

function backToCompose(): void {
  void router.push({ name: 'PaperCompose', params: { id: paperId.value } })
}
</script>

<template>
  <div v-loading="loading" class="pex">
    <!-- ── 页头 ── -->
    <div class="pex__head">
      <div class="pex__title-wrap">
        <h1 class="page-title">
          {{ paper?.title ?? '试卷导出' }}
          <el-tag
            v-if="paper" size="small"
            :type="P.PAPER_STATUS_TONE[paper.status]"
          >
            {{ P.PAPER_STATUS_LABEL[paper.status] }}
          </el-tag>
          <el-tag v-if="dirty" size="small" type="warning">未保存</el-tag>
        </h1>
        <p class="pex__sub">
          预览与 PDF / Word / 答案 PDF 使用同一份排版参数
          <span class="zf-dim">· {{ pageCount }} 页</span>
        </p>
      </div>
      <div class="pex__actions">
        <el-button @click="backToCompose">返回组卷</el-button>
        <el-button :loading="saving" @click="handleSave">保存版式</el-button>
        <el-button
          type="primary" :loading="exporting === 'pdf'"
          :disabled="!!blocking.length" @click="doExport('pdf')"
        >
          导出 PDF
        </el-button>
        <el-button
          :loading="exporting === 'docx'"
          :disabled="!!blocking.length" @click="doExport('docx')"
        >
          导出 Word
        </el-button>
        <el-button
          :loading="exporting === 'answer'"
          :disabled="!!blocking.length" @click="doExport('answer')"
        >
          导出答案
        </el-button>
      </div>
    </div>

    <!-- ★ 有未保存改动时：导出区必须显式提示（不静默保存/丢弃） -->
    <el-alert
      v-if="dirty" type="warning" :closable="false" show-icon
      title="版式参数有未保存改动 —— 导出会使用当前参数；可点「保存并导出」"
      class="pex__dirty"
    />

    <el-alert
      v-if="error" type="error" :title="error" show-icon
      :closable="false" class="pex__dirty"
    />

    <!-- ── 左参数 / 右预览 ── -->
    <div class="pex__body">
      <aside class="pex__left">
        <PaperLayoutForm
          v-model="cfg" :paper-title="paper?.title ?? ''"
          :question-options="questionOptions"
          :choice-questions="choiceQuestions"
          @interact="onInteract"
        />
      </aside>

      <section class="pex__right">
        <div class="pex__toolbar">
          <span class="zf-dim">
            <template v-if="previewing">排版中…</template>
            <template v-else>
              缩放
              <el-select
                :model-value="zoomPercent === null ? 'fit' : zoomPercent"
                size="small" style="width: 120px; margin-left: 6px"
                @update:model-value="(v: string | number) =>
                  zoomPercent = v === 'fit' ? null : Number(v)"
              >
                <el-option label="适应宽度" value="fit" />
                <el-option label="50%" :value="50" />
                <el-option label="75%" :value="75" />
                <el-option label="100%" :value="100" />
                <el-option label="125%" :value="125" />
                <el-option label="150%" :value="150" />
              </el-select>
            </template>
          </span>
          <span class="zf-dim">
            {{ layout?.page_width_mm ?? '—' }}×{{
              layout?.page_height_mm ?? '—' }}mm
            · 边距 {{ layout?.margin_mm ?? '—' }}mm
          </span>
        </div>
        <div class="pex__canvas">
          <PaperPreviewCanvas
            :layout="layout" :zoom-percent="zoomPercent"
            :image-urls="imageUrls" :loading="previewing"
          />
        </div>
      </section>
    </div>

    <!-- ── 底部问题区：阻断红 / 告警黄，分开列 ── -->
    <div class="pex__issues">
      <div class="pex__issues-col">
        <div class="pex__issues-title pex__issues-title--block">
          阻断项 {{ blocking.length }}
        </div>
        <div
          v-for="i in blocking" :key="`b-${i.code}`"
          class="pex__issue pex__issue--block"
        >
          <b>{{ i.code }}</b> {{ i.message }}
        </div>
        <div v-if="!blocking.length" class="pex__issue pex__issue--ok">
          无阻断项，可以导出
        </div>
      </div>
      <div class="pex__issues-col">
        <div class="pex__issues-title pex__issues-title--warn">
          告警项 {{ warnings.length }}
        </div>
        <div
          v-for="i in warnings" :key="`w-${i.code}`"
          class="pex__issue pex__issue--warn"
        >
          <b>{{ i.code }}</b> {{ i.message }}
        </div>
        <div v-if="!warnings.length" class="pex__issue pex__issue--ok">
          无告警
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pex {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: calc(100vh - 88px);
  min-height: 520px;
}
.pex__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.pex__title-wrap { min-width: 260px; }
.pex__sub { margin: 2px 0 0; font-size: 12px; color: var(--zf-text-secondary); }
.pex__actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pex__dirty { margin: 0; }

.pex__body {
  display: grid;
  grid-template-columns: 370px 1fr;
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.pex__left {
  overflow-y: auto;
  padding-right: 10px;
  border-right: 1px solid var(--zf-border);
  min-height: 0;
}
.pex__right { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.pex__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 6px;
  flex-wrap: wrap;
}
.pex__canvas { flex: 1; min-height: 0; }

.pex__issues {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  max-height: 150px;
  overflow: auto;
  border-top: 1px solid var(--zf-border);
  padding-top: 6px;
}
.pex__issues-col { min-width: 0; }
.pex__issues-title { font-size: 12px; font-weight: 700; margin-bottom: 4px; }
.pex__issues-title--block { color: #b91c1c; }
.pex__issues-title--warn { color: #b45309; }
.pex__issue {
  font-size: 12px;
  line-height: 1.6;
  padding: 3px 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  word-break: break-word;
}
.pex__issue--block { background: #fef2f2; color: #991b1b; }
.pex__issue--warn { background: #fffbeb; color: #92400e; }
.pex__issue--ok { color: var(--zf-text-tertiary); }
</style>
