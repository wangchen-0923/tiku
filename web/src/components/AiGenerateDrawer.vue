<script setup lang="ts">
/**
 * AI 生题抽屉。
 *
 * ★★ 这个组件存在的前提，是后端 `POST /ai/questions/generate/` **真的会
 *   往题库里落草稿题**（U28c 之前它只回一个 `wired: false` 的受理凭据 ——
 *   界面显示「已受理，正在生成」，而题库里一道题都没有）。
 *
 * ═══════════════════════════════════════════════════════════════
 *  三条必须在界面上**如实**表达的约束
 * ═══════════════════════════════════════════════════════════════
 *
 *  ① **生成的题只能是草稿**。`will_create_status` 恒 `DRAFT`，
 *     要老师审核通过才能进试卷。所以结果文案里绝不写「已添加到题库」，
 *     写「已生成 N 道草稿」。用词差异不是啰嗦：
 *     老师以为进库了、去组卷找不到，会认为功能坏了。
 *
 *  ② **知识库不足时拒绝生成是正确行为**，不是故障。
 *     这条走 `BusinessBranch`（HTTP 200 + `code != OK`），
 *     界面要给「去补知识库」的引导，**不能弹红框「生题失败」** ——
 *     重试一百次也过不了闸门，而老师会一直点。
 *
 *  ③ **queued 模式下 `created_count` 恒为 0**，绝不拿它判成败。
 *     判据只能是 `queued`：true 就轮询 `/api/v1/tasks/{run_id}/`，
 *     false 就直接用 `created`。把 0 当「失败」会让老师以为没生成，
 *     在任务其实正在跑的时候再点一次 —— 于是生成两批、付两次钱。
 *
 * ═══════════════════════════════════════════════════════════════
 *  与「新建题目」抽屉的分工
 * ═══════════════════════════════════════════════════════════════
 *
 *   新建：老师自己写题干与答案，逐字可控。
 *   生题：老师给「知识点 + 题型 + 数量」，模型写，老师**审**。
 *   两者不合并 —— 合并后会出现一个到处是 `if (isAi)` 的表单。
 */
import { ElMessage } from 'element-plus'
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import * as Ai from '@/api/ai'
import type {
  GenerateFailure,
  GeneratedQuestion,
  GateRejectedData,
} from '@/api/ai'
import { ApiError, BusinessBranch } from '@/api/http'
import { listCourseNodes, listCourses } from '@/api/knowledge'
import type { Course, KnowledgeNodeRow } from '@/api/knowledge'
import * as Q from '@/api/questionbank'
import * as Tasks from '@/api/tasks'
import type { TaskRunDetail } from '@/api/tasks'
import MathText from './MathText.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  /** 有新题落库（父组件据此刷新列表） */
  generated: []
  /** 打开某道题的详情（父组件持详情抽屉） */
  'open-question': [id: number]
  /** 请求父组件切到「草稿」视图（草稿不参与组卷，默认列表看不到） */
  'show-drafts': []
}>()

const router = useRouter()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

type Phase = 'form' | 'running' | 'done'

const courses = ref<Course[]>([])
const nodes = ref<KnowledgeNodeRow[]>([])
const availability = ref<Ai.GenerateAvailability | null>(null)
const availabilityErr = ref('')

const courseId = ref<number | undefined>(undefined)
const nodeIds = ref<number[]>([])

const form = reactive({
  questionType: 'SINGLE_CHOICE',
  count: 3,
  /** `null` = 由模型判断（不传 `difficulty`） */
  difficulty: null as number | null,
  stemHint: '',
})

const phase = ref<Phase>('form')
const submitting = ref(false)
/** inline 形态的结果（`queued: false`） */
const inlineResult = ref<Ai.GenerateResult | null>(null)
/** queued 形态的进度（轮询填充） */
const task = ref<TaskRunDetail | null>(null)
/** 闸门拒绝 / 不可用（业务分支，不是错误） */
const gate = ref<GateRejectedData | null>(null)
const errorMsg = ref('')
const timedOut = ref(false)

const NODE_LIMIT_HINT = 10

// ══════════════════════════════════════════════════
//  加载：课程 → 知识点 → 可用性
// ══════════════════════════════════════════════════

