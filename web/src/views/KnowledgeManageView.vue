<script setup lang="ts">
/**
 * 知识体系管理（`/knowledge/manage`）。
 *
 * ★★ 与 `/knowledge`（知识体系**浏览**页）的分工：
 *
 *     /knowledge          看指标 —— 哪个知识点没题、知识库够不够
 *     /knowledge/manage   改结构 —— 加章、加任务、加知识点、移动、删除
 *
 *   分开的理由不是「页面太长」，而是**动作性质不同**：
 *   浏览页是老师和教研天天打开的，管理页一学期动几次。
 *   把「删除知识点」这种不可逆按钮放在天天看的那一页上，迟早误点。
 *
 * ★★ 这一页的三条硬约束（都来自后端，不是前端自己定的）：
 *
 *   1. **课程编码有下级数据后锁定**（409 `KNOWLEDGE.CODE_LOCKED`）
 *      章/任务/知识点的编码都以课程编码为前缀，改它会让整棵树错位。
 *   2. **课程/知识点被引用不能删**（409 `KNOWLEDGE.IN_USE`）
 *      响应带 `data.references`，这一页把它**原样列出来**——
 *      只给一句「不能删除」等于让老师自己猜。
 *   3. **移动知识点不改编码**（`code` 是稳定标识，不是位置描述）
 *      界面上明确写出这一点，否则老师会以为「移动之后编号会跟着变」。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as K from '@/api/knowledge'
import type {
  Course,
  CourseDetail,
  CourseTree,
  EntityReference,
  KnowledgeNodeDetailWithRefs,
  TreeNode,
} from '@/api/knowledge'
import { ApiError } from '@/api/http'

// ══════════════════════════════════════════════════
//  基础状态
// ══════════════════════════════════════════════════

const courses = ref<Course[]>([])
const courseId = ref<number | undefined>(undefined)
const courseDetail = ref<CourseDetail | null>(null)
const tree = ref<CourseTree | null>(null)
const loading = ref(false)
const error = ref('')

interface FlatNode {
  key: string
  type: 'CHAPTER' | 'TASK' | 'NODE'
  id: number
  name: string
  code: string
  data: TreeNode
  children?: FlatNode[]
}

const selected = ref<FlatNode | null>(null)
const nodeDetail = ref<KnowledgeNodeDetailWithRefs | null>(null)

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : '请求失败'
}

/** 从 409 里取引用清单。★ `ApiError.data` 就是后端 `_build_response` 的 `data`。 */
function refsFromError(e: unknown): EntityReference[] {
  if (e instanceof ApiError) {
    const r = e.extra<EntityReference[]>('references')
    if (Array.isArray(r)) return r
  }
  return []
}

/**
 * 树数据。
 *
 * ★★ `node-key` 要拼 `type-id`：章 / 任务 / 知识点在**三张表**里，
 *   主键各自从 1 开始 —— 章 1 与知识点 1 是两条完全不同的记录，
 *   直接用 `id` 当 key 会让 el-tree 把两次点击认成同一个节点，
 *   表现为「点了知识点却展开/高亮了章」。
 */
const treeData = computed<FlatNode[]>(() => {
  if (!tree.value) return []
  const out: FlatNode[] = []
  for (const p of tree.value.parts) {
    for (const ch of p.chapters ?? []) {
      const chapter: FlatNode = {
        key: `CHAPTER-${ch.id}`,
        type: 'CHAPTER',
        id: ch.id,
        name: ch.name,
        code: ch.code,
        data: ch,
        children: [],
      }
      for (const t of ch.children ?? []) {
        const task: FlatNode = {
          key: `TASK-${t.id}`,
          type: 'TASK',
          id: t.id,
          name: t.name,
          code: t.code,
          data: t,
          children: [],
        }
        for (const n of t.children ?? []) {
          task.children!.push({
            key: `NODE-${n.id}`,
            type: 'NODE',
            id: n.id,
            name: n.name,
            code: n.code,
            data: n,
          })
        }
        chapter.children!.push(task)
      }
      out.push(chapter)
    }
  }
  return out
})

