<script setup lang="ts">
/**
 * 难度快速打标页（`P1-100b`）。
 *
 * ★★ 这一页的设计目标只有一个：**标 1900 道题要能标完**。
 *
 *   所以它不是「一个表单」，而是「**键盘流**」：
 *     按 `1`–`5` → 选难度 + 提交 + 跳下一题，**一次按键**。
 *
 *   ★ 为什么键盘流是**决定因素**而不是锦上添花：
 *     每题 2 次鼠标点击 ≈ 6–8 小时；每题 1 次按键 ≈ 2–3 小时。
 *     差一倍的时间决定了「老师愿不愿意标完」。
 *
 * ★ 三个刻意的取舍：
 *   ① **不嵌在题库列表里** —— 一屏一道题，没有表格/分页/筛选干扰
 *   ② **进页面即聚焦** —— 不用先点一下页面才能按数字键
 *      （否则第一次按键会落在 `<body>` 上，白按一次）
 *   ③ **下一题由打标接口带回** —— 键盘流下每题省一次往返
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import * as M from '@/api/questionMark'
import MathText from '@/components/MathText.vue'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const saving = ref(0)
const errorMsg = ref('')
const queue = ref<M.MarkQuestion[]>([])
const idx = ref(0)
const progress = ref<M.MarkProgress>({ total: 0, marked: 0, remaining: 0 })

/** 筛选：默认只看未标注（避免重复标注已有的） */
const onlyUnmarked = ref(true)
const courseId = ref<number | undefined>(
  Number(route.query.courseId) > 0 ? Number(route.query.courseId) : undefined,
)

const current = computed<M.MarkQuestion | null>(
  () => queue.value[idx.value] ?? null,
)
/** 已标 / 总数 —— ★ 分母是**全库**总数，不是当前这一批的长度 */
const progressText = computed(
  () => `${progress.value.marked} / ${progress.value.total}`,
)
const progressPct = computed(() => {
  const t = progress.value.total
  return t ? Math.round((progress.value.marked / t) * 100) : 0
})

/** 键盘提示（页面上要显示，否则老师不知道有快捷键） */
const KEYS: Array<{ key: string; label: string }> = [
  { key: '1–5', label: '选难度并前进' },
  { key: 'S / →', label: '跳过' },
  { key: '←', label: '上一个' },
  { key: 'D', label: '查看详情' },
]

async function load(keepAfterId?: number): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const q: M.MarkQueueQuery = {
      onlyUnmarked: onlyUnmarked.value,
      courseId: courseId.value,
      afterId: keepAfterId,
      limit: 20,
    }
    const d = await M.getMarkQueue(q)
    queue.value = d.items ?? []
    idx.value = 0
    progress.value = d.progress
    await focusPage()
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '加载失败'
    queue.value = []
  } finally {
    loading.value = false
  }
}

/**
 * 把焦点放回容器。
 *
 * ★★ 这个函数是「键盘流真的能用」的关键：
 *   若焦点在某个按钮上，按 `1` 不会触发我们的全局监听之外的
 *   意外行为（比如在 `<input>` 里输入 1）。
 *   每次前进后都重置焦点，保证「按了就生效」。
 */
async function focusPage(): Promise<void> {
  await nextTick()
  const el = document.getElementById('mark-stage')
  el?.focus()
}

/** 打标 + 前进（数字键与按钮共用这一条路径）。 */
async function mark(level: number): Promise<void> {
  const q = current.value
  if (!q || saving.value) return
  saving.value = level
  try {
    const r = await M.markDifficulty(q.id, level, {
      onlyUnmarked: onlyUnmarked.value,
      courseId: courseId.value,
    })
    progress.value = r.progress
    // ★★ 用接口**带回的**下一题，不再单独请求一次
    if (r.next) {
      // 就地替换：把下一题插到当前位置之后，前移一格
      queue.value.splice(idx.value + 1, 0, r.next)
      idx.value += 1
    } else {
      // 标完了：从队列里移除当前题
      queue.value.splice(idx.value, 1)
      if (idx.value >= queue.value.length) {
        // 本批标完 → 再取一批
        await load()
        if (!queue.value.length) {
          ElMessage.success('本范围内已经没有待打标的题目了')
        }
      }
    }
    await focusPage()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '打标失败')
  } finally {
    saving.value = 0
  }
}

