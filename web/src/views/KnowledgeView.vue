<script setup lang="ts">
/**
 * 知识体系（`/knowledge`）。
 *
 * ★★ 这个页面的价值不在「把树画出来」——`tree/` 端点只给结构。
 *   真正有用的是**三个运营指标**（来自 `courses/{id}/nodes/`）：
 *
 *       `question_count`  该知识点有多少道可用题
 *       `entry_count`     有多少条知识库条目
 *       `kb_sufficient`   知识库是否够 AI 生成
 *
 *   它们两两组合能直接指出**两类结构性问题**：
 *
 *     讲练脱节   有知识库、没题（`entry_count > 0 && question_count === 0`）
 *     无米之炊   有题、知识库不够（AI 生成会被 L1 闸门拦下）
 *
 *   所以这个页面同时是**知识库运营台**，不只是浏览树。
 *
 * ★★ 左树与右侧指标来自**两个端点**（`tree/` 与 `nodes/`），
 *   按 `id` 对齐。不合并的原因：树的层级信息只有 `tree/` 有，
 *   而指标的聚合只有 `nodes/` 有——两者职责不同，硬塞一个端点
 *   会让任何一方变化都要改对方的序列化器。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import * as K from '@/api/knowledge'
import MathText from '@/components/MathText.vue'
import type { Course, CourseTree, KnowledgeEntry, KnowledgeNodeRow } from '@/api/knowledge'

const courses = ref<Course[]>([])
const courseId = ref<number | undefined>(undefined)
const tree = ref<CourseTree | null>(null)
const nodes = ref<KnowledgeNodeRow[]>([])
const health = ref<K.KbHealth | null>(null)
const lacking = ref<Array<{ id: number; name: string; code: string
                            entry_count: number; question_count: number }>>([])

const loading = ref(false)
const error = ref('')
const keyword = ref('')
const route = useRoute()

/**
 * 只看有问题的知识点（讲练脱节 / 知识库不足 / 零题）。
 *
 * ★ 初值取 URL `?onlyProblem=1`：首页工作台「无题知识点」待办卡
 *   就是带这个参数跳进来的。不带参数时保持 `false`（正常浏览）。
 */
const onlyProblem = ref(route.query.onlyProblem === '1')

// 选中的知识点
const nodeId = ref<number | undefined>(undefined)
const detail = ref<K.KnowledgeNodeDetail | null>(null)
const entries = ref<KnowledgeEntry[]>([])
const detailLoading = ref(false)

/** `id → 指标行`，用于左树与表格快速取指标 */
const metricById = computed(() => {
  const m = new Map<number, KnowledgeNodeRow>()
  for (const n of nodes.value) m.set(n.id, n)
  return m
})

/**
 * ★★ 给 `el-tree` 用的数据。
 *
 *   后端 `tree.parts` 的元素是 `{part, part_seq, chapters}` ——
 *   **没有 `id`**。而 `el-tree` 的 `node-key="id"` 要求**每个**节点
 *   都有唯一 `id`，缺了会让组件**静默不渲染整棵树**
 *   （不报错，只是一个空框 —— 实测踩到过）。
 *
 *   ★ 所以这里加一层**人造根**，给它一个不可能与真实 id 冲突的负 id。
 *     根节点用 `class` 隐藏（它只是容器，「篇」在文档里是**属性**
 *     而不是一个层级，不该出现在树上）。
 */
const TREE_ROOT_ID = -1
const treeData = computed(() => {
  if (!tree.value) return []
  return [
    {
      id: TREE_ROOT_ID,
      name: tree.value.course?.name ?? '课程',
      level: 'ROOT',
      children: tree.value.parts.flatMap((p) => p.chapters ?? []),
    },
  ]
})

const filteredNodes = computed(() => {
  let rows = nodes.value
  if (keyword.value.trim()) {
    const k = keyword.value.trim().toLowerCase()
    rows = rows.filter(
      (n) =>
        n.name.toLowerCase().includes(k) ||
        n.code.toLowerCase().includes(k) ||
        n.task_name.toLowerCase().includes(k),
    )
  }
  if (onlyProblem.value) {
    rows = rows.filter(
      (n) =>
        K.isLectureWithoutPractice(n) ||
        K.isPracticeWithoutLecture(n) ||
        n.question_count === 0 ||
        n.entry_count === 0,
    )
  }
  return rows
})

