<script setup lang="ts">
/**
 * 作业列表 + 新建任务。
 *
 * ★★ 新建表单里没有「答题卡」字段（2026-10-19）：
 *
 *   答题卡绑定只在作业详情页的「答题卡绑定」区域完成。
 *   创建后未绑卡可保存草稿；发布时未绑卡会被阻断并提示去详情绑定。
 *   线下扫描作业必须关闭逐生乱序；纯线上作业可开启。
 *
 * ★★ 时间窗是**左闭右开**，且后端按本地时区解释。
 *   Element Plus 的 `datetime` 选择器给的是 Date 对象，
 *   `toISOString()` 会转成 UTC 带 `Z`，这是对的
 *   （README 第八节：ISO 8601 带时区）。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import * as A from '@/api/assignment'
import type { CreateAssignmentBody } from '@/api/assignment'
import { listPapers } from '@/api/paper'
import type { PaperBrief } from '@/api/paper'

const router = useRouter()

const rows = ref<A.AssignmentBrief[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')

const papers = ref<PaperBrief[]>([])
const classrooms = ref<Array<{ id: number; name: string; grade: string }>>([])
const filters = reactive({ status: '' })

const dialog = ref(false)
const creating = ref(false)
const form = reactive({
  paper_id: undefined as number | undefined,
  classroom_id: undefined as number | undefined,
  title: '',
  range: null as [Date, Date] | null,
  shuffle_questions: false,
  shuffle_options: false,
  allow_late: false,
  pass_ratio: 0.6,
  // ★ 交卷自动批改（2026-10-17）：默认关，由老师显式勾选
  auto_grade_on_submit: false,
})

/** 只有已定稿的卷能建任务（后端也拦，这里提前过滤，省一次往返） */
const usablePapers = computed(() =>
  papers.value.filter((p) => p.status === 'FINALIZED'),
)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const d = await A.listAssignments({
      page: page.value,
      pageSize: pageSize.value,
      status: filters.status || undefined,
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
  await load()
  try {
    const [p, c] = await Promise.allSettled([
      listPapers({ pageSize: 100 }),
      import('@/api/org').then((m) => m.listClassrooms()),
    ])
    if (p.status === 'fulfilled') papers.value = p.value.items
    if (c.status === 'fulfilled') classrooms.value = c.value
  } catch {
    /* 下拉数据拿不到不阻塞列表 */
  }
})

function openCreate(): void {
  form.paper_id = undefined
  form.classroom_id = undefined
  form.title = ''
  form.range = null
  form.shuffle_questions = false
  form.shuffle_options = false
  form.allow_late = false
  form.pass_ratio = 0.6
  dialog.value = true
}
async function handleCreate(): Promise<void> {
  // ★ 不要写 `return ElMessage.warning(...)`：`ElMessage.warning` 返回
  //   `MessageHandler` 而不是 void，TS 会报「类型不匹配」。
  //
  // ★★ 用**局部常量 + 显式收窄**，不要靠 `missing.length` 推断：
  //    TypeScript 无法从「missing 里没有 X」推出「X 不是 undefined」，
  //    所以即使校验通过了，下面 `paper_id: form.paper_id` 仍会报
  //    「`number | undefined` 不能赋给 `number`」。
  const { paper_id, classroom_id, range } = form
  const title = form.title.trim()

  const missing: string[] = []
  if (!paper_id) missing.push('已定稿的试卷')
  if (!classroom_id) missing.push('班级')
  if (!title) missing.push('任务标题')
  if (!range?.[0] || !range?.[1]) missing.push('开始与结束时间')
  if (missing.length) {
    ElMessage.warning(`请填写：${missing.join('、')}`)
    return
  }
  if (range![0] >= range![1]) {
    ElMessage.warning('结束时间必须晚于开始时间')
    return
  }
  // ★ 到这里这几个值一定存在；用非空断言把类型收窄到 `number` / `Date`
  const [startAt, endAt] = range as [Date, Date]

  const body: CreateAssignmentBody = {
    paper_id: paper_id as number,
    classroom_id: classroom_id as number,
    title,
    // ★ 转 ISO 8601 带时区（`toISOString()` 给 UTC + Z）
    start_at: startAt.toISOString(),
    end_at: endAt.toISOString(),
    // ★ 答题卡不在新建时选；绑定只在作业详情页完成。
    shuffle_questions: form.shuffle_questions,
    shuffle_options: form.shuffle_options,
    allow_late: form.allow_late,
    pass_ratio: form.pass_ratio,
    auto_grade_on_submit: form.auto_grade_on_submit,
  }

  creating.value = true
  try {
    const a = await A.createAssignment(body)
    dialog.value = false
    ElMessage.success('已创建（草稿）。下一步去详情页发布')
    await router.push({ name: 'AssignmentDetail', params: { id: a.id } })
  } catch (e) {
    ElMessage.error(String((e as Error)?.message ?? e))
  } finally {
    creating.value = false
  }
}

