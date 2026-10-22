<script setup lang="ts">
/**
 * 主观题阅卷（`/assignments/:id/review`，21 文档任务一）。
 *
 * ★★ 为什么要有这一页（而不是把「批改工作台」改一改）：
 *
 *   老师改卷的动线是**按题**的：拿起第 11 题的 45 份，一份份看、
 *   一份份给分，然后换第 12 题。而工作台是**按学生**的：
 *   点开一个人，看他的全部题。
 *
 *   两者不是同一个操作换了个皮肤：
 *     按人批  每换一个人就要重新定位「第 11 题在哪」，切图重看 45 次
 *     按题批  一道题的答题区固定在同一块屏幕位置，眼睛不用重新找
 *
 * ★★ 三个刻意的设计：
 *
 *   1. **保存后自动跳下一位**（后端返回 `next_student`）。
 *      不给「下一位」按钮让人点 —— 45 份就是 45 次多余的点击。
 *   2. **切图优先，整页兜底**。切图缺失时回退整页校正图 + 高亮 `rect`，
 *      而不是显示「无切图」让老师去跑命令行。
 *   3. **快捷键流**：Enter 保存并下一位 / ←→ 换人 / A 看整页 / Esc 退出。
 *      改卷是重复劳动，键盘比鼠标快一个数量级。
 *
 * ★★ 内存必须自己管：切图是 200–400KB/张的 JPEG，
 *   `URL.createObjectURL` 出来的 URL 不会自动释放。
 *   这里维护一个显式缓存（上限 60 条）+ 卸载时全部 `revokeObjectURL`，
 *   否则批一个班能吃掉几百 MB。
 */
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import * as G from '@/api/grading'
import { ApiError } from '@/api/http'
import { getAssetBlob } from '@/api/paper'
import MathText from '@/components/MathText.vue'
import ScanImageViewer from '@/components/ScanImageViewer.vue'
import { useAssignmentStore } from '@/stores/assignment'

const route = useRoute()
const router = useRouter()
const store = useAssignmentStore()
const id = computed(() => Number(route.params.id))

// ══════════════════════════════════════════════════
//  左栏：主观题清单
// ══════════════════════════════════════════════════

const items = ref<G.ReviewItemProgress[]>([])
const overall = ref<{ graded: number; total: number }>({ graded: 0, total: 0 })
const activeItemId = ref<number | null>(null)
const itemsLoading = ref(false)

const activeItem = computed(
  () => items.value.find((i) => i.paper_item_id === activeItemId.value) ?? null,
)

const overallPct = computed(() => {
  const t = overall.value.total
  return t ? Math.round((overall.value.graded / t) * 100) : 0
})

