<script setup lang="ts">
/**
 * 公共题审核工作台（U14）。
 *
 * ★★ 这一页的存在理由是 R7：「公共题审核靠人 → 提交堆积、教师等不到反馈」。
 *   所以页面上必须能一眼看出**积压有多深**：
 *
 *     - 顶部四个数字：待审 / 已通过 / 已驳回 / 已下架
 *     - 每行的「已等待 N 小时」，超过 72 小时标红
 *
 *   没有这两个东西，审核台就只是个列表，堆积不会有任何人察觉。

 * ★ 审核动作的约束（都来自后端，前端只负责把话说清楚）：
 *     驳回**必须**填理由 —— 那是老师收到的唯一反馈；
 *     批量一次最多 50 道 —— 减速带，不是技术限制。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import * as Q from '@/api/questionbank'
import MathText from '@/components/MathText.vue'
import type { PublicReviewItem, PublicReviewQueue, QuestionDetail } from '@/api/questionbank'

/** 超过这个小时数标红：72 小时 = 三个工作日，老师该有答复了 */
const SLA_HOURS = 72

const loading = ref(false)
const errorMsg = ref('')
const queue = ref<PublicReviewQueue | null>(null)
const rows = ref<PublicReviewItem[]>([])
const selected = ref<PublicReviewItem[]>([])

const filters = reactive({
  status: 'PENDING_REVIEW',
  keyword: '',
})

const statusOptions = [
  { value: 'PENDING_REVIEW', label: '待审核' },
  { value: 'APPROVED', label: '已通过' },
  { value: 'REJECTED', label: '已驳回' },
  { value: 'ARCHIVED', label: '已下架' },
  { value: '', label: '全部' },
]

const TYPE_LABEL: Record<string, string> = {
  SINGLE_CHOICE: '单选题',
  MULTIPLE_CHOICE: '多选题',
  TRUE_FALSE: '判断题',
  FILL_BLANK: '填空题',
  SHORT_ANSWER: '简答题',
  ANALYSIS: '分析题',
  CALCULATION: '计算题',
  DRAWING: '识图题',
  ESSAY: '论述题',
}

async function load(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const data = await Q.listPublicReviewQueue({
      status: filters.status || undefined,
      keyword: filters.keyword || undefined,
      limit: 200,
    })
    queue.value = data
    rows.value = data.items
  } catch (e) {
    errorMsg.value = String((e as Error)?.message ?? e)
    rows.value = []
  } finally {
    loading.value = false
  }
}

/** 详情抽屉（**必须走 public 详情端点**：待审题走普通端点会 404）。 */
const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<QuestionDetail | null>(null)

async function openDetail(row: PublicReviewItem): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await Q.getPublicQuestion(row.id)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '读取详情失败')
    detailOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

async function approveOne(row: PublicReviewItem): Promise<void> {
  try {
    await Q.reviewPublicQuestion(row.id, { action: 'APPROVE' })
    ElMessage.success('已通过')
    void load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '操作失败')
  }
}

/**
 * 驳回：理由**必填**。
 *
 * ★ 用 `ElMessageBox.prompt` 的 `inputValidator` 拦住空理由，
 *   与后端的 `QUESTION_BANK.REASON_REQUIRED` 同一条规则。
 *   前端拦是为了少一次往返，**不是**为了替代后端校验。
 */
async function rejectOne(row: PublicReviewItem): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      '请写明需要修改什么（老师只能看到这段话）',
      '驳回理由',
      {
        confirmButtonText: '驳回',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：题干缺少「销」的规格条件，请补齐',
        inputValidator: (v: string) => (v && v.trim().length >= 2
          ? true
          : '理由不能为空（至少 2 个字）'),
      },
    )
    await Q.reviewPublicQuestion(row.id, { action: 'REJECT', reason: value })
    ElMessage.success('已驳回')
    void load()
  } catch (e) {
    // 取消 prompt 会抛 'cancel'，不是错误
    if (String(e) === 'cancel' || String(e) === 'close') return
    ElMessage.error(e instanceof Error ? e.message : '操作失败')
  }
}

async function archiveOne(row: PublicReviewItem): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      '下架后其它机构立刻看不到它；已下载的副本不受影响。请写明原因。',
      '下架公共题',
      {
        confirmButtonText: '下架',
        cancelButtonText: '取消',
        inputValidator: (v: string) => (v && v.trim().length >= 2
          ? true
          : '原因不能为空（至少 2 个字）'),
      },
    )
    await Q.archivePublicQuestion(row.id, value)
    ElMessage.success('已下架')
    void load()
  } catch (e) {
    if (String(e) === 'cancel' || String(e) === 'close') return
    ElMessage.error(e instanceof Error ? e.message : '操作失败')
  }
}

