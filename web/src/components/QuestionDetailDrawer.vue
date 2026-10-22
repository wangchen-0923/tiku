<script setup lang="ts">
/**
 * 题目详情 / 编辑抽屉。
 *
 * ★★ 三个设计决定，都有理由：
 *
 *   1. **查看与编辑在同一个抽屉里切换**，而不是两个页面。
 *      老师的动作是「看到这题有个错 → 改掉」，
 *      中间跳一次页面就丢了上下文（列表筛选、滚动位置）。
 *
 *   2. **编辑时不直接绑 `detail` 对象**，而是复制到 `form`。
 *      直接绑会让「取消」失效 —— 改了字段但没保存，
 *      列表里已经显示新值，而后端还是旧值。
 *
 *   3. **`difficulty` 写 `difficulty_manual`，不写 `effective`**。
 *      `effective` 是按「人工 > AI > 统计」优先级算出来的**派生值**，
 *      改它会被下次重算覆盖。详见 `api/questionbank.ts` 的注释。
 *
 * ★★ 难度这一项特别重要：题库 1944 题的 `difficulty_effective`
 *   全是 3.00（默认值），而个性化练习按掌握度反推难度区间，
 *   于是永远选不出题。**这里是唯一能人工补标注的入口。**
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import * as Q from '@/api/questionbank'
import type { OptionInput, QuestionDetail } from '@/api/questionbank'
import MathText from './MathText.vue'

const props = defineProps<{
  modelValue: boolean
  questionId: number | null
  /** 课程 id：新建时必填（后端 `course_id` 是必填项） */
  courseId?: number
}>()

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  /** 保存/新建成功后通知父组件刷新列表 */
  saved: [id: number]
  deleted: [id: number]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

type Mode = 'view' | 'edit'
const mode = ref<Mode>('view')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
/**
 * ★ 自动撤回的**状态变化提示**（`P1-26b`）。
 *
 *   需求方要求「自动回退草稿、不用提醒」—— 弹窗去掉了，
 *   但**状态变化必须可见**：静默改状态会让「这道题动过」无处可见。
 *   所以在详情里显示一行「已从『已通过』撤回为草稿」。
 */
const revokedHint = ref('')
const detail = ref<QuestionDetail | null>(null)

/** AI 生成解析的状态与结果提示 */
const aiLoading = ref(false)
const aiMeta = ref('')
const aiHint = ref('')

/** 编辑表单（**独立于 detail**，取消时不污染页面） */
const form = reactive({
  stem_text: '',
  explanation: '',
  difficulty: null as number | null,
  task_id: null as number | null,
  node_ids: [] as number[],
  /** 选择题/判断题的选项 */
  options: [] as Array<OptionInput & { is_correct: boolean }>,
  /** 选择题答案（单选一个字母 / 多选多个） */
  choiceAnswer: [] as string[],
  /** 填空题逐空答案 */
  blanks: [''],
  /** 判断题答案 A(正确) / B(错误) */
  tfAnswer: '' as '' | 'A' | 'B',
})

const isChoice = computed(
  () =>
    detail.value?.type === 'SINGLE_CHOICE' ||
    detail.value?.type === 'MULTIPLE_CHOICE',
)
const isTrueFalse = computed(() => detail.value?.type === 'TRUE_FALSE')
const isFillBlank = computed(() => detail.value?.type === 'FILL_BLANK')
const isMulti = computed(() => detail.value?.type === 'MULTIPLE_CHOICE')

const typeLabel = computed(() =>
  Q.QUESTION_TYPE_OPTIONS.find((o) => o.value === detail.value?.type)?.label ??
  detail.value?.type ??
  '',
)