async function loadItems(): Promise<void> {
  itemsLoading.value = true
  try {
    const d = await G.getReviewItems(id.value)
    items.value = d.items
    overall.value = d.overall
    // ★ 默认落在**第一道还没批完**的题上：老师打开就是要接着批，
    //   而不是从头再看一遍已经批完的题。
    if (!items.value.some((i) => i.paper_item_id === activeItemId.value)) {
      const firstPending = items.value.find((i) => i.pending > 0)
      activeItemId.value = firstPending?.paper_item_id
        ?? items.value[0]?.paper_item_id
        ?? null
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载题目清单失败'
  } finally {
    itemsLoading.value = false
  }
}

// ══════════════════════════════════════════════════
//  中栏：同一题的逐份学生
// ══════════════════════════════════════════════════

const rows = ref<G.ReviewStudentRow[]>([])
const rowsLoading = ref(false)
const index = ref(0)
const onlyPending = ref(false)
const maxScore = ref(0)
const stemText = ref('')
//: 参考答案与解析——阅卷时对照用。空串表示题库未录入，不是加载失败
const answerText = ref('')
const explanationText = ref('')
const refOpen = ref(true)
const error = ref('')

const row = computed<G.ReviewStudentRow | null>(
  () => rows.value[index.value] ?? null,
)
const rowCount = computed(() => rows.value.length)

async function loadRows(): Promise<void> {
  const itemId = activeItemId.value
  if (!itemId) {
    rows.value = []
    answerText.value = ''
    explanationText.value = ''
    return
  }
  rowsLoading.value = true
  error.value = ''
  clearCache()
  try {
    const d = await G.getReviewStudents(id.value, itemId, {
      filter: onlyPending.value ? 'pending' : 'all',
      pageSize: 200,
    })
    rows.value = d.items
    maxScore.value = Number(d.max_score || 0)
    stemText.value = d.stem_text || ''
    // ★ 答案/解析随题目一起返回（服务端已按快照优先派生为纯文本），
    //   不要在前端拼填空题的多空答案——口径只允许有一套。
    answerText.value = d.answer_text || ''
    explanationText.value = d.explanation_text || ''
    // ★ 落点是「第一个还没批的」：`all` 模式下从头开始会在已批的人身上打转
    const firstPending = rows.value.findIndex((r) => !r.graded)
    index.value = firstPending >= 0 ? firstPending : 0
    partIndex.value = 1
    await syncInputs()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载学生名单失败'
  } finally {
    rowsLoading.value = false
  }
}

/** 把当前这一份的既有分数 / 评语填进输入框。 */
async function syncInputs(): Promise<void> {
  const r = row.value
  // ★ 用 `number | undefined` 而不是字符串：`el-input-number` 要数字，
  //   传字符串会让「清空」变成 NaN，而 NaN < max 的比较永远是 false，
  //   于是空分数会被当成合法值提交。
  scoreInput.value = r?.score != null ? Number(r.score) : undefined
  commentInput.value = r?.comment ?? ''
  partIndex.value = 1
  pageMode.value = false
  await loadImage()
  void prefetchNext()
}

// ══════════════════════════════════════════════════
//  图片：切图 / 整页兜底
// ══════════════════════════════════════════════════

/** 缓存上限：够覆盖「当前 + 前后各 20 份」，再多就该淘汰了。 */
const CACHE_LIMIT = 60
const cache = new Map<string, string>()
const cacheOrder: string[] = []

const cropUrl = ref('')
const cropLoading = ref(false)
/** 当前这条是回退出来的整页图（不是裁剪图） */
const usingPageFallback = ref(false)

const parts = computed(() => row.value?.crops ?? [])
const partIndex = ref(1)

function remember(key: string, url: string): void {
  cache.set(key, url)
  cacheOrder.push(key)
  while (cacheOrder.length > CACHE_LIMIT) {
    const old = cacheOrder.shift()
    if (!old) break
    const v = cache.get(old)
    if (v) URL.revokeObjectURL(v)
    cache.delete(old)
  }
}

function clearCache(): void {
  for (const url of cache.values()) URL.revokeObjectURL(url)
  cache.clear()
  cacheOrder.length = 0
  cropUrl.value = ''
  pageUrl.value = ''
}

/** 取某张资产图片的 objectURL（带缓存）。 */
async function assetUrl(assetId: number): Promise<string> {
  const key = `a${assetId}`
  const hit = cache.get(key)
  if (hit) return hit
  const blob = await getAssetBlob(assetId)
  const url = URL.createObjectURL(blob)
  remember(key, url)
  return url
}

/** 取当前这份学生的答题图（切图优先，缺切图时后端回退整页）。 */
async function loadImage(): Promise<void> {
  const r = row.value
  if (!r || !activeItemId.value) {
    cropUrl.value = ''
    return
  }
  cropLoading.value = true
  try {
    const part = parts.value.find((p) => p.part_index === partIndex.value)
      ?? parts.value[0]
    if (part) {
      usingPageFallback.value = false
      cropUrl.value = await assetUrl(part.asset_id)
    } else {
      // ★ 没有切图：走阅卷接口的兜底（后端会回退整页校正图 / 原图）
      usingPageFallback.value = true
      const key = `p${r.student_id}:${activeItemId.value}`
      const hit = cache.get(key)
      if (hit) {
        cropUrl.value = hit
      } else {
        const blob = await G.getReviewCrop(
          id.value, activeItemId.value, r.student_id,
        )
        const url = URL.createObjectURL(blob)
        remember(key, url)
        cropUrl.value = url
      }
    }
  } catch (e) {
    cropUrl.value = ''
    error.value = e instanceof Error ? e.message : '切图加载失败'
  } finally {
    cropLoading.value = false
  }
}

/**
 * 预取下一份。
 *
 * ★ 批卷时人手速比网络快：等老师按 Enter 才去取图，
 *   每一次都要白等 200–500ms。提前取好，翻页就变成瞬时的。
 * ★ 失败**静默**：预取只是优化，不该弹错误框。
 */
async function prefetchNext(): Promise<void> {
  const next = rows.value[index.value + 1]
  const itemId = activeItemId.value
  if (!next || !itemId) return
  const part = next.crops?.[0]
  try {
    if (part) await assetUrl(part.asset_id)
    else {
      const key = `p${next.student_id}:${itemId}`
      if (!cache.has(key)) {
        const blob = await G.getReviewCrop(id.value, itemId, next.student_id)
        remember(key, URL.createObjectURL(blob))
      }
    }
  } catch {
    /* 预取失败无所谓 */
  }
}

// ══════════════════════════════════════════════════
//  整页视图（切图不够看时：字迹出了框、要对照题干）
// ══════════════════════════════════════════════════

const pageMode = ref(false)
const pageUrl = ref('')
const pageLoading = ref(false)
const pageMap = ref<Record<number, NonNullable<
  G.GradingRosterRow['scan_pages']>>>({})

/** 当前这份的切图矩形 → 整页高亮（百分比）。 */
const rectStyle = computed(() => {
  const r = row.value?.rect
  if (!r || !r.w_mm) return null
  const pw = row.value?.page_width_mm || 210
  const ph = row.value?.page_height_mm || 297
  return {
    left: `${((r.x_mm ?? 0) / pw) * 100}%`,
    top: `${((r.y_mm ?? 0) / ph) * 100}%`,
    width: `${(r.w_mm / pw) * 100}%`,
    height: `${(r.h_mm ?? 0) / ph * 100}%`,
  }
})

async function togglePage(): Promise<void> {
  pageMode.value = !pageMode.value
  if (!pageMode.value) return
  const r = row.value
  if (!r) return
  pageLoading.value = true
  try {
    if (!pageMap.value[r.student_id]) {
      const roster = await G.getRoster(id.value)
      const hit = roster.items.find((x) => x.student_id === r.student_id)
      if (hit?.scan_pages?.length) {
        pageMap.value[r.student_id] = hit.scan_pages
      }
    }
    const pages = pageMap.value[r.student_id] ?? []
    const want = r.page_no ?? 1
    const page = pages.find((p) => p.page_no === want) ?? pages[0]
    if (!page) {
      ElMessage.warning('这名学生没有可用的整页扫描图')
      pageMode.value = false
      return
    }
    const assetId = page.display_asset_id || page.raw_asset_id
    pageUrl.value = await assetUrl(assetId)
  } catch (e) {
    ElMessage.warning(e instanceof Error ? e.message : '整页图加载失败')
    pageMode.value = false
  } finally {
    pageLoading.value = false
  }
}

// ══════════════════════════════════════════════════
//  打分
// ══════════════════════════════════════════════════

const scoreInput = ref<number | undefined>(undefined)
const commentInput = ref('')
const saving = ref(false)

const finishedAll = computed(
  () => overall.value.total > 0 && overall.value.graded >= overall.value.total,
)

/** 左栏进度就地更新（不整页刷新：刷新会把滚动位置和输入框都冲掉）。 */
function patchProgress(p: { graded: number; total: number }): void {
  const it = activeItem.value
  if (!it) return
  it.graded = p.graded
  it.pending = Math.max(0, p.total - p.graded)
  overall.value = {
    graded: items.value.reduce((s, i) => s + i.graded, 0),
    total: items.value.reduce((s, i) => s + i.total_students, 0),
  }
}

async function save(goNext = true): Promise<void> {
  const r = row.value
  const itemId = activeItemId.value
  if (!r || !itemId || saving.value) return
  const val = Number(scoreInput.value)
  if (scoreInput.value === undefined || !Number.isFinite(val)) {
    ElMessage.warning('请先填分数')
    return
  }
  if (val < 0 || val > maxScore.value) {
    ElMessage.warning(`分数应在 0–${maxScore.value} 之间`)
    return
  }
  saving.value = true
  try {
    const res = await G.reviewScore(id.value, {
      student_id: r.student_id,
      paper_item_id: itemId,
      score: val,
      reason: commentInput.value.trim(),
    })
    r.score = res.score
    r.score_state = res.state
    r.graded = true
    r.comment = commentInput.value.trim()
    patchProgress(res.item_progress)

    if (!goNext) {
      ElMessage.success('已保存')
      return
    }
    const next = res.next_student
    if (next) {
      const at = rows.value.findIndex((x) => x.student_id === next.student_id)
      if (at >= 0) {
        await goTo(at)
      } else {
        await loadRows()
      }
    } else {
      // ★ 这道题批完了：刷新清单，让左栏的「待批 0」立刻可见
      ElMessage.success(`第 ${activeItem.value?.seq ?? ''} 题已全部批完`)
      await loadItems()
      const remain = items.value.find((i) => i.pending > 0)
      if (remain) {
        activeItemId.value = remain.paper_item_id
        await loadRows()
      }
    }
  } catch (e) {
    if (e instanceof ApiError && e.code === 'GRADING.SCORE_EXCEED_MAX') {
      ElMessage.error(`分数超过该题配分（${maxScore.value} 分）`)
    } else {
      ElMessage.error(e instanceof Error ? e.message : '保存失败')
    }
  } finally {
    saving.value = false
  }
}

async function goTo(i: number): Promise<void> {
  if (i < 0 || i >= rows.value.length || i === index.value) return
  index.value = i
  await syncInputs()
}

async function prev(): Promise<void> {
  await goTo(index.value - 1)
}

async function next(): Promise<void> {
  await goTo(index.value + 1)
}

// ══════════════════════════════════════════════════
//  切换题目 / 筛选
// ══════════════════════════════════════════════════

async function pickItem(itemId: number): Promise<void> {
  if (itemId === activeItemId.value) return
  activeItemId.value = itemId
  await loadRows()
}

async function onFilterChange(): Promise<void> {
  await loadRows()
}

// ══════════════════════════════════════════════════
//  快捷键
// ══════════════════════════════════════════════════

function onKeydown(e: KeyboardEvent): void {
  const el = e.target as HTMLElement | null
  const inField = !!el
    && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
        || el.isContentEditable)

  // ★ 输入框里只接管 Ctrl/Cmd+Enter：
  //   评语要多行时 Enter 必须是换行，否则老师打不出第二句。
  if (inField && !(e.key === 'Enter' && (e.ctrlKey || e.metaKey))) return

  switch (e.key) {
    case 'Enter':
      e.preventDefault()
      void save(true)
      break
    case 'ArrowLeft':
      e.preventDefault()
      void prev()
      break
    case 'ArrowRight':
      e.preventDefault()
      void next()
      break
    case 'a':
    case 'A':
      e.preventDefault()
      void togglePage()
      break
    case 's':
    case 'S':
      e.preventDefault()
      void next()
      break
    case 'Escape':
      e.preventDefault()
      if (pageMode.value) void togglePage()
      else router.back()
      break
    default:
      break
  }
}

