<script setup lang="ts">
/**
 * 试卷预览画布（`P1-62b` / 契约 v3）。
 *
 * ★★ **只按排版 JSON 的毫米坐标画**，不估算行数、不分页、不算选项位置。
 *
 * v3 关键差异：
 *   · 题干独占全宽；每道题 = 题干块 + 选项块
 *   · 选项只有 `A. 内容`，没有方框；位置来自 `option_cells`
 *   · 判断题没有选项块；填空题序号与题干同段
 *   · 考生信息按 `student_items` 画标签 + 下划线（不是 `___`）
 *   · 分组标题的汇总来自 `title_lines`
 *   · 没有逐题分值、没有主观题作答区
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { LayoutBlock, PaperLayoutResult } from '@/api/paper'
import MathText from '@/components/MathText.vue'
import PaperFigure from './PaperFigure.vue'

const props = defineProps<{
  layout: PaperLayoutResult | null
  zoomPercent: number | null
  imageUrls: Record<number, string>
  loading?: boolean
}>()

const PT_TO_MM = 25.4 / 72
const viewport = ref<HTMLElement | null>(null)
const viewportW = ref(900)
let ro: ResizeObserver | null = null

onMounted(() => {
  ro = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect?.width
    if (w) viewportW.value = w
  })
  if (viewport.value) ro.observe(viewport.value)
})
onBeforeUnmount(() => ro?.disconnect())

const scale = computed(() => {
  const lay = props.layout
  if (!lay) return 1
  if (props.zoomPercent == null) {
    return Math.max((viewportW.value - 32) / lay.page_width_mm, 0.5)
  }
  return (props.zoomPercent / 100) * (96 / 25.4)
})

const baseFont = computed(() => props.layout?.config.base_font_pt ?? 10.5)

function px(mm: number): string {
  return `${mm * scale.value}px`
}

function fontPx(pt: number): string {
  return `${pt * PT_TO_MM * scale.value}px`
}

/** 文字基线 y_mm → CSS top（CSS 的 top 是盒子顶，不是基线） */
function topOfBaseline(yMm: number, fontPt: number): string {
  return `${(yMm - fontPt * PT_TO_MM * 0.82) * scale.value}px`
}

function sectionLineH(b: LayoutBlock): number {
  return b.line_h_mm || (baseFont.value + 1.5) * 1.9 * PT_TO_MM
}

function stemLineH(b: LayoutBlock): number {
  return b.line_h_mm || baseFont.value * 1.5 * PT_TO_MM
}

// ══════════════════════════════════════════════════
//  滚动锚点：按「当前可见页 + 页内偏移」恢复
// ══════════════════════════════════════════════════

let anchor: { pageIndex: number; offset: number } | null = null

function captureAnchor(): void {
  const el = viewport.value
  if (!el) return
  const pages = Array.from(el.querySelectorAll<HTMLElement>('[data-page]'))
  if (!pages.length) return
  const top = el.scrollTop
  let pageIndex = 0
  let offset = 0
  for (let i = 0; i < pages.length; i += 1) {
    if (pages[i].offsetTop <= top) {
      pageIndex = i
      offset = top - pages[i].offsetTop
    }
  }
  anchor = { pageIndex, offset }
}

async function restoreAnchor(): Promise<void> {
  if (!anchor || !viewport.value) return
  const el = viewport.value
  const pages = Array.from(el.querySelectorAll<HTMLElement>('[data-page]'))
  const page = pages[Math.min(anchor.pageIndex, pages.length - 1)]
  if (!page) return
  el.scrollTop = page.offsetTop + anchor.offset
}

watch(() => props.layout, () => captureAnchor(), { flush: 'pre' })
watch(() => props.layout, async () => {
  await nextTick()
  await restoreAnchor()
})
watch(scale, () => captureAnchor(), { flush: 'pre' })
watch(scale, async () => {
  await nextTick()
  await restoreAnchor()
})
</script>