/** 讲练脱节的知识点（有知识库、没题） */
const lectureNoPractice = computed(() =>
  nodes.value.filter(K.isLectureWithoutPractice),
)
/** 有题但知识库不够（AI 生成会被拦） */
const practiceNoLecture = computed(() =>
  nodes.value.filter(K.isPracticeWithoutLecture),
)
/** 一道题都没有的知识点 */
const zeroQuestion = computed(() =>
  nodes.value.filter((n) => n.question_count === 0),
)

async function loadCourses(): Promise<void> {
  const paged = await K.listCourses()
  courses.value = paged.items ?? []
  if (!courseId.value) courseId.value = courses.value[0]?.id
}

async function loadCourse(): Promise<void> {
  if (!courseId.value) return
  loading.value = true
  error.value = ''
  try {
    const [t, ns, h, lk] = await Promise.all([
      K.getCourseTree(courseId.value),
      K.listCourseNodes(courseId.value),
      K.getKbHealth(courseId.value),
      K.listNodesLackingKb(courseId.value),
    ])
    tree.value = t
    nodes.value = ns
    health.value = h
    lacking.value = lk
    // 默认选中第一个知识点，右侧不空着
    if (!nodeId.value && ns.length) await selectNode(ns[0].id)
    void loadTagging()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

// ══════════════════════════════════════════════════
//  ★ 标注覆盖率与人工确认（21 文档任务五）
// ══════════════════════════════════════════════════
//
//  ★★ 这一块存在的理由：**AI 标的 `PENDING` 不计入覆盖**。
//     没有确认队列，「覆盖率 90%」就变成了「AI 跑过一次」——
//     报表达标而没人看过那些标注，后面的组卷体检与学情全建立在
//     没人验证过的挂点上。

const coverage = ref<K.CoverageReport | null>(null)
const tagQueue = ref<K.TagReviewRow[]>([])
const tagTotal = ref(0)
const tagLoading = ref(false)
const reviewingId = ref<number | null>(null)

async function loadTagging(): Promise<void> {
  if (!courseId.value) return
  tagLoading.value = true
  try {
    const [cov, q] = await Promise.all([
      K.getCoverage(courseId.value),
      K.listTagReviewQueue({ state: 'PENDING', limit: 50 }),
    ])
    coverage.value = cov
    tagQueue.value = q.items
    tagTotal.value = q.total
  } catch {
    // ★ 覆盖报表失败不该让整页报错（老库可能还没有 review_state 数据）
    coverage.value = null
    tagQueue.value = []
    tagTotal.value = 0
  } finally {
    tagLoading.value = false
  }
}

async function reviewTag(
  linkId: number, state: 'CONFIRMED' | 'REJECTED',
): Promise<void> {
  reviewingId.value = linkId
  try {
    await K.reviewTag(linkId, { state })
    tagQueue.value = tagQueue.value.filter((r) => r.link_id !== linkId)
    tagTotal.value = Math.max(0, tagTotal.value - 1)
    // ★ 确认后覆盖率会变（PENDING → CONFIRMED 才计入），必须重取
    coverage.value = await K.getCoverage(courseId.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '更新失败'
  } finally {
    reviewingId.value = null
  }
}

/** 覆盖率是否达标（90% / 85%）——目标值由后端给，前端不写死。 */
const covMeets = computed(() => coverage.value?.meets_target ?? false)

async function selectNode(id: number): Promise<void> {
  nodeId.value = id
  detailLoading.value = true
  try {
    const [d, es] = await Promise.all([
      K.getNode(id),
      K.listEntries({ nodeId: id }),
    ])
    detail.value = d
    entries.value = es
  } catch {
    detail.value = null
    entries.value = []
  } finally {
    detailLoading.value = false
  }
}

onMounted(async () => {
  await loadCourses()
  await loadCourse()
})

watch(courseId, () => {
  nodeId.value = undefined
  detail.value = null
  entries.value = []
  void loadCourse()
})

const selectedMetric = computed(() =>
  nodeId.value ? metricById.value.get(nodeId.value) : undefined,
)

/** 左树：默认全展开（44 个知识点不多，收起反而多一次点击） */
const expanded = ref<number[]>([])
watch(tree, (t) => {
  if (!t) return
  const ids: number[] = [TREE_ROOT_ID]
  for (const p of t.parts) {
    for (const c of p.chapters) {
      ids.push(c.id)
      for (const task of c.children ?? []) ids.push(task.id)
    }
  }
  expanded.value = ids
})

const pct = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`

/** 指标的视觉标签 */
function metricTag(n: KnowledgeNodeRow): {
  text: string
  type: 'success' | 'info' | 'warning' | 'danger'
} {
  if (n.question_count === 0 && n.entry_count === 0) {
    return { text: '空知识点', type: 'danger' }
  }
  if (n.question_count === 0) return { text: '无题', type: 'warning' }
  if (!n.kb_sufficient) return { text: '知识库不足', type: 'info' }
  return { text: '齐备', type: 'success' }
}
</script>

<template>
  <div class="kb">
    <!-- ══════════ 顶部：课程 + 健康度 ══════════ -->
    <div class="kb__head">
      <div>
        <h1 class="kb__title">知识体系</h1>
        <p class="zf-muted kb__sub">
          课程 → 章 → 任务 → 知识点（4 层）
        </p>
      </div>
      <div class="zf-row">
        <el-select
          v-model="courseId"
          placeholder="选择课程"
          style="width: 190px"
        >
          <el-option
            v-for="c in courses"
            :key="c.id"
            :label="c.name"
            :value="c.id"
          />
        </el-select>
        <el-button @click="loadCourse">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <!-- ── 健康度概览 ── -->
    <section v-if="health" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">知识库健康度</h2>
        <el-tag
          size="small"
          :type="health.sufficient_for_generation ? 'success' : 'warning'"
        >
          {{
            health.sufficient_for_generation
              ? 'AI 可生成'
              : '未达 AI 生成门槛'
          }}
        </el-tag>
      </div>
      <div class="zf-card__body">
        <div class="stats">
          <div>
            <div class="n">{{ health.total_nodes }}</div>
            <div class="l">知识点</div>
          </div>
          <div>
            <div class="n ok">{{ health.covered_nodes }}</div>
            <div class="l">有知识库</div>
          </div>
          <div>
            <div class="n">{{ health.total_entries }}</div>
            <div class="l">知识库条目</div>
          </div>
          <div>
            <div class="n">{{ health.adopted_entries }}</div>
            <div class="l">被 AI 采纳</div>
          </div>
          <div>
            <div class="n">{{ pct(health.coverage) }}</div>
            <div class="l">覆盖率</div>
          </div>
          <div>
            <div class="n">{{ pct(health.utilisation) }}</div>
            <div class="l">利用率</div>
          </div>
        </div>

        <!-- ★ 两类结构性问题各自成一条，因为它们**处方不同** -->
        <div class="problems">
          <div
            class="probs"
            :class="{ 'probs--ok': !lectureNoPractice.length }"
          >
            <div class="probs__n">{{ lectureNoPractice.length }}</div>
            <div class="probs__t">讲练脱节</div>
            <div class="probs__d">
              有知识库但<b>一道题都没有</b> —— AI 生成不出练习依据
            </div>
          </div>
          <div
            class="probs"
            :class="{ 'probs--ok': !practiceNoLecture.length }"
          >
            <div class="probs__n">{{ practiceNoLecture.length }}</div>
            <div class="probs__t">知识库不足</div>
            <div class="probs__d">
              有题但条目少于门槛 —— <b>AI 生题会被 L1 闸门拦下</b>
            </div>
          </div>
          <div class="probs" :class="{ 'probs--ok': !zeroQuestion.length }">
            <div class="probs__n">{{ zeroQuestion.length }}</div>
            <div class="probs__t">零题知识点</div>
            <div class="probs__d">
              组卷按知识点抽题时<b>永远抽不到</b>这些点
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ══════════ ★ 标注覆盖率与人工确认（21 文档任务五） ══════════ -->
    <section v-loading="tagLoading" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">题目标注覆盖</h2>
        <div class="zf-row">
          <el-tag size="small" :type="covMeets ? 'success' : 'warning'">
            {{ covMeets ? '覆盖率达标' : '覆盖率未达标' }}
          </el-tag>
          <el-button size="small" @click="loadTagging">刷新</el-button>
        </div>
      </div>
      <div v-if="coverage" class="zf-card__body">
        <div class="zf-counts" style="margin-bottom: 12px">
          <div class="zf-count">
            <div class="zf-count__n">
              {{ (coverage.coverage_ratio * 100).toFixed(1) }}%
            </div>
            <div class="zf-count__l">
              覆盖率（目标 {{ (coverage.target.coverage * 100).toFixed(0) }}%）
            </div>
          </div>
          <div class="zf-count">
            <div class="zf-count__n">
              {{ (coverage.primary_ratio * 100).toFixed(1) }}%
            </div>
            <div class="zf-count__l">
              主标签率（目标 {{ (coverage.target.primary * 100).toFixed(0) }}%）
            </div>
          </div>
          <div class="zf-count">
            <div
              class="zf-count__n"
              :class="{ 'zf-count__n--zero': !coverage.conflict_questions }"
            >
              {{ coverage.conflict_questions }}
            </div>
            <div class="zf-count__l">无主标签（组卷抽不准）</div>
          </div>
          <div class="zf-count">
            <div
              class="zf-count__n"
              :class="{ 'zf-count__n--zero': !coverage.pending_ai_questions }"
            >
              {{ coverage.pending_ai_questions }}
            </div>
            <div class="zf-count__l">AI 待确认（不计入覆盖）</div>
          </div>
        </div>

        <p class="zf-dim" style="margin: 0 0 10px; font-size: var(--zf-text-xs)">
          共 {{ coverage.total_questions }} 道题，已确认标注
          {{ coverage.tagged_questions }} 道。
          ★ 只有**人工确认**的标注计入覆盖率：AI 标完没人看就「达标」，
          等于把校验环节跳过。
        </p>

        <div v-if="tagQueue.length" class="tagq">
          <div class="lbl">
            待确认标注（共 {{ tagTotal }} 条，按置信度从低到高）
          </div>
          <el-table :data="tagQueue" size="small" max-height="300">
            <el-table-column label="题目" min-width="260">
              <template #default="{ row }">
                <span class="zf-mono">#{{ row.question_id }}</span>
                <MathText
                  mode="text"
                  :source="row.stem_preview || '（无题干）'"
                />
              </template>
            </el-table-column>
            <el-table-column label="知识点" min-width="140">
              <template #default="{ row }">
                {{ row.node_name }}
                <el-tag v-if="row.is_primary" size="small" type="info">
                  主
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="80" prop="source" />
            <el-table-column label="置信度" width="90">
              <template #default="{ row }">
                <span v-if="row.confidence === null" class="zf-dim">—</span>
                <span v-else>{{ (row.confidence * 100).toFixed(0) }}%</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="primary"
                  link
                  :loading="reviewingId === row.link_id"
                  @click="reviewTag(row.link_id, 'CONFIRMED')"
                >
                  确认
                </el-button>
                <el-button
                  size="small"
                  type="danger"
                  link
                  :loading="reviewingId === row.link_id"
                  @click="reviewTag(row.link_id, 'REJECTED')"
                >
                  驳回
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div v-else class="zf-empty" style="padding: var(--zf-space-4)">
          没有待确认的标注
        </div>
      </div>
      <div v-else-if="!tagLoading" class="zf-empty">
        还没有标注数据
      </div>
    </section>

    <!-- ══════════ 主体：左树 + 右详情 ══════════ -->
    <div class="kb__body">
      <!-- ── 左：知识点树 ── -->
      <section class="zf-card kb__tree">
        <div class="zf-card__head">
          <h2 class="zf-card__title">知识点</h2>
          <span class="zf-dim kb__hint">
            {{ filteredNodes.length }} / {{ nodes.length }}
          </span>
        </div>
        <div class="zf-card__body">
          <el-input
            v-model="keyword"
            size="small"
            placeholder="搜名称 / 编码 / 任务"
            clearable
            style="margin-bottom: 8px"
          />
          <el-checkbox v-model="onlyProblem" size="small">
            只显示有问题的
          </el-checkbox>

          <el-tree
            v-if="tree"
            class="kb__el-tree"
            :data="treeData"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            :default-expanded-keys="expanded"
            :expand-on-click-node="false"
            highlight-current
            @node-click="(d: K.TreeNode) => {
              if (d.level === 'NODE') selectNode(d.id)
            }"
          >
            <template #default="{ data }">
              <span class="kb__node" :class="{ 'kb__node--root': data.level === 'ROOT' }">
                <span
                  class="kb__node-name"
                  :class="{ 'kb__node-name--leaf': data.level === 'NODE' }"
                >
                  {{ data.name }}
                </span>
                <!-- ★ 指标只对知识点显示；章/任务上显示会误导
                     （它们的计数是子树合计） -->
                <template v-if="data.level === 'NODE'">
                  <template v-for="m in [metricById.get(data.id)]" :key="data.id">
                    <el-tag
                      v-if="m"
                      size="small"
                      :type="metricTag(m).type"
                      effect="plain"
                    >
                      {{ metricTag(m).text }}
                    </el-tag>
                  </template>
                </template>
                <span
                  v-else-if="data.level !== 'ROOT'"
                  class="zf-dim kb__node-code"
                >
                  {{ data.code }}
                </span>
              </span>
            </template>
          </el-tree>
          <div v-else class="zf-empty">没有课程树</div>
        </div>
      </section>

      <!-- ── 右：知识点详情 + 知识库 ── -->
      <section class="kb__detail">
        <div v-if="!nodeId" class="zf-card">
          <div class="zf-card__body">
            <div class="zf-empty">从左侧点一个**知识点**查看详情</div>
          </div>
        </div>

        <template v-else>
          <div v-loading="detailLoading" class="zf-card">
            <div class="zf-card__head">
              <h2 class="zf-card__title">
                {{ detail?.name ?? '知识点' }}
                <span class="zf-dim kb__code">{{ detail?.code }}</span>
              </h2>
            </div>
            <div class="zf-card__body">
              <div v-if="selectedMetric" class="stats stats--sm">
                <div>
                  <div class="n">{{ selectedMetric.question_count }}</div>
                  <div class="l">可用题</div>
                </div>
                <div>
                  <div class="n">{{ selectedMetric.entry_count }}</div>
                  <div class="l">知识库条目</div>
                </div>
                <div>
                  <div class="n">
                    {{
                      detail?.kb_sufficient ? '是' : '否'
                    }}
                  </div>
                  <div class="l">够 AI 生成</div>
                </div>
                <div>
                  <div class="n">
                    {{ selectedMetric.exam_weight }}
                  </div>
                  <div class="l">考纲权重</div>
                </div>
              </div>

              <div class="kv">
                <span class="k">所属任务</span>
                <span class="v">{{ detail?.task_name ?? '—' }}</span>
              </div>

              <div v-if="detail?.prerequisites?.length" class="kv">
                <span class="k">前置知识点</span>
                <span class="v">
                  <el-tag
                    v-for="(p, i) in detail.prerequisites"
                    :key="i"
                    size="small"
                    style="margin-left: 4px"
                  >
                    {{ p.name ?? p.code ?? p.node_id }}
                  </el-tag>
                </span>
              </div>

              <div class="zf-row" style="margin-top: 12px">
                <el-button
                  size="small"
                  type="primary"
                  @click="$router.push({
                    name: 'QuestionList',
                    query: { nodeId: String(nodeId) },
                  })"
                >
                  看该知识点的题（{{ selectedMetric?.question_count ?? 0 }}）
                </el-button>
              </div>
            </div>
          </div>

          <!-- ── 知识库条目 ── -->
          <div class="zf-card">
            <div class="zf-card__head">
              <h2 class="zf-card__title">知识库条目</h2>
              <span class="zf-dim kb__hint">{{ entries.length }} 条</span>
            </div>
            <div class="zf-card__body">
              <el-table
                v-if="entries.length"
                :data="entries"
                size="small"
                max-height="380"
              >
                <el-table-column label="类型" width="98">
                  <template #default="{ row }">
                    <el-tag size="small" effect="plain">
                      {{ K.ENTRY_TYPE_LABEL[row.entry_type] ?? row.entry_type }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column
                  prop="title"
                  label="标题"
                  width="180"
                  show-overflow-tooltip
                />
                <el-table-column
                  prop="content"
                  label="内容"
                  min-width="240"
                  show-overflow-tooltip
                />
                <el-table-column label="来源" width="104">
                  <template #default="{ row }">
                    <span class="zf-dim">
                      {{
                        K.ENTRY_SOURCE_LABEL[row.source_type] ??
                        row.source_type
                      }}
                    </span>
                  </template>
                </el-table-column>
                <template #empty>
                  <div class="zf-empty">该知识点没有知识库条目</div>
                </template>
              </el-table>
              <div v-else class="zf-empty">
                该知识点没有知识库条目
                <div class="zf-dim kb__hint" style="margin-top: 6px">
                  ★ 没有知识库条目时，AI 生题会被 L1 闸门**拒绝** ——
                  这是三条不可绕过的保证之一
                </div>
              </div>
            </div>
          </div>
        </template>
      </section>
    </div>

    <!-- ══════════ 缺口清单（供运营逐项补） ══════════ -->
    <section v-if="lacking.length" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">知识库缺口清单</h2>
        <span class="zf-dim kb__hint">{{ lacking.length }} 个知识点</span>
      </div>
      <div class="zf-card__body">
        <p class="zf-dim kb__hint" style="margin: 0 0 8px">
          ★ 这些知识点**条目数不足**，AI 生题会被拦下。点一行可跳到它的详情。
        </p>
        <el-table :data="lacking" size="small" max-height="280">
          <el-table-column prop="code" label="编码" width="150" />
          <el-table-column prop="name" label="知识点" min-width="180" />
          <el-table-column prop="entry_count" label="条目数" width="90" />
          <el-table-column prop="question_count" label="题数" width="80" />
          <el-table-column label="操作" width="86">
            <template #default="{ row }">
              <el-button size="small" text type="primary"
                         @click="selectNode(row.id)">
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.kb {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-4);
}
.kb__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.kb__title {
  margin: 0;
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.kb__sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}
.kb__hint {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
.kb__code {
  font-size: var(--zf-text-xs);
  margin-left: 6px;
}

/* ── 指标 ── */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
  gap: var(--zf-space-3);
}
.stats--sm {
  grid-template-columns: repeat(auto-fit, minmax(84px, 1fr));
  margin-bottom: var(--zf-space-3);
}
.stats .n {
  font-size: var(--zf-text-lg);
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.stats .n.ok {
  color: var(--zf-success);
}
.stats .l {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}

/* ── 两类结构性问题 ── */
.problems {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--zf-space-3);
  margin-top: var(--zf-space-4);
}
.probs {
  padding: var(--zf-space-3);
  border: 1px solid var(--zf-warning);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-warning-bg);
}
/* ★ 没问题时收成绿色 —— 一排红色方块会让老师麻木 */
.probs--ok {
  border-color: var(--zf-border-subtle);
  background: var(--zf-bg-subtle);
}
.probs__n {
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.probs__t {
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  margin-top: 2px;
}
.probs__d {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  line-height: var(--zf-leading-loose);
  margin-top: 4px;
}

/* ── 主体两栏 ── */
.kb__body {
  display: grid;
  grid-template-columns: minmax(280px, 360px) 1fr;
  gap: var(--zf-space-4);
  align-items: start;
}
@media (max-width: 1100px) {
  .kb__body {
    grid-template-columns: 1fr;
  }
}
.kb__tree {
  position: sticky;
  top: var(--zf-space-4);
}
.kb__el-tree {
  margin-top: 8px;
  max-height: 560px;
  overflow: auto;
}
.kb__node {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
}
/* 人造根节点只是一个容器，不需要强调 */
.kb__node--root {
  font-weight: var(--zf-weight-semibold);
}
.kb__node-name--leaf {
  font-size: var(--zf-text-sm);
}
.kb__node-code {
  font-size: 10px;
}
.kb__detail {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-4);
}
.kv {
  display: flex;
  justify-content: space-between;
  gap: var(--zf-space-4);
  padding: 6px 0;
  border-bottom: 1px dashed var(--zf-border-subtle);
  font-size: var(--zf-text-sm);
}
.kv:last-of-type {
  border-bottom: none;
}
.kv .k {
  color: var(--zf-text-secondary);
  flex: none;
}
.kv .v {
  text-align: right;
}
</style>