// ══════════════════════════════════════════════════
//  生命周期
// ══════════════════════════════════════════════════

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  if (!store.detail || store.detail.id !== id.value) {
    void store.load(id.value).catch(() => undefined)
  }
  await loadItems()
  await loadRows()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  clearCache()
})
</script>

<template>
  <div class="review">
    <!-- ═════════ 顶栏 ═════════ -->
    <header class="review__head">
      <div class="review__title">
        <h1 class="page-title">主观题阅卷</h1>
        <p class="zf-muted review__sub">
          {{ store.detail?.title ?? '' }}
          <span v-if="store.detail?.classroom_name">
            · {{ store.detail.classroom_name }}
          </span>
        </p>
      </div>
      <div class="zf-row">
        <span class="review__overall">
          整卷 <b>{{ overall.graded }}</b> / {{ overall.total }}
          （{{ overallPct }}%）
        </span>
        <el-button
          size="small"
          @click="router.push({ name: 'GradingWorkbench', params: { id } })"
        >
          按学生批
        </el-button>
        <el-button
          size="small"
          @click="router.push({ name: 'AssignmentDetail', params: { id } })"
        >
          返回详情
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      class="review__alert"
    />

    <div class="review__body">
      <!-- ═════════ 左栏：题目清单 ═════════ -->
      <aside v-loading="itemsLoading" class="panel panel--left">
        <div class="panel__head">
          <span>主观题（{{ items.length }}）</span>
          <span class="zf-dim">已批 / 应批</span>
        </div>
        <div v-if="!items.length && !itemsLoading" class="zf-empty">
          这份卷没有主观题，全部由机器批改
        </div>
        <ul class="item-list">
          <li
            v-for="it in items"
            :key="it.paper_item_id"
            class="item"
            :class="{
              'item--active': it.paper_item_id === activeItemId,
              'item--done': it.pending === 0,
            }"
            @click="pickItem(it.paper_item_id)"
          >
            <div class="item__top">
              <span class="item__seq">第 {{ it.seq }} 题</span>
              <el-tag
                size="small"
                :type="it.pending === 0 ? 'success' : 'warning'"
              >
                {{ it.graded }}/{{ it.total_students }}
              </el-tag>
            </div>
            <div class="item__meta">
              {{ it.type_label }} · {{ it.max_score }} 分
              <span v-if="it.section_title"> · {{ it.section_title }}</span>
            </div>
            <!-- ★ 阅卷页也要渲染公式：老师判断「学生这一步对不对」
                 靠的就是题面里的公式能不能看清 -->
            <MathText
              tag="div"
              class="item__stem"
              :source="it.stem_text || '（无题干快照）'"
            />
            <div class="item__bar">
              <span
                class="item__bar-fill"
                :style="{
                  width: it.total_students
                    ? (it.graded / it.total_students) * 100 + '%'
                    : '0%',
                }"
              />
            </div>
          </li>
        </ul>
      </aside>

      <!-- ═════════ 中栏：答题图 ═════════ -->
      <main v-loading="rowsLoading || cropLoading" class="panel panel--mid">
        <div class="panel__head">
          <span>
            <template v-if="row">
              {{ row.student_no }} {{ row.student_name }}
              <span class="zf-dim">
                · 第 {{ activeItem?.seq }} 题 · 第
                {{ index + 1 }}/{{ rowCount }} 份
              </span>
            </template>
            <template v-else>答题图</template>
          </span>
          <div class="zf-row">
            <!-- ★ 默认显示全部：老师要能看到「已经给过几分」来保持一致
                 （同一道题前后给分尺度不能飘）。批完一遍后可切「只看未批」
                 做二次核对。 -->
            <el-checkbox
              v-model="onlyPending"
              size="small"
              @change="onFilterChange"
            >
              只看未批
            </el-checkbox>
            <el-button
              size="small"
              :disabled="index <= 0"
              @click="prev"
            >
              上一份
            </el-button>
            <el-button
              size="small"
              :disabled="index >= rowCount - 1"
              @click="next"
            >
              下一份
            </el-button>
            <el-button size="small" @click="togglePage">
              {{ pageMode ? '看切图' : '看整页' }}
            </el-button>
          </div>
        </div>

        <div class="viewer">
          <!-- 多段（跨页作文）：按段切换，不把 4 张图竖着挤在一屏 -->
          <div v-if="parts.length > 1 && !pageMode" class="parts">
            <el-radio-group
              v-model="partIndex"
              size="small"
              @change="loadImage"
            >
              <el-radio-button
                v-for="p in parts"
                :key="p.part_index"
                :value="p.part_index"
              >
                第 {{ p.part_index }} 段
                <span v-if="p.page_no">（P{{ p.page_no }}）</span>
              </el-radio-button>
            </el-radio-group>
          </div>

          <div
            v-if="pageMode"
            v-loading="pageLoading"
            class="page-view"
          >
            <div class="page-view__inner">
              <img v-if="pageUrl" :src="pageUrl" alt="整页扫描图">
              <span
                v-if="rectStyle"
                class="page-view__rect"
                :style="rectStyle"
              />
            </div>
            <p class="zf-dim page-view__hint">
              高亮框 = 本题答题区（按答题卡几何换算）。
              整页看不清时按 A 切回切图。
            </p>
          </div>

          <ScanImageViewer
            v-else-if="cropUrl"
            :src="cropUrl"
            alt="答题切图"
          />
          <div v-else class="zf-empty">
            <template v-if="usingPageFallback">
              这名学生暂时没有可用的扫描图（未上传或识别失败）
            </template>
            <template v-else>正在取图…</template>
          </div>
        </div>
      </main>

      <!-- ═════════ 右栏：打分 ═════════ -->
      <aside class="panel panel--right">
        <div class="panel__head">
          <span>评分</span>
          <span v-if="row?.graded" class="zf-dim">
            {{ G.scoreStateLabel(row.score_state) }}
          </span>
        </div>

        <div class="score">
          <div class="score__row">
            <label class="score__label">得分</label>
            <el-input-number
              v-model="scoreInput"
              :min="0"
              :max="maxScore"
              :step="0.5"
              :precision="1"
              controls-position="right"
              style="width: 150px"
            />
            <span class="zf-muted">/ {{ maxScore }} 分</span>
          </div>

          <div class="score__quick">
            <el-button
              v-for="v in [0, maxScore * 0.25, maxScore * 0.5,
                          maxScore * 0.75, maxScore]"
              :key="v"
              size="small"
              text
              @click="scoreInput = Math.round(v * 10) / 10"
            >
              {{ Math.round(v * 10) / 10 }}
            </el-button>
          </div>

          <div class="score__row score__row--col">
            <label class="score__label">评语 / 扣分原因</label>
            <el-input
              v-model="commentInput"
              type="textarea"
              :rows="4"
              maxlength="500"
              show-word-limit
              placeholder="写清楚扣在哪一步。会随成绩一起出现在学情与错题本里"
            />
          </div>

          <div class="score__actions">
            <el-button
              type="primary"
              :loading="saving"
              @click="save(true)"
            >
              保存并下一位
              <span class="kbd-inline">Enter</span>
            </el-button>
            <el-button :disabled="saving" @click="save(false)">
              仅保存
            </el-button>
          </div>

          <div v-if="stemText" class="score__stem">
            <div class="score__stem-title">题干预览</div>
            <div class="score__stem-body">{{ stemText }}</div>
          </div>

          <!-- ═════════ 答案与解析（阅卷对照用）═════════
               ★ 放在题干预览正下方：老师看完题干紧接着看答案，
                 不用在「题干」和「答案」之间来回滚动。
               ★ 只读展示，不提供编辑入口——题库是答案的唯一来源，
                 在这里改会让「快照/题库」两处口径不一致。 -->
          <div
            v-if="stemText || answerText || explanationText"
            class="refbox"
          >
            <button
              type="button"
              class="refbox__head"
              :aria-expanded="refOpen"
              @click="refOpen = !refOpen"
            >
              <span class="refbox__title">答案与解析</span>
              <span class="refbox__toggle">{{ refOpen ? '收起' : '展开' }}</span>
            </button>

            <div v-show="refOpen" class="refbox__body">
              <div class="refbox__sec refbox__sec--answer">
                <div class="refbox__label">参考答案</div>
                <MathText
                  v-if="answerText"
                  tag="div"
                  class="refbox__text refbox__text--answer"
                  :source="answerText"
                />
                <div v-else class="refbox__empty refbox__empty--warn">
                  题库未录入参考答案，请按评分标准把握
                </div>
              </div>

              <div class="refbox__sec">
                <div class="refbox__label">解析</div>
                <MathText
                  v-if="explanationText"
                  tag="div"
                  class="refbox__text"
                  :source="explanationText"
                />
                <div v-else class="refbox__empty">暂无解析</div>
              </div>
            </div>
          </div>

          <div class="score__keys">
            <div><kbd>Enter</kbd> 保存并下一位</div>
            <div><kbd>←</kbd> <kbd>→</kbd> 上一份 / 下一份</div>
            <div><kbd>S</kbd> 跳过（不给分）</div>
            <div><kbd>A</kbd> 看整页（高亮答题区）</div>
            <div><kbd>Esc</kbd> 退出整页 / 返回</div>
            <div class="zf-dim">在评语框里用 <kbd>Ctrl</kbd>+<kbd>Enter</kbd> 保存</div>
          </div>
        </div>
      </aside>
    </div>

    <!-- ═════════ 全部批完 ═════════ -->
    <el-dialog
      :model-value="finishedAll && !rowsLoading"
      title="这道卷的主观题已全部批完"
      width="420"
      @close="loadItems"
    >
      <p class="zf-muted">
        整卷 {{ overall.graded }} / {{ overall.total }} 份已完成。
        接下来可以去看学情与题目分析。
      </p>
      <template #footer>
        <el-button @click="router.push({ name: 'GradingWorkbench', params: { id } })">
          去核对成绩
        </el-button>
        <el-button
          type="primary"
          @click="router.push({ name: 'AssignmentAnalytics', params: { id } })"
        >
          看学情
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ★ 三栏用**固定左右 + 自适应中**：中栏是图，宽度必须能撑开；
   左右两栏是文字，宽度超出只是浪费（还会挤压答题图）。 */