async function batch(action: 'APPROVE' | 'REJECT'): Promise<void> {
  if (!selected.value.length) return
  const ids = selected.value.map((r) => r.id)
  if (ids.length > 50) {
    ElMessage.warning('一次最多 50 道，请分批处理')
    return
  }
  let reason = ''
  if (action === 'REJECT') {
    try {
      const { value } = await ElMessageBox.prompt(
        `将驳回 ${ids.length} 道，请写明统一理由`,
        '批量驳回',
        {
          confirmButtonText: '驳回',
          cancelButtonText: '取消',
          inputValidator: (v: string) => (v && v.trim().length >= 2
            ? true
            : '理由不能为空（至少 2 个字）'),
        },
      )
      reason = value
    } catch (e) {
      if (String(e) === 'cancel' || String(e) === 'close') return
      throw e
    }
  } else {
    try {
      await ElMessageBox.confirm(
        `将直接通过 ${ids.length} 道。批量通过等于没有逐条看，确认吗？`,
        '批量通过',
        { type: 'warning', confirmButtonText: '确认通过', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  try {
    const r = await Q.batchReviewPublic(ids, { action, reason })
    ElMessage.success(
      `已处理 ${r.done.length} 道`
      + (r.skipped.length ? `，跳过 ${r.skipped.length} 道（状态已变）` : ''),
    )
    selected.value = []
    void load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '批量操作失败')
  }
}

function waitedTone(h: number | null): string {
  if (h === null) return ''
  return h >= SLA_HOURS ? 'zf-danger' : ''
}

function fmtHours(h: number | null): string {
  if (h === null) return '—'
  if (h < 1) return '不到 1 小时'
  if (h < 24) return `${h.toFixed(0)} 小时`
  return `${(h / 24).toFixed(1)} 天`
}

/**
 * 顶部四个数字。
 *
 * ★ 写成 computed 而不是在模板里 `queue.stats[key as keyof ...]`：
 *   模板里的类型断言不会报错、也不会生效，取值容易变成 `undefined`，
 *   而界面只是安静地显示空白（这类"不报错的错"最难查）。
 */
const statCards = computed(() => {
  const s = queue.value?.stats
  if (!s) return []
  return [
    { key: 'pending', label: '待审核', tone: 'warning', value: s.pending },
    { key: 'approved', label: '已通过', tone: 'success', value: s.approved },
    { key: 'rejected', label: '已驳回', tone: 'danger', value: s.rejected },
    { key: 'archived', label: '已下架', tone: 'info', value: s.archived },
  ]
})

onMounted(() => void load())
</script>

<template>
  <div class="zf-stack">
    <div class="zf-row" style="justify-content: space-between">
      <div>
        <h1 class="page-title">公共题审核</h1>
        <p class="zf-muted page-sub">
          教师提交的公共题库题目在这里审核。通过后全平台可见；
          驳回必须填理由（那是老师唯一的反馈）。
        </p>
      </div>
      <el-button @click="load">刷新</el-button>
    </div>

    <!-- ★ 积压数字（R7 的可视化抓手） -->
    <section v-if="queue" class="stat-row">
      <div v-for="s in statCards" :key="s.key" class="stat-card">
        <div class="stat-card__num" :class="`is-${s.tone}`">{{ s.value }}</div>
        <div class="stat-card__label">{{ s.label }}</div>
      </div>
      <div class="stat-card stat-card--wide">
        <div class="stat-card__num">{{ queue.stats.total }}</div>
        <div class="stat-card__label">公共库总题数</div>
      </div>
    </section>

    <section class="zf-card">
      <div class="zf-card__body filter-bar">
        <el-select v-model="filters.status" style="width: 140px" @change="load">
          <el-option
            v-for="s in statusOptions"
            :key="s.value"
            :label="s.label"
            :value="s.value"
          />
        </el-select>
        <el-input
          v-model="filters.keyword"
          placeholder="搜索题干"
          clearable
          style="max-width: 240px"
          @keyup.enter="load"
          @clear="load"
        />
        <el-button size="small" :disabled="!selected.length" @click="batch('APPROVE')">
          批量通过（{{ selected.length }}）
        </el-button>
        <el-button
          size="small"
          type="danger"
          plain
          :disabled="!selected.length"
          @click="batch('REJECT')"
        >
          批量驳回
        </el-button>
      </div>
    </section>

    <el-alert
      v-if="errorMsg"
      type="error"
      :closable="false"
      show-icon
      title="加载失败"
      :description="errorMsg"
    />

    <section class="zf-card">
      <el-table
        v-loading="loading"
        :data="rows"
        row-key="id"
        style="width: 100%"
        @selection-change="(v: PublicReviewItem[]) => (selected = v)"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column prop="id" label="ID" width="76" />
        <el-table-column label="题型" width="96">
          <template #default="{ row }">
            {{ TYPE_LABEL[row.type] ?? row.type }}
          </template>
        </el-table-column>
        <el-table-column label="题干摘要" min-width="280">
          <template #default="{ row }">
            <a href="javascript:void(0)" class="stem-link" @click="openDetail(row)">
              <MathText mode="text" :source="row.stem_preview" />
            </a>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="150">
          <template #default="{ row }">
            <div>{{ row.submitted_from_org_name || '—' }}</div>
            <div class="zf-dim sub-note">
              {{ row.course_name || '—' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="知识点" width="110">
          <template #default="{ row }">
            <span v-if="row.node_codes.length">{{ row.node_codes.length }} 个</span>
            <span v-else class="zf-dim">无</span>
            <el-tag
              v-if="row.dropped_figures"
              size="small"
              type="warning"
              class="drop-tag"
            >
              丢图 {{ row.dropped_figures }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已等待" width="110">
          <template #default="{ row }">
            <span :class="waitedTone(row.waiting_hours)">
              {{ fmtHours(row.waiting_hours) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'APPROVED' ? 'success'
              : row.status === 'REJECTED' ? 'danger'
                : row.status === 'ARCHIVED' ? 'info' : 'warning'">
              {{ statusOptions.find((s) => s.value === row.status)?.label ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="196">
          <template #default="{ row }">
            <template v-if="row.status === 'PENDING_REVIEW'">
              <el-button size="small" text type="success" @click="approveOne(row)">
                通过
              </el-button>
              <el-button size="small" text type="danger" @click="rejectOne(row)">
                驳回
              </el-button>
            </template>
            <el-button
              v-else-if="row.status === 'APPROVED'"
              size="small"
              text
              type="warning"
              @click="archiveOne(row)"
            >
              下架
            </el-button>
            <span v-else class="zf-dim">—</span>
          </template>
        </el-table-column>
        <template #empty>
          <div class="zf-empty">
            {{ loading ? '读取中…' : '没有符合条件的题目' }}
          </div>
        </template>
      </el-table>
    </section>

    <!-- 详情：走 `/questions/public/{id}/`，待审状态也能看 -->
    <el-drawer v-model="detailOpen" title="题目详情" size="640px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="zf-muted detail-sub">
            #{{ detail.id }} · {{ TYPE_LABEL[detail.type] ?? detail.type }} ·
            状态 {{ detail.status }}
          </div>
          <MathText tag="div" class="detail-stem" :source="detail.stem_text" />
          <div v-if="detail.options?.length" class="detail-opts">
            <div
              v-for="o in detail.options"
              :key="o.id"
              class="detail-opt"
              :class="{ 'is-correct': o.is_correct }"
            >
              <b>{{ o.label }}.</b> <MathText :source="o.content_text" />
              <span v-if="o.is_correct" class="zf-dim">（正确项）</span>
            </div>
          </div>
          <div class="detail-line">
            <span class="zf-dim">答案：</span><MathText :source="detail.answer_text || '—'" />
          </div>
          <div v-if="detail.explanation_text" class="detail-line">
            <span class="zf-dim">解析：</span><MathText :source="detail.explanation_text" />
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
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
.stat-row {
  display: flex;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
}
.stat-card {
  flex: 1 1 120px;
  padding: var(--zf-space-4);
  background: var(--zf-bg-surface);
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-lg);
}
.stat-card--wide {
  flex: 0 0 150px;
}
.stat-card__num {
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.stat-card__num.is-warning {
  color: var(--zf-warning, #d97706);
}
.stat-card__num.is-success {
  color: var(--zf-success, #16a34a);
}
.stat-card__num.is-danger {
  color: var(--zf-danger, #dc2626);
}
.stat-card__label {
  margin-top: 2px;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
}
.stem-link {
  color: var(--zf-text-primary);
  text-decoration: none;
}
.stem-link:hover {
  color: var(--zf-brand);
  text-decoration: underline;
}
.sub-note {
  font-size: var(--zf-text-xs);
}
.drop-tag {
  margin-left: 6px;
}
.detail-sub {
  margin-bottom: var(--zf-space-3);
  font-size: var(--zf-text-xs);
}
.detail-stem {
  font-size: var(--zf-text-base);
  line-height: var(--zf-leading-loose);
  color: var(--zf-text-primary);
  white-space: pre-wrap;
}
.detail-opts {
  margin: var(--zf-space-3) 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.detail-opt {
  padding: 6px 10px;
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-subtle);
  font-size: var(--zf-text-sm);
}
.detail-opt.is-correct {
  outline: 1px solid var(--zf-success, #16a34a);
}
.detail-line {
  margin-top: var(--zf-space-2);
  font-size: var(--zf-text-sm);
  line-height: var(--zf-leading-loose);
}
</style>
