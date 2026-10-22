<script setup lang="ts">
/**
 * 首页 · 待办工作台（计划 U25）。
 *
 * ★★ 这一页回答的是**「我现在该做什么」**，不是「系统里有多少东西」。
 *
 *   旧版首页是「题库规模 + 题型分布 + 服务状态」——那是**交付验收**看的，
 *   老师每天打开它得不到任何动作提示：1944 道题跟今天要做的事没关系。
 *   新版按教学主链路「内容 → 组卷 → 发布 → 批改 → 扫描」给出 6 张卡，
 *   每张卡的回答都是「还剩几件 + 点哪儿去处理」。
 *
 * ★★ 6 个数字全部来自 `GET /api/v1/workbench/` 一个接口，而且
 *   **每个数字都与对应列表的 count 走同一套取数**（后端 `source` 字段
 *   就是那条对应关系，`scripts/verify_workbench.py` 会逐个比对）。
 *   理由很实际：首页写 12、点进去 13，老师从此不信首页的任何数字。
 *
 * ★★ `count === 0` 不显示「0」而显示「已清空」：
 *   待办为 0 是**好消息**，不该看起来像一个待处理的告警。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { http } from '@/api/http'
import { countQuestions } from '@/api/questionbank'
import { useAuthStore } from '@/stores/auth'

interface WorkbenchCard {
  key: string
  title: string
  count: number
  unit: string
  tone: 'info' | 'warning' | 'danger' | 'success'
  hint: string
  action: { label: string; path: string; query: Record<string, string | number> }
  source: { api: string; query: Record<string, unknown>; kind: string }
}

const auth = useAuthStore()
const router = useRouter()

const loading = ref(true)
const errorMsg = ref('')
const cards = ref<WorkbenchCard[]>([])
const note = ref('')
const generatedAt = ref('')

/** 题库规模（次要信息，放页脚一行，不占卡片位置） */
const total = ref<number | null>(null)
const usable = ref<number | null>(null)

const usableRatio = computed(() => {
  if (!total.value || usable.value === null) return ''
  return `${Math.round((usable.value / total.value) * 100)}%`
})

/** 有代办的总件数（卡片右上角的汇总） */
const pendingTotal = computed(() =>
  cards.value.reduce((n, c) => n + c.count, 0),
)

/** 有几张卡还有东西没处理 */
const activeCards = computed(() => cards.value.filter((c) => c.count > 0).length)

const TONE_STYLE: Record<string, string> = {
  danger: 'var(--zf-danger, #d9534f)',
  warning: 'var(--zf-warning, #e6a23c)',
  info: 'var(--zf-info, #409eff)',
  success: 'var(--zf-success, #67c23a)',
}

function go(card: WorkbenchCard): void {
  // ★ 0 件也允许点进去看一眼（「确认真的清空了」是合理的动作）
  void router.push({ path: card.action.path, query: card.action.query as never })
}

