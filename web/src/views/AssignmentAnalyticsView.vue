<script setup lang="ts">
/**
 * 学情分析（`/assignments/:id/analytics`）。
 *
 * ★★ 这个页面最重要的设计约束（直接来自设计文档 11.2）：
 *
 *     **`mastery` 低不代表没掌握——可能只是样本不足。**
 *
 *   所以热力图不按「掌握度」上色，而是按 **(掌握度, 置信度)** 二元组：
 *
 *     置信度够  →  按掌握度分档上色（这是结论）
 *     置信度不足 →  灰格「数据不足」（这不是结论）
 *
 *   只按掌握度上色会让「只考过 1 次答错」的格子显示成深红，
 *   老师会以为全班这个知识点都很差。
 *
 *   `weak-nodes` 端点已经把这个判断做成了 `verdict`，
 *   所以薄弱点清单必须显示「要补练」/「要再测」而不是只显示掌握度。
 *
 * ★★ 第二个约束：**`calc_version` 必须显示**。
 *   换掌握度算法后新旧值不可比；不显示版本号，
 *   老师会拿两个口径的数据做对比。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import * as A from '@/api/analytics'
import * as G from '@/api/grading'
import { downloadBlob } from '@/api/sheet'
import { useAssignmentStore } from '@/stores/assignment'

const route = useRoute()
const router = useRouter()
const store = useAssignmentStore()

const id = computed(() => Number(route.params.id))

const heat = ref<A.HeatmapResult | null>(null)
const errors = ref<A.QuestionError[]>([])
const tags = ref<A.ErrorTagRow[]>([])
const dist = ref<A.Distribution | null>(null)
/**
 * 评分明细（只用来算卷面满分，不在界面上展示）。
 *
 * ★ 学情页不需要展示逐题评分——那是批改工作台的职责。
 *   但直方图的档宽需要卷面满分，而 `max_score` 只在评分记录里。
 */
const allScores = ref<G.ScoreRow[]>([])
/** ★ 试卷测量指标（难度 / 区分度 / 信度 / 内容效度） */
const measure = ref<A.MeasurementReport | null>(null)
const loading = ref(false)
const error = ref('')
const minAnswered = ref(2)

/** 选中的学生（默认第一个），用于「个人学情」面板 */
const picked = ref<number | undefined>(undefined)
const mastery = ref<A.MasteryResult | null>(null)
const weak = ref<A.WeakNodesResult | null>(null)
const detailLoading = ref(false)

/**
 * 直方图的满分基准。
 *
 * ★ 用**卷面满分**定档宽，不用 `max(scores)`：
 *   后者会把「全班最高 12 分（满分 100）」放大成满格，
 *   看起来像考得不错。
 *
 * ★ 卷面满分 = **每题满分之和**（每题对所有人相同）。
 *   不读 `paper.total_score`——试卷快照与卷面题在小改后可能不同步，
 *   而这里的数据就来自真实的评分记录，自洽。
 */
const fullScore = computed(() => {
  const seen = new Map<number, number>()
  for (const r of allScores.value) {
    seen.set(r.paper_item_id, Number(r.max_score || 0))
  }
  let sum = 0
  for (const v of seen.values()) sum += v
  return sum > 0 ? sum : 100
})

const bins = computed(() =>
  A.histogram(dist.value?.scores ?? [], fullScore.value, 10),
)
const maxBin = computed(() =>
  Math.max(1, ...bins.value.map((b) => b.count)),
)

/**
 * 学生 id → 姓名 / 学号。
 *
 * ★★ 2026-10-17 修：优先用**接口返回的 `student_list`**。
 *   原来只查 `store.students`（= **本次任务**的名单），而热力图是按
 *   **班级** 查的（全班 45 人）——两者集合不同，名单外的学生
 *   一律显示成 `#123`。现在名字由接口给，不再依赖别处的名单。
 */
const studentIndex = computed(() => {
  const m = new Map<number, A.HeatmapStudent>()
  for (const s of heat.value?.student_list ?? []) m.set(s.student_id, s)
  return m
})

