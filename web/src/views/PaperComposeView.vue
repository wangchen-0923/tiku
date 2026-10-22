<script setup lang="ts">
/**
 * 组卷工作台。
 *
 * ★★ 布局是**左配置 / 右卷面**两栏，因为它们的关系是「因」与「果」：
 *
 *     左：抽题规则（题量、配分、难度区间、知识点范围）
 *     右：当前卷面（已抽到的题，可手删、可手动补）
 *
 *   把两者并排，老师改完左边的参数能立刻看到右边没跟着变——
 *   这个「没跟着变」**必须可见**，否则就会以为自己改完了。
 *   所以有一个显式的「配置已改、卷面尚未重新生成」提示。
 *
 * ★ 自动组卷的三种结果分开呈现，不做成一个 toast：
 *     成功         绿色摘要（题量/总分/难度均值）
 *     有降级       黄色警告（**哪一级放宽了、知识点没放宽**）
 *     候选不足     橙色 + 缺口清单（「少要几道还是换知识点」）
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import * as api from '@/api/paper'
import { flattenNodes, getCourseTree } from '@/api/knowledge'
import type { TreeNode } from '@/api/knowledge'
import MathInput from '@/components/MathInput.vue'
import { usePaperStore } from '@/stores/paper'

const route = useRoute()
const router = useRouter()
const store = usePaperStore()

const paperId = computed(() => Number(route.params.id))
const nodeOptions = ref<Array<TreeNode & { taskName: string }>>([])
const showNodePicker = ref(false)

/** 难度档位选择（对应后端 difficulty_range 的 1–5） */
const DIFF_STEPS = [1, 2, 3, 4, 5]

const candidatesInPaper = computed(() => store.selectedIdSet)

onMounted(async () => {
  store.reset()
  await store.load(paperId.value)
  if (store.paper) {
    await store.loadCandidates()
    await store.refreshFinalizeCheck()
    void loadCoverage()
    const cid = store.paper.course_id
    try {
      const tree = await getCourseTree(cid)
      nodeOptions.value = flattenNodes(tree)
    } catch {
      nodeOptions.value = []
    }
  }
})

// ══════════════════════════════════════════════════
//  ★ 发布前覆盖体检（21 文档任务六）
// ══════════════════════════════════════════════════
//
//  ★★ 与「定稿检查」是**两套门**，界面上也要分开摆：
//
//      定稿检查  这张卷**本身完整吗**：配图确认、答案、配分、重复题
//      体检      这张卷**考点结构对不对**：覆盖、超纲、难度偏斜
//
//  ★ 阻断项可以填理由**强制发布**，理由入审计 —— 系统不替教研
//    背「这张卷能不能考」的责任，但必须把「为什么被拦」写清楚。

const coverage = ref<api.CoverageReport | null>(null)
const coverageLoading = ref(false)

async function loadCoverage(): Promise<void> {
  if (!paperId.value) return
  coverageLoading.value = true
  try {
    coverage.value = await api.getPaperCoverage(paperId.value)
  } catch (e) {
    coverage.value = null
    ElMessage.warning(e instanceof Error ? e.message : '体检数据加载失败')
  } finally {
    coverageLoading.value = false
  }
}

/** 缺口考点 → 直接勾成组卷范围（下一步「重新组卷」就只从这些考点抽）。 */
function fillMissingNodes(): void {
  const missing = coverage.value?.knowledge.missing_required ?? []
  if (!missing.length) return
  store.selectedNodeIds = [...new Set([...store.selectedNodeIds, ...missing])]
  store.markDirty()
  ElMessage.success(`已把 ${missing.length} 个缺口考点勾选为组卷范围`)
}

/** 难度分布柱宽（按最大题数归一，避免「全班 3 题」的柱占满屏）。 */
const diffMax = computed(() =>
  Math.max(1, ...(coverage.value?.difficulty.buckets ?? []).map((b) => b.count)),
)

const DIFF_LABEL: Record<string, string> = {
  EASY: '易',
  MEDIUM: '中',
  HARD: '难',
}

watch(
  () => store.candidatePage,
  () => void store.loadCandidates(),
)
watch(
  () => store.candidateFilters,
  () => {
    store.candidatePage = 1
    void store.loadCandidates()
  },
  { deep: true },
)

