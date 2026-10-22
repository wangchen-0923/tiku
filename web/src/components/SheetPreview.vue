<script setup lang="ts">
/**
 * 答题卡预览：**只读 layout_json**，前端不算任何坐标。
 *
 * ★ 与 PDF 同源：后端 `preview/` 返回的 layout_json 就是打印用的坐标。
 *   这里只把它按 `mm → px` 等比画出来，用于设计器实时预览。
 */
import { computed } from 'vue'

import type { LayoutJson } from '@/api/sheet'

const props = defineProps<{
  layout: LayoutJson | null
  pageNo: number
}>()

const WIDTH = 640

const pageW = computed(() => props.layout?.page_width_mm ?? 210)
const pageH = computed(() => props.layout?.page_height_mm ?? 297)
const scale = computed(() => WIDTH / pageW.value)
const page = computed(() => {
  if (!props.layout) return null
  return props.layout.pages.find((p) => p.page_no === props.pageNo)
    ?? props.layout.pages[0] ?? null
})

function px(mm: number): string {
  return `${mm * scale.value}px`
}

const canvasH = computed(() => pageH.value * scale.value)

function box(x: number, y: number, w: number, h: number) {
  return {
    left: px(x), top: px(y), width: px(w), height: px(h),
  }
}

function at(x: number, y: number) {
  return { left: px(x), top: px(y) }
}

function textStyle(size = 9) {
  return { fontSize: `${Math.max(5, size * 0.3528 * scale.value)}px` }
}

interface HandwriteRow {
  label: string
  x_mm: number
  y_mm: number
  line_x1_mm: number
  line_x2_mm: number
  line_y_mm: number
  label_top_mm: number
  font_pt: number
}

function handwriteRows(header: NonNullable<
  NonNullable<LayoutJson['pages'][number]>['header']
>): HandwriteRow[] {
  const hw = header.handwrite as unknown
  const raw = (Array.isArray(hw)
    ? hw
    : (hw as { rows?: unknown[] } | undefined)?.rows ?? []) as Array<{
      label?: string; x_mm: number; y_mm: number;
      line_x1_mm: number; line_x2_mm: number;
      line_y_mm?: number; label_top_mm?: number; font_pt?: number
    }>
  return raw.map((f) => ({
    label: f.label ?? '',
    x_mm: f.x_mm,
    y_mm: f.y_mm,
    line_x1_mm: f.line_x1_mm,
    line_x2_mm: f.line_x2_mm,
    line_y_mm: f.line_y_mm ?? f.y_mm + 1.2,
    label_top_mm: f.label_top_mm ?? f.y_mm - 2.5,
    font_pt: f.font_pt ?? 10,
  }))
}

function sectionTitles(page: NonNullable<LayoutJson['pages'][number]>) {
  const t = (page as unknown as { section_titles?: unknown[] })
  return (t.section_titles ?? page.section_headers ?? []) as Array<{
    text: string; legend?: string; legend_x_mm?: number
    x_mm: number; y_mm: number;
    top_mm?: number; font_pt: number
    lines?: Array<{
      text: string; top_mm: number; baseline_mm: number; width_mm: number
    }>
  }>
}

function essayCells(e: {
  cell_mm: number; cell_gap_mm?: number; cols: number;
  start_row: number; end_row: number; total_chars: number
}) {
  const out: Array<{ left: string; top: string; size: string }> = []
  const gap = e.cell_gap_mm ?? 0
  const pitch = e.cell_mm + gap
  for (let r = e.start_row; r <= e.end_row; r++) {
    for (let c = 0; c < e.cols; c++) {
      const idx = r * e.cols + c
      if (e.total_chars && idx >= e.total_chars) break
      out.push({
        left: px(c * pitch),
        top: px((r - e.start_row) * pitch),
        size: px(e.cell_mm),
      })
    }
  }
  return out
}
</script>