async function loadCourses(): Promise<void> {
  try {
    const c = await listCourses()
    courses.value = c.items ?? []
    if (!courseId.value) courseId.value = courses.value[0]?.id
  } catch {
    courses.value = []
  }
}

async function loadNodes(): Promise<void> {
  nodes.value = []
  if (!courseId.value) return
  try {
    // ★ `listCourseNodes` 返回**裸数组**（不是分页信封）
    nodes.value = await listCourseNodes(courseId.value)
  } catch {
    nodes.value = []
  }
}

/**
 * 查可用性。
 *
 * ★ 这个请求是**给老师看状态的**，不是给按钮做开关的：
 *   `DISABLED` 时按钮置灰并显示后端的 `reason` 原话；
 *   `LIMITED` 时知识点下拉里白名单外的项不可选。
 *
 * ★ 请求失败时**不能当作可用**：那会让老师填完表单再被拒。
 *   失败 → 保持 `availability = null` → 界面上给一句「无法确认可用性」，
 *   但仍允许提交（后端是唯一判据，前端不该抢着拦）。
 */
async function loadAvailability(): Promise<void> {
  availabilityErr.value = ''
  if (!courseId.value) return
  try {
    availability.value = await Ai.getGenerateAvailability({
      courseId: courseId.value,
    })
  } catch (e) {
    availability.value = null
    availabilityErr.value = e instanceof Error ? e.message : '无法确认生题可用性'
  }
}

/**
 * LIMITED 模式的白名单（`null` = 不限制）。
 *
 * ★★ 后端把 `allowed_node_ids` 截断到 200 个。截断时**绝不能**用它判
 *   「这个知识点不可生题」——那会把 200 个之外本来允许的知识点全部置灰，
 *   而老师看到的是「明明有内容却选不了」，无从理解。
 *   所以两种 `null` 都表示「不禁用」：不限 / 无法判断。
 */
const allowedSet = computed<Set<number> | null>(() => {
  const a = availability.value
  if (!a || a.mode !== 'LIMITED') return null
  if (a.allowed_node_count > a.allowed_node_ids.length) return null
  return new Set(a.allowed_node_ids)
})

function nodeSelectable(n: KnowledgeNodeRow): boolean {
  const s = allowedSet.value
  return s === null ? true : s.has(n.id)
}

const disabledReason = computed(() => {
  const a = availability.value
  if (!a) return ''
  return a.mode === 'DISABLED' ? a.reason || '知识库内容不足，AI 生题暂不可用' : ''
})

// ★ 打开抽屉时初始化。**每次打开都重新查可用性** ——
//   老师可能在两次打开之间刚补了几条知识库内容。
watch(visible, async (v) => {
  if (!v) return
  resetState()
  await loadCourses()
  await loadNodes()
  await loadAvailability()
})

function resetState(): void {
  stopPolling()
  phase.value = 'form'
  inlineResult.value = null
  task.value = null
  gate.value = null
  errorMsg.value = ''
  timedOut.value = false
  nodeIds.value = []
  form.questionType = 'SINGLE_CHOICE'
  form.count = 3
  form.difficulty = null
  form.stemHint = ''
}

watch(courseId, async () => {
  nodeIds.value = []
  await loadNodes()
  await loadAvailability()
})

// ══════════════════════════════════════════════════
//  提交
// ══════════════════════════════════════════════════

/** 把 `BusinessBranch.data` 读成闸门结构（字段可能缺，全部可选）。 */
function asGate(d: unknown): GateRejectedData {
  const o = (d ?? {}) as Record<string, unknown>
  return {
    ok: false,
    code: String(o.code ?? ''),
    message: String(o.message ?? ''),
    suggestion: String(o.suggestion ?? ''),
    kb_hit_count: numOr(o.kb_hit_count),
    kb_min_entries: numOr(o.kb_min_entries),
    kb_entry_ids: Array.isArray(o.kb_entry_ids)
      ? (o.kb_entry_ids as number[])
      : undefined,
    knowledge_node_id: numOr(o.knowledge_node_id),
    coverage_ratio: typeof o.coverage_ratio === 'number' ? o.coverage_ratio : undefined,
    mode: (o.mode as Ai.GenerateMode | undefined) ?? undefined,
    reason: o.reason === undefined ? undefined : String(o.reason),
    total_nodes: numOr(o.total_nodes),
    covered_nodes: numOr(o.covered_nodes),
  }
}