/** 全部任务（移动目标下拉）。★ 含所在章，跨章移动时老师要知道自己选的是哪一章。 */
const allTasks = computed(() => {
  const out: Array<{ id: number; label: string; chapterName: string }> = []
  for (const ch of treeData.value) {
    for (const t of ch.children ?? []) {
      out.push({
        id: t.id,
        label: `${t.name}（${t.code}）`,
        chapterName: ch.name,
      })
    }
  }
  return out
})

/** 章下拉（建任务用） */
const allChapters = computed(() =>
  treeData.value.map((c) => ({ id: c.id, label: `${c.name}（${c.code}）` })),
)

// ══════════════════════════════════════════════════
//  加载
// ══════════════════════════════════════════════════

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
    const [t, d] = await Promise.all([
      K.getCourseTree(courseId.value),
      K.getCourse(courseId.value),
    ])
    tree.value = t
    courseDetail.value = d
    // 选中的节点在刷新后可能已经不在树里（被删/被移动），清掉
    if (selected.value) {
      const still = findNode(selected.value.key)
      if (!still) clearSelection()
    }
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    await loadCourses()
    await loadCourse()
  } catch (e) {
    error.value = errMsg(e)
  }
})

watch(courseId, () => {
  clearSelection()
  void loadCourse()
})

function findNode(key: string): FlatNode | null {
  const walk = (rows: FlatNode[]): FlatNode | null => {
    for (const r of rows) {
      if (r.key === key) return r
      const hit = walk(r.children ?? [])
      if (hit) return hit
    }
    return null
  }
  return walk(treeData.value)
}

function clearSelection(): void {
  selected.value = null
  nodeDetail.value = null
}

async function onNodeClick(node: FlatNode): Promise<void> {
  selected.value = node
  nodeDetail.value = null
  if (node.type !== 'NODE') return
  try {
    nodeDetail.value = await K.getNodeWithRefs(node.id)
  } catch (e) {
    // ★ 详情读失败不该让整页报错，右侧降级为「只有树上的信息」
    nodeDetail.value = null
    error.value = errMsg(e)
  }
}

/** 课程为什么不能删 / 不能改编码 —— 直接显示后端的清点结果。 */
const courseRefs = computed<EntityReference[]>(
  () => courseDetail.value?.references ?? [],
)
const courseLocked = computed(() => courseRefs.value.length > 0)

/** 知识点引用：只把 `blocking` 的当「拦住删除的原因」。 */
const nodeRefs = computed<EntityReference[]>(
  () => (nodeDetail.value?.references ?? []).filter(
    (r) => r.blocking !== false),
)
const nodeSoftRefs = computed<EntityReference[]>(
  () => (nodeDetail.value?.references ?? []).filter(
    (r) => r.blocking === false),
)

// ══════════════════════════════════════════════════
//  课程表单
// ══════════════════════════════════════════════════

const courseDlg = ref(false)
const courseMode = ref<'create' | 'edit'>('create')
const courseSaving = ref(false)
const courseForm = reactive({
  name: '',
  code: '',
  total_hours: 64 as number | null,
  sort_order: 0,
  status: 'ACTIVE',
})

function openCourseCreate(): void {
  courseMode.value = 'create'
  courseForm.name = ''
  courseForm.code = ''
  courseForm.total_hours = 64
  courseForm.sort_order = 0
  courseForm.status = 'ACTIVE'
  courseDlg.value = true
}

function openCourseEdit(): void {
  if (!courseDetail.value) return
  courseMode.value = 'edit'
  courseForm.name = courseDetail.value.name
  courseForm.code = courseDetail.value.code
  courseForm.total_hours = (courseDetail.value as { total_hours?: number | null })
    .total_hours ?? null
  courseForm.sort_order = courseDetail.value.sort_order ?? 0
  courseForm.status = courseDetail.value.status ?? 'ACTIVE'
  courseDlg.value = true
}

