<script setup lang="ts">
/**
 * 题库列表。
 *
 * ★★ 一个刻意的默认值：**`onlyUsable` 默认勾选**。
 *
 *   后端有两条查询路径：普通列表（含草稿题）与 `only_usable=true`
 *   （只给能组卷的题）。老师打开题库最常做的事是「挑题组卷」，
 *   而草稿题选了也组不进卷—— 默认给草稿题只会让人白费功夫。
 *   需要审草稿时**主动取消勾选**，这个动作比反过来更符合直觉。
 *
 * ★ 分页用后端返回的 `total`，不是 `items.length` ——
 *   后者只是当前页的条数。
 *
 * ★★ 搜索参数名是 **`keyword`**（后端读这个）。
 *   曾写 `search`，被后端静默忽略 → **搜索框一直返回全部 1944 题**，
 *   而且不报错。这类错只有「真发请求比对 total」才能发现。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import QuestionCreateDrawer from '@/components/QuestionCreateDrawer.vue'
import QuestionDetailDrawer from '@/components/QuestionDetailDrawer.vue'
import AiGenerateDrawer from '@/components/AiGenerateDrawer.vue'
import { ApiError } from '@/api/http'
import * as Q from '@/api/questionbank'
import MathText from '@/components/MathText.vue'
import type { QuestionBrief } from '@/api/questionbank'
// ★ 知识点下拉（`P1-25b`）要用到课程与知识点列表
import * as K from '@/api/knowledge'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/**
 * ★★ 题库双轨（v2.0）：`mine` = 本校私有题，`public` = 平台公共库。
 *
 *   - 私有 Tab：原有全部行为（新建 / 编辑 / 审核 / 批量通过）。
 *   - 公共 Tab：**只读**，唯一的动作是「下载到本校」。
 *     公共题不能在这里改、也不能直接组卷（决策 3：先下载成副本）。
 *
 * ★ 初始值从 URL 读（`?bank=public`），切换时写回 URL：
 *   这样「分享一个链接说『这道公共题不错』」是可用的，
 *   刷新也不会跳回私有 Tab。
 */
const bank = ref<'private' | 'public'>(
  route.query.bank === 'public' ? 'public' : 'private',
)
const isPublic = computed(() => bank.value === 'public')

const filters = reactive({
  search: '',
  type: '',
  status: '',
  difficultyBand: '',
  hasFigure: '' as '' | 'true' | 'false',
  /** ★ 只看「配图待确认」的题（首页待办卡带 `?figurePending=1` 跳进来） */
  figurePending: route.query.figurePending === '1',
  onlyUsable: true,
})

/**
 * 按知识点筛选（来自 URL `?nodeId=`）。
 *
 * ★ 知识体系页的「看该知识点的题」按钮会带这个参数跳过来。
 *   放在 URL 而不是组件状态：这样**刷新与分享都还筛着同一个知识点**，
 *   而老师从知识体系跳过来时最不想丢的就是「我在看哪个点」。
 */
const nodeId = ref<number | undefined>(
  Number(route.query.nodeId) > 0 ? Number(route.query.nodeId) : undefined,
)

/**
 * 知识点下拉的选项（`P1-25b`）。
 *
 * ★★ 为什么要在这里加载：题库页的知识点筛选原来**只能靠从知识体系页
 *   带 `?nodeId=` 跳进来** —— 想换一个知识点就得回知识体系重新找。
 *   后端一直支持 `node_id`，缺的只是这个选择器。
 *
 * ★ 数据源是 `listCourseNodes(courseId)`（该接口按课程给知识点，
 *   带 `question_count` 等指标）。本页不关心指标，只取 `id/name/code`。
 *
 * ★ 取**第一门课**：本期只有一门《机械基础》（`course=1`）。
 *   多课程时这里应换成「先选课程、再选知识点」两级 ——
 *   但**不要现在假装支持**（那会给出一个选不到别的课的下拉）。
 */