<template>
  <div v-if="!layout || !page" class="zf-empty" style="padding: 24px">
    暂无预览
  </div>
  <div
    v-else
    class="sheet"
    :style="{ width: `${WIDTH}px`, height: `${canvasH}px` }"
  >
    <!-- 双栏分界线：栏间距中线，正文顶到底 -->
    <div
      v-if="(page.columns ?? layout.columns ?? 1) === 2
        && page.divider_x_mm"
      class="column-divider"
      :style="{
        left: px(page.divider_x_mm),
        top: px(page.divider_y1_mm ?? layout.content_start_mm),
        height: px((page.divider_y2_mm
          ?? layout.content_start_mm + (layout.content_height_mm ?? 0))
          - (page.divider_y1_mm ?? layout.content_start_mm)),
      }"
    />

    <!-- 锚点 -->
    <div
      v-for="(a, k) in page.anchors"
      :key="`a-${k}`"
      class="anchor"
      :style="box(a.x_mm - (a.size_mm ?? 6) / 2,
                 a.y_mm - (a.size_mm ?? 6) / 2,
                 a.size_mm ?? 6, a.size_mm ?? 6)"
    />
    <!-- 页定位块 -->
    <div
      v-for="m in page.markers"
      :key="`m-${m.index}`"
      class="anchor"
      :style="box(m.x_mm - (m.size_mm ?? 4) / 2,
                 m.y_mm - (m.size_mm ?? 4) / 2,
                 m.size_mm ?? 4, m.size_mm ?? 4)"
    />

    <!-- 页眉 -->
    <template v-if="page.header">
      <template
        v-for="ln in (page.header.title?.lines?.length
          ? page.header.title.lines
          : (page.header.title
            ? [{ text: page.header.title.text,
                 top_mm: page.header.title.y_mm,
                 baseline_mm: page.header.title.baseline_mm
                   ?? page.header.title.y_mm,
                 width_mm: 0 }]
            : []))"
        :key="`title-${ln.top_mm}`"
      >
        <div
          class="title"
          :style="{
            ...at(page.header.title!.x_mm, ln.top_mm),
            ...textStyle(page.header.title!.font_pt),
            lineHeight: 1.2,
          }"
        >
          {{ ln.text }}
        </div>
      </template>
      <template v-for="(f, i) in handwriteRows(page.header)" :key="`h-${i}`">
        <div
          v-if="f.label"
          class="small"
          :style="at(f.x_mm, f.label_top_mm)"
        >
          {{ f.label }}：
        </div>
        <div
          class="underline"
          :style="{
            left: px(f.line_x1_mm),
            top: px(f.line_y_mm),
            width: px(f.line_x2_mm - f.line_x1_mm),
          }"
        />
      </template>
      <div
        v-if="page.header.notice"
        class="notice"
        :style="box(page.header.notice.x_mm, page.header.notice.y_mm,
                    page.header.notice.w_mm, page.header.notice.h_mm)"
      >
        <div
          v-for="(ln, i) in page.header.notice.lines"
          :key="`n-${i}`"
          class="notice-line"
          :style="{ top: px(ln.y_mm - page.header.notice.y_mm - 2),
                    left: px(2),
                    ...textStyle(page.header.notice.font_pt) }"
        >
          {{ ln.text }}
        </div>
      </div>
    </template>

    <!-- 考号区 -->
    <template v-if="page.student_no?.enabled">
      <div
        v-if="page.student_no.box"
        class="student-box"
        :style="box(page.student_no.box.x_mm, page.student_no.box.y_mm,
                    page.student_no.box.w_mm, page.student_no.box.h_mm)"
      />
      <div
        v-if="page.student_no.label"
        class="small"
        :style="at(page.student_no.label.x_mm,
                  page.student_no.label.y_mm - 2.5)"
      >
        {{ page.student_no.label.text }}
      </div>
      <div
        v-else
        class="small"
        :style="at(page.student_no.x_mm, page.student_no.y_mm - 2)"
      >
        考号
      </div>
      <template v-if="page.student_no.handwrite_box">
        <div
          v-for="hb in page.student_no.handwrite_box.boxes"
          :key="`sn-hw-${hb.digit_index}`"
          class="handwrite-box"
          :style="box(hb.x_mm, hb.y_mm, hb.w_mm, hb.h_mm)"
        />
      </template>
      <div
        v-for="pos in page.student_no.positions"
        :key="`sn-${pos.digit_index}`"
      >
        <div
          v-for="b in pos.bubbles"
          :key="`sn-${pos.digit_index}-${b.value}`"
          class="bubble"
          :style="box(b.x_mm - b.w_mm / 2, b.y_mm - b.h_mm / 2,
                     b.w_mm, b.h_mm)"
        >
          <span class="sn-digit">{{ b.value }}</span>
        </div>
      </div>
      <div
        v-if="page.student_no.aux_anchor"
        class="anchor"
        :style="box(page.student_no.aux_anchor.x_mm - 2,
                   page.student_no.aux_anchor.y_mm - 2, 4, 4)"
      />
      <div
        v-for="(g, i) in page.student_no.group_lines"
        :key="`g-${i}`"
        class="vline"
        :style="{ left: px(g.x_mm), top: px(g.y1_mm),
                  height: px(g.y2_mm - g.y1_mm) }"
      />
    </template>

    <!-- 大题边框 / 标题：只读 section_boxes / section_titles -->
    <div
      v-for="(b, i) in (page.section_boxes ?? [])"
      :key="`box-${i}`"
      class="section-box"
      :style="box(b.x_mm, b.y_mm, b.w_mm, b.h_mm)"
    />
    <template v-for="(h, i) in sectionTitles(page)" :key="`sh-${i}`">
      <template v-if="h.lines?.length">
        <div
          v-for="(ln, li) in h.lines"
          :key="`sh-${i}-${li}`"
          class="section-title"
          :style="{ ...at(h.x_mm, ln.top_mm), ...textStyle(h.font_pt) }"
        >
          {{ ln.text }}
        </div>
      </template>
      <div
        v-else
        class="section-title"
        :style="{ ...at(h.x_mm, h.top_mm ?? h.y_mm - 3),
                  ...textStyle(h.font_pt) }"
      >
        {{ h.text }}
      </div>
      <div
        v-if="h.legend"
        class="section-title section-legend"
        :style="{ ...at(h.legend_x_mm ?? h.x_mm + 42,
                        h.top_mm ?? h.y_mm - 3),
                  ...textStyle(h.font_pt - 1.5) }"
      >
        {{ h.legend }}
      </div>
    </template>

    <!-- 客观题 -->
    <template v-for="q in page.objectives" :key="`q-${q.seq}`">
      <div
        v-if="q.column_marker || q.row_marker"
        class="anchor"
        :style="box((q.column_marker ?? q.row_marker)!.x_mm - 1.5,
                   (q.column_marker ?? q.row_marker)!.y_mm - 1.5, 3, 3)"
      />
      <div class="qno" :style="{ ...at(q.x_mm - 2, q.y_mm - 2) }">
        {{ q.seq }}
      </div>
      <div
        v-for="o in q.options"
        :key="`o-${q.seq}-${o.label}`"
        class="bubble"
        :style="box(o.x_mm - o.w_mm / 2, o.y_mm - o.h_mm / 2,
                   o.w_mm, o.h_mm)"
      >
        <span class="opt">{{ o.label }}</span>
      </div>
    </template>

    <!-- 填空题 -->
    <template v-for="f in page.fill_blanks" :key="`f-${f.seq}`">
      <template v-for="(ln, li) in f.lines" :key="`fl-${f.seq}-${li}`">
        <div
          v-if="ln.show_qno"
          class="qno"
          :style="at(ln.qno_x_mm, ln.y_mm - 3)"
        >
          {{ ln.qno }}
        </div>
        <div
          v-for="(b, bi) in ln.blanks"
          :key="`fb-${f.seq}-${li}-${bi}`"
          class="underline"
          :style="{ left: px(b.x1_mm), top: px(b.y_mm),
                    width: px(b.x2_mm - b.x1_mm) }"
        />
      </template>
    </template>

    <!-- 解答题：与 PDF 同一份 style / line_ys -->
    <template v-for="s in page.subjective" :key="`s-${s.seq}`">
      <div
        v-if="s.style !== 'BLANK'"
        class="answer-box"
        :class="{ 'answer-box--dashed': s.style === 'DASHED' }"
        :style="box(s.x_mm, s.y_mm, s.w_mm, s.h_mm)"
      />
      <template v-for="(sb, i) in s.sub_blocks" :key="`sb-${s.seq}-${i}`">
        <div
          v-if="sb.label"
          class="small"
          :style="at(sb.x_mm + 1, sb.y_mm)"
        >
          {{ sb.label }}
        </div>
        <template v-if="s.style === 'SOLID' || s.style === 'DASHED'">
          <div
            v-for="(ly, li) in sb.line_ys"
            :key="`sl-${s.seq}-${i}-${li}`"
            class="hline"
            :class="{ 'hline--dashed': s.style === 'DASHED' }"
            :style="{ left: px(sb.x_mm), top: px(ly),
                      width: px(sb.w_mm) }"
          />
        </template>
      </template>
    </template>

    <!-- 作文格 -->
    <template v-for="e in page.essays" :key="`e-${e.seq}`">
      <div
        v-for="(cell, i) in essayCells(e)"
        :key="`ec-${e.seq}-${i}`"
        class="essay-cell"
        :style="{ left: `${parseFloat(cell.left) + parseFloat(px(e.x_mm))}px`,
                  top: `${parseFloat(cell.top) + parseFloat(px(e.y_mm))}px`,
                  width: cell.size, height: cell.size }"
      />
      <div
        v-if="e.min_marker"
        class="tiny"
        :style="at(e.min_marker.x_mm - 6, e.min_marker.y_mm - 2)"
      >
        {{ e.min_marker.text }}
      </div>
    </template>

    <!-- 页脚：与 PDF 同坐标 -->
    <div
      class="footer"
      :style="{ top: px(page.footer.y_mm),
                left: px(page.footer.left_x_mm ?? pageW / 2) }"
    >
      {{ page.footer.left }}
    </div>
    <div
      class="footer footer--right"
      :style="{ top: px(page.footer.y_mm),
                left: 'auto',
                right: px(pageW - (page.footer.right_x_mm
                  ?? pageW - layout.margin_mm)) }"
    >
      {{ page.footer.right }}
    </div>
  </div>