/** 跳过：**不写库**，只是前移。 */
function skip(): void {
  if (idx.value < queue.value.length - 1) {
    idx.value += 1
  } else {
    // 本批最后一道 → 取下一批
    const last = queue.value[queue.value.length - 1]
    void load(last?.id)
  }
  void focusPage()
}

/** 上一个：往回走（不改数据）。 */
function prev(): void {
  if (idx.value > 0) idx.value -= 1
  void focusPage()
}

/** 查看详情：跳到题库页并带上该题（复用题库的详情抽屉）。 */
function openDetail(): void {
  const q = current.value
  if (!q) return
  void router.push({ name: 'QuestionList', query: { open: String(q.id) } })
}

/** 全局键盘监听。 */
function onKey(e: KeyboardEvent): void {
  // ★ 在输入框里打字时不抢按键（否则老师改课程筛选时按 3 会变成打标）
  const t = e.target as HTMLElement | null
  const tag = t?.tagName?.toLowerCase()
  if (tag === 'input' || tag === 'textarea' || t?.isContentEditable) return
  if (e.metaKey || e.ctrlKey || e.altKey) return

  const k = e.key
  if (k >= '1' && k <= '5') {
    e.preventDefault()
    void mark(Number(k))
    return
  }
  const lower = k.toLowerCase()
  if (lower === 's' || k === 'ArrowRight') {
    e.preventDefault()
    skip()
  } else if (k === 'ArrowLeft') {
    e.preventDefault()
    prev()
  } else if (lower === 'd') {
    e.preventDefault()
    openDetail()
  }
}

/** 切筛选后重新加载 */
async function onFilterChange(): Promise<void> {
  await load()
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  await load()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
})

const TYPE_LABEL: Record<string, string> = {
  SINGLE_CHOICE: '单选题',
  MULTIPLE_CHOICE: '多选题',
  TRUE_FALSE: '判断题',
  FILL_BLANK: '填空题',
  SHORT_ANSWER: '简答题',
  CALCULATION: '计算题',
  ANALYSIS: '分析题',
  DRAWING: '制图题',
  PRACTICAL: '实操题',
}

function typeLabel(t: string): string {
  return TYPE_LABEL[t] ?? t
}
</script>

