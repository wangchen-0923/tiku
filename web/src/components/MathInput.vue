<script setup lang="ts">
/**
 * 公式输入框 —— 直接替换 `<el-input type="textarea">`。
 *
 * ```vue
 * <MathInput v-model="form.stem_text" :rows="4" label="题干" />
 * ```
 *
 * 它做四件事：
 *   ① 承载 TeX 原文（`$…$` / `$$…$$` 定界），**不改字段类型、不改库**
 *   ② 获得焦点时把自己登记为「当前活动输入框」，
 *      页面级 `<MathToolbar />` 因此知道该往哪儿插公式
 *   ③ 实时预览（可关）
 *   ④ 定界符未闭合 / TeX 语法错的即时提示
 *
 * ══════════════════════════════════════════════════════════
 *  为什么不是富文本编辑器
 * ══════════════════════════════════════════════════════════
 *
 * 题干是纯文本字段（`stem_text`），后端 `stem_html` 与它同值。
 * 换成富文本要同时改库、改去重哈希（`stem_hash` 由 TeX 原文算，
 * 同一道题两次录入天然一致）、改 PDF/Word 导出、改清洗白名单
 * —— 代价远超收益。TeX 定界符可移植、可 diff、可导出。
 *
 * ══════════════════════════════════════════════════════════
 *  两条光标规则
 * ══════════════════════════════════════════════════════════
 *
 *  · **选中内容会被包住**：选中 `1` 点「分数」→ `$\frac{1}{}$`
 *    （不是「在选区后面追加」—— 那是所有公式编辑器的肌肉记忆）
 *  · **已在公式里就不补定界符**：光标在 `$…$` 内 → 只插 `\frac{}{}`；
 *    在外面 → 插 `$\frac{}{}$`。否则点两次「分数」会得到
 *    `$\frac{}{}$\frac{}{}$` 这种半截公式
 */
import { ElInput } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { inMathAt, scanMath, unclosedMath, validateTex } from '@/utils/math'
import {
  clearMathTarget,
  setActiveMathTarget,
  type MathTarget,
} from '@/utils/mathInsert'
import MathText from './MathText.vue'
import MathToolbar from './MathToolbar.vue'

const props = withDefaults(
  defineProps<{
    /**
     * TeX 原文。允许 `undefined` —— 组卷配置里的大题标题是可选字段
     * （`section.title?: string`），不能因为一个空标题就让它传不进来。
     */
    modelValue?: string | null
    rows?: number
    placeholder?: string
    disabled?: boolean
    /** 是否显示实时预览；`compact` 下默认关 */
    preview?: boolean
    maxlength?: number
    /** 紧凑：用于选项这类一行一个的输入，默认不显示预览 */
    compact?: boolean
    /** `popover` = 自己带一个弹出工具条（页面没有共享工具条时用） */
    toolbar?: 'none' | 'popover'
    /** 登记表里的标识，只用于调试与提示 */
    label?: string
  }>(),
  {
    modelValue: '',
    rows: 4,
    placeholder: '',
    disabled: false,
    preview: true,
    maxlength: undefined,
    compact: false,
    toolbar: 'none',
    label: '输入框',
  },
)

const emit = defineEmits<{
  'update:modelValue': [v: string]
  blur: []
}>()

const inputRef = ref<InstanceType<typeof ElInput>>()
const popOpen = ref(false)

const showPreview = ref(props.preview && !props.compact)

watch(
  () => props.preview,
  (v) => {
    if (!v) showPreview.value = false
  },
)

/** 拿到原生 textarea（Element Plus 把 ref 暴露成 `.textarea`） */
function textarea(): HTMLTextAreaElement | null {
  const inst = inputRef.value as unknown as
    | { textarea?: HTMLTextAreaElement }
    | undefined
  return inst?.textarea ?? null
}

function emitValue(v: string, caret?: number): void {
  emit('update:modelValue', v)
  if (caret == null) return
  // ★ 必须等下一帧：`el-input` 的 value 是受控的，
  //   同步 setSelectionRange 会被随后的重渲染重置到末尾。
  void nextTick(() => {
    const el = textarea()
    if (!el) return
    el.focus()
    el.setSelectionRange(caret, caret)
  })
}

/** 插入片段（`§` = 选中内容 / 光标位置） */
function applySnippet(snippet: string, wrap?: 'inline' | 'block'): void {
  if (props.disabled) return
  const raw = props.modelValue || ''
  const el = textarea()
  const start = el?.selectionStart ?? raw.length
  const end = el?.selectionEnd ?? raw.length
  const selected = raw.slice(start, end)

  let tpl = snippet
  if (wrap === 'inline') {
    tpl = `$${snippet}$`
  } else if (wrap === 'block') {
    tpl = `$$${snippet}$$`
  } else if (!inMathAt(raw, start)) {
    // 光标不在公式里 → 自动补定界符；方程组这类块级用 `$$`
    tpl = snippet.includes('\\begin{') ? `$$${snippet}$$` : `$${snippet}$`
  }

  const markAt = tpl.indexOf('§')
  const body =
    markAt >= 0 ? tpl.slice(0, markAt) + selected + tpl.slice(markAt + 1) : tpl
  const caret =
    markAt >= 0 ? start + markAt + selected.length : start + body.length

  emitValue(raw.slice(0, start) + body + raw.slice(end), caret)
  popOpen.value = false
}