const nameOf = (sid: number): string =>
  studentIndex.value.get(sid)?.student_name
  || store.students.find((s) => s.student_id === sid)?.student_name
  || `#${sid}`

const noOf = (sid: number): string =>
  studentIndex.value.get(sid)?.student_no
  || store.students.find((s) => s.student_id === sid)?.student_no
  || ''

/** 热力图单元格查表：`${node}@${student}` */
const cellMap = computed(() => {
  const m = new Map<string, A.HeatmapCell>()
  for (const c of heat.value?.cells ?? []) {
    m.set(`${c.node_id}@${c.student_id}`, c)
  }
  return m
})

function bandClass(
  nodeId: number,
  sid: number,
): string {
  const c = cellMap.value.get(`${nodeId}@${sid}`)
  if (!c) return 'cell cell--none'
  const b = A.masteryBand(
    c.mastery,
    c.confidence,
    heat.value?.min_confidence ?? 0.5,
  )
  return `cell cell--${b}`
}

function cellTitle(nodeId: number, sid: number): string {
  const c = cellMap.value.get(`${nodeId}@${sid}`)
  if (!c) return `${nameOf(sid)}：没有数据`
  const b = A.masteryBand(
    c.mastery,
    c.confidence,
    heat.value?.min_confidence ?? 0.5,
  )
  return (
    `${nameOf(sid)} · ${A.BAND_LABEL[b]}\n` +
    `掌握度 ${(c.mastery * 100).toFixed(0)}%  ` +
    `置信度 ${(c.confidence * 100).toFixed(0)}%  ` +
    `作答 ${c.answered_count} 次`
  )
}

async function loadDetail(sid: number): Promise<void> {
  detailLoading.value = true
  try {
    const [m, w] = await Promise.all([
      A.getStudentMastery(sid),
      A.getWeakNodes(sid),
    ])
    mastery.value = m
    weak.value = w
  } catch {
    mastery.value = null
    weak.value = null
  } finally {
    detailLoading.value = false
  }
}

// ══════════════════════════════════════════════════
//  ★ 题目分析（21 文档任务四）
// ══════════════════════════════════════════════════

const tab = ref<'overview' | 'items'>('overview')
const itemAnalysis = ref<A.ItemAnalysisResult | null>(null)
const itemLoading = ref(false)

/**
 * 逐题指标。
 *
 * @param refresh `true` = 现算并落库（老师点了「重新计算」）；
 *   `false` = 读上一次的快照。
 */
async function loadItems(refresh: boolean): Promise<void> {
  itemLoading.value = true
  try {
    itemAnalysis.value = await A.getItemAnalysis(id.value, refresh)
  } catch (e) {
    // ★ 不覆盖整页的 `error`：题目分析算不出来（比如还没成绩）
    //   不该让「热力图/分布」整页报错。
    ElMessage.warning(e instanceof Error ? e.message : '题目分析加载失败')
    itemAnalysis.value = null
  } finally {
    itemLoading.value = false
  }
}

async function downloadItems(): Promise<void> {
  try {
    const blob = await A.exportItemAnalysis(id.value)
    downloadBlob(blob, `题目分析-${id.value}.csv`)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '导出失败')
  }
}

/** 区分度差的行淡红：一眼看出「白占卷面」的题。 */
function itemRowClass({ row }: { row: A.ItemStatRow }): string {
  if (!row.sample_sufficient) return 'row-muted'
  if (row.d !== null && row.d < 0.2) return 'row-bad'
  return ''
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [h, e, t, d, sc, ms] = await Promise.all([
      A.getHeatmap({ classroomId: store.detail?.classroom_id }),
      A.getQuestionErrors({
        assignmentId: id.value,
        minAnswered: minAnswered.value,
      }),
      A.getErrorTags({ assignmentId: id.value }),
      A.getDistribution(id.value),
      G.listScores({ assignmentId: id.value, limit: 1000 }),
      // ★ 试卷测量指标（难度/区分度/信度/内容效度，2026-10-17 新增）
      A.getMeasurement(id.value),
    ])
    heat.value = h
    errors.value = e
    tags.value = t
    dist.value = d
    allScores.value = sc
    measure.value = ms
    const first = picked.value ?? h.students[0]
    if (first) {
      picked.value = first
      await loadDetail(first)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  store.reset()
  await store.load(id.value)
  await store.loadStudents()
  await load()
  // ★ 题目分析默认**读快照**（不传 refresh）：每次打开学情页重算
  //   25 题 × 45 人要等十几秒。首次访问后端会自动算一次。
  void loadItems(false)
})