const nodeOptions = ref<Array<{ id: number; name: string; code: string }>>([])

async function loadNodeOptions() {
  try {
    const courses = await K.listCourses()
    const first = (courses.items ?? [])[0]
    if (!first) return
    // ★ `listCourseNodes` 返回**裸数组**（不是 `Paged`）—— 别写成 `.items`
    const nodes = await K.listCourseNodes(first.id)
    nodeOptions.value = (nodes ?? []).map((n) => ({
      id: n.id, name: n.name, code: n.code,
    }))
  } catch {
    // ★ 拉不到知识点不该让题库页整体报错 —— 下拉空着即可，
    //   老师仍可用 URL 带 `?nodeId=` 进来（那条路不依赖这个请求）
    nodeOptions.value = []
  }
}

const rows = ref<QuestionBrief[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const errorMsg = ref('')
const stats = ref<Q.QuestionStats | null>(null)
const selected = ref<QuestionBrief[]>([])

/** 详情抽屉 */
const detailOpen = ref(false)
const detailId = ref<number | null>(null)
/** 新建抽屉 */
const createOpen = ref(false)

/**
 * AI 生题抽屉。
 *
 * ★★ 为什么入口放在题库页，而不是单独开一个「AI 出题」菜单页：
 *
 *   老师做生题这件事的**上下文是题库** —— 他要先知道现在库里有什么、
 *   什么题型缺、哪些知识点还没题。跳到一个独立的页面去做，
 *   等于要求他先记住这些再过去。
 *
 *   ★ 生成结束后的下一步也在这里：「在题库里看这些草稿」、
 *     点开某道题审阅（详情抽屉就在同页）。
 *
 * ★ 只在本校私有库 Tab 出现：公共题是别校提交、平台审核过的，
 *   在这里「生成一批」既落不到公共库（生题恒落本机构 DRAFT），
 *   也会让人以为生成了公共题。
 */
const aiOpen = ref(false)

const DIFFICULTY_BANDS = [
  { value: '', label: '全部难度' },
  { value: 'EASY', label: '易' },
  { value: 'MEDIUM', label: '中' },
  { value: 'HARD', label: '难' },
]

async function load(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const data = await Q.listQuestions({
      page: page.value,
      pageSize: pageSize.value,
      keyword: filters.search || undefined,
      type: filters.type || undefined,
      status: filters.status || undefined,
      difficultyBand: filters.difficultyBand || undefined,
      hasFigure: filters.hasFigure === '' ? undefined : filters.hasFigure === 'true',
      // ★ 公共 Tab 下这两个筛选没有意义（公共题不看配图待确认、也不能组卷），
      //   一并置空，避免把私有 Tab 的状态带到公共查询里
      figurePending: isPublic.value ? undefined : filters.figurePending,
      onlyUsable: isPublic.value ? undefined : filters.onlyUsable,
      nodeId: nodeId.value,
      bank: bank.value,
    })
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    errorMsg.value = String((e as Error)?.message ?? e)
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await Q.getQuestionStats()
  } catch {
    stats.value = null
  }
}

/** 改筛选条件要回到第 1 页 —— 否则会停在一个不存在的页码上 */
watch(filters, () => {
  page.value = 1
  void load()
})
watch([page, pageSize], () => void load())
// 知识点筛选变了也要回第 1 页并重新拉
watch(nodeId, () => {
  page.value = 1
  void load()
})

onMounted(async () => {
  // ★ 知识点下拉与主列表**并行**加载 —— 它是独立请求，
  //   串行会让首屏白等一个不阻塞列表的往返
  void loadNodeOptions()
  await Promise.all([load(), loadStats()])
  // ★ 深链：`/questions?open=<id>` 直接打开某题的详情抽屉。
  //   好处有两个：① 从别处（学情、批改）可以链到具体题目；
  //   ② 截图 / 手工验证时可以给一个确定的 URL，
  //   不必靠脚本去点按钮（那种做法依赖渲染时序，很脆）。
  const openId = Number(route.query.open)
  if (Number.isFinite(openId) && openId > 0) {
    detailId.value = openId
    detailOpen.value = true
  }
})

