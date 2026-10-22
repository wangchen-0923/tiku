<script setup lang="ts">
/**
 * 试卷列表 + 新建。
 *
 * ★★ 新建时**必须能选定组卷方式**，因为那决定了后续能不能自动组卷：
 *
 *     MANUAL  手动加题（自己从候选池挑）
 *     RULE    规则组卷（填抽题规则，一键自动抽）
 *
 *   而 `RULE` 又**必须同时给 `compose_config`** —— 后端会拒
 *   （「按规则组卷必须提供 compose_config」）。所以在对话框里
 *   就得让老师把大题配置填好，不能建完空卷再补。
 *
 * ★ 「同 seed 可复现」（TC-PAP-107）需要一个能被钉死的种子，
 *   所以对话框里有 `random_seed`，留空则由后端随机生成。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import * as api from '@/api/paper'
import type { ComposeMode, PaperBrief, PaperPurpose } from '@/api/paper'
import { listCourses } from '@/api/knowledge'
import type { Course } from '@/api/knowledge'

const router = useRouter()

const rows = ref<PaperBrief[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')
const courses = ref<Course[]>([])
const filters = reactive({ status: '', courseId: undefined as number | undefined })

const dialog = ref(false)
const creating = ref(false)
const form = reactive({
  course_id: undefined as number | undefined,
  title: '',
  purpose: 'QUIZ' as PaperPurpose,
  compose_mode: 'RULE' as ComposeMode,
  difficulty_expect: undefined as number | undefined,
  random_seed: undefined as number | undefined,
  sections: [
    {
      type: 'SINGLE_CHOICE',
      count: 10,
      score_each: 4,
      difficulty_range: [1, 5] as [number, number],
      title: '一、单项选择题',
    },
  ],
})

const PURPOSE_LABEL: Record<string, string> = {
  EXAM: '考试',
  QUIZ: '小测',
  HOMEWORK: '作业',
  PRACTICE: '练习',
}

const typeOptions = Object.entries(api.QUESTION_TYPE_LABEL).map(([value, label]) => ({
  value,
  label,
}))

const expectTotal = computed(() =>
  form.sections.reduce((s, x) => s + (Number(x.count) || 0), 0),
)
const expectScore = computed(() =>
  form.sections.reduce(
    (s, x) => s + (Number(x.count) || 0) * (Number(x.score_each) || 0),
    0,
  ),
)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const d = await api.listPapers({
      page: page.value,
      pageSize: pageSize.value,
      status: filters.status || undefined,
      courseId: filters.courseId,
    })
    rows.value = d.items
    total.value = d.total
  } catch (e) {
    error.value = String((e as Error)?.message ?? e)
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const d = await listCourses()
    courses.value = d.items
    if (courses.value.length) form.course_id = courses.value[0].id
  } catch {
    courses.value = []
  }
  await load()
})

function openCreate(): void {
  form.title = ''
  form.purpose = 'QUIZ'
  form.compose_mode = 'RULE'
  form.difficulty_expect = undefined
  form.random_seed = undefined
  form.sections = [
    {
      type: 'SINGLE_CHOICE',
      count: 10,
      score_each: 4,
      difficulty_range: [1, 5],
      title: '一、单项选择题',
    },
  ]
  dialog.value = true
}

async function handleCreate(): Promise<void> {
  if (!form.course_id || !form.title.trim()) {
    ElMessage.warning('请填写课程与标题')
    return
  }
  if (form.compose_mode === 'RULE' && !form.sections.length) {
    ElMessage.warning('规则组卷必须至少配置一个大题')
    return
  }
  creating.value = true
  try {
    const p = await api.createPaper({
      course_id: form.course_id,
      title: form.title.trim(),
      purpose: form.purpose,
      compose_mode: form.compose_mode,
      // ★ MANUAL 模式**不传** `compose_config`，而不是传 `{}`。
      //   传 `{}` 会存一个「有 compose_config 但没有 sections」的空壳，
      //   而 `parse_config` 见到它会报「sections 必须是非空数组」——
      //   于是手动组卷的卷子在别处被当成「配置坏了」。
      //   不传才是「这份卷子没有抽题规则」的正确表达。
      compose_config:
        form.compose_mode === 'RULE'
          ? {
              sections: form.sections.map((s) => ({
                ...s,
                count: Number(s.count),
                score_each: Number(s.score_each),
              })),
            }
          : undefined,
      difficulty_expect: form.difficulty_expect ?? null,
      // ★ 留空则后端随机生成；填了就能「同 seed 复现同一份卷」
      random_seed: form.random_seed ?? null,
    })
    dialog.value = false
    ElMessage.success('已创建，正在打开组卷工作台')
    await router.push({ name: 'PaperCompose', params: { id: p.id } })
  } catch (e) {
    ElMessage.error(String((e as Error)?.message ?? e))
  } finally {
    creating.value = false
  }
}

async function handleDelete(row: PaperBrief): Promise<void> {
  try {
    await api.deletePaper(row.id)
    ElMessage.success(`已删除「${row.title}」`)
    await load()
  } catch (e) {
    ElMessage.error(String((e as Error)?.message ?? e))
  }
}

async function handleDuplicate(row: PaperBrief): Promise<void> {
  try {
    await api.duplicatePaper(row.id)
    ElMessage.success('已派生为新卷（草稿）')
    await load()
  } catch (e) {
    ElMessage.error(String((e as Error)?.message ?? e))
  }
}

function reload(): void {
  page.value = 1
  void load()
}
</script>

<template>
  <div class="zf-stack">
    <div class="zf-row" style="justify-content: space-between">
      <div>
        <h1 class="page-title">试卷</h1>
        <p class="zf-muted page-sub">共 {{ total }} 份</p>
      </div>
      <el-button type="primary" @click="openCreate">新建试卷</el-button>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      title="加载失败"
      :description="error"
    />

    <section class="zf-card">
      <div class="zf-card__body zf-row">
        <el-select
          v-model="filters.status"
          clearable
          placeholder="全部状态"
          size="small"
          style="width: 150px"
          @change="reload"
        >
          <el-option
            v-for="(l, v) in api.PAPER_STATUS_LABEL"
            :key="v"
            :label="l"
            :value="v"
          />
        </el-select>
        <el-select
          v-model="filters.courseId"
          clearable
          placeholder="全部课程"
          size="small"
          style="width: 200px"
          @change="reload"
        >
          <el-option
            v-for="c in courses"
            :key="c.id"
            :label="c.name"
            :value="c.id"
          />
        </el-select>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </section>

    <section class="zf-card">
      <el-table v-loading="loading" :data="rows" row-key="id">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="标题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <a
              href="javascript:void(0)"
              @click="router.push({ name: 'PaperCompose', params: { id: row.id } })"
            >
              {{ row.title }}
            </a>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="api.PAPER_STATUS_TONE[row.status]">
              {{ api.PAPER_STATUS_LABEL[row.status] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用途" width="76">
          <template #default="{ row }">
            {{ PURPOSE_LABEL[row.purpose] ?? row.purpose }}
          </template>
        </el-table-column>
        <el-table-column label="组卷方式" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.compose_mode === 'RULE' ? 'success' : 'info'"
            >
              {{ row.compose_mode === 'RULE' ? '规则' : row.compose_mode === 'DIAGNOSIS' ? '学情' : '手动' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="题量" width="76">
          <template #default="{ row }">{{ row.item_count }}</template>
        </el-table-column>
        <el-table-column label="总分" width="80">
          <template #default="{ row }">
            {{ Number(row.total_score).toFixed(0) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              size="small"
              text
              type="primary"
              @click="router.push({ name: 'PaperCompose', params: { id: row.id } })"
            >
              组卷
            </el-button>
            <el-button size="small" text @click="handleDuplicate(row)">
              派生
            </el-button>
            <el-button
              v-if="row.status === 'DRAFT'"
              size="small"
              text
              type="danger"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="zf-empty">
            {{ loading ? '读取中…' : '还没有试卷。点右上角「新建试卷」开始。' }}
          </div>
        </template>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="reload"
        />
      </div>
    </section>

    <!-- ── 新建对话框 ── -->
    <el-dialog v-model="dialog" title="新建试卷" width="720px">
      <el-form label-width="88px">
        <el-form-item label="课程">
          <el-select v-model="form.course_id" style="width: 260px">
            <el-option
              v-for="c in courses"
              :key="c.id"
              :label="`${c.name}（${c.code}）`"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="如：2026 秋季期中考试 A 卷" />
        </el-form-item>
        <el-form-item label="用途">
          <el-select v-model="form.purpose" style="width: 160px">
            <el-option
              v-for="(l, v) in PURPOSE_LABEL"
              :key="v"
              :label="l"
              :value="v"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="组卷方式">
          <el-radio-group v-model="form.compose_mode">
            <el-radio value="RULE">规则组卷（填规则后一键自动抽题）</el-radio>
            <el-radio value="MANUAL">手动组卷（自己从候选池挑）</el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.compose_mode === 'RULE'">
          <el-divider content-position="left">抽题规则</el-divider>
          <div
            v-for="(s, idx) in form.sections"
            :key="idx"
            class="sec"
          >
            <div class="zf-row">
              <el-input
                v-model="s.title"
                size="small"
                placeholder="大题标题"
                style="width: 180px"
              />
              <el-select v-model="s.type" size="small" style="width: 130px">
                <el-option
                  v-for="o in typeOptions"
                  :key="o.value"
                  :label="o.label"
                  :value="o.value"
                />
              </el-select>
              <el-input-number
                v-model="s.count"
                size="small"
                :min="1"
                :max="200"
                controls-position="right"
                style="width: 100px"
              />
              <span class="zf-dim">题 ×</span>
              <el-input-number
                v-model="s.score_each"
                size="small"
                :min="0.5"
                :max="100"
                :step="0.5"
                controls-position="right"
                style="width: 100px"
              />
              <span class="zf-dim">分</span>
              <el-button
                v-if="form.sections.length > 1"
                size="small"
                type="danger"
                text
                @click="form.sections.splice(idx, 1)"
              >
                删除
              </el-button>
            </div>
          </div>
          <el-button
            size="small"
            @click="form.sections.push({
              type: 'TRUE_FALSE', count: 5, score_each: 2,
              difficulty_range: [1, 5], title: '' })"
          >
            加大题
          </el-button>
          <p class="zf-muted" style="font-size: var(--zf-text-xs); margin-top: 8px">
            合计 <b>{{ expectTotal }}</b> 题 / <b>{{ expectScore }}</b> 分
          </p>
        </template>

        <el-divider content-position="left">可选</el-divider>
        <el-form-item label="期望难度">
          <el-input-number
            v-model="form.difficulty_expect"
            :min="1"
            :max="5"
            :step="0.1"
            controls-position="right"
            placeholder="留空自动算"
          />
        </el-form-item>
        <el-form-item label="随机种子">
          <el-input-number
            v-model="form.random_seed"
            :min="0"
            controls-position="right"
            placeholder="留空随机"
            style="width: 200px"
          />
          <span class="zf-dim" style="margin-left: 8px; font-size: var(--zf-text-xs)">
            填了就能「同种子复现同一份卷」（验收项 TC-PAP-107）
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">
          创建并打开
        </el-button>
      </template>
    </el-dialog>
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
.pager {
  display: flex;
  justify-content: flex-end;
  padding: var(--zf-space-4) var(--zf-space-5);
  border-top: 1px solid var(--zf-border-subtle);
}
.sec {
  padding: var(--zf-space-2) 0;
}
</style>