</template>

<style scoped>
.sheet {
  position: relative;
  background: #fff;
  border: 1px solid var(--zf-border);
  overflow: hidden;
  flex: none;
}
.column-divider {
  position: absolute;
  width: 0;
  border-left: 0.3mm solid #999;
  box-sizing: border-box;
}
.anchor {
  position: absolute;
  background: #111;
}
.bubble {
  position: absolute;
  border: 1px solid #333;
  border-radius: 1px;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
}
.opt,
.digit {
  color: #888;
  font-size: 6px;
  line-height: 1;
}
.title {
  position: absolute;
  transform: translateX(-50%);
  font-weight: 700;
  font-size: 14px;
  white-space: nowrap;
}
.small {
  position: absolute;
  font-size: 9px;
  white-space: nowrap;
}
.tiny {
  font-size: 7px;
  color: #666;
}
.underline {
  position: absolute;
  border-bottom: 1px solid #333;
  height: 0;
}
.hline {
  position: absolute;
  border-top: 1px solid #bbb;
  height: 0;
}
.vline {
  position: absolute;
  border-left: 1px solid #999;
  width: 0;
}
.notice {
  position: absolute;
  border: 0.4mm solid #999;
  background: #fafafa;
  overflow: hidden;
  box-sizing: border-box;
}
.notice-line {
  position: absolute;
  white-space: nowrap;
  color: #333;
}
.section-title {
  position: absolute;
  font-weight: 600;
  white-space: nowrap;
}
.section-box {
  position: absolute;
  border: 0.4mm solid #999;
  box-sizing: border-box;
}
.legend {
  font-weight: 400;
  color: #888;
  margin-left: 8px;
  font-size: 9px;
}
.section-legend {
  font-weight: 400;
  color: #888;
}
.qno {
  position: absolute;
  font-size: 9px;
  color: #333;
}
.answer-box {
  position: absolute;
  border: 1px solid #999;
}
.answer-box--dashed {
  border-style: dashed;
}
.hline--dashed {
  border-top-style: dashed;
}
.student-box {
  position: absolute;
  border: 0.4mm solid #999;
  box-sizing: border-box;
}
.handwrite-box {
  position: absolute;
  border: 1px solid #333;
  box-sizing: border-box;
}
.sn-digit {
  color: #888;
  font-size: 5px;
  line-height: 1;
}
.essay-cell {
  position: absolute;
  border: 1px solid #ccc;
  box-sizing: border-box;
}
.footer {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  font-size: 8px;
  color: #888;
}
.footer--right {
  left: auto;
  right: 12px;
  transform: none;
}
</style>