async function saveCourse(): Promise<void> {
  if (!courseForm.name.trim()) {
    ElMessage.warning('课程名不能为空')
    return
  }
  if (!courseForm.code.trim()) {
    ElMessage.warning('课程编码不能为空（它是全部知识点编码的前缀）')
    return
  }
  courseSaving.value = true
  try {
    const body: K.CourseWriteBody = {
      name: courseForm.name.trim(),
      code: courseForm.code.trim().toUpperCase(),
      total_hours: courseForm.total_hours,
      sort_order: courseForm.sort_order,
      status: courseForm.status,
    }
    if (courseMode.value === 'create') {
      const c = await K.createCourse(body)
      ElMessage.success(`课程「${c.name}」已创建`)
      await loadCourses()
      // ★ 建完直接切过去：老师新建课程的意图就是「开始往里加章」
      courseId.value = c.id
    } else if (courseId.value) {
      await K.updateCourse(courseId.value, body)
      ElMessage.success('课程已更新')
      await loadCourse()
      await loadCourses()
    }
    courseDlg.value = false
  } catch (e) {
    // ★ 改编码被锁时列引用，别只说「失败」
    const rs = refsFromError(e)
    if (rs.length) {
      showRefs('课程编码已锁定 / 不能删除的原因', rs)
    } else {
      ElMessage.error(errMsg(e))
    }
  } finally {
    courseSaving.value = false
  }
}