/** 清掉知识点筛选（并同步 URL） */
function clearNodeFilter(): void {
  nodeId.value = undefined
  const q = { ...route.query }
  delete q.nodeId
  void router.replace({ query: q })
}

/** 打开详情时把 id 写回 URL，刷新/分享都还能定位到同一题。 */
watch(detailOpen, (open) => {
  const q = { ...route.query }
  if (open && detailId.value) q.open = String(detailId.value)
  else delete q.open
  void router.replace({ query: q })
})

/** ★ 点击一行 → 打开详情（这是「查看题目详情」的入口） */
function openDetail(row: QuestionBrief): void {
  detailId.value = row.id
  detailOpen.value = true
}

function onSaved(): void {
  void load()
  void loadStats()
}

function onCreated(id: number): void {
  void load()
  void loadStats()
  // ★ 建完直接打开看 —— 老师下一步通常是核对或补知识点
  detailId.value = id
  detailOpen.value = true
}

function onDeleted(): void {
  void load()
  void loadStats()
}

/**
 * AI 生题结束（有新题落库，或部分成功）。
 *
 * ★ 只刷新，**不擅自改筛选条件**。
 *
 *   生成的题恒为 `DRAFT`，而本页默认勾着「只显示可用于组卷的题」
 *   —— 所以刷新后**看不到**刚生成的那几道。这是对的（草稿确实不能组卷），
 *   但老师会以为「生成完题目不见了」。
 *
 *   解决办法不是偷偷把筛选改掉（那会打乱正在看别的列表的人），
 *   而是在抽屉的结果区给一个明确的「在题库里看这些草稿」按钮 ——
 *   **由老师决定什么时候切**。
 */
function onAiGenerated(): void {
  void load()
  void loadStats()
}

/** 抽屉里点了某道生成出来的题 → 用同页的详情抽屉打开它。 */
function onAiOpenQuestion(id: number): void {
  detailId.value = id
  detailOpen.value = true
}

/**
 * 「在题库里看这些草稿」—— 把筛选切到「草稿 + 不看可组卷限制」。
 *
 * ★ 两个字段必须**一起**改：
 *   只取消 `onlyUsable` 的话，列表里会混着已通过的题，刚生成的那几道
 *   散在中间不好找；只把 `status` 设成 DRAFT 而留着 `onlyUsable`，
 *   则一道都不显示（`only_usable` 与 `DRAFT` 在语义上互斥）。
 */
function showAiDrafts(): void {
  filters.onlyUsable = false
  filters.status = 'DRAFT'
  aiOpen.value = false
  ElMessage.success('已切到「草稿」视图；审阅通过后才会进入可组卷的题')
}

/** 批量审核通过 */
async function batchApprove(): Promise<void> {
  if (!selected.value.length) {
    ElMessage.warning('请先勾选题目')
    return
  }
  const ids = selected.value.map((q) => q.id)
  let ok = 0
  const failed: string[] = []
  for (const id of ids) {
    try {
      // ★ 审核入参是 `action`，不是 `approve`
      await Q.approveQuestion(id, { action: 'APPROVE' })
      ok += 1
    } catch (e) {
      failed.push(`#${id}（${e instanceof Error ? e.message : '失败'}）`)
    }
  }
  await load()
  await loadStats()
  if (failed.length) {
    ElMessage.warning(`${ok} 题已通过，${failed.length} 题失败：${failed.slice(0, 3).join('；')}`)
  } else {
    ElMessage.success(`${ok} 题已通过`)
  }
  selected.value = []
}