function numOr(v: unknown): number | undefined {
  return typeof v === 'number' ? v : undefined
}

async function submit(): Promise<void> {
  if (!nodeIds.value.length) {
    ElMessage.warning('请先选择至少一个知识点')
    return
  }
  if (nodeIds.value.length > NODE_LIMIT_HINT) {
    ElMessage.warning(`一次最多选 ${NODE_LIMIT_HINT} 个知识点`)
    return
  }

  errorMsg.value = ''
  gate.value = null
  inlineResult.value = null
  task.value = null
  timedOut.value = false
  submitting.value = true

  try {
    const r = await Ai.generateQuestions({
      knowledgeNodeIds: nodeIds.value,
      questionType: form.questionType,
      count: form.count,
      difficulty: form.difficulty,
      stemHint: form.stemHint.trim(),
    })
    inlineResult.value = r

    if (r.queued && r.run_id) {
      // ★ 异步：此时 `created` 恒空，**不能**判成败，只能开始轮询。
      phase.value = 'running'
      startPolling(r.run_id)
    } else {
      phase.value = 'done'
      announce(r)
    }
  } catch (e) {
    if (e instanceof BusinessBranch) {
      // ★ 闸门拒绝 / 可用性不足 —— 可预期的结果，给引导不给红框。
      gate.value = asGate(e.data)
      phase.value = 'form'
      return
    }
    if (e instanceof ApiError && e.status === 402) {
      // ★★ 余额不足。**不要自己拼数字** —— 后端 `message` 里已经写了
      //   「本次需 0.7636 积分，当前余额 0.0000 积分」，
      //   而 `data` 里是 `{balance, required, unit}`。
      //
      //   我第一版按 `data.shortfall` 读，字段根本不存在 →
      //   `short` 恒 undefined → 那句「（还差 X 元）」永远不显示。
      //   这类错**不报错**，只是界面上少一句话，而少的那句正是
      //   老师判断「该充多少」的依据。所以直接用后端原话 + 一句去哪充。
      errorMsg.value = e.message
        + '。充值请联系学校平台账户，或在「设置 → 积分与凭证」查看余额明细。'
    } else {
      errorMsg.value = e instanceof Error ? e.message : '生题请求失败'
    }
  } finally {
    submitting.value = false
  }
}

/** 生成结束后的一句提示（两种形态共用）。 */
function announce(r: Ai.GenerateResult): void {
  if (r.created_count > 0) {
    emit('generated')
    if (r.dispatch_fallback) {
      ElMessage.warning({
        message: '队列不可用，已改为同步生成（本次耗时较长）',
        duration: 5000,
      })
    }
    return
  }
  if (r.failed_count > 0) emit('generated')
}

// ══════════════════════════════════════════════════
//  轮询
// ══════════════════════════════════════════════════

const POLL_INTERVAL = 1500
/** 5 分钟。超过就停，**不无限转圈** —— 转圈本身不提供任何信息。 */
const POLL_MAX_TRIES = 200

let timer: number | null = null
let tries = 0

function stopPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
  tries = 0
}

function startPolling(runId: number): void {
  stopPolling()
  timer = window.setInterval(() => {
    void tick(runId)
  }, POLL_INTERVAL)
}

async function tick(runId: number): Promise<void> {
  tries += 1
  if (tries > POLL_MAX_TRIES) {
    stopPolling()
    timedOut.value = true
    phase.value = 'done'
    return
  }
  try {
    const t = await Tasks.getTaskRun(runId)
    task.value = t
    if (t.is_terminal) {
      stopPolling()
      phase.value = 'done'
      // ★ `PARTIAL` 也要刷新列表：成功的那几道是真的落库了。
      emit('generated')
    }
  } catch {
    // ★ 单次失败**不停轮询**：网络抖动一秒钟很常见，
    //   停掉会让老师以为任务失败了。连续失败由上面的次数上限兜住。
  }
}

