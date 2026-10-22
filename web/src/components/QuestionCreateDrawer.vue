<script setup lang="ts">
/**
 * 新建题目抽屉。
 *
 * ★★ 与「编辑」的关键区别，也是这个组件存在的理由：
 *
 *     新建时**题型可改**（编辑时不可改）
 *     新建时必须选**课程**（编辑时课程不变）
 *     新建时知识点要从**整棵树**里选（编辑时只能动已有那几个）
 *     新建默认落 `DRAFT`，且默认开**三层去重**
 *
 *   所以不是「编辑表单加一个 id 判断」能覆盖的 —— 硬合在一起
 *   会长出一个到处是 `if (isNew)` 的组件。
 *
 * ★★ `check_duplicate` 默认 **true**：后端命中重复时返回
 *   **业务分支**（HTTP 200 + 非 OK 码）而不是 4xx，所以要用
 *   `BusinessBranch` 接住，让老师决定「仍然新建」还是「算了」。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { getCourseTree, flattenNodes, listCourses } from '@/api/knowledge'
import type { Course, TreeNode } from '@/api/knowledge'
import { BusinessBranch } from '@/api/http'
import * as Q from '@/api/questionbank'
import type { OptionInput } from '@/api/questionbank'
import MathInput from './MathInput.vue'
import MathToolbar from './MathToolbar.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  created: [id: number]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const courses = ref<Course[]>([])
const nodes = ref<TreeNode[]>([])
const saving = ref(false)
const error = ref('')

const form = reactive({
  course_id: undefined as number | undefined,
  task_id: undefined as number | undefined,
  question_type: 'SINGLE_CHOICE',
  stem_text: '',
  explanation: '',
  difficulty: null as number | null,
  node_ids: [] as number[],
  check_duplicate: true,
  options: [] as Array<OptionInput & { is_correct: boolean }>,
  tfAnswer: '' as '' | 'A' | 'B',
  blanks: [''],
})

const isChoice = computed(
  () =>
    form.question_type === 'SINGLE_CHOICE' ||
    form.question_type === 'MULTIPLE_CHOICE',
)
const isTrueFalse = computed(() => form.question_type === 'TRUE_FALSE')
const isFillBlank = computed(() => form.question_type === 'FILL_BLANK')
const isMulti = computed(() => form.question_type === 'MULTIPLE_CHOICE')
const needsAnswerStruct = computed(
  () => isChoice.value || isTrueFalse.value || isFillBlank.value,
)

/** 题型 → 重置答案区（切换题型时不能让上一个题型的答案残留） */
function resetAnswerArea(): void {
  form.options = []
  form.tfAnswer = ''
  form.blanks = ['']
  if (isChoice.value) {
    form.options = 'ABCD'.split('').map((l) => ({
      label: l,
      content_text: '',
      is_correct: false,
    }))
  } else if (isTrueFalse.value) {
    form.options = Q.TRUE_FALSE_OPTIONS.map((o) => ({
      ...o,
      is_correct: false,
    }))
  } else if (isFillBlank.value) {
    // 数题面里的空位，自动给出对应数量的输入框
    form.blanks = new Array(Q.countBlanks(form.stem_text)).fill('')
  }
}

watch(() => form.question_type, () => resetAnswerArea())
watch(
  () => form.stem_text,
  (s) => {
    // 填空题：题面空位数变了，跟随调整输入框数量
    if (!isFillBlank.value) return
    const n = Q.countBlanks(s)
    if (n > form.blanks.length) {
      while (form.blanks.length < n) form.blanks.push('')
    }
  },
)

function reset(): void {
  form.course_id = courses.value[0]?.id
  form.task_id = undefined
  form.question_type = 'SINGLE_CHOICE'
  form.stem_text = ''
  form.explanation = ''
  form.difficulty = null
  form.node_ids = []
  form.check_duplicate = true
  error.value = ''
  resetAnswerArea()
}

async function ensureLoaded(): Promise<void> {
  if (courses.value.length) return
  try {
    // ★ `listCourses()` 返回的是**分页信封**（`{items,total,...}`），
    //   不是裸数组 —— 直接当数组用会拿到 `undefined` 而不报错。
    const paged = await listCourses()
    courses.value = paged.items ?? []
    if (!form.course_id) form.course_id = courses.value[0]?.id
    if (form.course_id) {
      const tree = await getCourseTree(form.course_id)
      nodes.value = flattenNodes(tree)
    }
  } catch {
    /* 下拉拿不到不阻塞表单，保存时后端会报缺失 */
  }
}

onMounted(ensureLoaded)
watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      await ensureLoaded()
      reset()
    }
  },
)

/** 课程变了要换知识点树 */
async function onCourseChange(id: number): Promise<void> {
  form.node_ids = []
  form.task_id = undefined
  try {
    const tree = await getCourseTree(id)
    nodes.value = flattenNodes(tree)
  } catch {
    nodes.value = []
  }
}

function toggleCorrect(label: string): void {
  const o = form.options.find((x) => x.label === label)
  if (!o) return
  if (isMulti.value) o.is_correct = !o.is_correct
  else form.options.forEach((x) => (x.is_correct = x.label === label))
}

