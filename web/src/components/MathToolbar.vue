<script setup lang="ts">
/**
 * 公式工具条 —— **整页只放一条**，作用于最后获得焦点的公式输入框。
 *
 * 用法：直接 `<MathToolbar />`，不需要传 props、不需要绑定 v-model。
 * 它通过 `utils/mathInsert.ts` 的注册表找到当前输入框。
 *
 * ★★ 所有按钮都是 `@mousedown.prevent`，不是 `@click`：
 *   `click` 永远发生在 `blur` 之后，那时 `selectionStart` 已经归零，
 *   「包住选中内容」会变成「插到末尾」。
 *
 * ★ 按钮图标直接用 KaTeX 渲染 TeX 片段（`\frac{a}{b}` 画成分数样子），
 *   比写「分数」两个字更直观，也免了中英文混排对齐问题。
 */
import { computed, ref } from 'vue'

import { MATH_SNIPPETS, renderTex } from '@/utils/math'
import {
  insertMath,
  stripMathDelimiters,
  useHasMathTarget,
} from '@/utils/mathInsert'

withDefaults(
  defineProps<{
    /** 紧凑：不显示底部提示行（嵌在 Popover 里时用） */
    compact?: boolean
  }>(),
  { compact: false },
)

const tab = ref(0)

const items = computed(() => MATH_SNIPPETS[tab.value]?.items ?? [])

/** 是否已经有聚焦的输入框（没有时按钮置灰，避免点了没反应） */
const hasTarget = useHasMathTarget()

function onInsert(snippet: string, wrap?: 'inline' | 'block'): void {
  insertMath(snippet, wrap)
}

function onStrip(): void {
  stripMathDelimiters()
}
</script>

<template>
  <div class="zf-mtbar" :class="{ 'is-compact': compact }">
    <div class="zf-mtbar__bar">
      <button
        v-for="(g, gi) in MATH_SNIPPETS"
        :key="g.name"
        type="button"
        class="zf-mtbar__tab"
        :class="{ 'is-on': tab === gi }"
        @mousedown.prevent
        @click="tab = gi"
      >
        {{ g.name }}
      </button>

      <span class="zf-mtbar__gap" />

      <button
        type="button"
        class="zf-mtbar__act"
        title="把选中内容包成行内公式 $…$"
        :disabled="!hasTarget"
        @mousedown.prevent
        @click="onInsert('§', 'inline')"
      >
        行内 $…$
      </button>
      <button
        type="button"
        class="zf-mtbar__act"
        title="把选中内容包成独立公式 $$…$$（单独占行、居中）"
        :disabled="!hasTarget"
        @mousedown.prevent
        @click="onInsert('§', 'block')"
      >
        独立 $$…$$
      </button>
      <button
        type="button"
        class="zf-mtbar__act"
        title="去掉选中范围内的 $ 定界符（改回纯文本）"
        :disabled="!hasTarget"
        @mousedown.prevent
        @click="onStrip()"
      >
        转纯文本
      </button>
    </div>

    <div class="zf-mtbar__grid">
      <button
        v-for="it in items"
        :key="it.snippet"
        type="button"
        class="zf-mtbar__btn"
        :title="it.title"
        :disabled="!hasTarget"
        @mousedown.prevent
        @click="onInsert(it.snippet)"
      >
        <!-- eslint-disable-next-line vue/no-v-html -- 内容是常量 TeX，由 KaTeX 渲染 -->
        <span class="zf-mtbar__icon" v-html="renderTex(it.label)" />
      </button>
    </div>

    <div v-if="!compact" class="zf-mtbar__hint">
      工具条作用于**当前光标所在的输入框**。选中一段内容再点按钮，
      会把这段包进公式里。
    </div>
  </div>
</template>

<style scoped>
.zf-mtbar {
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-subtle);
  overflow: hidden;
}

.zf-mtbar__bar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  flex-wrap: wrap;
}

.zf-mtbar__gap {
  flex: 1 1 auto;
}

.zf-mtbar__tab,
.zf-mtbar__act {
  border: 1px solid transparent;
  background: transparent;
  color: var(--zf-text-secondary);
  font-size: 12px;
  line-height: 22px;
  padding: 0 8px;
  border-radius: var(--zf-radius-sm);
  cursor: pointer;
  white-space: nowrap;
}

.zf-mtbar__tab:hover,
.zf-mtbar__act:hover:not(:disabled) {
  background: var(--zf-bg-hover);
  color: var(--zf-text-primary);
}

.zf-mtbar__tab.is-on {
  background: var(--zf-brand-subtle);
  border-color: var(--zf-brand-border);
  color: var(--zf-brand);
  font-weight: 500;
}

.zf-mtbar__act {
  border-color: var(--zf-border);
  background: var(--zf-bg-surface);
}

.zf-mtbar__act:disabled,
.zf-mtbar__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.zf-mtbar__grid {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px;
  border-top: 1px solid var(--zf-border);
  background: var(--zf-bg-surface);
}

.zf-mtbar__btn {
  min-width: 40px;
  height: 30px;
  padding: 0 6px;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  color: var(--zf-text-primary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.zf-mtbar__btn:hover:not(:disabled) {
  border-color: var(--zf-brand-border);
  background: var(--zf-brand-subtle);
}

.zf-mtbar__icon {
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  pointer-events: none;
}

.zf-mtbar__icon :deep(.katex) {
  font-size: 1em;
}

.zf-mtbar__hint {
  padding: 0 6px 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--zf-text-tertiary);
  background: var(--zf-bg-subtle);
}

.is-compact .zf-mtbar__grid {
  border-top-color: var(--zf-border-subtle);
}
</style>