<template>
  <div ref="viewport" v-loading="loading" class="pvc">
    <div v-if="layout" class="pvc__stack">
      <div
        v-for="page in layout.pages" :key="page.page_no"
        class="pvc__page" :data-page="page.page_no"
        :style="{
          width: px(layout.page_width_mm),
          height: px(layout.page_height_mm),
        }"
      >
        <!-- ── 卷头 ── -->
        <template v-if="page.page_no === 1">
          <div
            v-for="(line, i) in (layout.header.title.lines || [])"
            :key="`t${i}`" class="pvc__title"
            :style="{
              top: topOfBaseline(layout.header.title.y_mm
                + i * layout.header.title.line_h_mm,
                layout.header.title.font_pt),
              fontSize: fontPx(layout.header.title.font_pt),
            }"
          >
            <MathText mode="text" :source="line" />
          </div>
          <div
            v-if="layout.header.subtitle.text"
            class="pvc__subtitle"
            :style="{
              top: topOfBaseline(layout.header.subtitle.y_mm,
                layout.header.subtitle.font_pt),
              fontSize: fontPx(layout.header.subtitle.font_pt),
            }"
          >
            {{ layout.header.subtitle.text }}
          </div>

          <!-- ★ 考生信息：标签 + 画出来的下划线（不是 ___） -->
          <template
            v-for="(item, i) in (layout.header.student_items || [])"
            :key="`s${i}`"
          >
            <div
              class="pvc__student"
              :style="{
                left: px(layout.margin_mm + item.x_mm),
                top: topOfBaseline(item.y_mm, baseFont),
                fontSize: fontPx(baseFont),
              }"
            >
              {{ item.label }}
            </div>
            <div
              class="pvc__underline"
              :style="{
                left: px(layout.margin_mm + item.underline_x_mm),
                top: px(item.y_mm + 1.8),
                width: px(item.underline_w_mm),
              }"
            />
          </template>

          <div
            v-if="layout.header.student_extra?.text"
            class="pvc__line"
            :style="{
              left: px(layout.margin_mm),
              top: topOfBaseline(layout.header.student_extra.y_mm,
                layout.header.student_extra.font_pt),
              fontSize: fontPx(layout.header.student_extra.font_pt),
            }"
          >
            {{ layout.header.student_extra.text }}
          </div>

          <div
            v-for="(line, i) in (layout.header.notes.lines || [])"
            :key="`n${i}`" class="pvc__notes"
            :style="{
              left: px(layout.margin_mm),
              top: topOfBaseline(layout.header.notes.y_mm
                + i * layout.header.notes.line_h_mm,
                layout.header.notes.font_pt),
              fontSize: fontPx(layout.header.notes.font_pt),
              lineHeight: px(layout.header.notes.line_h_mm),
            }"
          >
            <MathText mode="text" :source="line" />
          </div>
          <div
            v-for="(ry, i) in (layout.header.rules || [])"
            :key="`r${i}`" class="pvc__rule"
            :style="{
              left: px(layout.margin_mm),
              top: px(ry),
              width: px(layout.usable_width_mm),
            }"
          />
        </template>

        <!-- ── 逐块画 ── -->
        <template v-for="(b, bi) in page.blocks" :key="bi">
          <!-- 分组标题 + 汇总 -->
          <template v-if="b.kind === 'stem'">
            <div
              v-for="(ln, i) in b.title_lines" :key="`st${i}`"
              class="pvc__sec"
              :style="{
                left: px(layout.margin_mm),
                top: topOfBaseline(b.y_mm + i * sectionLineH(b),
                  baseFont + 1.5),
                fontSize: fontPx(baseFont + 1.5),
              }"
            >
              <MathText mode="text" :source="ln" />
            </div>
          </template>

          <!-- 题干（全宽） -->
          <template v-else-if="b.kind === 'q_stems'">
            <div
              v-for="(ln, i) in b.stem_lines" :key="`q${i}`"
              class="pvc__stem"
              :style="{
                left: px(layout.margin_mm),
                top: topOfBaseline(b.y_mm + i * stemLineH(b), baseFont),
                fontSize: fontPx(baseFont),
                lineHeight: px(stemLineH(b)),
              }"
            >
              <MathText mode="text" :source="ln" />
            </div>
          </template>

          <!-- 选项：A. 内容，等宽列；无框 -->
          <template v-else-if="b.kind === 'choice_row'">
            <template v-for="(cell, ci) in b.option_cells" :key="`c${ci}`">
              <div
                v-for="(ln, li) in cell.lines" :key="`c${ci}-${li}`"
                class="pvc__opt"
                :style="{
                  left: px(layout.margin_mm + cell.x_mm),
                  top: topOfBaseline(b.y_mm + cell.y_mm
                    + li * stemLineH(b), b.option_font_pt || baseFont - 1.5),
                  fontSize: fontPx(b.option_font_pt || baseFont - 1.5),
                  lineHeight: px(stemLineH(b)),
                }"
              >
                <MathText mode="text" :source="ln" />
              </div>
            </template>
          </template>

          <!-- 主观题：题号 + 题干，无作答区 -->
          <template v-else-if="b.kind === 'subjective'">
            <div
              v-if="b.seq_label"
              class="pvc__subj-label"
              :style="{
                left: px(layout.margin_mm),
                top: topOfBaseline(b.y_mm + stemLineH(b) * 0.9, baseFont),
                fontSize: fontPx(baseFont),
              }"
            >
              {{ b.seq_label }}
            </div>
            <div
              v-for="(ln, i) in b.stem_lines" :key="`sb${i}`"
              class="pvc__stem"
              :style="{
                left: px(layout.margin_mm),
                top: topOfBaseline(
                  b.y_mm + (b.seq_label ? stemLineH(b) : 0)
                    + i * stemLineH(b), baseFont),
                fontSize: fontPx(baseFont),
                lineHeight: px(stemLineH(b)),
              }"
            >
              <MathText mode="text" :source="ln" />
            </div>
          </template>

          <!-- 配图 -->
          <template v-else-if="b.kind === 'figure'">
            <PaperFigure
              :figure="b" :scale="scale"
              :image-url="b.asset_id ? imageUrls[b.asset_id] : undefined"
              :caption-font-pt="baseFont - 1.5"
              :gap-mm="layout.figure_gap_mm"
              :caption-line-h-mm="b.caption_line_h_mm"
            />
          </template>

          <!-- 空白区 -->
          <div
            v-else-if="b.kind === 'blank'"
            class="pvc__blank"
            :style="{
              left: px(layout.margin_mm),
              top: px(b.y_mm),
              width: px(layout.usable_width_mm),
              height: px(b.height_mm),
              fontSize: fontPx(baseFont - 2),
            }"
          >
            空白区 {{ b.height_mm }}mm
          </div>
        </template>

        <!-- 页码 -->
        <div
          v-if="page.page_number"
          class="pvc__page-no"
          :style="{
            top: px(page.page_number.y_mm),
            textAlign: page.page_number.align,
            fontSize: fontPx(8),
          }"
        >
          {{ page.page_number.text }}
        </div>
      </div>
    </div>
    <el-empty
      v-else-if="!loading"
      description="尚未生成预览"
      :image-size="80"
    />
  </div>