function addOption(): void {
  form.options.push({
    label: String.fromCharCode(65 + form.options.length),
    content_text: '',
    is_correct: false,
  })
}

function removeOption(label: string): void {
  form.options = form.options.filter((o) => o.label !== label)
  form.options.forEach((o, i) => (o.label = String.fromCharCode(65 + i)))
}

function buildAnswer(): Record<string, unknown> {
  // ★ 判断题的 value 是**布尔值**（不是 "A"/"B"）
  if (isTrueFalse.value) return { value: Q.tfLabelToValue(form.tfAnswer) }
  if (isFillBlank.value) {
    return {
      blanks: form.blanks.map((t, i) => ({
        index: i + 1,
        accepted: [t.trim()].filter(Boolean),
      })),
    }
  }
  // ★ 单选与多选**共用 `value` 键**（多选是数组）
  const correct = form.options.filter((o) => o.is_correct).map((o) => o.label)
  return Q.choiceAnswer(correct, isMulti.value)
}

function validate(): string {
  if (!form.course_id) return '请选择课程'
  if (!form.stem_text.trim()) return '题干不能为空'
  if (isTrueFalse.value && !form.tfAnswer) return '请选择判断题的标准答案'
  if (isChoice.value) {
    if (form.options.some((o) => !o.content_text.trim())) {
      return '选项内容不能为空'
    }
    if (!form.options.some((o) => o.is_correct)) return '请勾选正确答案'
  }
  if (isFillBlank.value && !form.blanks.some((b) => b.trim())) {
    return '至少填一个空的答案'
  }
  return ''
}