/** 把后端 `answer` 反解成表单字段 */
function answerToForm(d: QuestionDetail): void {
  const ans = (d.answer ?? {}) as Record<string, unknown>
  form.choiceAnswer = []
  form.blanks = ['']
  form.tfAnswer = ''

  if (d.type === 'TRUE_FALSE') {
    // ★ 判断题的 answer.value 是**布尔值**，不是 "A"/"B"
    form.tfAnswer = Q.tfValueToLabel(ans) as '' | 'A' | 'B'
  } else if (d.type === 'FILL_BLANK') {
    const bs = (ans.blanks ?? []) as Array<{ accepted?: string[] }>
    form.blanks = bs.length
      ? bs.map((b) => (b.accepted ?? [])[0] ?? '')
      : ['']
  } else {
    // ★ 单选与多选**共用 `value` 键**（多选是数组）
    form.choiceAnswer = Q.choiceLabels(ans)
  }
}

/** 表单 → 后端 `answer` 结构（★ 键名与类型都照实测契约） */
function formToAnswer(): Record<string, unknown> {
  if (isTrueFalse.value) {
    // ★ 布尔值；未选时给 null，由 save() 的校验拦住
    return { value: Q.tfLabelToValue(form.tfAnswer) }
  }
  if (isFillBlank.value) {
    return {
      blanks: form.blanks.map((t, i) => ({
        index: i + 1,
        accepted: [t.trim()].filter(Boolean),
      })),
    }
  }
  return Q.choiceAnswer(form.choiceAnswer, isMulti.value)
}

function loadIntoForm(d: QuestionDetail): void {
  // ★ 换了一道题就把上一条撤回提示清掉 —— 否则会挂在无关的题上
  revokedHint.value = ''
  form.stem_text = d.stem_text || ''
  form.explanation = d.explanation_text || ''
  form.difficulty = d.difficulty_manual
    ? Number(d.difficulty_manual)
    : // ★ 没人工标注过时**不预填 effective** —— 那会让老师在
      //   不知情的情况下「确认」了一个默认值 3.00。
      null
  form.task_id = d.task_id
  form.node_ids = (d.knowledge_links ?? []).map((k) => k.knowledge_node_id)
  form.options = (d.options ?? []).map((o) => ({
    label: o.label,
    content_text: o.content_text,
    is_correct: o.is_correct,
  }))
  if (!form.options.length && (isChoice.value || isTrueFalse.value)) {
    form.options = isTrueFalse.value
      ? Q.TRUE_FALSE_OPTIONS.map((o) => ({ ...o, is_correct: false }))
      : 'ABCD'.split('').map((l) => ({
          label: l,
          content_text: '',
          is_correct: false,
        }))
  }
  answerToForm(d)
}

async function load(id: number): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const d = await Q.getQuestion(id)
    detail.value = d
    loadIntoForm(d)
    mode.value = 'view'
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
    detail.value = null
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.questionId] as const,
  ([open, id]) => {
    if (open && id) void load(id)
    if (!open) {
      detail.value = null
      error.value = ''
      mode.value = 'view'
    }
  },
  { immediate: true },
)

function startEdit(): void {
  if (detail.value) loadIntoForm(detail.value)
  mode.value = 'edit'
}

function cancelEdit(): void {
  if (detail.value) loadIntoForm(detail.value)
  mode.value = 'view'
}

/**
 * AI 生成解析（`gen_explanation`）。
 *
 * ★★ 它只**填进编辑框**，不直接保存：
 *   解析是要展示给学生看的内容，老师必须先看一眼；
 *   直接落库还会覆盖掉老师可能已经改过的版本。
 *
 * ★ 后端会把「生成了但不该用」的三种情况放在 `blocked_reason` 里
 *   （空解析 / 模型自述依据不足 / 置信度低）—— 必须当提示显示，
 *   否则老师会以为生成失败了，或者更糟：以为可以用。
 */
