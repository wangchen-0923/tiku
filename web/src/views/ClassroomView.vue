<script setup lang="ts">
/**
 * 班级与名册管理（`/classrooms`）。
 *
 * ★★ 这个页面是「**发布链路在第一步就断了**」的修复。
 *
 *   后端 `/org/classrooms/` 早已就绪，但前端 13 个页面里**没有班级管理页** ——
 *   于是「建班 → 导入学生 → 发布作业 → 学生答题 → 批改 → 学情」
 *   这条链路**根本没法开始**，需求方原话是「作业板块无班级管理，无法测试」。
 *
 * ★★ 后端本轮补齐了两个缺口（原来只有建，没有改/删）：
 *     · `PATCH  /org/classrooms/{id}/`              改班级
 *     · `DELETE /org/classrooms/{id}/`              删班级（软删）
 *     · `POST   .../roster/students/`               加一名学生
 *     · `PATCH  .../roster/students/{sid}/`         改一名学生
 *     · `DELETE .../roster/students/{sid}/`         转出一名学生
 *
 *   为什么单条接口是必需的：`import_roster(replace=True)` 是**整表替换**，
 *   为了加一个转学生而重传全班名册，风险远大于收益。
 *
 * ★★ 两条后端语义约束，界面上必须**如实反映**（不能藏）：
 *     ① **学号不可改** —— 它是答题卡认人的唯一依据，改学号等于换一个人
 *        （历史成绩会错挂）。所以编辑学生时学号是**只读**的，
 *        要换学号只能「转出旧的 + 加入新的」。
 *     ② **删除是转出**（置 `LEFT`），不物理删除 —— 保留历史成绩可追溯。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as A from '@/api/accounts'
import * as O from '@/api/org'
import { BusinessBranch } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

// ★ 只有学校账户需要「归属」列与指派入口（见模版里的 v-if）
const auth = useAuthStore()

// ── 班级列表 ──
const classrooms = ref<O.Classroom[]>([])
const majors = ref<O.Major[]>([])
const loading = ref(false)
const error = ref('')
const keyword = ref('')

const majorName = computed(() => {
  const m = new Map<number, string>()
  for (const x of majors.value) m.set(x.id, x.name)
  return m
})

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return classrooms.value
  return classrooms.value.filter((c) =>
    [c.name, c.grade, c.academic_year, c.major_name ?? '']
      .join(' ').toLowerCase().includes(k))
})


async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cs, ms] = await Promise.all([
      O.listClassrooms({ keyword: keyword.value || undefined }),
      O.listMajors().catch(() => [] as O.Major[]),
    ])
    classrooms.value = cs
    majors.value = ms
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  void loadTeachers()
})

// ══════════════════════════════════════════════════════════════
//  班级：新建 / 编辑 / 删除
// ══════════════════════════════════════════════════════════════

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const formSaving = ref(false)
const formError = ref('')
const editId = ref<number | null>(null)
const form = reactive<O.ClassroomInput>({
  name: '', grade: '', academic_year: '', grade_band: 'SECONDARY_VOCATIONAL',
  major_id: null, head_teacher_id: null,
  owner_id: null,
})

/**
 * 可被指派为归属人的教师。
 *
 * ★ 只有学校账户会加载这份数据 —— 教师改不了归属（后端 403），
 *   给他一个下拉框只会让他白点一次。
 * ★ 无主班（历史数据迁移时没有班主任可回填）靠这里指派，
 *   否则它永远不出现在任何教师的列表里。
 */
const teachers = ref<A.AccountUser[]>([])

async function loadTeachers(): Promise<void> {
  if (!auth.can('org.classroom.assign')) return
  try {
    const r = await A.listAccounts({ role: 'TEACHER', isActive: true, pageSize: 200 })
    teachers.value = r.items
  } catch {
    teachers.value = []
  }
}

const GRADE_BANDS = [
  { value: 'SECONDARY_VOCATIONAL', label: '中职' },
  { value: 'SECONDARY', label: '普通高中' },
  { value: 'HIGHER_VOCATIONAL', label: '高职' },
]