async function pick(sid: number): Promise<void> {
  picked.value = sid
  await loadDetail(sid)
}
</script>

<template>
  <div v-loading="loading" class="zf-stack">
    <div class="head">
      <div>
        <h1 class="page-title">学情 · {{ store.detail?.title ?? '' }}</h1>
        <p class="zf-muted page-sub">
          {{ store.detail?.classroom_name }} ·
          {{ store.detail?.paper_title }}
        </p>
      </div>
      <div class="zf-row">
        <el-button
          @click="router.push({ name: 'AssignmentDetail', params: { id } })"
        >
          返回详情
        </el-button>
        <el-button
          @click="router.push({ name: 'GradingWorkbench', params: { id } })"
        >
          去批改
        </el-button>
        <el-button @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <!-- ══════════ ★ 页签：总览 / 题目分析（21 文档任务四） ══════════
         分成两页签而不是再加一块卡片：「总览」是**这张卷怎么样**，
         「题目分析」是**第 11 题哪里有问题**——查问题的动线不一样，
         混在一页会让老师在 6 个卡片里找那道题。 -->
    <el-radio-group v-model="tab" size="small" class="tabbar">
      <el-radio-button value="overview">总览</el-radio-button>
      <el-radio-button value="items">
        题目分析（{{ itemAnalysis?.items.length ?? 0 }}）
      </el-radio-button>
    </el-radio-group>

    <div v-show="tab === 'overview'" class="zf-stack">
    <!-- ══════════ ★ 试卷分析（难度 / 信度 / 效度） ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">试卷分析（难度 · 信度 · 效度）</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          依据 {{ measure?.student_count ?? 0 }} 名有成绩的学生 ·
          {{ measure?.item_count ?? 0 }} 道计分题
        </span>
      </div>
      <div class="zf-card__body">
        <div class="measure">
          <div class="measure__card">
            <div class="measure__label">全卷难度（得分率）</div>
            <div class="measure__value">
              {{ measure?.difficulty.value !== null && measure?.difficulty.value !== undefined
                ? (measure.difficulty.value * 100).toFixed(0) + '%'
                : '—' }}
            </div>
            <el-tag v-if="measure?.difficulty.label" size="small" type="info">
              {{ measure.difficulty.label }}
            </el-tag>
            <div class="measure__note">
              {{ measure?.difficulty.note || '越大越容易；50% 左右为中等难度' }}
            </div>
          </div>

          <div class="measure__card">
            <div class="measure__label">信度（Cronbach's α）</div>
            <div class="measure__value">
              {{ measure?.reliability.value !== null && measure?.reliability.value !== undefined
                ? measure.reliability.value.toFixed(3)
                : '样本不足' }}
            </div>
            <el-tag
              v-if="measure?.reliability.label"
              size="small"
              :type="measure.reliability.value !== null
                && (measure.reliability.value ?? 0) >= 0.7 ? 'success' : 'warning'"
            >
              {{ measure.reliability.label }}
            </el-tag>
            <div class="measure__note">
              {{ measure?.reliability.reason
                || measure?.reliability.note
                || '≥0.9 优 / 0.8–0.9 好 / 0.7–0.8 可接受 / <0.7 偏低' }}
            </div>
          </div>

          <div class="measure__card">
            <div class="measure__label">内容效度（考点覆盖）</div>
            <div class="measure__value">
              {{ measure?.validity.coverage !== null && measure?.validity.coverage !== undefined
                ? (measure.validity.coverage * 100).toFixed(0) + '%'
                : '—' }}
            </div>
            <div class="measure__note">
              覆盖 {{ measure?.validity.covered_node_count ?? 0 }} /
              {{ measure?.validity.total_node_count ?? 0 }} 个知识点；
              逐题挂点率
              {{ measure?.validity.item_binding_rate !== null && measure?.validity.item_binding_rate !== undefined
                ? (measure.validity.item_binding_rate * 100).toFixed(0) + '%'
                : '—' }}
            </div>
            <div class="measure__note zf-dim">
              {{ measure?.validity.note }}
            </div>
          </div>
        </div>

        <!-- ★ 区分度汇总：这类题「白占卷面」，老师最需要看到 -->
        <el-alert
          v-if="(measure?.discrimination_summary.poor_count ?? 0) > 0"
          type="warning"
          :closable="false"
          show-icon
          :title="`有 ${measure?.discrimination_summary.poor_count} 道题区分度低于 0.20 —— 好学生与差学生在这道题上表现差不多，建议复核题目或答案`"
          style="margin: 12px 0"
        />

        <el-table :data="measure?.items ?? []" size="small" max-height="360">
          <el-table-column prop="seq" label="题号" width="60" />
          <el-table-column label="题型" width="110">
            <template #default="{ row }">
              {{ G.questionTypeLabel(row.question_type) }}
            </template>
          </el-table-column>
          <el-table-column prop="max_score" label="满分" width="70" />
          <el-table-column label="难度（得分率）" width="170">
            <template #default="{ row }">
              <span v-if="row.difficulty !== null">
                {{ (row.difficulty * 100).toFixed(0) }}%
                <el-tag size="small" type="info">{{ row.difficulty_label }}</el-tag>
              </span>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column label="区分度" width="180">
            <template #default="{ row }">
              <span v-if="row.discrimination !== null">
                {{ row.discrimination.toFixed(2) }}
                <el-tag
                  size="small"
                  :type="row.discrimination >= 0.3 ? 'success'
                    : row.discrimination >= 0.2 ? 'warning' : 'danger'"
                >
                  {{ row.discrimination_label }}
                </el-tag>
              </span>
              <span v-else class="zf-dim" :title="row.discrimination_reason">—</span>
            </template>
          </el-table-column>
          <el-table-column label="高/低分组得分率" min-width="150">
            <template #default="{ row }">
              <span v-if="row.discrimination_high !== null" class="zf-dim">
                {{ (row.discrimination_high * 100).toFixed(0) }}% /
                {{ (row.discrimination_low * 100).toFixed(0) }}%
              </span>
              <span v-else class="zf-dim" style="font-size: var(--zf-text-xs)">
                {{ row.discrimination_reason || '—' }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <!-- ══════════ 分数分布 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">分数分布</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          {{ dist?.count ?? 0 }} 人有成绩 ·
          平均 {{ (dist?.avg ?? 0).toFixed(1) }}
        </span>
      </div>
      <div class="zf-card__body">
        <div v-if="dist?.count" class="hist">
          <div v-for="b in bins" :key="b.label" class="hist__col">
            <div class="hist__wrap">
              <div
                class="hist__bar"
                :style="{ height: `${(b.count / maxBin) * 100}%` }"
                :title="`${b.label} 分：${b.count} 人`"
              />
            </div>
            <div class="hist__n">{{ b.count || '' }}</div>
            <div class="hist__label">{{ b.label }}</div>
          </div>
        </div>
        <div v-else class="zf-empty">
          还没有成绩。先去批改页执行自动批改。
        </div>
      </div>
    </section>

    <!-- ══════════ 班级热力图 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">班级热力图（知识点 × 学生）</h2>
        <!-- ★ 图例必须解释「灰色」的含义，否则老师会以为灰色=掌握度 0 -->
        <div class="legend">
          <span class="legend__item"><i class="sw sw--bad" />较差</span>
          <span class="legend__item"><i class="sw sw--weak" />偏弱</span>
          <span class="legend__item"><i class="sw sw--mid" />一般</span>
          <span class="legend__item"><i class="sw sw--good" />较好</span>
          <span class="legend__item">
            <i class="sw sw--unknown" />数据不足（置信度低于
            {{ ((heat?.min_confidence ?? 0.5) * 100).toFixed(0) }}%）
          </span>
        </div>
      </div>
      <div class="zf-card__body">
        <div v-if="heat?.cells.length" class="heatwrap">
          <table class="heat">
            <thead>
              <tr>
                <th class="heat__corner">知识点</th>
                <th
                  v-for="sid in heat.students"
                  :key="sid"
                  class="heat__sh"
                  :title="`${noOf(sid)} ${nameOf(sid)}`"
                >
                  <span>{{ nameOf(sid) }}</span>
                </th>
                <th class="heat__avg">班均</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="n in heat.nodes" :key="n.id">
                <th class="heat__row" :title="`${n.name}（${n.code}）`">
                  <!-- ★ 显示知识点**名称**（原来只显示编码，老师看不懂） -->
                  <span class="heat__name">{{ n.name || n.code }}</span>
                  <span class="heat__code">{{ n.code }}</span>
                </th>
                <td
                  v-for="sid in heat.students"
                  :key="sid"
                  :class="bandClass(n.id, sid)"
                  :title="cellTitle(n.id, sid)"
                  @click="pick(sid)"
                />
                <td class="heat__avg">
                  <template
                    v-for="s in heat.node_summary.filter((x) => x.node_id === n.id)"
                    :key="s.node_id"
                  >
                    <span v-if="s.avg_mastery !== null">
                      {{ (s.avg_mastery * 100).toFixed(0) }}%
                      <span class="zf-dim" style="font-size: 10px">
                        ({{ s.sample_students }})
                      </span>
                    </span>
                    <span v-else class="zf-dim">—</span>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="zf-empty">
          还没有掌握度数据。掌握度在批改后自动重算；若刚批改完，点「刷新」。
        </div>
        <p
          v-if="heat?.cells.length"
          class="zf-dim"
          style="margin: 12px 0 0; font-size: var(--zf-text-xs)"
        >
          ★ 灰色格表示**样本不足**，不是「掌握度 0」。
          点任意格子可查看该学生的个人学情。
        </p>
      </div>
    </section>

    <!-- ══════════ 个人学情 ══════════ -->
    <section v-if="picked" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">
          个人学情 · {{ nameOf(picked) }}
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            {{ noOf(picked) }}
          </span>
        </h2>
        <!-- ★ 算法版本必须显示：换算法后新旧值不可比 -->
        <span
          v-if="mastery?.calc_version"
          class="zf-dim"
          style="font-size: var(--zf-text-xs)"
        >
          掌握度算法版本 {{ mastery.calc_version }}
        </span>
      </div>
      <div v-loading="detailLoading" class="zf-card__body">
        <div v-if="weak?.items.length">
          <p class="zf-muted" style="margin: 0 0 10px; font-size: var(--zf-text-sm)">
            薄弱知识点：<b>{{ weak.critical }}</b> 个要补练 ·
            <b>{{ weak.suspected }}</b> 个要再测
          </p>
          <el-table :data="weak.items" size="small" max-height="360">
            <el-table-column prop="node_code" label="编号" width="122" />
            <el-table-column prop="node_name" label="知识点" min-width="180" />
            <el-table-column label="掌握度" width="92">
              <template #default="{ row }">
                {{ (row.mastery * 100).toFixed(0) }}%
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="92">
              <template #default="{ row }">
                {{ (row.confidence * 100).toFixed(0) }}%
              </template>
            </el-table-column>
            <el-table-column label="作答次数" width="92" prop="answered_count" />
            <el-table-column label="判定" width="106">
              <template #default="{ row }">
                <el-tag size="small" :type="A.VERDICT_TONE[row.verdict]">
                  {{ A.VERDICT_LABEL[row.verdict] }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <p class="zf-dim" style="margin: 10px 0 0; font-size: var(--zf-text-xs)">
            ★「要补练」＝ 置信度足够且掌握度低；
            「要再测」＝ 掌握度低但**样本不足**，还不能下结论。
            两者处置方式不同，不要混。
          </p>
        </div>
        <div v-else class="zf-empty">该生暂无薄弱知识点（或还没有掌握度数据）</div>
      </div>
    </section>

    <!-- ══════════ 高频错题 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">高频错题</h2>
        <div class="zf-row">
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            最少作答人数
          </span>
          <el-input-number
            v-model="minAnswered"
            :min="1"
            :max="200"
            size="small"
            controls-position="right"
            style="width: 108px"
            @change="load"
          />
        </div>
      </div>
      <div class="zf-card__body">
        <el-table v-if="errors.length" :data="errors" size="small" max-height="420">
          <el-table-column prop="question_id" label="题目 ID" width="94" />
          <el-table-column label="作答" width="74" prop="answered_count" />
          <el-table-column label="答错" width="74" prop="wrong_count" />
          <el-table-column label="未答" width="74" prop="blank_count" />
          <el-table-column label="正确率" width="92">
            <template #default="{ row }">
              {{ G.pct(row.correct_rate) }}
            </template>
          </el-table-column>
          <el-table-column label="平均分" width="92">
            <template #default="{ row }">
              {{ row.avg_score.toFixed(1) }}
            </template>
          </el-table-column>
          <el-table-column label="最强干扰项" min-width="170">
            <template #default="{ row }">
              <template
                v-for="d in [A.topDistractor(row.option_distribution)]"
                :key="d?.label ?? 'none'"
              >
                <span v-if="d">
                  选项 <b>{{ d.label }}</b> 被选 {{ d.count }} 次
                </span>
                <span v-else class="zf-dim">—</span>
              </template>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">没有达到「最少作答人数」的题目</div>
          </template>
        </el-table>
        <div v-else class="zf-empty">
          还没有错题统计。它随批改自动重算；若刚批改完，点「刷新」。
        </div>
        <p class="zf-dim" style="margin: 10px 0 0; font-size: var(--zf-text-xs)">
          ★ 干扰项比正确率有用：「80% 的人选了 B」说明这是**普遍误解**，
          而不是随机猜错。
        </p>
      </div>
    </section>

    <!-- ══════════ 错因分布 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">错因分布</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          {{ tags.length }} 项
        </span>
      </div>
      <div class="zf-card__body">
        <el-table v-if="tags.length" :data="tags" size="small" max-height="300">
          <el-table-column prop="error_tag" label="错因" width="130" />
          <el-table-column label="知识点" width="110">
            <template #default="{ row }">
              <span v-if="row.knowledge_node_id" class="zf-dim">
                #{{ row.knowledge_node_id }}
              </span>
              <span v-else class="zf-dim">全卷</span>
            </template>
          </el-table-column>
          <el-table-column prop="count" label="次数" width="80" />
          <el-table-column label="占比" width="90">
            <template #default="{ row }">{{ G.pct(row.ratio, 1) }}</template>
          </el-table-column>
        </el-table>
        <div v-else class="zf-empty">还没有错因统计</div>
        <!-- ★ 如实说明当前口径，不让老师误以为「只有空白未答」是全部错因 -->
        <p class="zf-dim" style="margin: 10px 0 0; font-size: var(--zf-text-xs)">
          ★ 当前只统计「空白未答」——这是唯一**客观可判定**的错因。
          「概念混淆」「计算错误」等标签需由教师标注或 AI 判定，
          判分侧不产生它们（不会用关键词猜）。
        </p>
      </div>
    </section>
    </div>

    <!-- ══════════ ★ 题目分析（逐题指标，21 文档任务四） ══════════ -->
    <section v-show="tab === 'items'" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">题目分析</h2>
        <div class="zf-row">
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            难度 p（得分率，越大越易）· 区分度 D（前 27% − 后 27%）
            · 点二列相关
          </span>
          <el-button
            size="small"
            :loading="itemLoading"
            @click="loadItems(true)"
          >
            重新计算
          </el-button>
          <el-button
            size="small"
            :disabled="!itemAnalysis"
            @click="downloadItems"
          >
            导出 CSV
          </el-button>
        </div>
      </div>
      <div v-loading="itemLoading" class="zf-card__body">
        <!-- ★ 样本不足必须显式说：3 个人算出来的 D 是噪声，
             标红会让教研去改一道本来没问题的题。 -->
        <el-alert
          v-if="itemAnalysis?.summary.warning"
          type="warning"
          :closable="false"
          show-icon
          :title="itemAnalysis.summary.warning"
          style="margin-bottom: 12px"
        />
        <el-table
          v-if="itemAnalysis?.items.length"
          :data="itemAnalysis.items"
          size="small"
          max-height="560"
          :row-class-name="itemRowClass"
        >
          <el-table-column prop="seq" label="题号" width="64" />
          <el-table-column prop="question_type" label="题型" width="110" />
          <el-table-column label="分值" width="70">
            <template #default="{ row }">{{ row.max_score }}</template>
          </el-table-column>
          <el-table-column label="样本" width="70">
            <template #default="{ row }">{{ row.n }}</template>
          </el-table-column>
          <el-table-column label="平均分" width="80">
            <template #default="{ row }">{{ row.mean }}</template>
          </el-table-column>
          <el-table-column label="难度 p" width="120">
            <template #default="{ row }">
              <span v-if="row.p === null" class="zf-dim">—</span>
              <span v-else>
                {{ (row.p * 100).toFixed(0) }}%
                <span class="zf-dim">{{ row.p_label }}</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="区分度 D" width="120">
            <template #default="{ row }">
              <span v-if="row.d === null" class="zf-dim">—</span>
              <span v-else :class="{ 'bad': row.d < 0.2 }">
                {{ row.d.toFixed(2) }}
                <span class="zf-dim">{{ row.d_label }}</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="点二列" width="90">
            <template #default="{ row }">
              <span v-if="row.point_biserial === null" class="zf-dim">—</span>
              <span v-else>{{ row.point_biserial.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="选项分布" min-width="200">
            <template #default="{ row }">
              <span v-if="!Object.keys(row.option_counts).length" class="zf-dim">
                主观题
              </span>
              <span v-else class="dist">
                <span
                  v-for="(cnt, key) in row.option_counts"
                  :key="key"
                  class="dist__item"
                  :class="{ 'dist__item--zero': !cnt }"
                >
                  {{ key }}:{{ cnt }}
                </span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="提示" min-width="220">
            <template #default="{ row }">
              <el-tag
                v-for="w in row.warnings"
                :key="w"
                size="small"
                type="warning"
                style="margin: 0 4px 2px 0"
              >
                {{ w }}
              </el-tag>
              <span v-if="!row.warnings.length" class="zf-dim">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else-if="!itemLoading" class="zf-empty">
          还没有逐题数据（先批改出成绩）
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.page-title {
  margin: 0;
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.page-sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}

/* ── 直方图 ── */
.hist {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 168px;
}
.hist__col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
}
.hist__wrap {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
}
/* ★ `display: block` 不能省：`.hist__bar` 是 div 默认 block，
   但设了 height 百分比后若父级不是 flex 布局会塌成 0。 */
.hist__bar {
  display: block;
  width: 100%;
  min-height: 2px;
  background: var(--zf-brand);
  border-radius: 2px 2px 0 0;
}
.hist__n {
  font-size: 10px;
  color: var(--zf-text-secondary);
  height: 14px;
  line-height: 14px;
}
.hist__label {
  font-size: 10px;
  color: var(--zf-text-secondary);
  white-space: nowrap;
  transform: scale(0.9);
}

/* ── 图例 ── */
.legend {
  display: flex;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  align-items: center;
}
.legend__item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.sw {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 3px;
  border: 1px solid var(--zf-border);
}
.sw--bad { background: #c0392b; }
.sw--weak { background: #e67e22; }
.sw--mid { background: #f1c40f; }
.sw--good { background: #27ae60; }
.sw--unknown { background: var(--zf-bg-hover); }

/* ── 热力图 ── */
.heatwrap {
  overflow: auto;
  max-height: 520px;
}
.heat {
  border-collapse: separate;
  border-spacing: 2px;
  font-size: var(--zf-text-xs);
}
.heat th,
.heat td {
  padding: 0;
}
.heat__corner,
.heat__row {
  position: sticky;
  left: 0;
  z-index: 2;
  background: var(--zf-bg-surface);
  text-align: left;
  font-weight: var(--zf-weight-normal);
  color: var(--zf-text-secondary);
  max-width: 96px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 知识点列要放得下名称（编码仍在下行小字） */
.heat__row {
  max-width: 150px;
}
.heat__row {
  padding-right: 6px;
}
/* ★ 名称是主体、编码是辅助（原来只有编码，老师看不懂） */
.heat__name {
  display: block;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-primary);
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.heat__code {
  display: block;
  font-family: var(--zf-font-mono, monospace);
  font-size: 10px;
  color: var(--zf-text-tertiary, #909399);
}

/* ★ 试卷分析的三张卡（难度 / 信度 / 效度） */
.measure {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--zf-space-4);
  margin-bottom: var(--zf-space-4);
}
.measure__card {
  padding: var(--zf-space-3) var(--zf-space-4);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
}
.measure__label {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.measure__value {
  margin: 4px 0;
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.measure__note {
  margin-top: 6px;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  line-height: 1.5;
}
/* ★★ 表头姓名竖排：`vertical-rl` 让中文**从上往下**读（厉→子→昂）——
   这是中文竖排的正常形态。
   ★ 绝不能加 `transform: rotate(180deg)`：竖排后再整体翻转，
     中文名就会显示成「昂子厉」（2026-10-17 实测的缺陷）。
     `rotate(180deg)` 只对拉丁字母（自下而上）有意义，中文必须去掉。 */
.heat__sh {
  writing-mode: vertical-rl;
  max-height: 92px;
  color: var(--zf-text-secondary);
  font-weight: var(--zf-weight-normal);
  white-space: nowrap;
  overflow: hidden;
  font-size: 10px;
  /* 让姓名在格子里居中，不贴着上边 */
  vertical-align: bottom;
  padding-bottom: 4px;
}
.heat__avg {
  text-align: center;
  color: var(--zf-text-secondary);
  white-space: nowrap;
  padding: 0 4px;
  font-variant-numeric: tabular-nums;
}
.cell {
  width: 22px;
  height: 22px;
  min-width: 22px;
  border-radius: 3px;
  cursor: pointer;
}
.cell--none { background: var(--zf-bg-hover); }
.cell--unknown { background: var(--zf-bg-hover); }
.cell--bad { background: #c0392b; }
.cell--weak { background: #e67e22; }
.cell--mid { background: #f1c40f; }
.cell--good { background: #27ae60; }

/* ── 题目分析 ── */
.tabbar { align-self: flex-start; }
.dist {
  display: inline-flex;
  gap: 8px;
  flex-wrap: wrap;
  font-variant-numeric: tabular-nums;
  font-size: var(--zf-text-xs);
}
/* ★ 零选择的选项标灰：它有位置却没人选，说明这个干扰项是废的 */
.dist__item--zero {
  color: var(--zf-text-tertiary);
  text-decoration: line-through;
}
.bad { color: var(--zf-danger, #d93025); }
/* ★ 区分度差的行整行淡红底：一眼扫出「白占卷面」的题。
   样本不足的行不标红 —— 那是数据不够，不是题目有问题。 */
:deep(.row-bad) { background: rgba(217, 48, 37, 0.06); }
:deep(.row-muted) { color: var(--zf-text-tertiary); }
</style>