async function submit(forceNoDupCheck = false): Promise<void> {
  const bad = validate()
  if (bad) {
    error.value = bad
    ElMessage.warning(bad)
    return
  }
  const body: Q.CreateQuestionBody = {
    course_id: form.course_id as number,
    question_type: form.question_type,
    stem_text: form.stem_text.trim(),
    answer: buildAnswer(),
    explanation: form.explanation,
    difficulty: form.difficulty,
    task_id: form.task_id ?? null,
    node_ids: form.node_ids,
    source_type: 'MANUAL',
    check_duplicate: forceNoDupCheck ? false : form.check_duplicate,
  }
  if (isChoice.value) {
    body.options = form.options.map((o) => ({
      label: o.label,
      content_text: o.content_text,
      is_correct: o.is_correct,
    }))
  } else if (isTrueFalse.value) {
    body.options = Q.TRUE_FALSE_OPTIONS
  }

  saving.value = true
  error.value = ''
  try {
    const d = await Q.createQuestion(body)
    ElMessage.success(`已创建（草稿）#${d.id}`)
    visible.value = false
    emit('created', d.id)
  } catch (e) {
    // ★★ 命中重复是**业务分支**（HTTP 200 + 非 OK 码），不是 4xx。
    //   所以要用 Branch 接住，并让老师决定「仍然新建」。
    if (e instanceof BusinessBranch) {
      saving.value = false
      const hits = (e.data as { similar?: unknown[] } | undefined)?.similar
      const extra = Array.isArray(hits) && hits.length
        ? `\n系统找到 ${hits.length} 道相似题。`
        : ''
      try {
        await ElMessageBox.confirm(
          `疑似重复：${e.message}${extra}\n\n仍然新建吗？`,
          '三层去重提示',
          { confirmButtonText: '仍然新建', cancelButtonText: '算了' },
        )
      } catch {
        return
      }
      await submit(true)
      return
    }
    error.value = e instanceof Error ? e.message : '创建失败'
    ElMessage.error(error.value)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-drawer v-model="visible" :size="720" title="新建题目" destroy-on-close>
    <div class="qc">
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="error"
        style="margin-bottom: 12px"
      />

      <!-- ★ 公式工具条只放这一条，作用于「光标所在的输入框」 -->
      <div class="qc__toolbar">
        <MathToolbar />
      </div>

      <el-form label-width="88px" label-position="left">
        <el-form-item label="课程" required>
          <el-select
            v-model="form.course_id"
            placeholder="选择课程"
            style="width: 240px"
            @change="onCourseChange"
          >
            <el-option
              v-for="c in courses"
              :key="c.id"
              :label="c.name"
              :value="c.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="题型" required>
          <el-radio-group v-model="form.question_type">
            <el-radio
              v-for="t in Q.QUESTION_TYPE_OPTIONS"
              :key="t.value"
              :value="t.value"
            >
              {{ t.label }}
            </el-radio>
          </el-radio-group>
          <span class="zf-dim qc__hint">
            ★ 只有「单项/多项选择、判断、填空」能自动批改
          </span>
        </el-form-item>

        <el-form-item label="题干" required>
          <!-- ★ 公式用 `$…$` 定界，直接写在题干里；支持选中后包成公式 -->
          <MathInput
            v-model="form.stem_text"
            label="题干"
            :rows="5"
            placeholder="题干。公式用 $…$ 包起来（如 $\frac{1}{2}$）；填空题的空位用 8 个下划线 ________ 标记"
          />
        </el-form-item>

        <!-- 选择题 / 判断题的选项 -->
        <el-form-item v-if="isChoice || isTrueFalse" label="选项">
          <div class="qc__rows">
            <div v-for="o in form.options" :key="o.label" class="qc__row">
              <el-checkbox
                :model-value="o.is_correct"
                @change="toggleCorrect(o.label)"
              >
                {{ o.label }}
              </el-checkbox>
              <MathInput
                v-model="o.content_text"
                :label="`选项 ${o.label}`"
                compact
                :rows="1"
                :disabled="isTrueFalse"
                placeholder="选项内容"
                style="flex: 1"
              />
              <el-button
                v-if="isChoice"
                size="small"
                text
                type="danger"
                :disabled="form.options.length <= 2"
                @click="removeOption(o.label)"
              >
                删
              </el-button>
            </div>
            <el-button v-if="isChoice" size="small" @click="addOption">
              加一个选项
            </el-button>
          </div>
        </el-form-item>

        <el-form-item v-if="isTrueFalse" label="标准答案" required>
          <el-radio-group
            :model-value="form.tfAnswer"
            @update:model-value="(v: string | number | boolean | undefined) => {
              form.tfAnswer = String(v) as 'A' | 'B'
              form.options.forEach((o) => (o.is_correct = o.label === String(v)))
            }"
          >
            <el-radio value="A">正确（A）</el-radio>
            <el-radio value="B">错误（B）</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="isFillBlank" label="标准答案" required>
          <div class="qc__rows">
            <div v-for="(_, i) in form.blanks" :key="i" class="qc__row">
              <span class="zf-dim" style="width: 54px">第 {{ i + 1 }} 空</span>
              <!-- ★ 这里**故意**不做公式输入：填空答案要跟学生手打的字符串
                   比对，写 `$30^\circ$` 会让学生打「30°」永远判错。
                   需要带公式的答案请放题干或解析里。 -->
              <el-input
                v-model="form.blanks[i]"
                size="small"
                placeholder="该空的正确答案（纯文本，不要写公式）"
                style="flex: 1"
              />
            </div>
          </div>
        </el-form-item>

        <el-form-item v-if="isChoice || isTrueFalse" label="题面选项">
          <span class="zf-dim qc__hint">
            ★ 选择题的 A/B/C/D 正文是否也要写进题干，取决于本校
            排印习惯；系统判分只看上面的「选项」与勾选的正确项。
          </span>
        </el-form-item>

        <el-form-item v-if="!needsAnswerStruct" label="参考答案">
          <MathInput
            v-model="form.explanation"
            label="参考答案"
            :rows="3"
            placeholder="主观题（简答/计算/作图/论述）不自动批改，此内容供教师线下对照"
          />
        </el-form-item>

        <el-form-item label="解析">
          <MathInput v-model="form.explanation" label="解析" :rows="3" />
        </el-form-item>

        <el-form-item label="难度">
          <!-- ★ 人工难度是 IntegerField，只能整数 1–5（0.5 会被静默取整） -->
          <el-select
            v-model="form.difficulty"
            clearable
            placeholder="未标注（沿用默认值）"
            style="width: 200px"
          >
            <el-option
              v-for="c in Q.DIFFICULTY_CHOICES"
              :key="c.value"
              :label="c.label"
              :value="c.value"
            />
          </el-select>
          <span class="zf-dim qc__hint" style="margin-left: 8px">
            ★ 不填则沿用默认值 3.00 —— 全库都是 3.00 会让个性化练习选不出题
          </span>
        </el-form-item>

        <el-form-item label="知识点">
          <el-select
            v-model="form.node_ids"
            multiple
            filterable
            placeholder="选择知识点"
            style="width: 100%"
          >
            <el-option
              v-for="n in nodes"
              :key="n.id"
              :label="`${n.code ?? ''} ${n.name}`"
              :value="n.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="去重检查">
          <el-switch v-model="form.check_duplicate" />
          <span class="zf-dim qc__hint" style="margin-left: 8px">
            开启后命中相似题会先问一次（三层去重：题干哈希 / SimHash / 向量）
          </span>
        </el-form-item>
      </el-form>

      <div class="qc__actions">
        <el-button type="primary" :loading="saving" @click="submit()">
          创建（草稿）
        </el-button>
        <el-button @click="visible = false">取消</el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.qc {
  padding-bottom: 24px;
}
/* ★ 工具条吸顶：题干在抽屉顶部，选项/解析要往下滚，
   不吸顶的话老师写到解析时得滚回去才能插公式 */
.qc__toolbar {
  position: sticky;
  top: 0;
  z-index: 3;
  padding-bottom: 10px;
  margin-bottom: 10px;
  background: var(--zf-bg-surface);
  border-bottom: 1px solid var(--zf-border-subtle);
}
.qc__hint {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
.qc__rows {
  width: 100%;
}
.qc__row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.qc__actions {
  display: flex;
  gap: 8px;
  padding-top: 14px;
  border-top: 1px solid var(--zf-border-subtle);
  position: sticky;
  bottom: 0;
  background: var(--zf-bg-surface);
}
</style>