const studentStatus = (s: string): 'success' | 'info' | 'warning' | 'danger' =>
  Q.QUESTION_STATUS_TONE[s] ?? 'info'

// ══════════════════════════════════════════════════
//  双轨：Tab 切换 / 提交公共库 / 下载到本校
// ══════════════════════════════════════════════════

/** 切换 Tab：写回 URL 并回到第 1 页（不清空的话会停在不存在的页码上）。 */
watch(bank, (v) => {
  page.value = 1
  selected.value = []
  const q = { ...route.query }
  if (v === 'public') q.bank = 'public'
  else delete q.bank
  void router.replace({ query: q })
  void load()
})

/**
 * 提交一道私有题到公共库。
 *
 * ★★ 带配图的题会先被后端拒绝（图片按机构授权，不能跨机构共享）。
 *   这里捕获那条 409，**把原话给老师看**，并让他显式确认「不带配图提交」。
 *   绝不默认传 `dropFigures: true` —— 那等于静默丢图。
 */
async function submitRow(row: QuestionBrief): Promise<void> {
  const doSubmit = async (dropFigures: boolean): Promise<void> => {
    try {
      const r = await Q.submitToPublic(row.id, { dropFigures })
      ElMessage.success(
        `已提交到公共库（公共题 #${r.public_question_id}），等待平台审核`,
      )
    } catch (e) {
      if (
        e instanceof ApiError
        && e.code === 'QUESTION_BANK.INVALID_STATE'
        && e.message.includes('配图')
      ) {
        try {
          await ElMessageBox.confirm(e.message, '这道题带配图', {
            confirmButtonText: '确认不带配图提交',
            cancelButtonText: '取消',
            type: 'warning',
          })
        } catch {
          return
        }
        await doSubmit(true)
        return
      }
      ElMessage.error(e instanceof Error ? e.message : '提交失败')
    }
  }
  await doSubmit(false)
}

const downloadOpen = ref(false)
const downloadRow = ref<QuestionBrief | null>(null)
const downloadCourseId = ref<number | undefined>(undefined)
const courseOptions = ref<Array<{ id: number; name: string; code: string }>>([])
const downloading = ref(false)

/**
 * 打开发下载弹窗。
 *
 * ★ 必须让老师**选接收课程**：公共题的课程属于来源机构，
 *   照抄会挂到别人的课程上（后端 `/download/` 因此要求传本机构课程）。
 */
async function openDownload(row: QuestionBrief): Promise<void> {
  downloadRow.value = row
  if (!courseOptions.value.length) {
    try {
      const c = await K.listCourses()
      courseOptions.value = (c.items ?? []).map((x) => ({
        id: x.id, name: x.name, code: x.code,
      }))
    } catch {
      courseOptions.value = []
    }
  }
  downloadCourseId.value = courseOptions.value[0]?.id
  downloadOpen.value = true
}

async function confirmDownload(): Promise<void> {
  if (!downloadRow.value) return
  if (!downloadCourseId.value) {
    ElMessage.warning('请选择接收课程')
    return
  }
  downloading.value = true
  try {
    const r = await Q.downloadPublicQuestion(downloadRow.value.id, {
      courseId: downloadCourseId.value,
    })
    downloadOpen.value = false
    let msg = `已下载为草稿（题目 #${r.question_id}），审核通过后即可组卷`
    if (r.unmapped_node_codes.length) {
      msg += `；${r.unmapped_node_codes.length} 个知识点未能在本校匹配`
    }
    if (r.lost_figures) {
      msg += `；原题 ${r.lost_figures} 张配图未随复制`
    }
    ElMessage.success({ message: msg, duration: 6000 })
    void load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '下载失败')
  } finally {
    downloading.value = false
  }
}
</script>