function openCreate() {
  formMode.value = 'create'
  editId.value = null
  formError.value = ''
  const y = new Date().getFullYear()
  Object.assign(form, {
    name: '', grade: `${y}级`, academic_year: `${y}-${y + 1}`,
    grade_band: 'SECONDARY_VOCATIONAL', major_id: null,
    head_teacher_id: null, owner_id: null,
  })
  formVisible.value = true
}

function openEdit(c: O.Classroom) {
  formMode.value = 'edit'
  editId.value = c.id
  formError.value = ''
  Object.assign(form, {
    name: c.name, grade: c.grade, academic_year: c.academic_year,
    grade_band: c.grade_band, major_id: c.major_id,
    head_teacher_id: c.head_teacher_id,
    owner_id: c.owner_id ?? null,
  })
  formVisible.value = true
}

async function save() {
  if (!form.name.trim() || !form.grade.trim()
      || !form.academic_year.trim()) {
    formError.value = '班级名称、年级、学年都不能为空'
    return
  }
  formSaving.value = true
  formError.value = ''
  try {
    const payload: O.ClassroomInput & { status?: string } = { ...form }
    // ★ 只有学校账户能变更归属。教师若把回填的 `owner_id` 原样
    //   发回去，后端会因为「请求里出现了 owner_id」直接 403 ——
    //   哪怕值没变。所以这里必须删掉，不能靠值相等来规避。
    if (!auth.can('org.classroom.assign')) {
      delete (payload as { owner_id?: number | null }).owner_id
    }
    if (formMode.value === 'create') {
      await O.createClassroom(payload)
      ElMessage.success('班级已创建')
    } else if (editId.value != null) {
      await O.updateClassroom(editId.value, payload)
      ElMessage.success('已保存')
    }
    formVisible.value = false
    await load()
  } catch (e) {
    // ★ 把后端的说明原样展示（如「2026-2027 学年已存在班级「X」」），
    //   不要吞掉换成「保存失败」—— 那句话正是老师照做所需的全部信息
    formError.value = e instanceof Error ? e.message : '保存失败'
  } finally {
    formSaving.value = false
  }
}

async function remove(c: O.Classroom) {
  try {
    await ElMessageBox.confirm(
      `确定删除班级「${c.name}」？\n\n` +
      '删除是**软删除**（保留历史成绩可追溯）。\n' +
      '若班内还有在册学生，需要先把学生转出。',
      '删除班级',
      { type: 'warning', confirmButtonText: '删除',
        cancelButtonText: '取消', dangerouslyUseHTMLString: false },
    )
  } catch {
    return
  }
  try {
    await O.deleteClassroom(c.id, '界面删除')
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    // ★ 409 的 message 是「该班级还有 N 名在册学生，不能删除。请先把学生转出」
    //   这类**可照做的说明**，用 alert 展示全文而不是一行 toast
    ElMessageBox.alert(
      e instanceof Error ? e.message : '删除失败', '不能删除',
      { type: 'warning' },
    ).catch(() => {})
  }
}

// ══════════════════════════════════════════════════════════════
//  名册
// ══════════════════════════════════════════════════════════════

const rosterVisible = ref(false)
const rosterClassroom = ref<O.Classroom | null>(null)
const roster = ref<O.RosterRow[]>([])
const rosterLoading = ref(false)
const rosterError = ref('')
const rosterBrief = ref<O.RosterBrief | null>(null)
/** ★ 默认只看「在册」；转出的学生默认不显示（但可以切过来看） */
const rosterStatus = ref<'ACTIVE' | 'LEFT' | ''>('ACTIVE')

const activeCount = computed(
  () => roster.value.filter((r) => r.status !== 'LEFT').length)
const boundCount = computed(
  () => roster.value.filter((r) => r.is_bound).length)

async function openRoster(c: O.Classroom) {
  rosterClassroom.value = c
  rosterVisible.value = true
  await loadRoster()
}