<template>
  <div class="mk">
    <!-- ══════════ 工具栏 ══════════ -->
    <div class="zf-card mk__bar">
      <div class="zf-card__head">
        <h2 class="zf-card__title">难度打标</h2>
        <div class="zf-row">
          <el-checkbox
            v-model="onlyUnmarked" @change="onFilterChange"
          >
            仅未标注
          </el-checkbox>
          <el-button size="small" :loading="loading" @click="load()">
            刷新
          </el-button>
          <el-button size="small" @click="router.push({ name: 'QuestionList' })">
            返回题库
          </el-button>
        </div>
      </div>
      <div class="mk__progress">
        <span>进度 <b>{{ progressText }}</b></span>
        <el-progress
          :percentage="progressPct" :stroke-width="10"
          style="flex: 1; margin: 0 12px"
        />
        <span class="zf-dim">剩 {{ progress.remaining }} 道</span>
      </div>
      <p class="mk__keys">
        <b>快捷键</b>（不用鼠标）：
        <span v-for="k in KEYS" :key="k.key" class="mk__key">
          <kbd>{{ k.key }}</kbd> {{ k.label }}
        </span>
      </p>
    </div>

    <el-alert
      v-if="errorMsg" type="error" :title="errorMsg" show-icon
      :closable="false" style="margin-bottom: 12px"
    />

    <!-- ══════════ 打标区（一屏一道题） ══════════ -->
    <div
      id="mark-stage"
      class="zf-card mk__stage"
      tabindex="-1"
      v-loading="loading"
    >
      <template v-if="current">
        <div class="mk__head">
          <span class="mk__seq">第 {{ idx + 1 }} 题</span>
          <el-tag size="small">{{ typeLabel(current.type) }}</el-tag>
          <el-tag
            v-if="current.difficulty_manual"
            size="small" type="success" effect="plain"
          >
            已标 {{ current.difficulty_manual }} 级
          </el-tag>
          <el-tag
            v-else size="small" type="info" effect="plain"
          >
            未标（系统值 {{ current.difficulty_effective }}）
          </el-tag>
          <span class="zf-dim">
            {{ current.task_name }}
            <template v-if="current.chapter_name">
              / {{ current.chapter_name }}
            </template>
          </span>
        </div>

        <!-- 题干：★ 完整题干，不是摘要 —— 摘要会让老师标错 -->
        <MathText tag="div" class="mk__stem" :source="current.stem_text" />

        <!-- 选项（有才显示） -->
        <ul v-if="current.options.length" class="mk__opts">
          <li
            v-for="o in current.options" :key="o.label"
            :class="{ 'mk__opt--right': o.is_correct }"
          >
            <b>{{ o.label }}.</b> <MathText :source="o.content_text" />
          </li>
        </ul>

        <div v-if="current.answer" class="mk__answer">
          <b>标准答案：</b>{{ current.answer }}
        </div>

        <!-- 难度选择：★ 五个按钮，数字键 1–5 -->
        <div class="mk__pick">
          <span class="mk__pick-label">难度</span>
          <el-button
            v-for="lv in [1, 2, 3, 4, 5]" :key="lv"
            :type="(M.DIFFICULTY_TONE[lv] as never)"
            :loading="saving === lv"
            :disabled="!!saving"
            @click="mark(lv)"
          >
            <kbd class="mk__pick-k">{{ lv }}</kbd>
            {{ M.DIFFICULTY_LABELS[lv] }}
          </el-button>
        </div>

        <div class="mk__acts">
          <el-button :disabled="idx === 0" @click="prev">
            上一个 <kbd>←</kbd>
          </el-button>
          <el-button @click="skip">跳过 <kbd>S</kbd></el-button>
          <el-button @click="openDetail">查看详情 <kbd>D</kbd></el-button>
        </div>
      </template>

      <el-empty
        v-else-if="!loading"
        :description="onlyUnmarked
          ? '没有待打标的题目了 —— 可以取消「仅未标注」复核已标过的'
          : '这个范围内没有题目'"
      />
    </div>
  </div>
</template>

<style scoped>
.mk { display: flex; flex-direction: column; gap: 12px; }
.mk__bar { padding-bottom: 14px; }
.mk__progress {
  display: flex; align-items: center;
  font-size: var(--zf-text-sm); margin-top: 4px;
}
.mk__keys {
  margin: 10px 0 0; font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary);
}
.mk__key { margin-right: 14px; white-space: nowrap; }
.mk__keys kbd,
.mk__acts kbd,
.mk__pick-k {
  display: inline-block;
  padding: 0 5px; margin-right: 4px;
  border: 1px solid var(--zf-border);
  border-bottom-width: 2px;
  border-radius: 3px;
  background: var(--zf-bg-subtle);
  font-family: var(--zf-font-mono);
  font-size: 11px; line-height: 16px;
}

/* ★ 打标区：给足留白，让「一屏一道题」的视觉焦点明确 */
.mk__stage {
  min-height: 52vh;
  outline: none;
  display: flex;
  flex-direction: column;
}
.mk__head {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; margin-bottom: 14px;
}
.mk__seq { font-weight: 700; font-size: var(--zf-text-lg); }
.mk__stem {
  font-size: 16px;
  line-height: 1.9;
  white-space: pre-wrap;
  margin-bottom: 14px;
}
.mk__opts {
  list-style: none; padding: 0; margin: 0 0 14px;
  font-size: 14px; line-height: 1.9;
}
.mk__opt--right {
  font-weight: 600;
  /* ★ 令牌名是 `--zf-success`（不是 `--zf-color-success`）。
       写错会被 `sync-tokens.mjs --check` 抓出来 ——
       这就是那道校验的价值：写错令牌不会报错，只会**静默不着色**。 */
  color: var(--zf-success);
}
.mk__answer {
  font-size: 14px; padding: 8px 12px;
  background: var(--zf-bg-subtle);
  border-radius: var(--zf-radius-sm);
  margin-bottom: 18px;
}
.mk__pick {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; margin-bottom: 16px;
}
.mk__pick-label {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  margin-right: 4px;
}
.mk__acts { display: flex; gap: 8px; margin-top: auto; }
</style>
