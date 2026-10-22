<script setup lang="ts">
/**
 * 预览里的**一张图**（`kind === 'figure'` 或主观题块内嵌图）。
 *
 * ★ 位置/宽高全部来自排版 JSON（mm），本组件只做 mm → px 的换算。
 * ★ 缺图时印「［配图缺失］」占位（与 PDF / Word 一致），不静默留白。
 */
import { computed } from 'vue'

import type { LayoutFigure } from '@/api/paper'

const props = defineProps<{
  figure: LayoutFigure
  /** 像素 / 毫米 */
  scale: number
  /** `asset_id` → blob URL（父组件缓存） */
  imageUrl?: string
  /** 图注字号（pt） */
  captionFontPt: number
  /** 图与图注的间隙（mm，来自排版 JSON） */
  gapMm: number
  /** 图注行高（mm，来自排版 JSON） */
  captionLineHMm: number
}>()

const x = computed(() => (props.figure.x_mm ?? 0) * props.scale)
const y = computed(() => (props.figure.y_mm ?? 0) * props.scale)
const w = computed(() => (props.figure.width_mm ?? 0) * props.scale)
const imgH = computed(() => (props.figure.image_h_mm ?? 0) * props.scale)
const captionTop = computed(
  () => imgH.value + props.gapMm * 0.6 * props.scale,
)
const showPlaceholder = computed(
  () => !!props.figure.missing || !props.imageUrl,
)
const fontSize = computed(
  () => props.captionFontPt * 25.4 / 72 * props.scale,
)
</script>

<template>
  <div
    class="pf" :style="{
      left: `${x}px`, top: `${y}px`, width: `${w}px`,
    }"
  >
    <img
      v-if="!showPlaceholder && imageUrl"
      class="pf__img" :src="imageUrl" alt=""
      :style="{ width: `${w}px`, height: `${imgH}px` }"
      draggable="false"
    >
    <div
      v-else
      class="pf__missing"
      :style="{ width: `${w}px`, height: `${imgH}px`, fontSize: `${fontSize}px` }"
    >
      ［配图缺失］
    </div>
    <div
      v-for="(line, li) in (figure.caption_lines || [])"
      :key="li"
      class="pf__caption"
      :style="{
        top: `${captionTop + li * captionLineHMm * scale}px`,
        fontSize: `${fontSize}px`,
        lineHeight: `${captionLineHMm * scale}px`,
      }"
    >
      {{ line }}
    </div>
  </div>
</template>

<style scoped>
.pf { position: absolute; }
.pf__img { display: block; object-fit: fill; }
.pf__missing {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed #b9c0c9;
  color: #6b7280;
  background: #f8fafc;
  box-sizing: border-box;
}
.pf__caption {
  position: absolute;
  left: 0;
  width: 100%;
  text-align: center;
  color: #4b5563;
  white-space: nowrap;
  overflow: hidden;
}
</style>