async function loadRoster() {
  const c = rosterClassroom.value
  if (!c) return
  rosterLoading.value = true
  rosterError.value = ''
  try {
    roster.value = await O.listRoster(c.id, {})
    // 详情里带名册摘要（推断位数）——独立取一次，失败不影响名册展示
    const detail = await O.getClassroom(c.id).catch(() => null)
    rosterBrief.value = detail?.roster_brief ?? null
  } catch (e) {
    rosterError.value = e instanceof Error ? e.message : '名册加载失败'
  } finally {
    rosterLoading.value = false
  }
}

const shownRoster = computed(() => {
  if (!rosterStatus.value) return roster.value
  return roster.value.filter(
    (r) => (r.status ?? 'ACTIVE') === rosterStatus.value)
})

// ── 单个学生：加 / 改 ──

const stuVisible = ref(false)
const stuMode = ref<'create' | 'edit'>('create')
const stuSaving = ref(false)
const stuError = ref('')
const stuEditId = ref<number | null>(null)
const stuForm = reactive({ student_no: '', name: '', seat_no: '' })

function openStuCreate() {
  stuMode.value = 'create'
  stuEditId.value = null
  stuError.value = ''
  Object.assign(stuForm, { student_no: '', name: '', seat_no: '' })
  stuVisible.value = true
}

function openStuEdit(r: O.RosterRow) {
  stuMode.value = 'edit'
  stuEditId.value = r.id
  stuError.value = ''
  Object.assign(stuForm, {
    student_no: r.student_no, name: r.name, seat_no: r.seat_no ?? '',
  })
  stuVisible.value = true
}

async function saveStu() {
  const c = rosterClassroom.value
  if (!c) return
  if (!stuForm.name.trim()) {
    stuError.value = '姓名不能为空'
    return
  }
  if (stuMode.value === 'create' && !stuForm.student_no.trim()) {
    stuError.value = '学号不能为空'
    return
  }
  stuSaving.value = true
  stuError.value = ''
  try {
    if (stuMode.value === 'create') {
      await O.addRosterStudent(c.id, {
        student_no: stuForm.student_no.trim(),
        name: stuForm.name.trim(),
        seat_no: stuForm.seat_no.trim(),
      })
      ElMessage.success('已加入名册')
    } else if (stuEditId.value != null) {
      // ★ 刻意**不传** student_no —— 后端也不接收（改学号等于换人）
      await O.updateRosterStudent(c.id, stuEditId.value, {
        name: stuForm.name.trim(),
        seat_no: stuForm.seat_no.trim(),
      })
      ElMessage.success('已保存')
    }
    stuVisible.value = false
    await loadRoster()
    await load()
  } catch (e) {
    stuError.value = e instanceof Error ? e.message : '保存失败'
  } finally {
    stuSaving.value = false
  }
}

async function removeStu(r: O.RosterRow) {
  const c = rosterClassroom.value
  if (!c) return
  try {
    await ElMessageBox.confirm(
      `把「${r.name}（${r.student_no}）」转出该班？\n\n` +
      '★ 是**转出**不是删除：保留其历史成绩与答题记录，' +
      '状态置为「已转出」。若将来转回，会复用同一行。',
      '转出学生',
      { type: 'warning', confirmButtonText: '转出',
        cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await O.removeRosterStudent(c.id, r.id, '界面转出')
    ElMessage.success('已转出')
    await loadRoster()
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '转出失败')
  }
}

// ══════════════════════════════════════════════════════════════
//  导入（粘贴文本 / CSV）
// ══════════════════════════════════════════════════════════════

const impVisible = ref(false)
const impText = ref('')
const impReplace = ref(false)
const impBusy = ref(false)
const impError = ref('')
const impPreview = ref<Array<{ student_no: string; name: string
                               seat_no?: string }>>([])
const impValidation = ref<O.RosterValidation | null>(null)

/**
 * 解析粘贴的文本。
 *
 * ★ 容错优先：老师手上的名册来自各种表格，分隔符可能是
 *   制表符 / 逗号 / 全角逗号 / 多个空格，还可能带表头。
 *   所以这里**按行多切分符**处理，并跳过明显是表头的行。
 *
 * ★ 自动识别表头：第一行两个字段都不是纯数字学号时，视为表头丢弃。
 */
function parseRows(text: string) {
  const out: Array<{ student_no: string; name: string; seat_no?: string }> = []
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)
  lines.forEach((line, idx) => {
    const parts = line.split(/[\t,，;；]+|\s{2,}/).map((s) => s.trim())
      .filter(Boolean)
    if (parts.length < 2) return
    // 表头：首格不是「像学号」的内容就跳过（只在第一行判断）
    if (idx === 0 && !/\d/.test(parts[0])) return
    const [no, name, seat] = parts
    out.push({ student_no: no, name, seat_no: seat })
  })
  return out
}

