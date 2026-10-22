<script setup lang="ts">
/**
 * 公式渲染组件 —— 全站**唯一**的富文本渲染出口。
 *
 * ★★ 为什么必须收成一个组件，而不是各页面自己 `v-html`：
 *
 *   「公式在题目详情里正常、在答题页里是 `$x^2$` 原文」这类不一致，
 *   只要渲染逻辑散落在 6 个页面里就一定会发生（一份漏改就够了）。
 *   所有页面都走这里，改一处即六处生效。
 *
 * `mode`：
 *   · `auto`（默认）—— 自动判断这段是 HTML 还是纯文本。题库里
 *     `stem_html` 大量与 `stem_text` 同值（都是纯文本），不能只看字段名。
 *   · `html` —— 强制当 HTML（已知是富文本时用，省一次嗅探）
 *   · `text` —— 强制当纯文本（答案、快照等场景）
 *
 * 根元素默认是 `<span>`（`display:inline`），这样放进
 * `<p>` / `<div>` / 表格单元格都不会改变原有排版。
 */
import { computed } from 'vue'

import { renderMathInHtml, renderMathText, renderRich } from '@/utils/math'

const props = withDefaults(
  defineProps<{
    source?: string | null
    mode?: 'auto' | 'html' | 'text'
    tag?: string
  }>(),
  { source: '', mode: 'auto', tag: 'span' },
)

const html = computed(() => {
  const src = props.source ?? ''
  if (!src) return ''
  if (props.mode === 'html') return renderMathInHtml(src)
  if (props.mode === 'text') return renderMathText(src)
  return renderRich(src)
})
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- 内容由 math.ts 转义/渲染，见该文件头部说明 -->
  <component :is="tag" class="zf-math" v-html="html" />
</template>

<style scoped>
.zf-math {
  word-break: break-word;
}

/* 行内公式不要撑高行距（KaTeX 默认会给 .katex 一个行高） */
.zf-math :deep(.katex) {
  font-size: 1.05em;
}

/* 独立公式居中且允许横向滚动（长公式在窄屏上不该撑破容器） */
.zf-math :deep(.katex-display) {
  margin: 0.4em 0;
  overflow-x: auto;
  overflow-y: hidden;
}

/* 语法错时 KaTeX 会输出红色原文，这里补一个浅底让它更容易被注意到 */
.zf-math :deep(.katex-error) {
  background: #fdecea;
  border-radius: 3px;
  padding: 0 2px;
}
</style>