onUnmounted(stopPolling)

// ══════════════════════════════════════════════════
//  结果视图（两种形态归一）
// ══════════════════════════════════════════════════

interface Outcome {
  created: GeneratedQuestion[]
  failed: GenerateFailure[]
  createdCount: number
  failedCount: number
  cost: number
  provider: string
  model: string
  /** 整体失败的原因（一道都没落库时） */
  error: string
  queued: boolean
  /** 需要额外说明的话（回退同步 / worker 丢失） */
  note: string
}

function fromTask(t: TaskRunDetail | null): Outcome {
  const r = (t?.result ?? {}) as Record<string, unknown>
  const created = Array.isArray(r.created)
    ? (r.created as GeneratedQuestion[])
    : []
  const failed = Array.isArray(r.failed) ? (r.failed as GenerateFailure[]) : []
  const ids = Array.isArray(r.question_ids) ? (r.question_ids as number[]) : []
  // ★ 老任务（`created` 字段引入之前落的）只有 `question_ids`，没有题干摘要。
  //   兜底成「有 id 没题干」的条目，界面上只显示 id —— 不假装知道题干。
  const fill: GeneratedQuestion[] = created.length
    ? created
    : ids.map((id) => ({
        question_id: id,
        stem: '',
        difficulty: null,
        confidence: null,
        has_explanation: false,
      }))
  return {
    created: fill,
    failed,
    createdCount: Number(r.created_count ?? fill.length ?? 0),
    failedCount: Number(r.failed_count ?? failed.length ?? 0),
    cost: Number(r.cost_yuan ?? 0),
    provider: String(r.provider ?? ''),
    model: String(r.model ?? ''),
    error: String(t?.error ?? ''),
    queued: true,
    note: '',
  }
}

const outcome = computed<Outcome | null>(() => {
  if (phase.value !== 'done') return null
  const r = inlineResult.value
  if (r && !r.queued) {
    return {
      created: r.created,
      failed: r.failed,
      createdCount: r.created_count,
      failedCount: r.failed_count,
      cost: r.cost_yuan,
      provider: r.provider,
      model: r.model,
      error: r.status === 'FAILED' ? (r.failed[0]?.error ?? '') : '',
      queued: false,
      note: r.dispatch_fallback
        ? `队列不可用，已改为同步生成：${r.dispatch_fallback}`
        : '',
    }
  }
  return fromTask(task.value)
})

/** 整体结论的语气。★ `PARTIAL` 一律 warning，绝不染绿。 */
const verdict = computed(() => {
  const o = outcome.value
  if (!o) return { tone: 'info' as const, title: '', detail: '' }
  if (o.createdCount === 0) {
    return {
      tone: 'error' as const,
      title: '一道都没有生成',
      detail: o.error || '模型没有返回可落库的题目，可调整题型或知识点后重试',
    }
  }
  if (o.failedCount > 0) {
    return {
      tone: 'warning' as const,
      title: `生成 ${o.createdCount} 道，${o.failedCount} 道未成`,
      detail: '未成的条目见下方明细；成功的那几道已作为草稿入库',
    }
  }
  return {
    tone: 'success' as const,
    title: `已生成 ${o.createdCount} 道草稿`,
    detail: '草稿不参与组卷，需审核通过后才会出现在「可用于组卷」的题里',
  }
})

const progress = computed(() => {
  const t = task.value
  if (!t) return 0
  return Math.max(0, Math.min(100, t.percent ?? 0))
})

const taskStatusText = computed(() => {
  const t = task.value
  // ★ 还没拿到第一条进度时，说的必须是「已受理」而不是「正在提交」——
  //   请求早已返回 202，此刻欠的是**进度**，不是提交。
  //   写成「正在提交」会让人以为请求还没发出去，去重点一次。
  if (!t) return '已受理，正在读取进度…'
  return `${Tasks.TASK_STATUS_LABEL[t.status] ?? t.status}（${t.done}/${t.total}）`
})

function openQuestion(id: number): void {
  emit('open-question', id)
}