// ══════════════════════════════════════════════════
//  操作
// ══════════════════════════════════════════════════

async function handleSaveConfig(): Promise<void> {
  const ok = await store.saveConfig()
  ElMessage[ok ? 'success' : 'error'](ok ? '配置已保存' : store.error)
}

async function handleCompose(allowPartial = false): Promise<void> {
  const r = await store.compose(allowPartial)
  if (r === 'ok') {
    ElMessage.success(
      `组卷完成：${store.lastResult?.item_count} 题 / ` +
        `${store.lastResult?.total_score} 分`,
    )
  } else if (r === 'gaps') {
    // ★ 业务分支，不是失败。不弹红色错误。
    ElMessage.warning('题量不足，请看下方缺口清单')
  } else {
    ElMessage.error(store.error || '组卷失败')
  }
}

async function handleAdd(qid: number): Promise<void> {
  // 手动加题时按哪个大题？取配置里同题型的第一个大题
  const cand = store.candidates.find((c) => c.id === qid)
  const sec =
    store.config.sections.find((s) => s.type === cand?.type) ??
    store.config.sections[0]
  const ok = await store.addQuestions(
    [qid],
    sec?.title || api.typeLabel(sec?.type ?? ''),
    Number(sec?.score_each ?? 4),
  )
  ElMessage[ok ? 'success' : 'error'](ok ? '已加入卷面' : store.error)
}

async function handleRemove(itemId: number): Promise<void> {
  const ok = await store.removeQuestion(itemId)
  ElMessage[ok ? 'success' : 'error'](ok ? '已移出卷面' : store.error)
}

async function handleFinalize(): Promise<void> {
  await store.refreshFinalizeCheck()
  await loadCoverage()
  const info = store.finalizeInfo
  if (!info) return

  const cov = coverage.value
  const checkBlocked = !info.can_finalize
  const covBlocked = !!cov && !cov.can_publish

  // ★★ 两套门都拦人时，**一次**跟老师把账算清：
  //   分别弹两次「不能定稿」等于让他改一次、再撞一次。
  if (checkBlocked || covBlocked) {
    const parts: string[] = []
    if (checkBlocked) {
      parts.push('卷面检查：' + info.blocking.map((i) => i.detail).join('；'))
    }
    if (covBlocked && cov) {
      parts.push('考点体检：' + cov.blocking.map((b) => b.message).join('；'))
    }
    try {
      const r = await ElMessageBox.prompt(
        `${parts.join('\n\n')}\n\n`
        + '可以填理由**强制发布**（会记入审计，事后能查到是哪几条被跳过）。\n'
        + '理由请写清楚为什么这次必须先发，如「期中考试缺 2 题，考后补进 B 卷」。',
        '有阻断项，不能直接定稿',
        {
          inputPlaceholder: '强制发布理由（必填）',
          confirmButtonText: '强制发布',
          cancelButtonText: '回去改',
          inputValidator: (v: string) =>
            (v || '').trim() ? true : '必须填写理由',
        },
      )
      const ok = await store.finalize(true, (r.value || '').trim())
      ElMessage[ok ? 'success' : 'error'](ok ? '已强制发布' : store.error)
      if (ok) void loadCoverage()
    } catch {
      /* 用户取消 */
    }
    return
  }

  try {
    let reason = ''
    if (info.warnings.length || (cov?.warnings.length ?? 0) > 0) {
      // ★ 有告警要老师确认，并留下理由（写审计）
      const warnText = [
        ...info.warnings.map((i) => i.detail),
        ...(cov?.warnings.map((w) => w.message) ?? []),
      ].slice(0, 6).join('；')
      const r = await ElMessageBox.prompt(
        `${info.summary}\n\n告警：${warnText}\n\n`
        + '定稿后不能再改题，且题目快照会固化。\n请填写定稿理由（将记入审计）：',
        '确认定稿',
        { inputPlaceholder: '如：期中考试 A 卷', confirmButtonText: '定稿',
          cancelButtonText: '再想想' },
      )
      reason = r.value ?? ''
    } else {
      await ElMessageBox.confirm(
        `${info.summary}\n\n定稿后不能再改题。`,
        '确认定稿',
        { confirmButtonText: '定稿', cancelButtonText: '再想想' },
      )
    }
    const ok = await store.finalize(false, reason)
    ElMessage[ok ? 'success' : 'error'](ok ? '已定稿' : store.error)
    if (ok) void loadCoverage()
  } catch {
    /* 用户取消 */
  }
}

