<script setup lang="ts">
/**
 * 试卷版式参数表单（`P1-62b` / 契约 v3）。
 *
 * ★★ 只负责**收集参数**，不计算任何几何、不估算分页。
 *
 * ★ v3 关键变化：
 *   · 排列是「**一道题的四个选项怎么摆**」：一行四列 / 两行两列 / 四行一列
 *   · 删除「全卷默认排列」「选项呈现」「主观题作答区」
 *   · 考生信息固定为 学号、座号、班级、姓名、得分
 *   · 逐题覆盖表只列**有选项的题**（单选 / 多选）
 */
import { computed } from 'vue'

import type {
  BlankBlock,
  PaperLayoutConfig,
} from '@/api/paper'

const props = defineProps<{
  /** 试卷名称（标题输入框的 placeholder） */
  paperTitle: string
  /** 全部卷面题（空白区插入位置用） */
  questionOptions: Array<{ seq: number; label: string; type: string }>
  /** 单选题 / 多选题（逐题选项覆盖表用；判断题不出现） */
  choiceQuestions: Array<{ seq: number; preview: string; type: string }>
}>()

const model = defineModel<PaperLayoutConfig>({ required: true })

const emit = defineEmits<{
  (e: 'interact', kind: 'input' | 'slider'): void
}>()

/** ★ 顺序固定：学号、座号、班级、姓名、得分 */
const STUDENT_FIELDS = ['学号', '座号', '班级', '姓名', '得分']

const OPTION_COLS_OPTIONS = [
  { value: '', label: '自动（4 → 2 → 1 逐题判定）' },
  { value: '4', label: '一行四列（A B C D）' },
  { value: '2', label: '两行两列（A B / C D）' },
  { value: '1', label: '四行一列（A / B / C / D）' },
]

function onAnyInput(e: Event): void {
  const el = e.target as HTMLElement | null
  emit('interact', el?.closest?.('.el-slider') ? 'slider' : 'input')
}

const overrides = computed(
  () => model.value.option_cols_by_seq ?? {},
)

/** 选项字号留空时跟随正文的当前值（提示文案用） */
const followOptionFont = computed(() =>
  Math.max((model.value.base_font_pt ?? 10.5) - 1.5, 6),
)

function overrideOf(seq: number): string {
  const v = overrides.value[String(seq)]
  return v === undefined || v === null ? '' : String(v)
}

function setOverride(seq: number, value: string): void {
  const next = { ...(model.value.option_cols_by_seq ?? {}) }
  if (!value) {
    delete next[String(seq)]
  } else {
    next[String(seq)] = Number(value) as 4 | 2 | 1
  }
  model.value.option_cols_by_seq = next
}

const blanks = computed<BlankBlock[]>(
  () => model.value.blank_blocks ?? [],
)

function addBlank(): void {
  if (blanks.value.length >= 50) return
  const next = [...blanks.value, {
    id: `blank-${Date.now()}-${blanks.value.length + 1}`,
    after_seq: 0,
    height_mm: 30,
  }]
  model.value.blank_blocks = next
}

function removeBlank(index: number): void {
  model.value.blank_blocks = blanks.value.filter((_, i) => i !== index)
}

const blankPositions = computed(() => [
  { value: 0, label: '卷首（标题下方）' },
  ...props.questionOptions.map((q) => ({
    value: q.seq,
    label: `第 ${q.seq} 题后${q.label ? `（${q.label.slice(0, 12)}）` : ''}`,
  })),
])
</script>