function goKnowledge(): void {
  void router.push({ name: 'Knowledge' })
  visible.value = false
}

function rebuild(): void {
  stopPolling()
  phase.value = 'form'
  inlineResult.value = null
  task.value = null
  gate.value = null
  errorMsg.value = ''
  timedOut.value = false
}

/** 置信度 → 百分比（`null` 时不给数字，显示「—」） */
function confPct(v: number | null | undefined): string {
  if (typeof v !== 'number') return '—'
  return `${Math.round(v * 100)}%`
}

/**
 * 成本文案。
 *
 * ★★ 不能直接 `toFixed(2)`：一次生题的真实成本常在 0.0006 元这个量级，
 *   `toFixed(2)` 会显示「0.00 元」—— 老师看到「消耗 0 元」会以为没计费，
 *   而下个月账单里确实有这一笔。
 *
 *   「不到 0.01 元」既不编数字，也不假装是 0。
 */
function costText(cost: number): string {
  if (cost > 0 && cost < 0.01) return '不到 0.01 元'
  return `${cost.toFixed(2)} 元`
}
</script>

<template>
  <el-drawer
    v-model="visible"
    class="ai-gen-drawer"
    title="AI 生题"
    size="620px"
    :close-on-click-modal="!submitting"
  >
    <!-- ══════ 可用性 ══════ -->
    <el-alert
      v-if="disabledReason"
      type="warning"
      :closable="false"
      show-icon
      :title="disabledReason"
      style="margin-bottom: 12px"
    >
      <div class="ai-hint">
        知识库内容不足时，模型只能凭印象编题 —— 那些题看着像题，
        但答案与教材对不上。所以这里直接不让生成，不是功能坏了。
        <div style="margin-top: 6px">
          当前覆盖率
          <b>{{ ((availability?.kb_coverage ?? 0) * 100).toFixed(0) }}%</b>，
          需要达到
          <b>{{ ((availability?.thresholds.kb_coverage_disabled ?? 0) * 100).toFixed(0) }}%</b>
          以上才能开启。
        </div>
      </div>
    </el-alert>

    <el-alert
      v-else-if="availability?.mode === 'LIMITED'"
      type="info"
      :closable="false"
      show-icon
      :title="availability.reason || '仅部分知识点支持生题'"
      style="margin-bottom: 12px"
    >
      <div class="ai-hint">
        只有「有定义/定理类内容」的知识点才能生题 ——
        只有例题的知识点，模型只能照着例题编，编出来的与原题高度雷同。
        下拉框里不可选的就是这类。
      </div>
    </el-alert>

    <el-alert
      v-else-if="availabilityErr"
      type="info"
      :closable="false"
      show-icon
      title="无法确认生题可用性，仍可尝试提交"
      :description="availabilityErr"
      style="margin-bottom: 12px"
    />

    <!-- ══════ 闸门拒绝（业务分支，不是错误）══════ -->
    <el-alert
      v-if="gate"
      type="warning"
      :closable="false"
      show-icon
      :title="gate.message || '知识库依据不足，未生成'"
      style="margin-bottom: 12px"
    >
      <div class="ai-hint">
        <div v-if="gate.kb_min_entries !== undefined">
          当前命中 <b>{{ gate.kb_hit_count ?? 0 }}</b> 条知识库依据，
          至少需要 <b>{{ gate.kb_min_entries }}</b> 条。
        </div>
        <div>
          ★ 这不是失败：没有依据生成的题一定是编的。
          给这个知识点补两条教材内容（定义、定理、公式），再回来点生成即可。
        </div>
        <div class="ai-acts">
          <el-button size="small" type="primary" plain @click="goKnowledge">
            去知识体系补内容
          </el-button>
        </div>
      </div>
    </el-alert>

    <!-- ══════ 表单 ══════ -->
    <el-form
      v-if="phase === 'form'"
      label-width="92px"
      :disabled="submitting || !!disabledReason"
    >
      <el-form-item label="课程">
        <el-select v-model="courseId" style="width: 100%" placeholder="选择课程">
          <el-option
            v-for="c in courses"
            :key="c.id"
            :label="`${c.name}（${c.code}）`"
            :value="c.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="知识点" required>
        <el-select
          v-model="nodeIds"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择知识点（有知识库内容的才能选）"
          style="width: 100%"
        >
          <el-option
            v-for="n in nodes"
            :key="n.id"
            :label="`${n.code} ${n.name}`"
            :value="n.id"
            :disabled="!nodeSelectable(n)"
          />
        </el-select>
        <div class="ai-hint ai-hint--tight">
          一次最多 {{ NODE_LIMIT_HINT }} 个。多个知识点会合并成一次生成，
          题目会挂在这些知识点上。
        </div>
      </el-form-item>

      <el-form-item label="题型">
        <el-select v-model="form.questionType" style="width: 100%">
          <el-option
            v-for="t in Q.QUESTION_TYPE_OPTIONS"
            :key="t.value"
            :label="t.label"
            :value="t.value"
          />
        </el-select>
        <div
          v-if="!Q.isAutoGradable(form.questionType)"
          class="ai-hint ai-hint--tight"
        >
          ★ 该题型不能自动批改，生成后需要人工判分。
        </div>
      </el-form-item>

      <el-form-item label="数量">
        <el-input-number v-model="form.count" :min="1" :max="10" />
        <span class="ai-hint ai-hint--inline">
          1–10 道。每道要重复送一遍知识库正文，数量越多越慢也越贵。
        </span>
      </el-form-item>

      <el-form-item label="难度">
        <el-select
          v-model="form.difficulty"
          style="width: 100%"
          clearable
          placeholder="由 AI 判断"
        >
          <el-option
            v-for="d in Q.DIFFICULTY_CHOICES"
            :key="d.value"
            :label="d.label"
            :value="d.value"
          />
        </el-select>
        <div class="ai-hint ai-hint--tight">
          留空则由模型按知识点自行判断。这里填的值只作为<b>建议</b>，
          最终难度仍按「人工标注 &gt; AI 估算 &gt; 统计推断」取值。
        </div>
      </el-form-item>

      <el-form-item label="题目要求">
        <el-input
          v-model="form.stemHint"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
          placeholder="可选。例如：结合车床加工的实际场景，考螺纹自锁条件"
        />
      </el-form-item>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="生成的是草稿，需审核后才能组卷"
        style="margin-bottom: 12px"
      />

      <el-alert
        v-if="errorMsg"
        type="error"
        :closable="false"
        show-icon
        title="提交失败"
        :description="errorMsg"
      />
    </el-form>

    <!-- ══════ 进行中 ══════ -->
    <div v-if="phase === 'running'" class="ai-running">
      <el-progress :percentage="progress" :stroke-width="12" />
      <div class="ai-running__line">
        {{ taskStatusText }}
      </div>
      <div class="ai-hint ai-hint--note">
        进度每 1.5 秒刷新一次。可以关掉这个抽屉去做别的事 ——
        任务在后台继续跑，回来再打开也能看到结果。
      </div>
    </div>

    <!-- ══════ 结果 ══════ -->
    <div v-if="phase === 'done'" class="ai-result">
      <el-alert
        :type="verdict.tone === 'error' ? 'error' : verdict.tone"
        :closable="false"
        show-icon
        :title="verdict.title"
        :description="verdict.detail"
      />

      <div v-if="outcome?.note" class="ai-hint ai-hint--note">
        {{ outcome.note }}
      </div>

      <div v-if="timedOut" class="ai-hint ai-hint--note">
        ★ 等待超过 5 分钟，已停止刷新。任务可能仍在后台执行 ——
        稍后到题库刷一下看有没有新草稿；若长时间没有，
        多半是 worker 未启动（任务会一直停在「排队中」）。
      </div>

      <!-- 成功的题：可点开看详情 -->
      <div v-if="outcome?.created.length" class="ai-list">
        <div class="ai-list__title">
          已生成的草稿（{{ outcome.createdCount }}）
        </div>
        <div
          v-for="(c, i) in outcome.created"
          :key="c.question_id"
          class="ai-item"
          @click="openQuestion(c.question_id)"
        >
          <span class="ai-item__no">{{ i + 1 }}</span>
          <span class="ai-item__id">#{{ c.question_id }}</span>
          <span class="ai-item__stem">
            <MathText
              mode="text"
              :source="c.stem || '（题干摘要未记录，点开查看）'"
            />
          </span>
          <el-tag size="small" type="info">草稿</el-tag>
          <el-tag v-if="c.has_explanation" size="small" type="success">
            有解析
          </el-tag>
          <span class="ai-item__conf zf-dim">置信 {{ confPct(c.confidence) }}</span>
        </div>
      </div>

      <!-- 失败的题：说清原因，重复的给出可点的 id -->
      <div v-if="outcome?.failed.length" class="ai-list">
        <div class="ai-list__title ai-list__title--warn">
          未生成（{{ outcome.failedCount }}）
        </div>
        <div
          v-for="(f, i) in outcome.failed.slice(0, 10)"
          :key="i"
          class="ai-item ai-item--fail"
        >
          <span class="ai-item__no">{{ f.index }}</span>
          <span class="ai-item__stem">{{ f.error }}</span>
          <el-button
            v-if="f.duplicate_id"
            size="small"
            text
            type="primary"
            @click="openQuestion(f.duplicate_id)"
          >
            查看重复的 #{{ f.duplicate_id }}
          </el-button>
        </div>
        <div v-if="outcome.failed.length > 10" class="ai-hint ai-hint--tight">
          还有 {{ outcome.failed.length - 10 }} 条未展示
        </div>
      </div>

      <div v-if="outcome" class="ai-meta zf-dim">
        <span v-if="outcome.provider">模型 {{ outcome.provider }}</span>
        <span v-if="outcome.model">/ {{ outcome.model }}</span>
        <span v-if="outcome.cost > 0">
          本次消耗 {{ costText(outcome.cost) }}
        </span>
        <span>本次要了 {{ form.count }} 道</span>
      </div>

      <div class="ai-acts">
        <el-button
          v-if="outcome?.createdCount"
          type="primary"
          plain
          @click="emit('show-drafts')"
        >
          在题库里看这些草稿
        </el-button>
        <el-button @click="rebuild">再生成一批</el-button>
      </div>
    </div>

    <template #footer>
      <div class="ai-footer">
        <el-button @click="visible = false">关闭</el-button>
        <el-button
          v-if="phase === 'form'"
          type="primary"
          :loading="submitting"
          :disabled="!nodeIds.length || !!disabledReason"
          @click="submit"
        >
          开始生成
        </el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.ai-hint {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
  color: var(--zf-text-secondary);
}
.ai-hint--tight {
  margin-top: 2px;
}
.ai-hint--inline {
  margin-left: var(--zf-space-3);
}
.ai-hint--note {
  margin-top: var(--zf-space-2);
  padding: 6px 8px;
  background: var(--zf-bg-subtle);
  border-radius: var(--zf-radius-sm);
}
.ai-acts {
  margin-top: var(--zf-space-3);
  display: flex;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}
.ai-running {
  padding: var(--zf-space-3) 0;
}
.ai-running__line {
  margin-top: var(--zf-space-2);
  font-size: var(--zf-text-sm);
}
.ai-result {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
}
.ai-list__title {
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  margin-bottom: var(--zf-space-2);
}
.ai-list__title--warn {
  color: var(--zf-danger);
}
.ai-item {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  padding: 6px 8px;
  border-radius: var(--zf-radius-sm);
  font-size: var(--zf-text-xs);
  cursor: pointer;
}
.ai-item:hover {
  background: var(--zf-bg-subtle);
}
.ai-item--fail {
  cursor: default;
  color: var(--zf-text-secondary);
}
.ai-item--fail:hover {
  background: transparent;
}
.ai-item__no {
  flex: 0 0 18px;
  color: var(--zf-text-secondary);
  font-variant-numeric: tabular-nums;
}
.ai-item__id {
  flex: 0 0 auto;
  font-family: var(--zf-font-mono);
  color: var(--zf-text-secondary);
}
.ai-item__stem {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ai-item__conf {
  flex: 0 0 auto;
}
.ai-meta {
  display: flex;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
  font-size: var(--zf-text-xs);
}
.ai-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--zf-space-2);
}
</style>