/** 题型选项（含后端可能返回但前端没登记的） */
const typeOptions = computed(() =>
  Object.entries(api.QUESTION_TYPE_LABEL).map(([v, l]) => ({
    value: v,
    label: l,
  })),
)
</script>

<template>
  <div v-loading="store.loading" class="zf-stack">
    <!-- ── 头部 ── -->
    <div class="head">
      <div>
        <h1 class="page-title">
          {{ store.paper?.title ?? '组卷' }}
          <el-tag
            v-if="store.paper"
            size="small"
            :type="api.PAPER_STATUS_TONE[store.paper.status]"
          >
            {{ api.PAPER_STATUS_LABEL[store.paper.status] }}
          </el-tag>
        </h1>
        <p class="zf-muted page-sub">
          课程：{{ store.paper?.course_name || '—' }}
          <span class="zf-dim">
            · 组卷方式 {{ store.paper?.compose_mode }}
            · seed {{ store.paper?.random_seed }}
          </span>
        </p>
      </div>
      <div class="zf-row">
        <el-button @click="router.push({ name: 'PaperList' })">返回列表</el-button>
        <el-button
          v-if="store.canEdit"
          type="primary"
          :loading="store.saving"
          @click="handleSaveConfig"
        >
          保存配置
        </el-button>
        <el-button
          v-if="store.canEdit"
          type="success"
          :loading="store.saving"
          @click="handleCompose(false)"
        >
          按规则组卷
        </el-button>
        <el-button
          v-if="store.canEdit"
          :disabled="!store.paper?.items.length"
          @click="handleFinalize"
        >
          定稿
        </el-button>
        <!-- ★ 预览与导出：独立页面（`/papers/:id/export`，`P1-62b`）。
             原抽屉已删除，不再有两套导出界面。 -->
        <el-button
          :disabled="!store.paper?.items.length"
          @click="router.push({ name: 'PaperExport', params: { id: paperId } })"
        >
          预览与导出
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
    />

    <!-- ★ 配置改了但没重新组卷：这个提示必须显眼 -->
    <el-alert
      v-if="store.configDirty || store.countMismatch"
      type="warning"
      :closable="false"
      show-icon
      title="配置与卷面不一致"
    >
      <template #default>
        <p style="margin: 0">
          配置要求
          <b>{{ store.expectedCount }}</b> 题 / <b>{{ store.expectedScore }}</b> 分，
          当前卷面
          <b>{{ store.actualCount }}</b> 题 / <b>{{ store.actualScore }}</b> 分。
          <span v-if="store.configDirty">
            配置尚未保存，或保存后未重新组卷。
          </span>
          请点「保存配置」再点「按规则组卷」。
        </p>
      </template>
    </el-alert>

    <!-- ── 组卷结果 ── -->
    <el-card v-if="store.lastResult" shadow="never">
      <template #header>
        <span>上次组卷结果</span>
        <el-tag
          v-if="!store.lastGaps.length"
          size="small"
          type="success"
          style="margin-left: 8px"
        >
          成功
        </el-tag>
        <el-tag v-else size="small" type="warning" style="margin-left: 8px">
          有缺口
        </el-tag>
      </template>
      <div class="result-grid">
        <div><div class="num">{{ store.lastResult.item_count }}</div><div class="lbl">题量</div></div>
        <div><div class="num">{{ store.lastResult.total_score }}</div><div class="lbl">总分</div></div>
        <div><div class="num">{{ store.lastResult.difficulty_mean.toFixed(2) }}</div><div class="lbl">平均难度</div></div>
        <div><div class="num">{{ store.lastResult.seed }}</div><div class="lbl">seed</div></div>
      </div>

      <el-table :data="store.lastResult.sections" size="small" style="margin-top: 12px">
        <el-table-column prop="title" label="大题" min-width="140" />
        <el-table-column label="题量" width="90">
          <template #default="{ row }">{{ row.got }}/{{ row.need }}</template>
        </el-table-column>
        <el-table-column label="每道分" width="90" prop="score_each" />
        <el-table-column label="难度区间" width="130">
          <template #default="{ row }">
            {{ row.effective_difficulty_range[0] }}–{{ row.effective_difficulty_range[1] }}
          </template>
        </el-table-column>
        <el-table-column label="降级" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.degrade_level === 0" size="small" type="success">
              未降级
            </el-tag>
            <el-tag v-else size="small" type="warning">
              第 {{ row.degrade_level }} 级
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <!-- ★ 降级警告：说清「放宽了什么、没放宽什么」 -->
      <el-alert
        v-for="(w, i) in store.lastResult.warnings"
        :key="i"
        type="warning"
        :closable="false"
        show-icon
        style="margin-top: 8px"
        :title="w.message"
      />

      <!-- ★ 缺口：不弹错误，给决策依据 -->
      <div v-if="store.lastGaps.length" class="gaps">
        <div class="gaps-title">缺口清单（候选不足）</div>
        <el-table :data="store.lastGaps" size="small">
          <el-table-column label="大题" min-width="140">
            <template #default="{ row }">
              {{ row.section_title || api.typeLabel(row.type) }}
            </template>
          </el-table-column>
          <el-table-column prop="need" label="需要" width="80" />
          <el-table-column prop="got" label="凑到" width="80" />
          <el-table-column prop="short" label="缺" width="80" />
        </el-table>
        <p class="zf-muted" style="margin: 8px 0 0; font-size: var(--zf-text-xs)">
          可以：① 调小题量；② 放宽难度区间；③ 增加知识点范围；④ 点下面的
          「允许不足额组卷」先把能组的组出来。
        </p>
        <el-button
          v-if="store.canEdit"
          size="small"
          style="margin-top: 8px"
          :loading="store.saving"
          @click="handleCompose(true)"
        >
          允许不足额组卷
        </el-button>
      </div>
    </el-card>

    <div class="cols">
      <!-- ══════════ 左：抽题规则 ══════════ -->
      <section class="zf-card">
        <div class="zf-card__head">
          <h2 class="zf-card__title">抽题规则</h2>
          <el-button
            v-if="store.canEdit"
            size="small"
            @click="store.addSection()"
          >
            加大题
          </el-button>
        </div>
        <div class="zf-card__body">
          <div
            v-for="(s, idx) in store.config.sections"
            :key="idx"
            class="sec"
          >
            <div class="sec-head">
              <!-- ★ 大题标题也支持公式（「三、计算题（$F=ma$ 应用）」这类）。
                   这一页没有共享工具条，所以让输入框自带一个弹出工具条。 -->
              <MathInput
                v-model="s.title"
                :label="`大题标题 ${idx + 1}`"
                compact
                :rows="1"
                toolbar="popover"
                placeholder="大题标题（如：一、选择题）"
                :disabled="!store.canEdit"
                style="flex: 1"
                @blur="store.markDirty()"
              />
              <el-button
                v-if="store.canEdit && store.config.sections.length > 1"
                size="small"
                type="danger"
                text
                @click="store.removeSection(idx)"
              >
                删除
              </el-button>
            </div>
            <div class="sec-body">
              <el-select
                v-model="s.type"
                size="small"
                :disabled="!store.canEdit"
                style="width: 130px"
                @change="store.markDirty()"
              >
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
                :disabled="!store.canEdit"
                controls-position="right"
                style="width: 100px"
                @change="store.markDirty()"
              />
              <span class="zf-dim">题 ×</span>
              <el-input-number
                v-model="s.score_each"
                size="small"
                :min="0.5"
                :max="100"
                :step="0.5"
                :disabled="!store.canEdit"
                controls-position="right"
                style="width: 100px"
                @change="store.markDirty()"
              />
              <span class="zf-dim">分</span>
            </div>
            <div class="sec-body">
              <span class="zf-dim">难度</span>
              <el-select
                v-model="s.difficulty_range[0]"
                size="small"
                :disabled="!store.canEdit"
                style="width: 74px"
                @change="store.markDirty()"
              >
                <el-option v-for="n in DIFF_STEPS" :key="n" :label="n" :value="n" />
              </el-select>
              <span class="zf-dim">–</span>
              <el-select
                v-model="s.difficulty_range[1]"
                size="small"
                :disabled="!store.canEdit"
                style="width: 74px"
                @change="store.markDirty()"
              >
                <el-option v-for="n in DIFF_STEPS" :key="n" :label="n" :value="n" />
              </el-select>
            </div>
          </div>

          <!-- 知识点范围 -->
          <div class="sec">
            <div class="sec-head">
              <span class="lbl">知识点范围</span>
              <el-button
                v-if="store.canEdit"
                size="small"
                text
                @click="showNodePicker = true"
              >
                选择
              </el-button>
            </div>
            <div class="zf-dim" style="font-size: var(--zf-text-xs)">
              <template v-if="store.selectedNodeIds.length">
                已选 {{ store.selectedNodeIds.length }} 个知识点
                <span class="zf-dim">（★ 组卷时**不会**放宽知识点范围）</span>
              </template>
              <template v-else>不限（全部知识点）</template>
            </div>
          </div>

          <div class="sum">
            合计 <b>{{ store.expectedCount }}</b> 题 /
            <b>{{ store.expectedScore }}</b> 分
          </div>
        </div>
      </section>

      <!-- ══════════ 右：卷面 ══════════ -->
      <section class="zf-card">
        <div class="zf-card__head">
          <h2 class="zf-card__title">当前卷面</h2>
          <span class="zf-dim" style="font-size: var(--zf-text-xs)">
            {{ store.actualCount }} 题 / {{ store.actualScore }} 分
          </span>
        </div>
        <div class="zf-card__body">
          <el-table
            :data="store.paper?.items ?? []"
            size="small"
            max-height="420"
          >
            <el-table-column prop="seq" label="#" width="48" />
            <el-table-column label="题型" width="86">
              <template #default="{ row }">
                <el-tag size="small" type="info">
                  {{ api.typeLabel(row.question_type) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="题干" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <MathText mode="text" :source="row.stem_preview" />
              </template>
            </el-table-column>
            <el-table-column label="难度" width="62">
              <template #default="{ row }">
                {{ row.difficulty ?? '—' }}
              </template>
            </el-table-column>
            <el-table-column prop="score" label="分" width="56" />
            <el-table-column v-if="store.canEdit" label="" width="62">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="danger"
                  text
                  @click="handleRemove(row.id)"
                >
                  移出
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <div class="zf-empty">
                卷面还没有题。点上面的「按规则组卷」自动抽，
                或从下方候选池手动加入。
              </div>
            </template>
          </el-table>
        </div>
      </section>
    </div>

    <!-- ══════════ 候选池 ══════════ -->
    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">候选池</h2>
        <span class="zf-dim" style="font-size: var(--zf-text-xs)">
          共 {{ store.candidateTotal }} 道可用题
          <el-tooltip
            content="只列出可入卷的题：APPROVED 且配图已确认。这是组卷取数的唯一入口。"
          >
            <span style="cursor: help"> ⓘ</span>
          </el-tooltip>
        </span>
      </div>
      <div class="zf-card__body">
        <div class="zf-row" style="margin-bottom: 12px">
          <el-select
            v-model="store.candidateFilters.types"
            multiple
            collapse-tags
            clearable
            size="small"
            placeholder="题型"
            style="width: 240px"
          >
            <el-option
              v-for="o in typeOptions"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
          <el-select
            v-model="store.candidateFilters.difficultyBands"
            multiple
            collapse-tags
            clearable
            size="small"
            placeholder="难度档"
            style="width: 200px"
          >
            <el-option
              v-for="(l, v) in api.DIFFICULTY_BAND_LABEL"
              :key="v"
              :label="l"
              :value="v"
            />
          </el-select>
          <!-- ★ 知识点筛选（按反馈新增）。
               后端 `PaperCandidatesView` 早已支持 `node_id`（逗号分隔），
               缺的只是这个控件 —— 组卷时「想从某个知识点里挑题」
               是很常见的动作，之前只能靠任务名去猜。 -->
          <el-select
            v-model="store.candidateFilters.nodeIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            clearable
            size="small"
            placeholder="知识点"
            style="width: 260px"
          >
            <!-- ★ 只列 **NODE 级** 知识点：`nodeOptions` 是整棵课程树
                 （章 / 任务 / 知识点都在里面），不过滤的话下拉里会出现
                 「第一章」「分析轴的力学性能」这类非知识点条目，
                 而后端 `node_id` 只认知识点 id。 -->
            <el-option
              v-for="n in nodeOptions.filter((x) => x.level === 'NODE')"
              :key="n.id"
              :label="`${n.code} ${n.name}`"
              :value="n.id"
            />
          </el-select>
          <el-button size="small" @click="store.loadCandidates()">刷新</el-button>
        </div>

        <el-table
          v-loading="store.candidateLoading"
          :data="store.candidates"
          size="small"
          max-height="380"
        >
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column label="题型" width="86">
            <template #default="{ row }">
              <el-tag size="small" type="info">{{ api.typeLabel(row.type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="题干" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">
              <MathText mode="text" :source="row.stem_preview" />
            </template>
          </el-table-column>
          <el-table-column label="任务" width="130" show-overflow-tooltip prop="task_name" />
          <el-table-column label="难度" width="96">
            <template #default="{ row }">
              {{ Number(row.difficulty_effective).toFixed(1) }}
              <span class="zf-dim">{{ api.DIFFICULTY_BAND_LABEL[row.difficulty_band] }}</span>
            </template>
          </el-table-column>
          <el-table-column label="配图" width="60">
            <template #default="{ row }">
              <span v-if="row.has_figure">{{ row.figure_count }}</span>
              <span v-else class="zf-dim">—</span>
            </template>
          </el-table-column>
          <el-table-column v-if="store.canEdit" label="" width="84">
            <template #default="{ row }">
              <el-tag v-if="candidatesInPaper.has(row.id)" size="small" type="success">
                已在卷
              </el-tag>
              <el-button v-else size="small" text type="primary" @click="handleAdd(row.id)">
                加入
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">
              {{ store.candidateLoading ? '读取中…' : '没有符合条件的候选题' }}
            </div>
          </template>
        </el-table>

        <div class="pager">
          <el-pagination
            v-model:current-page="store.candidatePage"
            v-model:page-size="store.candidatePageSize"
            :total="store.candidateTotal"
            :page-sizes="[20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            background
            small
          />
        </div>
      </div>
    </section>

    <!-- ══════════ 定稿检查 ══════════ -->
    <section v-if="store.finalizeInfo" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">定稿检查</h2>
        <el-tag
          size="small"
          :type="store.finalizeInfo.can_finalize ? 'success' : 'danger'"
        >
          {{ store.finalizeInfo.can_finalize ? '可定稿' : '有阻断项' }}
        </el-tag>
      </div>
      <div class="zf-card__body">
        <p class="zf-muted" style="margin: 0 0 8px">
          {{ store.finalizeInfo.summary }}
        </p>
        <div v-if="store.finalizeInfo.blocking.length">
          <div class="lbl" style="color: var(--zf-danger)">阻断项（必须解决）</div>
          <el-table :data="store.finalizeInfo.blocking" size="small">
            <el-table-column label="码" width="70" prop="code" />
            <el-table-column label="说明" min-width="200">
              <template #default="{ row }">
                {{ api.finalizeCodeLabel(row.code) }}
              </template>
            </el-table-column>
            <el-table-column label="详情" min-width="240" prop="detail" />
          </el-table>
        </div>
        <div v-if="store.finalizeInfo.warnings.length" style="margin-top: 10px">
          <div class="lbl" style="color: var(--zf-warning)">告警（可忽略）</div>
          <el-table :data="store.finalizeInfo.warnings" size="small">
            <el-table-column label="码" width="70" prop="code" />
            <el-table-column label="说明" min-width="200">
              <template #default="{ row }">
                {{ api.finalizeCodeLabel(row.code) }}
              </template>
            </el-table-column>
            <el-table-column label="详情" min-width="240" prop="detail" />
          </el-table>
        </div>
      </div>
    </section>

    <!-- ══════════ ★ 发布前体检（21 文档任务六） ══════════ -->
    <section v-loading="coverageLoading" class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">发布前体检</h2>
        <div class="zf-row">
          <el-tag
            v-if="coverage"
            size="small"
            :type="coverage.can_publish ? 'success' : 'danger'"
          >
            {{ coverage.can_publish ? '可发布' : `阻断 ${coverage.blocking.length}` }}
          </el-tag>
          <el-tag
            v-if="coverage?.warnings.length"
            size="small"
            type="warning"
          >
            告警 {{ coverage.warnings.length }}
          </el-tag>
          <el-button size="small" @click="loadCoverage">重新体检</el-button>
        </div>
      </div>
      <div class="zf-card__body">
        <template v-if="coverage">
          <p class="zf-muted" style="margin: 0 0 10px">
            {{ coverage.summary }}
            <span class="zf-dim">
              （知识点只统计**已人工确认**的标注，AI 待确认不计入）
            </span>
          </p>

          <!-- 顶部：总分与题量对照 -->
          <div class="zf-counts" style="margin-bottom: 14px">
            <div class="zf-count">
              <div class="zf-count__n">{{ coverage.item_count }}</div>
              <div class="zf-count__l">题量</div>
            </div>
            <div class="zf-count">
              <div class="zf-count__n">{{ coverage.total_score }}</div>
              <div class="zf-count__l">
                卷面总分
                <span v-if="coverage.target_total_score !== null">
                  / 期望 {{ coverage.target_total_score }}
                </span>
              </div>
            </div>
            <div class="zf-count">
              <div class="zf-count__n">{{ coverage.knowledge.node_count }}</div>
              <div class="zf-count__l">覆盖考点</div>
            </div>
            <div class="zf-count">
              <div
                class="zf-count__n"
                :class="{ 'zf-count__n--zero': !coverage.knowledge.untagged_count }"
              >
                {{ coverage.knowledge.untagged_count }}
              </div>
              <div class="zf-count__l">未挂考点的题</div>
            </div>
          </div>

          <!-- 阻断 / 告警 -->
          <div v-if="coverage.blocking.length" style="margin-bottom: 12px">
            <div class="lbl" style="color: var(--zf-danger)">阻断项（必须解决）</div>
            <el-table :data="coverage.blocking" size="small">
              <el-table-column label="码" width="110" prop="code" />
              <el-table-column label="说明" min-width="320" prop="message" />
            </el-table>
          </div>
          <div v-if="coverage.warnings.length" style="margin-bottom: 12px">
            <div class="lbl" style="color: var(--zf-warning)">告警（可忽略）</div>
            <el-table :data="coverage.warnings" size="small">
              <el-table-column label="码" width="110" prop="code" />
              <el-table-column label="说明" min-width="320" prop="message" />
            </el-table>
          </div>

          <!-- 缺口清单 -->
          <div
            v-if="coverage.knowledge.missing_required.length"
            style="margin-bottom: 12px"
          >
            <div class="lbl" style="color: var(--zf-danger)">
              蓝图必需但零覆盖的考点
            </div>
            <div class="zf-row">
              <el-tag
                v-for="id in coverage.knowledge.missing_required"
                :key="id"
                size="small"
                type="danger"
              >
                节点 #{{ id }}
              </el-tag>
              <el-button size="small" type="primary" plain @click="fillMissingNodes">
                去补题（勾选为组卷范围）
              </el-button>
            </div>
          </div>

          <div class="grid2">
            <!-- 知识点覆盖 -->
            <div>
              <div class="lbl">知识点覆盖</div>
              <el-table
                v-if="coverage.knowledge.nodes.length"
                :data="coverage.knowledge.nodes"
                size="small"
                max-height="260"
              >
                <el-table-column label="考点" min-width="160">
                  <template #default="{ row }">
                    {{ row.node_name }}
                    <el-tag
                      v-if="row.required"
                      size="small"
                      type="info"
                      style="margin-left: 4px"
                    >
                      必需
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="题数" width="70" prop="count" />
                <el-table-column label="分值" width="70" prop="score" />
                <el-table-column label="占比" width="80">
                  <template #default="{ row }">
                    {{ (row.share * 100).toFixed(0) }}%
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="zf-empty">还没有挂知识点的题</div>
            </div>

            <!-- 难度分布 -->
            <div>
              <div class="lbl">
                难度分布
                <span class="zf-dim" style="font-weight: normal">
                  （目标 易 30% / 中 30% / 难 40%，
                  容差 ±{{ (coverage.difficulty.tolerance * 100).toFixed(0) }}%）
                </span>
              </div>
              <div
                v-for="b in coverage.difficulty.buckets"
                :key="b.band"
                class="zf-bar"
              >
                <span class="zf-bar__label">
                  {{ DIFF_LABEL[b.band] ?? b.band }}
                  <span class="zf-dim">
                    （目标 {{ (b.target_ratio * 100).toFixed(0) }}%）
                  </span>
                </span>
                <span class="zf-bar__track">
                  <span
                    class="zf-bar__fill"
                    :class="{ 'bar--skew': Math.abs(b.drift) > coverage.difficulty.tolerance }"
                    :style="{ width: (b.count / diffMax) * 100 + '%' }"
                  />
                </span>
                <span class="zf-bar__num">
                  {{ b.count }} 题 / {{ (b.ratio * 100).toFixed(0) }}%
                </span>
              </div>
              <p v-if="coverage.difficulty.skewed" class="zf-dim hint">
                ★ 难度偏斜：最大偏差 {{ (coverage.difficulty.max_drift * 100).toFixed(0) }}%
                超过容差。偏斜是**告警**不是阻断——老师可能就是要一张全是基础题的卷。
              </p>
            </div>
          </div>

          <!-- 重复题 -->
          <div v-if="coverage.duplicates.length" style="margin-top: 12px">
            <div class="lbl">重复题</div>
            <el-table :data="coverage.duplicates" size="small">
              <el-table-column label="类型" width="90">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="row.kind === 'EXACT' ? 'danger' : 'warning'"
                  >
                    {{ row.kind === 'EXACT' ? '完全重复' : '高度相似' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="题号" width="140">
                <template #default="{ row }">
                  第 {{ row.seqs.join(' / ') }} 题
                </template>
              </el-table-column>
              <el-table-column label="相似度" width="100">
                <template #default="{ row }">
                  {{ (row.similarity * 100).toFixed(0) }}%
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
        <div v-else-if="!coverageLoading" class="zf-empty">
          还没有体检数据（先给卷面加题）
        </div>
      </div>
    </section>

    <!-- 知识点选择器 -->
    <el-dialog v-model="showNodePicker" title="选择知识点范围" width="620px">
      <el-checkbox-group v-model="store.selectedNodeIds">
        <div class="nodes">
          <el-checkbox
            v-for="n in nodeOptions"
            :key="n.id"
            :value="n.id"
            style="display: block; margin: 4px 0"
          >
            <span class="zf-mono">{{ n.code }}</span>
            {{ n.name }}
            <span class="zf-dim">（{{ n.taskName }}）</span>
          </el-checkbox>
        </div>
      </el-checkbox-group>
      <template #footer>
        <el-button @click="showNodePicker = false">关闭</el-button>
        <el-button type="primary" @click="store.markDirty(); showNodePicker = false">
          确定
        </el-button>
      </template>
    </el-dialog>
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
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
}
.page-sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}
/* ★ 两栏：左配置、右卷面。窄屏自动堆叠。 */
.cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: var(--zf-space-4);
}
.sec {
  border: 1px solid var(--zf-border-subtle);
  border-radius: var(--zf-radius-md);
  padding: var(--zf-space-3);
  margin-bottom: var(--zf-space-3);
  background: var(--zf-bg-subtle);
}
.sec-head {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  margin-bottom: var(--zf-space-2);
}
.sec-body {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
  margin-top: var(--zf-space-2);
}
.lbl {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
  font-weight: var(--zf-weight-medium);
}
.sum {
  padding-top: var(--zf-space-3);
  border-top: 1px solid var(--zf-border-subtle);
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
  gap: var(--zf-space-3);
}
.result-grid .num {
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.result-grid .lbl {
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}
.gaps {
  margin-top: var(--zf-space-4);
  padding-top: var(--zf-space-3);
  border-top: 1px dashed var(--zf-border-strong);
}
.gaps-title {
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-warning);
  margin-bottom: var(--zf-space-2);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--zf-space-3);
}
.nodes {
  max-height: 46vh;
  overflow-y: auto;
}

/* ── 发布前体检 ── */
.grid2 {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: var(--zf-space-4);
}
.hint {
  margin: 6px 0 0;
  font-size: var(--zf-text-xs);
}
/* ★ 偏斜的柱用告警色：光看百分比看不出「偏了多少」 */
.bar--skew { background: var(--zf-warning, #e6a23c); }

@media (max-width: 1100px) {
  .grid2 { grid-template-columns: 1fr; }
}
</style>