async function removeCourse(): Promise<void> {
  if (!courseId.value || !courseDetail.value) return
  try {
    await ElMessageBox.confirm(
      `删除课程「${courseDetail.value.name}」？\n\n`
      + '只有**完全没有章、题、卷**的课程才能删。\n'
      + '想保留数据但不再使用，请把状态改为「归档」。',
      '删除课程',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await K.deleteCourse(courseId.value)
    ElMessage.success('课程已删除')
    courseId.value = undefined
    await loadCourses()
    await loadCourse()
  } catch (e) {
    const rs = refsFromError(e)
    if (rs.length) showRefs('这门课还有下级数据，不能删除', rs)
    else ElMessage.error(errMsg(e))
  }
}

// ══════════════════════════════════════════════════
//  章 / 任务表单
// ══════════════════════════════════════════════════

const chapterDlg = ref(false)
const chapterSaving = ref(false)
const chapterForm = reactive({
  seq: 1,
  name: '',
  part_seq: null as number | null,
  part_name: '',
})

function openChapterCreate(): void {
  chapterForm.seq = treeData.value.length + 1
  chapterForm.name = ''
  chapterForm.part_seq = null
  chapterForm.part_name = ''
  chapterDlg.value = true
}

async function saveChapter(): Promise<void> {
  if (!courseId.value) return
  if (!chapterForm.name.trim()) {
    ElMessage.warning('章名不能为空')
    return
  }
  chapterSaving.value = true
  try {
    await K.createChapter({
      course_id: courseId.value,
      name: chapterForm.name.trim(),
      seq: chapterForm.seq,
      part_seq: chapterForm.part_seq || null,
      part_name: chapterForm.part_name.trim(),
    })
    ElMessage.success('章已创建')
    chapterDlg.value = false
    await loadCourse()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    chapterSaving.value = false
  }
}

const taskDlg = ref(false)
const taskSaving = ref(false)
const taskForm = reactive({
  chapter_id: null as number | null,
  seq: 1,
  name: '',
  is_practical: false,
})

function openTaskCreate(chapterId?: number): void {
  taskForm.chapter_id = chapterId ?? allChapters.value[0]?.id ?? null
  const ch = treeData.value.find((c) => c.id === taskForm.chapter_id)
  taskForm.seq = (ch?.children?.length ?? 0) + 1
  taskForm.name = ''
  taskForm.is_practical = false
  taskDlg.value = true
}

async function saveTask(): Promise<void> {
  if (!taskForm.chapter_id) {
    ElMessage.warning('请选择所属章')
    return
  }
  if (!taskForm.name.trim()) {
    ElMessage.warning('任务名不能为空')
    return
  }
  taskSaving.value = true
  try {
    await K.createTask({
      chapter_id: taskForm.chapter_id,
      name: taskForm.name.trim(),
      seq: taskForm.seq,
      is_practical: taskForm.is_practical,
    })
    ElMessage.success('任务已创建')
    taskDlg.value = false
    await loadCourse()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    taskSaving.value = false
  }
}

// ══════════════════════════════════════════════════
//  知识点表单（新建 / 编辑 + 移动）
// ══════════════════════════════════════════════════

const nodeDlg = ref(false)
const nodeMode = ref<'create' | 'edit'>('create')
const nodeSaving = ref(false)
const nodeForm = reactive({
  task_id: null as number | null,
  name: '',
  code: '',
  seq_in_task: 1 as number | null,
  exam_weight: 1,
  sort_order: 0,
  status: 'ACTIVE',
  deprecated: false,
})

function openNodeCreate(taskId?: number): void {
  nodeMode.value = 'create'
  nodeForm.task_id = taskId ?? allTasks.value[0]?.id ?? null
  const task = treeData.value
    .flatMap((c) => c.children ?? [])
    .find((t) => t.id === nodeForm.task_id)
  const siblings = task?.children ?? []
  nodeForm.seq_in_task = siblings.length + 1
  nodeForm.name = ''
  nodeForm.exam_weight = 1
  nodeDlg.value = true
}

function openNodeEdit(): void {
  if (!nodeDetail.value || selected.value?.type !== 'NODE') return
  nodeMode.value = 'edit'
  nodeForm.task_id = nodeDetail.value.task_id
  nodeForm.name = nodeDetail.value.name
  nodeForm.code = nodeDetail.value.code
  nodeForm.seq_in_task = nodeDetail.value.seq_in_task
  nodeForm.exam_weight = Number(nodeDetail.value.exam_weight ?? 1)
  nodeForm.sort_order = nodeDetail.value.sort_order ?? 0
  nodeForm.status = nodeDetail.value.status ?? 'ACTIVE'
  nodeForm.deprecated = false
  nodeDlg.value = true
}

async function saveNode(): Promise<void> {
  if (!nodeForm.name.trim()) {
    ElMessage.warning('知识点名不能为空')
    return
  }
  nodeSaving.value = true
  try {
    if (nodeMode.value === 'create') {
      if (!nodeForm.task_id) {
        ElMessage.warning('请选择所属任务')
        return
      }
      // ★ 建节点走的是既有端点（`POST /knowledge/nodes/`），
      //   它的必填项是 task_id + name + seq_in_task。
      await K.createNode({
        task_id: nodeForm.task_id,
        name: nodeForm.name.trim(),
        seq_in_task: nodeForm.seq_in_task ?? 1,
        exam_weight: nodeForm.exam_weight,
      })
      ElMessage.success('知识点已创建')
    } else if (selected.value?.type === 'NODE') {
      const body: K.NodeWriteBody = {
        name: nodeForm.name.trim(),
        seq_in_task: nodeForm.seq_in_task,
        exam_weight: nodeForm.exam_weight,
        sort_order: nodeForm.sort_order,
        status: nodeForm.status,
        deprecated: nodeForm.deprecated,
        task_id: nodeForm.task_id ?? undefined,
      }
      await K.updateNode(selected.value.id, body)
      ElMessage.success('知识点已更新')
    }
    nodeDlg.value = false
    await loadCourse()
    if (selected.value?.type === 'NODE') await onNodeClick(selected.value)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    nodeSaving.value = false
  }
}

async function removeNode(): Promise<void> {
  if (selected.value?.type !== 'NODE') return
  const name = selected.value.name
  try {
    await ElMessageBox.confirm(
      `删除知识点「${name}」？\n\n`
      + '只要有题目、知识库条目或学生掌握度挂在它上面，就会被拒绝。\n'
      + '想保留历史数据，请改为「废弃」。',
      '删除知识点',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await K.deleteNode(selected.value.id)
    ElMessage.success('知识点已删除')
    clearSelection()
    await loadCourse()
  } catch (e) {
    const rs = refsFromError(e)
    if (rs.length) showRefs('这个知识点还有引用，不能删除', rs)
    else ElMessage.error(errMsg(e))
  }
}

// ══════════════════════════════════════════════════
//  引用清单弹窗
// ══════════════════════════════════════════════════

const refsDlg = ref(false)
const refsTitle = ref('')
const refsRows = ref<EntityReference[]>([])

function showRefs(title: string, rows: EntityReference[]): void {
  refsTitle.value = title
  refsRows.value = rows
  refsDlg.value = true
}
</script>

<template>
  <div class="km">
    <!-- ══════════ 顶栏 ══════════ -->
    <div class="km__head">
      <div>
        <h1 class="km__title">知识体系管理</h1>
        <p class="zf-muted km__sub">
          课程 → 章 → 任务 → 知识点。改结构在这里，看指标去
          <RouterLink :to="{ name: 'Knowledge' }">知识体系</RouterLink>。
        </p>
      </div>
      <div class="zf-row">
        <el-select
          v-model="courseId"
          placeholder="选择课程"
          style="width: 200px"
          filterable
        >
          <el-option
            v-for="c in courses"
            :key="c.id"
            :label="`${c.name}（${c.code}）`"
            :value="c.id"
          />
        </el-select>
        <el-button @click="openCourseCreate">新建课程</el-button>
        <el-button :disabled="!courseDetail" @click="openCourseEdit">
          改课程
        </el-button>
        <el-button
          type="danger"
          plain
          :disabled="!courseDetail"
          @click="removeCourse"
        >
          删课程
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <!-- ── 课程级提示：编码被锁 / 不能删 ── -->
    <el-alert
      v-if="courseLocked"
      type="info"
      :closable="false"
      show-icon
    >
      <template #title>
        课程「{{ courseDetail?.name }}」下已有
        {{ courseRefs.map((r) => `${r.label} ${r.count}`).join('、') }}
      </template>
      <p class="km__note">
        因此<strong>课程编码不可修改</strong>、<strong>课程不可删除</strong>——
        章 / 任务 / 知识点的编码都以前缀
        <span class="zf-mono">{{ courseDetail?.code }}</span> 生成。
        不想再使用请把状态改为「归档」。
      </p>
    </el-alert>

    <div class="km__body">
      <!-- ══════════ 左：树 ══════════ -->
      <section class="zf-card km__tree">
        <div class="zf-card__head">
          <h2 class="zf-card__title">结构</h2>
          <div class="zf-row">
            <el-button size="small" @click="openChapterCreate">新建章</el-button>
            <el-button size="small" :disabled="!courseId" @click="openTaskCreate()">
              新建任务
            </el-button>
            <el-button
              size="small"
              :disabled="!selected || selected.type === 'NODE'"
              @click="openNodeCreate(selected?.type === 'TASK'
                ? selected.id : undefined)"
            >
              新建知识点
            </el-button>
            <el-button size="small" @click="loadCourse">刷新</el-button>
          </div>
        </div>
        <div v-loading="loading" class="zf-card__body km__treebody">
          <el-tree
            v-if="treeData.length"
            :data="treeData"
            node-key="key"
            :props="{ label: 'name', children: 'children' }"
            default-expand-all
            highlight-current
            :expand-on-click-node="false"
            @node-click="onNodeClick"
          >
            <template #default="{ data }">
              <span class="km__node">
                <el-tag
                  size="small"
                  :type="data.type === 'CHAPTER' ? 'info'
                    : data.type === 'TASK' ? 'success' : 'warning'"
                  effect="plain"
                >
                  {{ data.type === 'CHAPTER' ? '章'
                    : data.type === 'TASK' ? '任务' : '知识点' }}
                </el-tag>
                <span class="km__nodename">{{ data.name }}</span>
                <span class="zf-mono zf-dim km__code">{{ data.code }}</span>
                <span
                  v-if="data.type === 'NODE'"
                  class="zf-dim km__entry"
                >知识库 {{ data.data.entry_count ?? 0 }}</span>
              </span>
            </template>
          </el-tree>
          <div v-else-if="!loading" class="zf-empty">
            这门课还没有章。点「新建章」开始。
          </div>
        </div>
      </section>

      <!-- ══════════ 右：详情 ══════════ -->
      <section class="zf-card km__detail">
        <div class="zf-card__head">
          <h2 class="zf-card__title">
            <template v-if="selected">{{ selected.name }}</template>
            <template v-else>未选择</template>
          </h2>
          <div class="zf-row">
            <el-button
              v-if="selected?.type === 'NODE'"
              size="small"
              @click="openNodeEdit"
            >
              编辑 / 移动
            </el-button>
            <el-button
              v-if="selected?.type === 'NODE'"
              size="small"
              type="danger"
              plain
              @click="removeNode"
            >
              删除
            </el-button>
            <el-button
              v-if="selected?.type === 'CHAPTER'"
              size="small"
              @click="openTaskCreate(selected.id)"
            >
              在本章建任务
            </el-button>
            <el-button
              v-if="selected?.type === 'TASK'"
              size="small"
              @click="openNodeCreate(selected.id)"
            >
              在任务下建知识点
            </el-button>
          </div>
        </div>

        <div class="zf-card__body">
          <div v-if="!selected" class="zf-empty">
            在左侧选一个章 / 任务 / 知识点
          </div>

          <template v-else>
            <div class="zf-kv">
              <span class="zf-kv__k">编码</span>
              <span class="zf-kv__v"><span class="zf-mono">{{ selected.code }}</span></span>
            </div>
            <div class="zf-kv">
              <span class="zf-kv__k">层级</span>
              <span class="zf-kv__v">{{ selected.type }}</span>
            </div>

            <!-- 章 -->
            <template v-if="selected.type === 'CHAPTER'">
              <div class="zf-kv">
                <span class="zf-kv__k">章号</span>
                <span class="zf-kv__v">{{ selected.data.seq }}</span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">篇</span>
                <span class="zf-kv__v">
                  {{ selected.data.part_name || '（未设）' }}
                  <span v-if="selected.data.part_seq" class="zf-dim">
                    · 第 {{ selected.data.part_seq }} 篇
                  </span>
                </span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">任务数</span>
                <span class="zf-kv__v">{{ selected.children?.length ?? 0 }}</span>
              </div>
            </template>

            <!-- 任务 -->
            <template v-if="selected.type === 'TASK'">
              <div class="zf-kv">
                <span class="zf-kv__k">章内序号</span>
                <span class="zf-kv__v">{{ selected.data.seq }}</span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">实训任务</span>
                <span class="zf-kv__v">
                  {{ selected.data.is_practical ? '是' : '否' }}
                </span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">知识点数</span>
                <span class="zf-kv__v">{{ selected.children?.length ?? 0 }}</span>
              </div>
            </template>

            <!-- 知识点 -->
            <template v-if="selected.type === 'NODE'">
              <div class="zf-kv">
                <span class="zf-kv__k">教材序号</span>
                <span class="zf-kv__v">{{ nodeDetail?.seq_in_task ?? '—' }}</span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">考纲权重</span>
                <span class="zf-kv__v">{{ nodeDetail?.exam_weight ?? '—' }}</span>
              </div>
              <div class="zf-kv">
                <span class="zf-kv__k">状态</span>
                <span class="zf-kv__v">{{ nodeDetail?.status ?? '—' }}</span>
              </div>

              <el-alert
                v-if="nodeRefs.length"
                type="warning"
                :closable="false"
                show-icon
                class="km__refs"
              >
                <template #title>
                  已被引用，不能删除：{{
                    nodeRefs.map((r) => `${r.label} ${r.count}`).join('、')
                  }}
                </template>
                <p v-for="r in nodeRefs" :key="r.kind" class="km__note">
                  · {{ r.label }} {{ r.count }}
                  <span v-if="r.hint" class="zf-dim">—— {{ r.hint }}</span>
                </p>
              </el-alert>

              <el-alert
                v-else-if="nodeDetail"
                type="success"
                :closable="false"
                show-icon
                title="没有引用，可以删除"
                class="km__refs"
              />

              <p v-if="nodeSoftRefs.length" class="zf-dim km__note">
                删除时会顺带清理：{{
                  nodeSoftRefs.map((r) => `${r.label} ${r.count}`).join('、')
                }}
              </p>

              <p class="zf-dim km__note">
                ★ 移动知识点到别的任务时，<strong>编码不会重算</strong>——
                编码是稳定标识（题目引用它、老师教案上写着它），
                不是位置描述。
              </p>
            </template>
          </template>
        </div>
      </section>
    </div>

    <!-- ══════════ 课程表单 ══════════ -->
    <el-dialog
      v-model="courseDlg"
      :title="courseMode === 'create' ? '新建课程' : '修改课程'"
      width="460px"
    >
      <el-form label-width="88px">
        <el-form-item label="课程名" required>
          <el-input v-model="courseForm.name" placeholder="机械基础" />
        </el-form-item>
        <el-form-item label="课程编码" required>
          <el-input
            v-model="courseForm.code"
            placeholder="JXJC"
            :disabled="courseMode === 'edit' && courseLocked"
          />
          <div class="km__fieldnote">
            <template v-if="courseMode === 'edit' && courseLocked">
              已有下级数据，<strong>编码已锁定</strong>（改它会让整棵树错位）。
            </template>
            <template v-else>
              全部章 / 任务 / 知识点编码以此为前缀，确定后尽量别改。
            </template>
          </div>
        </el-form-item>
        <el-form-item label="总课时">
          <el-input-number
            v-model="courseForm.total_hours"
            :min="0"
            :max="2000"
          />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="courseForm.sort_order" :min="0" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="courseForm.status" style="width: 100%">
            <el-option label="启用" value="ACTIVE" />
            <el-option label="归档" value="ARCHIVED" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="courseDlg = false">取消</el-button>
        <el-button type="primary" :loading="courseSaving" @click="saveCourse">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 新建章 ══════════ -->
    <el-dialog v-model="chapterDlg" title="新建章" width="420px">
      <el-form label-width="88px">
        <el-form-item label="章号" required>
          <el-input-number v-model="chapterForm.seq" :min="1" :max="99" />
        </el-form-item>
        <el-form-item label="章名" required>
          <el-input v-model="chapterForm.name" placeholder="第1章 金属材料" />
        </el-form-item>
        <el-form-item label="篇号">
          <el-input-number
            v-model="chapterForm.part_seq"
            :min="1"
            :max="99"
            placeholder="可空"
          />
        </el-form-item>
        <el-form-item label="篇名">
          <el-input v-model="chapterForm.part_name" placeholder="可空" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="chapterDlg = false">取消</el-button>
        <el-button type="primary" :loading="chapterSaving" @click="saveChapter">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 新建任务 ══════════ -->
    <el-dialog v-model="taskDlg" title="新建任务" width="420px">
      <el-form label-width="88px">
        <el-form-item label="所属章" required>
          <el-select v-model="taskForm.chapter_id" style="width: 100%">
            <el-option
              v-for="c in allChapters"
              :key="c.id"
              :label="c.label"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="章内序号" required>
          <el-input-number v-model="taskForm.seq" :min="1" :max="99" />
        </el-form-item>
        <el-form-item label="任务名" required>
          <el-input v-model="taskForm.name" placeholder="任务1 认识金属材料" />
        </el-form-item>
        <el-form-item label="实训">
          <el-switch v-model="taskForm.is_practical" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="taskDlg = false">取消</el-button>
        <el-button type="primary" :loading="taskSaving" @click="saveTask">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 知识点表单 ══════════ -->
    <el-dialog
      v-model="nodeDlg"
      :title="nodeMode === 'create' ? '新建知识点' : '编辑 / 移动知识点'"
      width="480px"
    >
      <el-form label-width="96px">
        <el-form-item :label="nodeMode === 'create' ? '所属任务' : '移到任务'" required>
          <el-select v-model="nodeForm.task_id" style="width: 100%" filterable>
            <el-option
              v-for="t in allTasks"
              :key="t.id"
              :label="`${t.chapterName} / ${t.label}`"
              :value="t.id"
            />
          </el-select>
          <div v-if="nodeMode === 'edit'" class="km__fieldnote">
            换任务 = 移动（可跨章、跨课程）。<strong>编码不变。</strong>
          </div>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="nodeForm.name" placeholder="金属材料的力学性能" />
        </el-form-item>
        <el-form-item label="教材序号">
          <el-input-number
            v-model="nodeForm.seq_in_task"
            :min="1"
            :max="99"
          />
        </el-form-item>
        <el-form-item label="考纲权重">
          <el-input-number
            v-model="nodeForm.exam_weight"
            :min="0"
            :max="9.99"
            :step="0.5"
          />
        </el-form-item>
        <template v-if="nodeMode === 'edit'">
          <el-form-item label="排序">
            <el-input-number v-model="nodeForm.sort_order" :min="0" />
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="nodeForm.status" style="width: 100%">
              <el-option label="启用" value="ACTIVE" />
              <el-option label="已归档/已并入" value="ARCHIVED" />
            </el-select>
          </el-form-item>
          <el-form-item label="废弃">
            <el-switch v-model="nodeForm.deprecated" />
            <span class="zf-dim km__inline">
              考纲已删、不再参与组卷。与「状态=ARCHIVED」不同：
              废弃保留历史引用，归档表示已并入别处。
            </span>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="nodeDlg = false">取消</el-button>
        <el-button type="primary" :loading="nodeSaving" @click="saveNode">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 引用清单 ══════════ -->
    <el-dialog v-model="refsDlg" :title="refsTitle" width="560px">
      <el-table :data="refsRows" size="small" border>
        <el-table-column prop="label" label="类型" width="110" />
        <el-table-column prop="count" label="数量" width="80" align="right" />
        <el-table-column label="示例">
          <template #default="{ row }">
            <span v-if="row.samples?.length" class="zf-dim">
              {{ row.samples.join('；') }}
            </span>
            <span v-else class="zf-dim">—</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="km__refsnote">
        <p v-for="r in refsRows" :key="r.kind" class="zf-muted km__note">
          <strong>{{ r.label }}</strong>
          <span v-if="r.hint">：{{ r.hint }}</span>
        </p>
      </div>
      <template #footer>
        <el-button type="primary" @click="refsDlg = false">知道了</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.km__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}

.km__title {
  margin: 0;
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}

.km__sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}

.km__body {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: var(--zf-space-4);
  align-items: start;
}

.km__treebody {
  max-height: 62vh;
  overflow: auto;
}

.km__node {
  display: inline-flex;
  align-items: center;
  gap: var(--zf-space-2);
  font-size: var(--zf-text-sm);
  max-width: 100%;
}

.km__nodename {
  color: var(--zf-text-primary);
}

.km__code {
  font-size: var(--zf-text-xs);
}

.km__entry {
  font-size: var(--zf-text-xs);
}

.km__note {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}

.km__refs {
  margin: var(--zf-space-3) 0;
}

.km__refsnote {
  margin-top: var(--zf-space-3);
}

.km__fieldnote {
  margin-top: 2px;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary, var(--zf-text-secondary));
  line-height: var(--zf-leading-loose);
}

.km__inline {
  margin-left: var(--zf-space-2);
  font-size: var(--zf-text-xs);
}

@media (max-width: 1100px) {
  .km__body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