function openImport() {
  impText.value = ''
  impReplace.value = false
  impPreview.value = []
  impValidation.value = null
  impError.value = ''
  impVisible.value = true
}

async function doPreview() {
  const c = rosterClassroom.value
  if (!c) return
  impError.value = ''
  impValidation.value = null
  const rows = parseRows(impText.value)
  impPreview.value = rows
  if (!rows.length) {
    impError.value = '没有解析出任何学生。每行至少要有「学号」和「姓名」，用 Tab、逗号或空格分隔。'
    return
  }
  impBusy.value = true
  try {
    impValidation.value = await O.validateRoster(c.id, rows)
  } catch (e) {
    // ★ 后端预检失败时会把 `blocking` 放在错误 detail 里（`BusinessBranch.data`）
    if (e instanceof BusinessBranch) {
      const d = e.data as { blocking?: O.RosterIssue[] } | undefined
      impValidation.value = {
        can_import: false,
        blocking: d?.blocking ?? [],
        warnings: [],
        summary: e.message,
      }
    } else {
      impError.value = e instanceof Error ? e.message : '预检失败'
    }
  } finally {
    impBusy.value = false
  }
}

async function doImport() {
  const c = rosterClassroom.value
  if (!c || !impPreview.value.length) return
  impBusy.value = true
  impError.value = ''
  try {
    const r = await O.importRoster(c.id, impPreview.value, impReplace.value)
    ElMessage.success(
      `导入完成：新增 ${r.created}、更新 ${r.updated}`
      + (r.left ? `、转出 ${r.left}` : ''),
    )
    impVisible.value = false
    await loadRoster()
    await load()
  } catch (e) {
    impError.value = e instanceof Error ? e.message : '导入失败'
  } finally {
    impBusy.value = false
  }
}

// ── 下载导入模板（前端生成，不依赖后端） ──