/** 清掉选区内的公式定界符（老师想把公式改回纯文本时用） */
function stripDelimiters(): void {
  const raw = props.modelValue || ''
  const el = textarea()
  const start = el?.selectionStart ?? 0
  const end = el?.selectionEnd ?? raw.length
  const scope = raw.slice(start, end).replace(/\$\$/g, '').replace(/\$/g, '')
  emitValue(raw.slice(0, start) + scope + raw.slice(end), start + scope.length)
}

// ══════════════════════════════════════════════════════════
//  注册为「当前活动输入框」
// ══════════════════════════════════════════════════════════

const target: MathTarget = {
  label: props.label,
  insert: (snippet, wrap) => applySnippet(snippet, wrap),
  strip: () => stripDelimiters(),
}

onBeforeUnmount(() => clearMathTarget(target))

// ══════════════════════════════════════════════════════════
//  状态提示
// ══════════════════════════════════════════════════════════

const mathCount = computed(
  () => scanMath(props.modelValue || '').filter((s) => s.type === 'math').length,
)

const unbalanced = computed(() => unclosedMath(props.modelValue || ''))

/** 第一条语法错的公式（只报一条，避免刷屏） */
const firstError = computed(() => {
  for (const seg of scanMath(props.modelValue || '')) {
    if (seg.type !== 'math') continue
    const r = validateTex(seg.value)
    if (!r.ok) return `公式「${seg.value.slice(0, 24)}」有语法问题：${r.message}`
  }
  return ''
})
</script>

<template>
  <div class="zf-minput" :class="{ 'is-disabled': disabled }">
    <div class="zf-minput__field">
      <el-input
        ref="inputRef"
        :model-value="modelValue"
        type="textarea"
        :rows="rows"
        :placeholder="placeholder"
        :disabled="disabled"
        :maxlength="maxlength"
        resize="vertical"
        @focus="setActiveMathTarget(target)"
        @update:model-value="(v: string | number) => emit('update:modelValue', String(v))"
        @blur="emit('blur')"
      />

      <!-- 自带弹出工具条（页面没有共享工具条时用） -->
      <el-popover
        v-if="!disabled && toolbar === 'popover'"
        v-model:visible="popOpen"
        placement="bottom-end"
        :width="520"
        trigger="click"
      >
        <template #reference>
          <!-- ★ mousedown.prevent 保焦点，否则 selectionStart 归零 -->
          <button
            type="button"
            class="zf-minput__fx"
            title="插入公式"
            @mousedown.prevent
          >
            ƒx
          </button>
        </template>
        <MathToolbar compact />
      </el-popover>
    </div>

    <!-- 预览 -->
    <div v-if="showPreview" class="zf-minput__preview">
      <div class="zf-minput__ph">
        <span>预览</span>
        <span v-if="mathCount" class="zf-minput__count">
          {{ mathCount }} 处公式
        </span>
        <span class="zf-minput__gap" />
        <button
          type="button"
          class="zf-minput__link"
          @click="showPreview = false"
        >
          隐藏
        </button>
      </div>
      <div v-if="modelValue?.trim()" class="zf-minput__body">
        <MathText :source="modelValue" />
      </div>
      <div v-else class="zf-minput__empty">还没有内容</div>
    </div>
    <div v-else-if="!compact" class="zf-minput__foot">
      <button
        type="button"
        class="zf-minput__link"
        @click="showPreview = true"
      >
        显示预览
      </button>
    </div>

    <!-- 提示 -->
    <div v-if="unbalanced" class="zf-minput__warn">
      有 {{ unbalanced }} 处公式定界符没闭合（`$` 或 `\(` / `\[` 少了一半），
      这一段会按原文显示。
    </div>
    <div v-else-if="firstError" class="zf-minput__warn">{{ firstError }}</div>
  </div>
</template>

<style scoped>
.zf-minput {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

.zf-minput__field {
  position: relative;
  width: 100%;
}

/* 右上角的 ƒx 触发器：不占行、不挡文字 */
.zf-minput__fx {
  position: absolute;
  top: 4px;
  right: 6px;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  color: var(--zf-text-secondary);
  font-size: 12px;
  line-height: 18px;
  cursor: pointer;
  opacity: 0.75;
}

.zf-minput__fx:hover {
  opacity: 1;
  border-color: var(--zf-brand-border);
  color: var(--zf-brand);
  background: var(--zf-brand-subtle);
}

/* ── 预览 ── */
.zf-minput__preview {
  border: 1px dashed var(--zf-border-strong);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  padding: 6px 8px 8px;
}

.zf-minput__ph {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--zf-text-tertiary);
  margin-bottom: 4px;
}

.zf-minput__count {
  color: var(--zf-brand);
}

.zf-minput__gap {
  flex: 1 1 auto;
}

.zf-minput__body {
  font-size: 14px;
  line-height: 1.7;
  color: var(--zf-text-primary);
  max-height: 220px;
  overflow: auto;
}

.zf-minput__empty,
.zf-minput__foot {
  font-size: 12px;
  color: var(--zf-text-tertiary);
}

.zf-minput__link {
  border: 0;
  background: transparent;
  padding: 0;
  color: var(--zf-brand);
  font-size: 12px;
  cursor: pointer;
}

/* ── 警告 ── */
.zf-minput__warn {
  font-size: 12px;
  line-height: 1.6;
  color: var(--zf-warning);
  background: var(--zf-warning-bg);
  border-radius: var(--zf-radius-sm);
  padding: 4px 8px;
}
</style>