.review {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
  height: calc(100vh - 96px);
  min-height: 520px;
}
.review__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.review__title { min-width: 0; }
.review__sub { margin: 2px 0 0; font-size: var(--zf-text-sm); }
.review__overall {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  font-variant-numeric: tabular-nums;
}
.review__alert { margin: 0; }
.review__body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 340px;
  gap: var(--zf-space-3);
}
.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--zf-bg-surface);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-lg);
  box-shadow: var(--zf-shadow-sm);
  overflow: hidden;
}
.panel__head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
  padding: var(--zf-space-3) var(--zf-space-4);
  border-bottom: 1px solid var(--zf-border-subtle);
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-medium);
}

/* ── 左栏 ── */
.item-list {
  list-style: none;
  margin: 0;
  padding: var(--zf-space-2);
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.item {
  padding: var(--zf-space-2) var(--zf-space-3);
  border-radius: var(--zf-radius-md);
  cursor: pointer;
  border: 1px solid transparent;
  transition: background var(--zf-duration-fast) var(--zf-ease-base);
}
.item:hover { background: var(--zf-bg-hover); }
.item--active {
  background: var(--zf-brand-soft, var(--zf-bg-hover));
  border-color: var(--zf-brand);
}
.item--done .item__seq { color: var(--zf-text-tertiary); }
.item__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
}
.item__seq { font-weight: var(--zf-weight-medium); }
.item__meta {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  margin-top: 2px;
}
.item__stem {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
  margin-top: 4px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.item__bar {
  margin-top: 6px;
  height: 4px;
  background: var(--zf-bg-hover);
  border-radius: var(--zf-radius-pill);
  overflow: hidden;
}
.item__bar-fill {
  display: block;
  height: 100%;
  background: var(--zf-brand);
}

/* ── 中栏 ── */
.viewer {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: var(--zf-space-2) var(--zf-space-3) var(--zf-space-3);
}
.parts { flex: none; padding: 0 0 var(--zf-space-2); }
.page-view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  overflow: auto;
}
.page-view__inner {
  position: relative;
  display: inline-block;
  max-width: 100%;
}
.page-view__inner img {
  display: block;
  max-width: 100%;
  height: auto;
  user-select: none;
}
/* ★ 高亮框用百分比定位：整页图会随容器缩放，用 px 会错位。 */
.page-view__rect {
  position: absolute;
  border: 2px solid var(--zf-danger, #d93025);
  background: rgba(217, 48, 37, 0.12);
  border-radius: 2px;
  pointer-events: none;
  box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.06);
}
.page-view__hint {
  font-size: var(--zf-text-xs);
  margin: var(--zf-space-2) 0 0;
  text-align: center;
}