function downloadTemplate() {
  const csv = '\uFEFF学号,姓名,座位号\n'
    + '2026001,张三,01\n2026002,李四,02\n'
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '名册导入模板.csv'
  a.click()
  // ★ 必须回收：不释放会让 blob 常驻内存（大文件时明显）
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="cls">
    <!-- ══════════ 页头 ══════════ -->
    <header class="zf-page-head">
      <div>
        <h1 class="zf-page-title">班级与名册</h1>
        <p class="zf-page-desc">
          班级归你个人：这里只显示<b>你建的班</b>，同事建的班不会出现。
          名册里的<b>学号</b>是答题卡认人的唯一依据 ——
          同班内不可重复，且<b>不建议事后修改</b>（改学号等于换人）。
        </p>
      </div>
      <div class="cls__acts">
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreate">新建班级</el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      type="error"
      :title="error"
      show-icon
      :closable="false"
      style="margin-bottom: 12px"
    />

    <!-- ══════════ 筛选 + 列表 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">班级</h2>
        <span class="zf-dim">{{ filtered.length }} / {{ classrooms.length }}</span>
      </div>
      <div class="zf-card__body">
        <el-input
          v-model="keyword"
          size="small"
          placeholder="搜班级名 / 年级 / 学年 / 专业"
          clearable
          style="max-width: 320px; margin-bottom: 10px"
        />

        <el-table
          v-loading="loading"
          :data="filtered"
          size="small"
          empty-text="还没有班级 —— 点右上角「新建班级」开始"
          style="width: 100%"
        >
          <el-table-column prop="name" label="班级" min-width="180">
            <template #default="{ row }">
              <el-link type="primary" @click="openRoster(row)">
                {{ row.name }}
              </el-link>
            </template>
          </el-table-column>
          <el-table-column prop="grade" label="年级" width="90" />
          <el-table-column prop="academic_year" label="学年" width="120" />
          <el-table-column label="专业" width="150">
            <template #default="{ row }">
              {{ row.major_name || majorName.get(row.major_id) || '—' }}
            </template>
          </el-table-column>
          <!-- ★ 只有学校账户需要这一列：他看得到全校的班，必须能分辨
               每个班是谁的。教师只看到自己的班，这一列永远是同一个名字，
               纯噪音。 -->
          <el-table-column
            v-if="auth.can('org.classroom.assign')"
            label="归属"
            width="130"
          >
            <template #default="{ row }">
              <span v-if="row.owner_name">{{ row.owner_name }}</span>
              <el-tag v-else size="small" type="warning">无归属</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="在册人数" width="100" align="right">
            <template #default="{ row }">
              <b>{{ row.student_count ?? 0 }}</b>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" align="right">
            <template #default="{ row }">
              <el-button size="small" @click="openRoster(row)">名册</el-button>
              <el-button size="small" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" type="danger" plain
                         @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <!-- ══════════ 班级表单 ══════════ -->
    <el-dialog
      v-model="formVisible"
      :title="formMode === 'create' ? '新建班级' : '编辑班级'"
      width="520px"
    >
      <el-alert
        v-if="formError" type="error" :title="formError" show-icon
        :closable="false" style="margin-bottom: 12px"
      />
      <el-form label-width="88px" label-position="right">
        <!-- ★ 归属教师：只有学校账户能改（指派历史无主班 / 离职转接）。
             教师改不了自己的归属，所以这项对他不显示。 -->
        <el-form-item
          v-if="auth.can('org.classroom.assign')"
          label="归属教师"
        >
          <el-select
            v-model="form.owner_id"
            clearable
            filterable
            placeholder="未指派（任何教师都看不到这个班）"
            style="width: 100%"
          >
            <el-option
              v-for="t in teachers"
              :key="t.id"
              :label="`${t.real_name || t.username}（${t.username}）`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="班级名称" required>
          <el-input v-model="form.name" placeholder="如：机械制造 1 班" />
        </el-form-item>
        <el-form-item label="年级" required>
          <el-input v-model="form.grade" placeholder="如：2026级" />
        </el-form-item>
        <el-form-item label="学年" required>
          <el-input v-model="form.academic_year" placeholder="如：2026-2027" />
        </el-form-item>
        <el-form-item label="学段">
          <el-select v-model="form.grade_band" style="width: 100%">
            <el-option
              v-for="g in GRADE_BANDS" :key="g.value"
              :label="g.label" :value="g.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="专业">
          <el-select
            v-model="form.major_id" clearable
            placeholder="可不填" style="width: 100%"
          >
            <el-option
              v-for="m in majors" :key="m.id" :label="m.name" :value="m.id"
            />
          </el-select>
        </el-form-item>
        <p class="cls__note">
          ★ 同一学年内<b>班级名不可重复</b>（不同学年可以同名）。
        </p>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="formSaving" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 名册抽屉 ══════════ -->
    <el-drawer
      v-model="rosterVisible"
      :title="`名册 · ${rosterClassroom?.name ?? ''}`"
      size="900px"
    >
      <div v-loading="rosterLoading" class="cls__roster">
        <el-alert
          v-if="rosterError" type="error" :title="rosterError" show-icon
          :closable="false" style="margin-bottom: 10px"
        />

        <!-- 摘要：在册 / 已绑定 / 学号位数 -->
        <div class="cls__stats">
          <div class="cls__stat">
            <div class="cls__stat-n">{{ activeCount }}</div>
            <div class="cls__stat-t">在册学生</div>
          </div>
          <div class="cls__stat">
            <div class="cls__stat-n">{{ boundCount }}</div>
            <div class="cls__stat-t">已绑系统账号</div>
          </div>
          <div
            class="cls__stat"
            :class="{ 'cls__stat--warn':
                      (rosterBrief?.inferred_digits ?? null) == null }"
          >
            <div class="cls__stat-n">
              {{ rosterBrief?.inferred_digits ?? '—' }}
            </div>
            <div class="cls__stat-t">推断学号位数</div>
          </div>
        </div>

        <!-- 位数不统一告警：这会让 OMR 整片读错 -->
        <el-alert
          v-if="rosterBrief
                && Object.keys(rosterBrief.digit_distribution || {}).length > 1"
          type="warning"
          show-icon
          :closable="false"
          style="margin-bottom: 10px"
        >
          <template #title>
            <b>学号位数不统一</b> —— 答题卡的学号区按「出现最多的位数」排版，
            位数不一致的学生会整片读错。
          </template>
          <div class="cls__dim">
            {{
              Object.entries(rosterBrief.digit_distribution)
                .map(([k, v]) => `${k} 位 ${v} 人`).join('、')
            }}
          </div>
        </el-alert>

        <div class="cls__toolbar">
          <el-radio-group v-model="rosterStatus" size="small">
            <el-radio-button label="ACTIVE">在册</el-radio-button>
            <el-radio-button label="LEFT">已转出</el-radio-button>
            <el-radio-button label="">全部</el-radio-button>
          </el-radio-group>
          <div class="cls__toolbar-acts">
            <el-button size="small" @click="downloadTemplate">
              下载导入模板
            </el-button>
            <el-button size="small" @click="openImport">批量导入</el-button>
            <el-button size="small" type="primary" @click="openStuCreate">
              加一名学生
            </el-button>
          </div>
        </div>

        <el-table :data="shownRoster" size="small" style="width: 100%">
          <el-table-column prop="seat_no" label="座号" width="70" />
          <el-table-column prop="student_no" label="学号" width="140" />
          <el-table-column prop="name" label="姓名" min-width="120" />
          <el-table-column label="系统账号" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.is_bound" size="small" type="success">
                已绑定
              </el-tag>
              <el-tag v-else size="small" type="info">未绑定</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="(row.status ?? 'ACTIVE') === 'ACTIVE'
                  ? 'success' : 'warning'"
              >
                {{ (row.status ?? 'ACTIVE') === 'ACTIVE' ? '在册' : '已转出' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" align="right">
            <template #default="{ row }">
              <el-button size="small" @click="openStuEdit(row)">编辑</el-button>
              <el-button
                v-if="(row.status ?? 'ACTIVE') === 'ACTIVE'"
                size="small" type="warning" plain
                @click="removeStu(row)"
              >
                转出
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-drawer>

    <!-- ══════════ 学生表单 ══════════ -->
    <el-dialog
      v-model="stuVisible"
      :title="stuMode === 'create' ? '加一名学生' : '编辑学生'"
      width="480px"
      append-to-body
    >
      <el-alert
        v-if="stuError" type="error" :title="stuError" show-icon
        :closable="false" style="margin-bottom: 12px"
      />
      <el-form label-width="72px">
        <el-form-item label="学号" required>
          <el-input
            v-model="stuForm.student_no"
            :disabled="stuMode === 'edit'"
            placeholder="如：2026001"
          />
        </el-form-item>
        <el-form-item label="姓名" required>
          <el-input v-model="stuForm.name" />
        </el-form-item>
        <el-form-item label="座号">
          <el-input v-model="stuForm.seat_no" placeholder="可不填" />
        </el-form-item>
      </el-form>
      <el-alert
        v-if="stuMode === 'edit'"
        type="info" :closable="false" show-icon
      >
        <template #title>
          学号<b>不可修改</b>
        </template>
        <div class="cls__dim">
          它是答题卡认人的唯一依据，改学号等于换一个人（历史成绩会错挂）。
          需要换学号请「转出旧的 + 加入新的」。
        </div>
      </el-alert>
      <template #footer>
        <el-button @click="stuVisible = false">取消</el-button>
        <el-button type="primary" :loading="stuSaving" @click="saveStu">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 批量导入 ══════════ -->
    <el-dialog
      v-model="impVisible" title="批量导入名册" width="640px"
      append-to-body
    >
      <p class="cls__note">
        从 Excel / WPS 里<b>复制两列（学号、姓名）</b>粘贴到下面即可。
        支持 Tab、逗号、空格分隔，自动跳过表头行。
      </p>
      <el-input
        v-model="impText" type="textarea" :rows="8"
        placeholder="2026001&#9;张三&#10;2026002&#9;李四"
      />
      <div class="cls__improw">
        <el-checkbox v-model="impReplace">
          先清空现有名册（<b>整表替换</b>，会把现有学生标为「已转出」）
        </el-checkbox>
        <el-button size="small" :loading="impBusy" @click="doPreview">
          预检（不写库）
        </el-button>
      </div>

      <el-alert
        v-if="impError" type="error" :title="impError" show-icon
        :closable="false" style="margin-top: 10px"
      />

      <div v-if="impPreview.length" class="cls__impres">
        <div class="cls__dim">
          解析出 <b>{{ impPreview.length }}</b> 行
          <template v-if="impValidation">
            · 阻断 <b class="zf-danger">{{ impValidation.blocking.length }}</b>
            · 告警 <b>{{ impValidation.warnings.length }}</b>
          </template>
        </div>

        <el-alert
          v-if="impValidation && !impValidation.can_import"
          type="error" :closable="false" show-icon
          title="有阻断项，导入会被整体拒绝"
          style="margin: 8px 0"
        >
          <div class="cls__dim">
            ★ 不做部分导入 —— 那会让名册处于「一半旧一半新」的状态，
            比导入失败更难排查。
          </div>
        </el-alert>

        <el-table
          v-if="impValidation?.blocking.length"
          :data="impValidation.blocking" size="small"
          max-height="200" style="margin-top: 6px"
        >
          <el-table-column prop="row" label="行" width="60" />
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="detail" label="问题" min-width="220" />
        </el-table>

        <el-table
          v-else :data="impPreview.slice(0, 20)" size="small"
          max-height="200" style="margin-top: 6px"
        >
          <el-table-column prop="student_no" label="学号" width="140" />
          <el-table-column prop="name" label="姓名" min-width="120" />
          <el-table-column prop="seat_no" label="座号" width="80" />
        </el-table>
        <div v-if="impPreview.length > 20" class="cls__dim">
          仅预览前 20 行，共 {{ impPreview.length }} 行
        </div>
      </div>

      <template #footer>
        <el-button @click="impVisible = false">取消</el-button>
        <el-button
          type="primary" :loading="impBusy"
          :disabled="!impPreview.length
                     || (impValidation ? !impValidation.can_import : false)"
          @click="doImport"
        >
          确认导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cls__acts { display: flex; gap: 8px; }
.cls__note {
  margin: 0;
  font-size: 12px;
  color: var(--zf-text-tertiary);
  line-height: 1.7;
}
.cls__dim {
  font-size: 12px;
  color: var(--zf-text-tertiary);
  line-height: 1.7;
}
.cls__roster { padding-bottom: 12px; }

.cls__stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 12px;
}
.cls__stat {
  border: 1px solid var(--zf-border);
  border-radius: 8px;
  padding: 10px 14px;
  background: var(--zf-bg-subtle);
}
.cls__stat--warn { border-color: var(--zf-warning); }
.cls__stat-n {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}
.cls__stat-t {
  font-size: 12px;
  color: var(--zf-text-tertiary);
  margin-top: 2px;
}

.cls__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.cls__toolbar-acts { display: flex; gap: 8px; }

.cls__improw {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
  gap: 10px;
}
.cls__impres { margin-top: 12px; }
</style>