<template>
  <div class="plf" @input.capture="onAnyInput">
    <!-- ══════════ 纸张 ══════════ -->
    <div class="plf__section">纸张</div>
    <el-form size="small" label-position="top" class="plf__form">
      <div class="plf__grid2">
        <el-form-item label="纸张大小">
          <el-select v-model="model.page_size" style="width: 100%">
            <el-option label="A4（210×297mm）" value="A4" />
            <el-option label="A3（297×420mm）" value="A3" />
            <el-option label="A5（148×210mm）" value="A5" />
            <el-option label="B5（176×250mm）" value="B5" />
          </el-select>
        </el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="model.orientation">
            <el-radio-button value="PORTRAIT">纵向</el-radio-button>
            <el-radio-button value="LANDSCAPE">横向</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </div>

      <el-form-item label="页边距（四边统一，5–40mm）">
        <el-slider
          v-model="model.margin_mm" :min="5" :max="40" :step="1"
          show-input size="small"
        />
      </el-form-item>
      <el-form-item label="行距（1.0–3.0 倍）">
        <el-slider
          v-model="model.line_spacing" :min="1" :max="3" :step="0.1"
          show-input size="small"
        />
      </el-form-item>
      <div class="plf__grid2">
        <el-form-item label="正文字号（pt）">
          <el-input-number
            v-model="model.base_font_pt" :min="6" :max="24" :step="0.5"
            controls-position="right" style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="标题字号（pt）">
          <el-input-number
            v-model="model.title_font_pt" :min="10" :max="36" :step="1"
            controls-position="right" style="width: 100%"
          />
        </el-form-item>
      </div>
    </el-form>

    <!-- ══════════ 卷头 ══════════ -->
    <div class="plf__section">卷头</div>
    <el-form size="small" label-position="top" class="plf__form">
      <el-form-item label="卷面标题（留空则用试卷名称；只改卷面，不改试卷名）">
        <el-input
          v-model="model.title_text" maxlength="120" show-word-limit
          :placeholder="paperTitle || '试卷标题'"
        >
          <template #prepend>
            <el-switch v-model="model.show_title" size="small" />
          </template>
        </el-input>
        <div class="plf__hint">左侧开关 = 是否印标题</div>
      </el-form-item>

      <el-form-item label="考生信息（一行两端对齐，标签后画下划线）">
        <el-checkbox-group v-model="model.student_fields">
          <el-checkbox v-for="f in STUDENT_FIELDS" :key="f" :value="f">
            {{ f }}
          </el-checkbox>
        </el-checkbox-group>
        <div class="plf__hint">顺序固定：学号、座号、班级、姓名、得分</div>
      </el-form-item>
      <el-form-item label="追加文本（≤60 字，单独一行）">
        <el-input
          v-model="model.student_line_extra" maxlength="60"
          placeholder="如：考场：____"
        />
      </el-form-item>

      <el-form-item label="注意事项（印在考生信息下方）">
        <el-input
          v-model="model.notes_text" type="textarea" :rows="3"
          maxlength="1000" show-word-limit
          placeholder="每行一条，如：&#10;1. 请用黑色签字笔作答。&#10;2. 答案写在答题卡上。"
        />
        <div class="plf__hint">
          开关：<el-switch v-model="model.show_notes" size="small" />
          （与文本非空同时成立才印）
        </div>
      </el-form-item>

      <el-form-item label="页码（固定「第 X 页 / 共 Y 页」）">
        <div class="plf__row">
          <el-switch v-model="model.show_page_number" />
          <el-radio-group
            v-model="model.page_number_align"
            :disabled="!model.show_page_number"
          >
            <el-radio-button value="center">居中</el-radio-button>
            <el-radio-button value="right">靠右</el-radio-button>
          </el-radio-group>
        </div>
      </el-form-item>
    </el-form>

    <!-- ══════════ 选项与分值 ══════════ -->
    <div class="plf__section">选项与分值</div>
    <el-form size="small" label-position="top" class="plf__form">
      <el-form-item label="逐题选项排列（只列单选题 / 多选题）">
        <el-table
          v-if="choiceQuestions.length" :data="choiceQuestions"
          size="small" max-height="300" border
        >
          <el-table-column prop="seq" label="题号" width="58" />
          <el-table-column label="题干（前 20 字）" min-width="120">
            <template #default="{ row }">
              <span :title="row.preview">{{ row.preview }}</span>
            </template>
          </el-table-column>
          <el-table-column label="选项排列" width="176">
            <template #default="{ row }">
              <el-select
                :model-value="overrideOf(row.seq)" size="small"
                @update:model-value="(v: string) => setOverride(row.seq, v)"
              >
                <el-option
                  v-for="o in OPTION_COLS_OPTIONS" :key="o.value"
                  :label="o.label" :value="o.value"
                />
              </el-select>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="plf__hint">这份卷子没有单选题或多选题。</div>
        <div class="plf__hint">
          自动判定：能一行四列就四列，否则两行两列，再否则四行一列；
          仍超宽按列宽折行（不截断）。手动指定优先，不再自动降级。
        </div>
      </el-form-item>

      <el-form-item label="选项字号（pt）">
        <el-input-number
          :model-value="model.option_font_pt ?? undefined"
          :min="6" :max="24" :step="0.5" controls-position="right"
          style="width: 100%"
          :placeholder="`跟随正文（当前 ${followOptionFont} pt）`"
          @update:model-value="
            (v: number | undefined) => (model.option_font_pt = v ?? null)
          "
        />
        <div class="plf__hint">
          留空 = 跟随正文（当前 {{ followOptionFont }} pt）；调大会同步变宽、
          变高并影响 4→2→1 自动判定
        </div>
      </el-form-item>

      <div class="plf__grid2">
        <el-form-item label="题号前印（　　）">
          <el-switch v-model="model.answer_bracket" />
        </el-form-item>
        <el-form-item label="印分值">
          <el-switch v-model="model.show_score" />
          <div class="plf__hint">
            在分组标题后显示本大题题数与总分
          </div>
        </el-form-item>
      </div>
    </el-form>

    <!-- ══════════ 空白区 ══════════ -->
    <div class="plf__section">空白区</div>
    <div class="plf__blanks">
      <div v-for="(b, i) in blanks" :key="b.id" class="plf__blank">
        <el-select v-model="b.after_seq" size="small" style="flex: 1">
          <el-option
            v-for="p in blankPositions" :key="p.value"
            :label="p.label" :value="p.value"
          />
        </el-select>
        <el-input-number
          v-model="b.height_mm" :min="5" :max="200" :step="5"
          controls-position="right" size="small" style="width: 110px"
        />
        <span class="plf__unit">mm</span>
        <el-button size="small" text type="danger" @click="removeBlank(i)">
          删除
        </el-button>
      </div>
      <el-button
        size="small" plain :disabled="blanks.length >= 50" @click="addBlank"
      >
        + 新增空白区（最多 50 个）
      </el-button>
      <div class="plf__hint">
        空白区按「第 N 题后」定位；放不下时整块移到下一页。
      </div>
    </div>
  </div>
</template>

<style scoped>
.plf { padding-bottom: 8px; }
.plf__section {
  margin: 14px 0 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--zf-text-secondary, #4b5563);
  padding-bottom: 4px;
  border-bottom: 1px solid var(--zf-border, #e5e7eb);
}
.plf__section:first-child { margin-top: 0; }
.plf__form { padding: 0 2px; }
.plf__grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 10px;
}
.plf__row { display: flex; align-items: center; gap: 10px; width: 100%; }
.plf__hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--zf-text-tertiary, #9ca3af);
  margin-top: 2px;
}
.plf__blanks { display: flex; flex-direction: column; gap: 6px; }
.plf__blank { display: flex; align-items: center; gap: 6px; }
.plf__unit { font-size: 12px; color: var(--zf-text-tertiary, #9ca3af); }
</style>