</template>

<style scoped>
.pvc { height: 100%; overflow: auto; background: #eef0f3; }
.pvc__stack {
  padding: 16px; display: flex; flex-direction: column; align-items: center;
}
.pvc__page {
  position: relative;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.14);
  margin-bottom: 18px;
  flex: none;
  overflow: hidden;
  color: #111827;
  font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", sans-serif;
}
.pvc__title,
.pvc__subtitle {
  position: absolute; left: 0; width: 100%;
  text-align: center; white-space: nowrap;
}
.pvc__title { font-weight: 700; }
.pvc__subtitle { color: #6b7280; }
.pvc__line, .pvc__notes, .pvc__stem, .pvc__sec,
.pvc__subj-label, .pvc__opt, .pvc__student {
  position: absolute;
  white-space: pre;
}
.pvc__notes { color: #4b5563; }
.pvc__sec { font-weight: 700; }
.pvc__rule { position: absolute; border-top: 1px solid #9aa3ad; }
.pvc__underline {
  position: absolute;
  border-bottom: 1px solid #111827;
  height: 0;
}
.pvc__blank {
  position: absolute;
  border: 1px dashed #cbd5e1;
  color: #94a3b8;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  background: #fafafa;
}
.pvc__page-no {
  position: absolute;
  left: 0; width: 100%;
  color: #6b7280;
  padding: 0 12px;
  box-sizing: border-box;
}
</style>