async function aiGenerateExplanation(): Promise<void> {
  if (!detail.value) return
  aiLoading.value = true
  aiHint.value = ''
  aiMeta.value = ''
  try {
    const r = await Q.generateExplanation(detail.value.id, {
      apply: false,
      force: true,
    })
    if (r.blocked_reason) {
      aiHint.value =
        r.blocked_reason === 'INSUFFICIENT_EVIDENCE'
          ? '模型自述依据不足，未采用。请补充题干信息后重试。'
          : r.blocked_reason === 'LOW_CONFIDENCE'
            ? `置信度偏低（${r.confidence ?? 0}），未采用。建议人工撰写。`
            : '模型没有给出解析内容，请重试或人工撰写。'
      return
    }
    if (!r.explanation) {
      aiHint.value = `生成失败：${r.error || r.status || '未知原因'}`
      return
    }
    form.explanation = r.explanation
    const bits: string[] = []
    if (r.confidence != null) bits.push(`置信度 ${r.confidence}`)
    if (r.grounded) bits.push(`引用 ${(r.citations ?? []).length} 条知识库`)
    if (r.model) bits.push(r.model)
    aiMeta.value = bits.join(' · ')
    ElMessage.success('已生成，请检查后保存')
  } catch (e) {
    aiHint.value = `生成失败：${(e as Error).message}`
  } finally {
    aiLoading.value = false
  }
}

/** 保存（PATCH）。 */
async function save(): Promise<void> {
  if (!detail.value) return
  if (!form.stem_text.trim()) {
    ElMessage.warning('题干不能为空')
    return
  }
  if (isTrueFalse.value && !form.tfAnswer) {
    ElMessage.warning('请选择判断题的标准答案（正确 / 错误）')
    return
  }
  if (isFillBlank.value && !form.blanks.some((b) => b.trim())) {
    ElMessage.warning('至少填一个空的答案')
    return
  }
  if ((isChoice.value || isTrueFalse.value) && !form.choiceAnswer.length
      && !form.tfAnswer) {
    ElMessage.warning('请选择正确答案')
    return
  }

  const body: Q.UpdateQuestionBody = {
    stem_text: form.stem_text.trim(),
    answer: formToAnswer(),
    explanation: form.explanation,
    difficulty: form.difficulty,
    task_id: form.task_id,
    node_ids: form.node_ids,
  }
  if (isChoice.value) {
    body.options = form.options.map((o) => ({
      label: o.label,
      content_text: o.content_text,
      content_html: o.content_html,
      is_correct: o.is_correct,
    })) as OptionInput[]
  }

  saving.value = true
  try {
    // ★★★ 已审核的题：**自动撤回为草稿，不弹确认框**（`P1-26b`）。
    //
    //   需求方明确要求「点击编辑，自动回退为草稿，不用提醒」。
    //   这个要求**是安全的**，依据是：
    //
    //     `PaperItem.snapshot_json` 是**题干快照** ——
    //     已生成的试卷用的是快照，**改题库不影响它**。
    //     所以「改题把卷子改坏」这件事从机制上不会发生。
    //
    //   ★ 我原来加的那层弹窗是**基于错误假设的过度保护**
    //     （怕「老师以为卷子也变了」）——
    //     需求方指出这一点后已改掉。
    //
    //   ★ 但仍**保留状态可见性**：页面上显示一行「已从『已通过』
    //     撤回为草稿」。静默改状态会让「这道题动过」无处可见，
    //     而显式一行提示既快又不丢信息。
    let revokedFrom = ''
    if (detail.value.status === 'APPROVED') {
      const before = detail.value.status
      const rv = await Q.reviseQuestion(detail.value.id, '编辑前自动撤回为草稿')
      detail.value = rv
      revokedFrom = before
    }

    const d = await Q.updateQuestion(detail.value.id, body)
    detail.value = d
    loadIntoForm(d)
    mode.value = 'view'
    if (revokedFrom) {
      // ★ 一行状态变化，而不是弹窗
      ElMessage.success('已保存（状态已从「已通过」自动撤回为「草稿」）')
      revokedHint.value = '已从「已通过」撤回为草稿 —— 记得重新审核'
    } else {
      ElMessage.success('已保存')
    }
    emit('saved', d.id)
  } catch (e) {
    const err = e as { status?: number; code?: string; message?: string }
    // ★ 归档的题不能撤回（后端会拒）—— 这种情况仍要如实报错
    ElMessage.error(err.message ?? '保存失败')
  } finally {
    saving.value = false
  }
}