<template>
  <div class="zf-stack">
    <div class="zf-row" style="justify-content: space-between">
      <div>
        <h1 class="page-title">题库</h1>
        <p class="zf-muted page-sub">
          <!-- ★ `usable_total` 是**可组卷题数**，不是题库总数 ——
               DRAFT 与配图未确认的题不算。这不一致是刻意的：
               老师最关心的是「能用来组卷的有多少」。
               ★ 公共 Tab 下这个口径不成立（公共题一道都不能直接组卷），
                 改成「公共库共 N 道（已审核）」。 -->
          <template v-if="isPublic">
            公共库共 {{ total }} 道（已审核）
          </template>
          <template v-else>
            可组卷 {{ stats?.usable_total ?? total }} 道
          </template>
          <!-- ★ 这里**不放**难度档位分布。
               档位全是一个值时，它就是「中 1944」这种冗余信息，
               而且会与下面的难度告警重复；档位变多时那条告警会自己消失，
               分布交给告警里的那行文字说。 -->
          <template v-if="nodeId">
            ｜
            <el-tag size="small" type="info" closable @close="clearNodeFilter">
              已按知识点 #{{ nodeId }} 筛选
            </el-tag>
          </template>
        </p>
      </div>
      <!-- ★★ 二级入口（2026-10-23 导航分组）。
           「导入题目」「难度打标」从顶栏一级收进「题库」菜单后，
           这里必须各留一个入口：
             1. 顶栏菜单是**划过即开**的，触屏用户不一定摸得到；
             2. 用户真正的动线是「先看到库里有多少题，再决定要不要补」——
                他已经在题库页上了，不该再回顶栏去找。
           ★ 公共题库 Tab 下两个都不显示：公共题是别校提交、平台审核过的，
             既不能导入到这里，也不能改它的难度档。 -->
      <div v-if="!isPublic" class="zf-row">
        <el-button @click="router.push({ name: 'ImportPaper' })">
          导入题目
        </el-button>
        <el-button @click="router.push({ name: 'QuestionMarkDifficulty' })">
          难度打标
        </el-button>
        <!-- ★★ AI 生题入口。
             能力位 `ai.generate`（L1 起，与后端 `IsTeacherAccount` 一致）。
             结果恒为**草稿**，所以它不是「新建」的快捷方式 ——
             文案写「AI 生题」而不是「AI 新建」，因为它产出的是待审内容。 -->
        <el-button
          v-if="auth.can('ai.generate')"
          type="success"
          plain
          @click="aiOpen = true"
        >
          AI 生题
        </el-button>
        <el-button type="primary" @click="createOpen = true">
          新建题目
        </el-button>
      </div>
    </div>

    <!-- ★★ 题库双轨（v2.0）。两个 Tab 的**权限与动作完全不同**：
         「我的题库」可增删改审；「公共题库」只读，唯一动作是下载。
         所以不是把两批数据混在一张表里加个来源列 ——
         那样「全选 → 批量通过」会误操作到别的机构提交的题。 -->
    <el-tabs v-model="bank" class="bank-tabs">
      <el-tab-pane label="我的题库" name="private" />
      <el-tab-pane label="公共题库" name="public" />
    </el-tabs>

    <el-alert
      v-if="isPublic"
      type="info"
      :closable="false"
      show-icon
      title="公共题库：平台审核通过的题目，各机构按需下载"
      style="margin-bottom: 12px"
    >
      <div class="pub-hint">
        下载得到的是<b>本校私有副本</b>（草稿状态），审核通过后即可组卷。
        副本与公共题<b>各自独立</b>：公共题日后被修订或下架，
        已下载的副本不会跟着变；来源题有更新时列表里会显示「来源已更新」角标。
      </div>
    </el-alert>

    <!-- ★★ 难度分层健康度 —— 决策 7 的事实显性化。
         个性化练习按 `difficulty_effective` 落在推导出的区间里取题
         （全错 → 目标 1.0，阶梯最多放宽到 2.0）。若全库都是默认 3.00，
         3.00 **永远落不进**这个区间 → 练习一道也生不出来，
         而报错只说 `PRACTICE.BANK_INSUFFICIENT`，看不出根因。
         ★ 这里把根因直接摆出来：就是「一个档位 / 没有人工标注」。 -->
    <el-alert
      v-if="stats?.difficulty && stats.difficulty.needs_annotation"
      type="warning"
      :closable="false"
      show-icon
      title="题库难度没有分层 —— 个性化练习选不出题"
      style="margin-bottom: 12px"
    >
      <template #default>
        <div class="diff-alert">
          <div>
            ★ 可组卷 <b>{{ stats.usable_total }}</b> 道题中，仍有
            <b class="zf-danger">{{ stats.difficulty?.unrated ?? 0 }}</b>
            道是<b>默认难度</b>；已人工标注
            <b>{{ stats.difficulty?.manually_rated ?? 0 }}</b> 道
            （{{
              ((1 - (stats.difficulty?.unrated_ratio ?? 1)) * 100).toFixed(1)
            }}%）。
            难度档位只有
            <b>{{ stats.difficulty?.band_count ?? 0 }}</b> 种
            <span class="zf-dim">
              （{{
                Object.entries(stats.difficulty?.by_band ?? {})
                  .map(([k, v]) =>
                    `${Q.DIFFICULTY_BAND_LABEL[k] ?? k} ${v}`)
                  .join('、')
              }}）
            </span>
          </div>
          <div class="zf-dim diff-alert__why">
            ★ 个性化练习按难度区间取题：目标难度从掌握度推导，
            <b>全错 → 目标 1.0，阶梯最多放宽到 2.0</b>。
            全库都是默认值
            <code>{{ stats.difficulty?.effective_mode }}</code>
            时，这个值<b>永远落不进区间</b>，于是练习一道也生不出来。
          </div>
          <!-- ★★ 「去快速打标」是**问题驱动**的入口（2026-10-23）。
               原来的文案写「点任意一题的详情 → 编辑即可补人工难度」——
               那句在打标页做出来之前是对的，之后就成了**过时的指引**：
               逐题进详情是一条一道走的路，而「还有 1200 道没标」的
               现实场景需要的是「一屏一题、按 1–5 即前进」。
               入口就该长在这个告警卡里：看到问题的地方，下一步就在这里。 -->
          <div class="diff-alert__act">
            <el-button
              size="small"
              type="primary"
              plain
              @click="router.push({ name: 'QuestionMarkDifficulty' })"
            >
              去快速打标（还剩
              {{ stats.difficulty?.unrated ?? 0 }} 道）
            </el-button>
            <span class="zf-dim" style="margin-left: 8px">
              一屏一题、按 <b>1–5</b> 即前进，中途可离开再续标；
              也可以逐题「详情 → 编辑」（人工值最高优先级，覆盖默认值）。
            </span>
          </div>
        </div>
      </template>
    </el-alert>

    <section class="zf-card">
      <div class="zf-card__body filter-bar">
        <el-input
          v-model="filters.search"
          placeholder="搜索题干（输入即筛选）"
          clearable
          style="max-width: 260px"
        />
        <el-select v-model="filters.type" placeholder="全部题型" style="width: 140px">
          <el-option label="全部题型" value="" />
          <el-option
            v-for="t in Q.QUESTION_TYPE_OPTIONS"
            :key="t.value"
            :label="t.label"
            :value="t.value"
          />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" style="width: 120px">
          <el-option label="全部状态" value="" />
          <el-option
            v-for="(l, v) in Q.QUESTION_STATUS_LABEL"
            :key="v"
            :label="l"
            :value="v"
          />
        </el-select>
        <el-select
          v-model="filters.difficultyBand"
          placeholder="全部难度"
          style="width: 120px"
        >
          <el-option
            v-for="b in DIFFICULTY_BANDS"
            :key="b.value"
            :label="b.label"
            :value="b.value"
          />
        </el-select>
        <el-select v-model="filters.hasFigure" placeholder="不限配图" style="width: 120px">
          <el-option label="不限配图" value="" />
          <el-option label="含配图" value="true" />
          <el-option label="不含配图" value="false" />
        </el-select>
        <!-- ★ 知识点筛选（`P1-25b`）。
             原来只能靠从「知识体系」页带 `?nodeId=` 跳进来，
             题库页自己**没有选择器** —— 想换一个知识点就得回知识体系。
             后端一直支持 `node_id`，缺的只是这个控件。 -->
        <el-select
          v-model="nodeId" filterable clearable placeholder="全部知识点"
          style="width: 220px" size="small"
        >
          <el-option label="全部知识点" :value="undefined" />
          <el-option
            v-for="n in nodeOptions" :key="n.id"
            :label="`${n.code} ${n.name}`" :value="n.id"
          />
        </el-select>
        <!-- ★ 配图待确认（首页工作台「配图待确认」卡的落点）。
             这些题**进不了组卷池**，但在列表里看起来和正常题一样，
             老师只会在「组卷时题不够」时间接感受到它。 -->
        <el-checkbox v-if="!isPublic" v-model="filters.figurePending">
          只看配图待确认
        </el-checkbox>
        <el-checkbox v-if="!isPublic" v-model="filters.onlyUsable">
          只显示可用于组卷的题
        </el-checkbox>
        <el-button
          v-if="!isPublic"
          size="small"
          :disabled="!selected.length"
          @click="batchApprove"
        >
          批量通过（{{ selected.length }}）
        </el-button>
        <span v-if="isPublic" class="zf-dim pub-tip">
          公共题不能直接组卷，请先下载到本校
        </span>
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
        style="width: 100%"
        row-key="id"
        size="default"
        @selection-change="(v: QuestionBrief[]) => (selected = v)"
      >
        <!-- ★ 公共 Tab 不提供勾选：这里的题都不是本校的，
             批量操作一个都不该有 -->
        <el-table-column v-if="!isPublic" type="selection" width="42" />
        <el-table-column prop="id" label="ID" width="76" />
        <el-table-column label="题型" width="104">
          <template #default="{ row }">
            {{
              Q.QUESTION_TYPE_OPTIONS.find((o) => o.value === row.type)?.label ??
              row.type
            }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="studentStatus(row.status)">
              {{ Q.QUESTION_STATUS_LABEL[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="题干摘要" min-width="320">
          <template #default="{ row }">
            <!-- ★ 点题干进详情；也能点整行的「详情」按钮 -->
            <a
              href="javascript:void(0)"
              class="stem-link"
              @click="openDetail(row)"
            >
              <MathText mode="text" :source="row.stem_preview" />
            </a>
            <!-- ★ 漂移角标（§3.3）：只提示，不自动同步。
                 点开详情能看到来源与下载时的版本。 -->
            <el-tag
              v-if="row.source_drift"
              size="small"
              type="warning"
              class="drift-tag"
            >
              来源已更新
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="配图" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.has_figure" size="small" type="info">
              {{ row.figure_count }}
            </el-tag>
            <span v-else class="zf-dim">—</span>
          </template>
        </el-table-column>
        <el-table-column label="难度" width="132">
          <template #default="{ row }">
            <!-- ★ 后端给的是字符串（Decimal 序列化成 "3.00"），
                 不能直接 .toFixed()——那会抛 "is not a function"。 -->
            <span v-if="row.difficulty_effective" class="diff">
              <span class="zf-num">
                {{ Number(row.difficulty_effective).toFixed(2) }}
              </span>
              <!-- ★ 档位显示中文；英文枚举值对老师没有意义。
                   用 `<span>` 包住并给左外边距，避免两段文字贴在一起
                   （模板里的换行会被 Vue 折叠掉）。 -->
              <span class="diff__band">
                {{ Q.DIFFICULTY_BAND_LABEL[row.difficulty_band] ?? row.difficulty_band }}
              </span>
            </span>
            <span v-else class="zf-dim">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" :width="isPublic ? 150 : 168">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row)">
              详情
            </el-button>
            <!-- ★ 公共 Tab 只有「下载」一个动作 -->
            <el-button
              v-if="isPublic"
              size="small"
              text
              type="success"
              @click="openDownload(row)"
            >
              下载
            </el-button>
            <!-- ★ 私有 Tab：「提交到公共库」（U13 的入口）。
                 能力位 `question.public.submit`（L1 起）。
                 状态是否允许**不做前端预判**——判据只维护一份（后端）。 -->
            <el-button
              v-else-if="auth.can('question.public.submit')"
              size="small"
              text
              type="info"
              @click="submitRow(row)"
            >
              提交公共库
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="zf-empty">
            {{ loading ? '读取中…' : '没有符合条件的题目' }}
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
        />
      </div>
    </section>

    <QuestionDetailDrawer
      v-model="detailOpen"
      :question-id="detailId"
      @saved="onSaved"
      @deleted="onDeleted"
    />
    <QuestionCreateDrawer v-model="createOpen" @created="onCreated" />
    <!-- ★ AI 生题：生成完的题可以在抽屉里直接点开审阅（复用本页的详情抽屉），
         避免老师「生成 → 关抽屉 → 自己在题库里翻」来回找。 -->
    <AiGenerateDrawer
      v-model="aiOpen"
      @generated="onAiGenerated"
      @open-question="onAiOpenQuestion"
      @show-drafts="showAiDrafts"
    />

    <!-- 下载到本校：必须选接收课程（公共题的课程属于来源机构） -->
    <el-dialog v-model="downloadOpen" title="下载到本校私有库" width="520px">
      <p class="zf-muted dl-tip">
        下载得到的是<b>草稿</b>副本，需本校审核通过后才能组卷。
        知识点会按编码自动匹配到所选课程，匹配不上的会列出来。
      </p>
      <el-form label-width="88px">
        <el-form-item label="接收课程">
          <el-select
            v-model="downloadCourseId"
            placeholder="选择本机构的课程"
            style="width: 100%"
          >
            <el-option
              v-for="c in courseOptions"
              :key="c.id"
              :label="`${c.name}（${c.code}）`"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="downloadOpen = false">取消</el-button>
        <el-button type="primary" :loading="downloading" @click="confirmDownload">
          下载
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
/* ★ 难度告警：把「为什么生不出练习」讲清楚，分三层 ——
   现状（数字）→ 原因（取题区间）→ 怎么办（去哪补）。 */
.diff-alert {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
.diff-alert__why,
.diff-alert__act {
  padding-top: 4px;
}
.diff-alert code {
  font-family: var(--zf-font-mono);
  background: var(--zf-bg-subtle);
  padding: 0 4px;
  border-radius: var(--zf-radius-sm);
}

.stem-link:hover {
  color: var(--zf-brand);
  text-decoration: underline;
}
/* ★ 难度：数值与档位必须**显式**留出间距。
   靠模板里的换行/空格不可靠 —— Vue 会折叠空白，
   结果渲染成「3.00MEDIUM」这种贴在一起的文本。 */
.diff {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.diff__band {
  color: var(--zf-text-secondary);
  font-size: var(--zf-text-xs);
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: var(--zf-space-4) var(--zf-space-5);
  border-top: 1px solid var(--zf-border-subtle);
}
/* ★ 双轨 */
.bank-tabs {
  margin-bottom: var(--zf-space-1);
}
.pub-hint {
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
.pub-tip {
  font-size: var(--zf-text-xs);
}
.drift-tag {
  margin-left: var(--zf-space-2);
}
.dl-tip {
  margin: 0 0 var(--zf-space-3);
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}
</style>