async function load(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  // ★ 两件事互不依赖：待办读失败也不该让题库规模跟着空掉
  const [wb, t, u] = await Promise.allSettled([
    http.get<{ cards: WorkbenchCard[]; note: string; generated_at: string }>(
      '/api/v1/workbench/',
    ),
    countQuestions(),
    countQuestions({ onlyUsable: true }),
  ])

  if (wb.status === 'fulfilled') {
    cards.value = wb.value.cards ?? []
    note.value = wb.value.note ?? ''
    generatedAt.value = wb.value.generated_at ?? ''
  } else {
    errorMsg.value = String(wb.reason?.message ?? wb.reason)
  }
  if (t.status === 'fulfilled') total.value = t.value
  if (u.status === 'fulfilled') usable.value = u.value

  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="zf-stack">
    <div class="wb__head">
      <div>
        <h1 class="page-title">待办</h1>
        <p class="zf-muted page-sub">
          欢迎回来，{{ auth.user?.real_name || auth.user?.username }}
          <span class="zf-dim">（{{ auth.roleLabel }}）</span>
        </p>
      </div>
      <div class="wb__summary">
        <span v-if="loading" class="zf-dim">统计中…</span>
        <template v-else-if="cards.length">
          <span class="wb__num">{{ activeCards }}</span>
          <span class="zf-muted">
            项待处理（共 {{ pendingTotal }} 件）
          </span>
        </template>
      </div>
    </div>

    <el-alert
      v-if="errorMsg"
      type="error"
      :closable="false"
      show-icon
      title="待办统计读取失败"
      :description="errorMsg"
    />

    <el-alert
      v-if="note"
      type="info"
      :closable="false"
      show-icon
      :title="note"
    />

    <!-- ══════════ 6 张待办卡片 ══════════ -->
    <div v-loading="loading" class="wb__grid">
      <button
        v-for="c in cards"
        :key="c.key"
        class="wb__card"
        :class="{ 'wb__card--empty': c.count === 0 }"
        type="button"
        @click="go(c)"
      >
        <div class="wb__cardhead">
          <span class="wb__title">{{ c.title }}</span>
          <span
            class="wb__dot"
            :style="{ background: c.count ? TONE_STYLE[c.tone] : 'transparent' }"
          />
        </div>

        <div class="wb__count">
          <template v-if="c.count === 0">
            <span class="wb__clear">已清空</span>
          </template>
          <template v-else>
            <span class="wb__num" :style="{ color: TONE_STYLE[c.tone] }">
              {{ c.count }}
            </span>
            <span class="zf-muted wb__unit">{{ c.unit }}</span>
          </template>
        </div>

        <p class="wb__hint">{{ c.hint }}</p>

        <span class="wb__action">
          {{ c.action.label }}
          <span aria-hidden="true">→</span>
        </span>
      </button>
    </div>

    <div v-if="!loading && !cards.length && !errorMsg" class="zf-empty">
      没有需要处理的待办。
    </div>

    <!-- ══════════ 页脚：题库规模（次要信息） ══════════ -->
    <section class="zf-card wb__foot">
      <div class="zf-card__body wb__footbody">
        <span class="zf-muted">
          题库
          <strong>{{ total ?? '—' }}</strong> 题，其中
          <strong>{{ usable ?? '—' }}</strong> 题可用于组卷
          <span v-if="usableRatio" class="zf-dim">（{{ usableRatio }}）</span>
        </span>
        <span class="zf-dim wb__footnote">
          ★「可用于组卷」排除草稿题与配图未确认的题 ——
          判据在后端 <span class="zf-mono">usable_for_paper()</span>。
          <span v-if="generatedAt" class="wb__ts">
            统计时间 {{ new Date(generatedAt).toLocaleString() }}
          </span>
        </span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.wb__head {
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

.wb__summary {
  font-size: var(--zf-text-sm);
  padding-top: var(--zf-space-2);
}

.wb__grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--zf-space-4);
}

/* ★ 用 <button> 而不是 <div @click>：待办卡是可点击控件，
   键盘 Tab 要能到达、回车要能触发。 */
.wb__card {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-2);
  padding: var(--zf-space-4);
  text-align: left;
  cursor: pointer;
  background: var(--zf-bg-card, #fff);
  border: 1px solid var(--zf-border, #e4e7ed);
  border-radius: var(--zf-radius-lg, 10px);
  transition: border-color 0.15s, box-shadow 0.15s;
  font: inherit;
}

.wb__card:hover {
  border-color: var(--zf-color-primary, #409eff);
  box-shadow: 0 2px 10px rgb(0 0 0 / 6%);
}

.wb__card--empty {
  opacity: 0.62;
}

.wb__cardhead {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.wb__title {
  font-size: var(--zf-text-base);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}

.wb__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.wb__count {
  display: flex;
  align-items: baseline;
  gap: var(--zf-space-1);
  min-height: 40px;
}

.wb__num {
  font-size: 30px;
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
  color: var(--zf-text-primary);
}

.wb__unit {
  font-size: var(--zf-text-xs);
}

.wb__clear {
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}

.wb__hint {
  margin: 0;
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
  color: var(--zf-text-secondary);
}

.wb__action {
  margin-top: auto;
  font-size: var(--zf-text-xs);
  color: var(--zf-color-primary, #409eff);
}

.wb__footbody {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-1);
  font-size: var(--zf-text-sm);
}

.wb__footnote {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}

.wb__ts {
  margin-left: var(--zf-space-2);
}

@media (max-width: 1100px) {
  .wb__grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .wb__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