/** 审核（通过 / 驳回）。 */
async function review(approve: boolean): Promise<void> {
  if (!detail.value) return
  if (!approve) {
    try {
      const { value } = await ElMessageBox.prompt(
        '驳回原因（会记入审计）',
        '驳回题目',
        { confirmButtonText: '驳回', cancelButtonText: '取消' },
      )
      // ★ 审核入参是 `action: "APPROVE" | "REJECT"`，不是 `approve: bool`
      await Q.approveQuestion(detail.value.id, {
        action: 'REJECT',
        reason: value ?? '',
      })
      ElMessage.success('已驳回')
    } catch {
      return
    }
  } else {
    try {
      await Q.approveQuestion(detail.value.id, { action: 'APPROVE' })
      ElMessage.success('已通过')
    } catch (e) {
      ElMessage.error(e instanceof Error ? e.message : '审核失败')
      return
    }
  }
  await load(detail.value.id)
  emit('saved', detail.value.id)
}

/** 删除（软删，且后端会校验引用）。 */
async function remove(): Promise<void> {
  if (!detail.value) return
  const id = detail.value.id
  try {
    const { value } = await ElMessageBox.prompt(
      '删除原因（会记入审计）。\n' +
        '★ 已被试卷引用的题目会被后端拒绝 —— 那是有意的保护。',
      '删除题目',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        inputPlaceholder: '如：重复题 / 题干错误',
      },
    )
    await Q.deleteQuestion(id, value ?? '')
    ElMessage.success('已删除')
    visible.value = false
    emit('deleted', id)
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e instanceof Error ? e.message : '删除失败')
  }
}

/** 选择题：切换正确项（单选时互斥）。 */
function toggleCorrect(label: string): void {
  const opt = form.options.find((o) => o.label === label)
  if (!opt) return
  if (isMulti.value) {
    opt.is_correct = !opt.is_correct
  } else {
    form.options.forEach((o) => (o.is_correct = o.label === label))
  }
  syncChoiceFromOptions()
}

/** 把 `options[].is_correct` 同步成 `choiceAnswer`（单一数据源） */
function syncChoiceFromOptions(): void {
  form.choiceAnswer = form.options
    .filter((o) => o.is_correct)
    .map((o) => o.label)
}

function addOption(): void {
  const next = String.fromCharCode(65 + form.options.length)
  form.options.push({ label: next, content_text: '', is_correct: false })
}

function removeOption(label: string): void {
  form.options = form.options.filter((o) => o.label !== label)
  // 重排标签，保证连续（后端按 label 判答案，跳号会出错）
  form.options.forEach((o, i) => (o.label = String.fromCharCode(65 + i)))
  syncChoiceFromOptions()
}

function addBlank(): void {
  form.blanks.push('')
}

function removeBlank(i: number): void {
  if (form.blanks.length <= 1) return
  form.blanks.splice(i, 1)
}

/** 题干里的空位数（用于提示老师「题面有 N 个空」） */
const stemBlanks = computed(() => Q.countBlanks(form.stem_text))
</script>