/* ── 右栏 ── */
.score {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--zf-space-4);
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
}
.score__row {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
}
.score__row--col {
  flex-direction: column;
  align-items: stretch;
  gap: var(--zf-space-1);
}
.score__label {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  min-width: 44px;
}
.score__quick {
  display: flex;
  gap: var(--zf-space-1);
  flex-wrap: wrap;
}
.score__actions {
  display: flex;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.kbd-inline {
  margin-left: 6px;
  font-size: var(--zf-text-xs);
  opacity: 0.75;
}
.score__stem {
  border: 1px dashed var(--zf-border);
  border-radius: var(--zf-radius-md);
  padding: var(--zf-space-2) var(--zf-space-3);
  background: var(--zf-bg-subtle);
}
.score__stem-title {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  margin-bottom: 2px;
}
.score__stem-body {
  font-size: var(--zf-text-xs);
  white-space: pre-wrap;
  max-height: 160px;
  overflow-y: auto;
  color: var(--zf-text-primary);
}

/* ── 答案与解析 ──
   ★ 与题干预览刻意用不同视觉：题干是「虚线」框（辅助信息），
     答案是「左侧色条 + 实线」的对照区（判分依据），
     老师扫一眼就能分清哪个是题目、哪个是标准。 */
.refbox {
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
  overflow: hidden;
}
.refbox__head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--zf-space-2);
  padding: var(--zf-space-2) var(--zf-space-3);
  background: var(--zf-bg-subtle);
  border: 0;
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.refbox__head:hover { background: var(--zf-bg-hover); }
.refbox__title {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.refbox__toggle {
  font-size: var(--zf-text-xs);
  color: var(--zf-brand);
}
.refbox__body {
  padding: var(--zf-space-2) var(--zf-space-3) var(--zf-space-3);
  display: grid;
  gap: var(--zf-space-3);
}
.refbox__sec {
  border-left: 2px solid var(--zf-border-strong);
  padding-left: var(--zf-space-2);
}
.refbox__sec--answer { border-left-color: var(--zf-brand); }
.refbox__label {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
  margin-bottom: 2px;
}
.refbox__text {
  font-size: var(--zf-text-xs);
  line-height: 1.7;
  white-space: pre-wrap;
  max-height: 180px;
  overflow-y: auto;
  color: var(--zf-text-primary);
}
.refbox__text--answer {
  font-size: var(--zf-text-sm);
  font-weight: 500;
  max-height: 140px;
}
.refbox__empty {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}
/* ★ 缺答案是「需要老师自己拿标准」的警示，不能和「暂无解析」同色 */
.refbox__empty--warn { color: var(--zf-warning); }
.score__keys {
  margin-top: auto;
  padding-top: var(--zf-space-3);
  border-top: 1px dashed var(--zf-border-subtle);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  display: grid;
  gap: 4px;
}
kbd {
  font-family: var(--zf-font-mono);
  border: 1px solid var(--zf-border);
  border-bottom-width: 2px;
  border-radius: 4px;
  padding: 0 4px;
  background: var(--zf-bg-subtle);
  font-size: 11px;
}

/* 窄屏：三栏塌成上下 */
@media (max-width: 1200px) {
  .review { height: auto; }
  .review__body { grid-template-columns: 1fr; }
  .panel--left .item-list { max-height: 240px; }
  .panel--mid { min-height: 420px; }
}
</style>
