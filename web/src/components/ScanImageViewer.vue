<script setup lang="ts">
/**
 * 扫描图查看器：适应窗口 / 100% / 200%，滚轮缩放、拖拽平移。
 *
 * ★ 关键：容器必须有**确定高度**，否则 `object-fit: contain` 不生效，
 *   图片按宽度铺满后会被弹窗 body 裁掉下半部分。
 */
import { ElImageViewer } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  src: string
  previewList?: string[]
  initialIndex?: number
  alt?: string
}>(), {
  previewList: () => [],
  initialIndex: 0,
  alt: '扫描图',
})

const box = ref<HTMLElement | null>(null)
const naturalW = ref(0)
const naturalH = ref(0)
const boxW = ref(0)
const boxH = ref(0)
const scale = ref(1)
const tx = ref(0)
const ty = ref(0)
const mode = ref<'fit' | '100' | '200' | 'custom'>('fit')
const previewOpen = ref(false)
const dragging = ref(false)
let dragStart = { x: 0, y: 0, tx: 0, ty: 0 }
let moved = false
let ro: ResizeObserver | null = null

const fitScale = computed(() => {
  if (!naturalW.value || !naturalH.value || !boxW.value || !boxH.value) {
    return 1
  }
  return Math.min(boxW.value / naturalW.value,
                  boxH.value / naturalH.value)
})

const imgStyle = computed(() => ({
  width: `${naturalW.value || 1}px`,
  height: `${naturalH.value || 1}px`,
  transform: `translate(${tx.value}px, ${ty.value}px) scale(${scale.value})`,
}))

const previewList = computed(() =>
  props.previewList.length ? props.previewList : [props.src])

function applyMode(): void {
  if (mode.value === 'fit') scale.value = fitScale.value
  else if (mode.value === '100') scale.value = 1
  else if (mode.value === '200') scale.value = 2
  tx.value = 0
  ty.value = 0
}

function setMode(m: 'fit' | '100' | '200'): void {
  mode.value = m
  applyMode()
}

function onLoad(e: Event): void {
  const img = e.target as HTMLImageElement
  naturalW.value = img.naturalWidth || 1
  naturalH.value = img.naturalHeight || 1
  if (mode.value === 'fit') scale.value = fitScale.value
}

function onWheel(e: WheelEvent): void {
  e.preventDefault()
  const factor = e.deltaY > 0 ? 1 / 1.12 : 1.12
  scale.value = Math.min(6, Math.max(0.05, scale.value * factor))
  mode.value = 'custom'
}

function onPointerDown(e: PointerEvent): void {
  dragging.value = true
  moved = false
  dragStart = { x: e.clientX, y: e.clientY, tx: tx.value, ty: ty.value }
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent): void {
  if (!dragging.value) return
  const dx = e.clientX - dragStart.x
  const dy = e.clientY - dragStart.y
  if (Math.abs(dx) + Math.abs(dy) > 3) moved = true
  tx.value = dragStart.tx + dx
  ty.value = dragStart.ty + dy
}

function onPointerUp(e: PointerEvent): void {
  dragging.value = false
  ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
}

function onClick(): void {
  if (moved) {
    moved = false
    return
  }
  previewOpen.value = true
}

function measure(): void {
  if (!box.value) return
  boxW.value = box.value.clientWidth
  boxH.value = box.value.clientHeight
  if (mode.value === 'fit') scale.value = fitScale.value
}

onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(measure)
    if (box.value) ro.observe(box.value)
  }
})

onBeforeUnmount(() => {
  ro?.disconnect()
})

watch(() => props.src, () => {
  // ★ 切换页码 / 标注模式时保留当前缩放；只把平移归零。
  tx.value = 0
  ty.value = 0
  nextTick(() => {
    if (mode.value === 'fit') scale.value = fitScale.value
  })
})

watch(fitScale, (v) => {
  if (mode.value === 'fit') scale.value = v
})
</script>

<template>
  <div class="viewer-shell">
    <div class="viewer-toolbar">
      <el-radio-group
        :model-value="mode === 'custom' ? '' : mode"
        size="small"
        @change="(v: string | number | boolean) => setMode(v as 'fit' | '100' | '200')"
      >
        <el-radio-button value="fit">适应窗口</el-radio-button>
        <el-radio-button value="100">100%</el-radio-button>
        <el-radio-button value="200">200%</el-radio-button>
      </el-radio-group>
      <span class="zoom-text">{{ Math.round(scale * 100) }}%</span>
      <span class="hint">滚轮缩放 · 拖拽平移 · 点击全屏</span>
    </div>
    <div
      ref="box"
      class="viewer-box"
      @wheel="onWheel"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
      <img
        v-if="src"
        :src="src"
        :alt="alt"
        :style="imgStyle"
        draggable="false"
        @load="onLoad"
        @click="onClick"
      />
      <div v-else class="empty">图片加载中…</div>
    </div>
    <ElImageViewer
      v-if="previewOpen"
      :url-list="previewList"
      :initial-index="initialIndex"
      teleported
      @close="previewOpen = false"
    />
  </div>
</template>

<style scoped>
.viewer-shell {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
}
.viewer-toolbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 0 0 8px;
}
.zoom-text {
  min-width: 48px;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  font-variant-numeric: tabular-nums;
}
.hint {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.viewer-box {
  flex: 1;
  min-height: 0;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: #f5f5f5;
  cursor: grab;
  touch-action: none;
}
.viewer-box:active { cursor: grabbing; }
.viewer-box img {
  /* ★ 用自然像素尺寸 + transform 缩放；不要再用 max-width/height 二次约束，
     否则 100%/200% 会被压回容器尺寸。 */
  max-width: none;
  max-height: none;
  object-fit: contain;
  user-select: none;
  transition: transform 0.08s ease-out;
}
.empty {
  color: var(--zf-text-secondary);
  font-size: var(--zf-text-sm);
}
</style>