<template>
  <el-drawer
    v-model="visible"
    :size="720"
    :title="mode === 'edit' ? `编辑题目 #${questionId}` : `题目 #${questionId}`"
    destroy-on-close
  >
    <div v-loading="loading" class="qd">
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="error"
      />

      <template v-if="detail && mode === 'view'">
        <!-- ── 头部状态 ── -->
        <div class="qd__bar">
          <el-tag size="small">{{ typeLabel }}</el-tag>
          <el-tag
            size="small"
            :type="Q.QUESTION_STATUS_TONE[detail.status] ?? 'info'"
          >
            {{ Q.QUESTION_STATUS_LABEL[detail.status] ?? detail.status }}
          </el-tag>
          <!-- ★ 自动撤回草稿的状态变化提示（`P1-26b`）：
               弹窗去掉了，但变化必须可见 —— 否则「这道题动过」无处可见 -->
          <el-tag
            v-if="revokedHint" size="small" type="warning" effect="plain"
          >
            {{ revokedHint }}
          </el-tag>
          <span class="zf-dim qd__meta">
            难度
            <b>{{ Number(detail.difficulty_effective).toFixed(2) }}</b>
            {{
              Q.DIFFICULTY_SOURCE_LABEL[detail.difficulty_source] ??
              detail.difficulty_source
            }}
            <template v-if="detail.difficulty_manual">
              ｜人工标注
              <b>{{ Number(detail.difficulty_manual).toFixed(2) }}</b>
            </template>
          </span>
          <span v-if="detail.has_figure" class="zf-dim qd__meta">
            含图 {{ detail.figure_count }} 张
          </span>
        </div>

        <!-- ★★ 提醒只在**真的没人工标注**时出现。
             判据用 `difficulty_manual`（有没有填过），
             **不**用 `difficulty_source` —— 后者在导入数据里曾经
             全部是 `MANUAL`（模型字段默认值），拿它判断会自相矛盾：
             状态栏写「人工标注」而警告写「没有人工标注」。
             ★ 已修导入脚本 + 数据，但判据仍应以「填没填过」为准。 -->
        <el-alert
          v-if="!detail.difficulty_manual"
          type="warning"
          :closable="false"
          show-icon
          title="此题没有人工难度标注"
          style="margin-bottom: 12px"
        >
          <template #default>
            <span style="font-size: var(--zf-text-xs)">
              当前生效难度
              {{ Number(detail.difficulty_effective).toFixed(2) }}
              是「{{
                Q.DIFFICULTY_SOURCE_LABEL[detail.difficulty_source] ??
                detail.difficulty_source
              }}」。
              全库都是默认值 3.00 会让个性化练习**选不出题** ——
              点「编辑」可手工补标注。
            </span>
          </template>
        </el-alert>

        <!-- ── 题干 ── -->
        <section class="qd__sec">
          <h4 class="qd__h">题干</h4>
          <MathText tag="div" class="qd__stem" :source="detail.stem_html || detail.stem_text" />
        </section>

        <!-- ── 选项 ── -->
        <section v-if="detail.options.length" class="qd__sec">
          <h4 class="qd__h">选项</h4>
          <div
            v-for="o in detail.options"
            :key="o.id"
            class="qd__opt"
            :class="{ 'qd__opt--ok': o.is_correct }"
          >
            <span class="qd__label">{{ o.label }}</span>
            <MathText :source="o.content_html || o.content_text" />
            <el-tag v-if="o.is_correct" size="small" type="success">正确</el-tag>
          </div>
        </section>

        <!-- ── 答案 ── -->
        <section class="qd__sec">
          <h4 class="qd__h">标准答案</h4>
          <div v-if="isTrueFalse" class="qd__ans">
            {{ Q.tfValueToLabel(detail.answer) === 'A' ? '正确' : '错误' }}
            <span class="zf-dim">
              （boolean {{ String((detail.answer ?? {}).value) }}）
            </span>
          </div>
          <div v-else-if="isFillBlank" class="qd__ans">
            <span
              v-for="(b, i) in (detail.answer?.blanks ?? []) as Array<{ accepted?: string[] }>"
              :key="i"
              class="qd__blank"
            >
              第 {{ i + 1 }} 空：
              <b>{{ (b.accepted ?? []).join(' / ') || '（空）' }}</b>
            </span>
          </div>
          <div v-else class="qd__ans">
            {{ detail.answer_text || JSON.stringify(detail.answer) }}
          </div>
        </section>

        <!-- ── 解析 ── -->
        <section v-if="detail.explanation_text || detail.explanation_html"
                 class="qd__sec">
          <h4 class="qd__h">解析</h4>
          <MathText
            tag="div"
            class="qd__stem"
            :source="detail.explanation_html || detail.explanation_text"
          />
        </section>

        <!-- ── 知识点 ── -->
        <section class="qd__sec">
          <h4 class="qd__h">知识点关联</h4>
          <div v-if="detail.knowledge_links.length" class="qd__links">
            <el-tag
              v-for="k in detail.knowledge_links"
              :key="k.knowledge_node_id"
              size="small"
              :type="k.is_primary ? 'primary' : 'info'"
            >
              {{ k.node_code }} {{ k.node_name }}
              <span v-if="k.is_primary">· 主</span>
            </el-tag>
          </div>
          <div v-else class="zf-dim">
            未关联知识点 —— 组卷时按知识点抽题会抽不到它
          </div>
        </section>

        <!-- ── 配图 ── -->
        <section v-if="detail.figures.length" class="qd__sec">
          <h4 class="qd__h">配图</h4>
          <div v-for="f in detail.figures" :key="f.id" class="qd__fig">
            <el-tag
              size="small"
              :type="f.review_status === 'CONFIRMED' ? 'success' : 'warning'"
            >
              {{ f.review_status }}
            </el-tag>
            <span class="zf-dim">
              asset #{{ f.asset_id }} · {{ f.attach_point || '未指定位置' }}
            </span>
          </div>
          <p class="zf-dim qd__hint">
            ★ 配图未确认（`PENDING`）会**阻止**该题用于组卷 ——
            这是三条不可绕过的保证之一。
          </p>
        </section>

        <!-- ── 操作 ── -->
        <div class="qd__actions">
          <el-button type="primary" @click="startEdit">编辑</el-button>
          <el-button @click="review(true)">通过</el-button>
          <el-button @click="review(false)">驳回</el-button>
          <el-button type="danger" plain @click="remove">删除</el-button>
        </div>
      </template>

      <!-- ══════════ 编辑 ══════════ -->
      <template v-else-if="detail && mode === 'edit'">
        <el-form label-width="88px" label-position="left">
          <el-form-item label="题型">
            <el-tag size="small">{{ typeLabel }}</el-tag>
            <span class="zf-dim qd__hint" style="margin-left: 8px">
              题型不可改 —— 换了题型就是另一道题
            </span>
          </el-form-item>

          <el-form-item label="题干" required>
            <el-input
              v-model="form.stem_text"
              type="textarea"
              :rows="5"
              placeholder="题干。填空题的空位用 8 个下划线 ________ 标记"
            />
            <span v-if="isFillBlank" class="zf-dim qd__hint">
              题面里有 {{ stemBlanks }} 个空位标记
            </span>
          </el-form-item>

          <!-- 选择题选项 -->
          <el-form-item v-if="isChoice || isTrueFalse" label="选项">
            <div class="qd__optsedit">
              <div
                v-for="o in form.options"
                :key="o.label"
                class="qd__optrow"
              >
                <el-checkbox
                  :model-value="o.is_correct"
                  @change="toggleCorrect(o.label)"
                >
                  {{ o.label }}
                </el-checkbox>
                <el-input
                  v-model="o.content_text"
                  size="small"
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
            <span class="zf-dim qd__hint">
              {{ isMulti ? '多选题：勾选所有正确项' : '单选题：勾选唯一正确项' }}
            </span>
          </el-form-item>

          <!-- 判断题答案 -->
          <el-form-item v-if="isTrueFalse" label="标准答案" required>
            <el-radio-group
              :model-value="form.tfAnswer"
              @update:model-value="(v: string | number | boolean | undefined) => {
                form.tfAnswer = String(v) as 'A' | 'B'
                form.choiceAnswer = [String(v)]
              }"
            >
              <el-radio value="A">正确（A）</el-radio>
              <el-radio value="B">错误（B）</el-radio>
            </el-radio-group>
          </el-form-item>

          <!-- 填空题答案 -->
          <el-form-item v-if="isFillBlank" label="标准答案" required>
            <div class="qd__blanksedit">
              <div v-for="(_, i) in form.blanks" :key="i" class="qd__optrow">
                <span class="zf-dim" style="width: 54px">第 {{ i + 1 }} 空</span>
                <el-input
                  v-model="form.blanks[i]"
                  size="small"
                  placeholder="该空的正确答案"
                  style="flex: 1"
                />
                <el-button
                  size="small"
                  text
                  type="danger"
                  :disabled="form.blanks.length <= 1"
                  @click="removeBlank(i)"
                >
                  删
                </el-button>
              </div>
              <el-button size="small" @click="addBlank">加一个空</el-button>
            </div>
            <span class="zf-dim qd__hint">
              ★ 顺序必须与题面里的空位一致 —— 判分按「第 i 空」对位
            </span>
          </el-form-item>

          <el-form-item label="解析">
            <div class="qd__explain">
              <el-input v-model="form.explanation" type="textarea" :rows="3" />
              <div class="qd__explain-acts">
                <!-- ★ 只「生成并填进编辑框」，不直接回写：
                     老师看过、改过再点保存 —— 解析是要给学生看的内容。 -->
                <el-button
                  size="small"
                  :loading="aiLoading"
                  @click="aiGenerateExplanation"
                >
                  AI 生成解析
                </el-button>
                <span v-if="aiMeta" class="zf-dim qd__ai-meta">{{ aiMeta }}</span>
              </div>
              <div v-if="aiHint" class="qd__ai-hint">{{ aiHint }}</div>
            </div>
          </el-form-item>

          <el-form-item label="难度">
            <!-- ★ 人工难度是 **IntegerField**（实测），只能填整数 1–5。
                 给 0.5 步长会被后端静默取整，那比不让填更糟。 -->
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
            <span class="zf-dim qd__hint" style="margin-left: 8px">
              ★ 人工标注优先级**最高**。留空表示不标注。
              全库都是默认值 3.00 会让个性化练习选不出题
            </span>
          </el-form-item>

          <el-form-item label="知识点">
            <el-select
              v-model="form.node_ids"
              multiple
              filterable
              placeholder="选择知识点（组卷按知识点抽题时必需）"
              style="width: 100%"
            >
              <el-option
                v-for="k in detail.knowledge_links"
                :key="k.knowledge_node_id"
                :label="`${k.node_code} ${k.node_name}`"
                :value="k.knowledge_node_id"
              />
            </el-select>
            <span class="zf-dim qd__hint">
              ★ 这里只能增删**已有**关联（后端 PATCH 收 `node_ids` 全量替换）。
              完整知识点选择器在「知识体系」页
            </span>
          </el-form-item>
        </el-form>

        <div class="qd__actions">
          <el-button type="primary" :loading="saving" @click="save">
            保存
          </el-button>
          <el-button @click="cancelEdit">取消</el-button>
        </div>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.qd {
  padding-bottom: 24px;
}
/* AI 生成解析：输入框 + 按钮 + 元信息竖排 */
.qd__explain {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.qd__explain-acts {
  display: flex;
  align-items: center;
  gap: 10px;
}
.qd__ai-meta {
  font-size: var(--zf-text-xs);
}
.qd__ai-hint {
  font-size: var(--zf-text-xs);
  color: var(--zf-warning, #b26a00);
}
.qd__bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.qd__meta {
  font-size: var(--zf-text-xs);
}
.qd__sec {
  margin-bottom: 18px;
}
.qd__h {
  margin: 0 0 6px;
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-secondary);
}
.qd__stem {
  font-size: var(--zf-text-base);
  line-height: var(--zf-leading-loose);
  word-break: break-word;
}
.qd__opt {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-sm);
}
.qd__opt--ok {
  border-color: var(--zf-success);
  background: var(--zf-success-bg);
}
.qd__label {
  flex: none;
  font-weight: var(--zf-weight-semibold);
  width: 18px;
}
.qd__ans {
  font-size: var(--zf-text-base);
}
.qd__blank {
  display: block;
  padding: 3px 0;
}
.qd__links {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.qd__fig {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
}
.qd__hint {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
.qd__actions {
  display: flex;
  gap: 8px;
  padding-top: 14px;
  border-top: 1px solid var(--zf-border-subtle);
  position: sticky;
  bottom: 0;
  background: var(--zf-bg-surface);
}
.qd__optsedit,
.qd__blanksedit {
  width: 100%;
}
.qd__optrow {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
</style>