function reload(): void {
  page.value = 1
  void load()
}

// ══════════════════════════════════════════════════
//  列表单元格的紧凑格式化
//
//  ★★ 这个表格的目标是「一屏看完一次作业」：
//     原来的列宽合计 1330px，1366 屏还要横向滚动才能看到「操作」。
//     现在把「试卷 + 答题卡」并成一列双行、时间窗压成一行，
//     列宽合计降到 ~940px，常见 1280 分辨率一屏即可看完。
//
//  ★ 压缩显示不等于丢信息：完整内容一律进 tooltip。
// ══════════════════════════════════════════════════

const pad2 = (n: number): string => String(n).padStart(2, '0')

function fmtClock(d: Date): string {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function fmtDay(d: Date): string {
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 时间窗压一行：同一天只写一次日期；跨年时日期带年份。 */
function fmtRange(start: string, end: string): string {
  const s = new Date(start)
  const e = new Date(end)
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return '—'
  if (s.getFullYear() !== e.getFullYear()) {
    const full = (x: Date): string => `${x.getFullYear()}-${fmtDay(x)}`
    return `${full(s)} ${fmtClock(s)} → ${full(e)} ${fmtClock(e)}`
  }
  if (fmtDay(s) === fmtDay(e)) {
    return `${fmtDay(s)} ${fmtClock(s)}–${fmtClock(e)}`
  }
  return `${fmtDay(s)} ${fmtClock(s)} → ${fmtDay(e)} ${fmtClock(e)}`
}

/** 时间窗的完整文本（tooltip 用，避免压缩后看不出年份）。 */
function fullRange(start: string, end: string): string {
  const s = new Date(start)
  const e = new Date(end)
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return '—'
  return `${s.toLocaleString()} – ${e.toLocaleString()}`
}

/** 答题卡的一行文本（双行主信息列的第二行 + tooltip）。 */
function sheetText(row: A.AssignmentBrief): string {
  if (!row.sheet_template_id) return '未绑定答题卡'
  const name = row.sheet_template_name || `#${row.sheet_template_id}`
  const ver = row.sheet_template_version ? ` v${row.sheet_template_version}` : ''
  return `${name}${ver}`
}
</script>

<template>
  <div class="zf-stack">
    <div class="zf-row" style="justify-content: space-between">
      <div>
        <h1 class="page-title">作业</h1>
        <p class="zf-muted page-sub">共 {{ total }} 次</p>
      </div>
      <el-button type="primary" @click="openCreate">新建任务</el-button>
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
            v-for="(l, v) in A.ASSIGNMENT_STATUS_LABEL"
            :key="v"
            :label="l"
            :value="v"
          />
        </el-select>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </section>

    <section class="zf-card">
      <el-table v-loading="loading" :data="rows" row-key="id" class="list-table">
        <!-- ★ 不显示 ID 列：它对老师没有意义，64px 却是压不进一屏的主因之一。
             要 ID 时在详情页 URL 上就能看到。 -->
        <el-table-column label="标题" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <a
              href="javascript:void(0)"
              @click="router.push({ name: 'AssignmentDetail', params: { id: row.id } })"
            >
              {{ row.title }}
            </a>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="A.ASSIGNMENT_STATUS_TONE[row.status]">
              {{ A.assignmentStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="班级"
          width="120"
          show-overflow-tooltip
          prop="classroom_name"
        />
        <!-- ★★ 试卷 + 答题卡合成一列（双行）：
             这两者在老师眼里是「这份作业考什么、用什么卡」，
             拆成两列各占 170/190px，实际大部分文字是溢出的。
             双行后 210px 就装得下，完整内容走 tooltip。 -->
        <el-table-column label="试卷 / 答题卡" min-width="210">
          <template #default="{ row }">
            <el-tooltip placement="top" :show-after="300">
              <template #content>
                <div>试卷：{{ row.paper_title }}</div>
                <div>
                  答题卡：{{ sheetText(row) }}
                  <template v-if="!row.sheet_template_id">（发布前需绑定）</template>
                </div>
              </template>
              <div class="pcell">
                <div class="pcell__paper">{{ row.paper_title }}</div>
                <div class="pcell__sheet">
                  <template v-if="row.sheet_template_id">
                    <el-tag size="small" type="success">卡</el-tag>
                    <span class="pcell__name">{{ sheetText(row) }}</span>
                  </template>
                  <span v-else class="zf-dim">未绑定答题卡</span>
                </div>
              </div>
            </el-tooltip>
          </template>
        </el-table-column>
        <!-- ★ 时间窗一行显示：190px 够放「10-20 08:00 → 10-21 18:00」，
             完整时间（含年份）在 tooltip 里。 -->
        <el-table-column label="时间窗" width="190">
          <template #default="{ row }">
            <el-tooltip placement="top" :show-after="300" :content="fullRange(row.start_at, row.end_at)">
              <span class="zftime">{{ fmtRange(row.start_at, row.end_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="乱序" width="84">
          <template #default="{ row }">
            <el-tag v-if="row.shuffle_questions" size="small" type="info" class="shuffle-tag">
              题
            </el-tag>
            <el-tag v-if="row.shuffle_options" size="small" type="info" class="shuffle-tag">
              选
            </el-tag>
            <span
              v-if="!row.shuffle_questions && !row.shuffle_options"
              class="zf-dim"
            >
              关
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ row }">
            <el-button
              size="small"
              text
              type="primary"
              @click="router.push({ name: 'AssignmentDetail', params: { id: row.id } })"
            >
              详情
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="zf-empty">
            {{ loading ? '读取中…' : '还没有作业。点右上角「新建任务」开始。' }}
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

    <!-- ── 新建任务 ── -->
    <el-dialog v-model="dialog" title="新建任务" width="680px">
      <el-form label-width="96px">
        <el-form-item label="试卷">
          <el-select
            v-model="form.paper_id"
            filterable
            placeholder="只能选已定稿的试卷"
            style="width: 100%"
          >
            <el-option
              v-for="p in usablePapers"
              :key="p.id"
              :label="`${p.title}（${p.item_count} 题 / ${Number(p.total_score).toFixed(0)} 分）`"
              :value="p.id"
            />
          </el-select>
          <span
            v-if="!usablePapers.length"
            class="zf-dim"
            style="font-size: var(--zf-text-xs)"
          >
            还没有已定稿的试卷。先在「试卷」页组卷并定稿。
          </span>
        </el-form-item>

        <el-form-item label="班级">
          <el-select
            v-model="form.classroom_id"
            filterable
            placeholder="选择班级"
            style="width: 260px"
          >
            <el-option
              v-for="c in classrooms"
              :key="c.id"
              :label="c.name"
              :value="c.id"
            />
          </el-select>
          <span
            v-if="!classrooms.length"
            class="zf-dim"
            style="font-size: var(--zf-text-xs)"
          >
            还没有班级与名册，请先在「机构」里建班并导入名册。
          </span>
        </el-form-item>

        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="如：2026 秋季期中考试" />
        </el-form-item>

        <el-form-item label="时间窗">
          <el-date-picker
            v-model="form.range"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="乱序">
          <el-checkbox v-model="form.shuffle_questions">题目乱序</el-checkbox>
          <el-checkbox v-model="form.shuffle_options">选项乱序</el-checkbox>
          <div class="zf-dim" style="width: 100%; font-size: var(--zf-text-xs)">
            乱序按「每个学生」冻结；线下扫描作业必须关闭乱序，
            纯线上作业可开启。答题卡在作业详情页绑定。
          </div>
        </el-form-item>

        <el-form-item label="允许迟交">
          <el-switch v-model="form.allow_late" />
          <span class="zf-dim" style="margin-left: 8px; font-size: var(--zf-text-xs)">
            关闭后，到截止时间学生无法再进入答题
          </span>
        </el-form-item>

        <el-form-item label="交卷自动批改">
          <el-switch v-model="form.auto_grade_on_submit" />
          <span class="zf-dim" style="margin-left: 8px; font-size: var(--zf-text-xs)">
            勾选后，学生一交卷就自动批改他这一份（客观题与填空题）；
            发布后也能在作业详情页改
          </span>
        </el-form-item>

        <el-form-item label="及格线">
          <el-input-number
            v-model="form.pass_ratio"
            :min="0"
            :max="1"
            :step="0.05"
            controls-position="right"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">
          创建（草稿）
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

/* ── 紧凑表格 ──
   行高从默认 ~48px 收到 ~40px，配合压窄的列宽让一次作业一屏看完。 */
.list-table :deep(.el-table__cell) {
  padding: 6px 0;
}
.list-table :deep(.cell) {
  line-height: 1.35;
}
/* 试卷 / 答题卡 双行单元格：第一行主信息，第二行弱化 */
.pcell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.pcell__paper {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pcell__sheet {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.pcell__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 时间窗：等宽数字，避免每行宽度跳动 */
.zftime {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.shuffle-tag + .shuffle-tag {
  margin-left: 4px;
}
</style>
