/**
 * Pinia store：组卷工作区的状态。
 *
 * ★★ 一个刻意的设计：**「配置」与「已组出的结果」分开存**。
 *
 *     config     老师填的规则（题量/配分/难度区间/知识点）
 *     paper      后端当前的实际卷面（可能是手加的、也可能是自动组的）
 *     lastResult 上一次自动组卷的执行报告（含降级与缺口）
 *
 *   为什么不能合并：老师改完 `config` 之后，卷面**不会自动变**
 *   （要显式点「按规则组卷」）。若合并成一个状态，
 *   界面就会显示「配置说 10 题、卷面只有 3 题」而看不出谁对。
 *   分开之后可以明确提示「配置已改，卷面尚未重新生成」。
 *
 * ★ 候选池分页与筛选也放在这里，因为组卷页的左右两栏都要读它。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { BusinessBranch } from '@/api/http'
import type {
  CandidateItem,
  ComposeConfig,
  ComposeResult,
  ComposeSection,
  FinalizeCheck,
  PaperDetail,
} from '@/api/paper'
import * as api from '@/api/paper'

/** 一个空的大题（新建组卷配置时的初始行）。 */
export function emptySection(type = 'SINGLE_CHOICE'): ComposeSection {
  return {
    type,
    count: 5,
    score_each: 4,
    difficulty_range: [1, 5],
    title: '',
  }
}

export const usePaperStore = defineStore('paper', () => {
  // ── 当前试卷 ──
  const paper = ref<PaperDetail | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')

  // ── 组卷配置（可编辑副本）──
  const config = ref<ComposeConfig>({ sections: [emptySection()] })
  const selectedNodeIds = ref<number[]>([])

  /** 配置是否已被改动但尚未重新组卷 */
  const configDirty = ref(false)

  // ── 自动组卷结果 ──
  const lastResult = ref<ComposeResult | null>(null)
  const lastGaps = ref<ComposeResult['gaps']>([])

  // ── 候选池 ──
  const candidates = ref<CandidateItem[]>([])
  const candidateTotal = ref(0)
  const candidatePage = ref(1)
  const candidatePageSize = ref(20)
  const candidateLoading = ref(false)
  const candidateFilters = ref<{
    types: string[]
    difficultyBands: string[]
    /** ★ 按知识点筛选（后端 `node_id`，见 `PaperCandidatesView`） */
    nodeIds: number[]
    keyword: string
  }>({ types: [], difficultyBands: [], nodeIds: [], keyword: '' })

  // ── 定稿检查 ──
  const finalizeInfo = ref<FinalizeCheck | null>(null)

  // ══════════════════════════════════════════════════
  //  派生
  // ══════════════════════════════════════════════════

  /** 配置里各题型的期望题量合计 */
  const expectedCount = computed(() =>
    config.value.sections.reduce((s, x) => s + (Number(x.count) || 0), 0),
  )
  /** 配置的期望总分 */
  const expectedScore = computed(() =>
    config.value.sections.reduce(
      (s, x) => s + (Number(x.count) || 0) * (Number(x.score_each) || 0),
      0,
    ),
  )
  /** 卷面实际题量 */
  const actualCount = computed(() => paper.value?.items?.length ?? 0)
  /** 卷面实际总分（★ 后端给字符串，要转数字） */
  const actualScore = computed(() => {
    const items = paper.value?.items ?? []
    return items.reduce((s, i) => s + (Number(i.score) || 0), 0)
  })
  /** 卷面题量是否与配置不符 */
  const countMismatch = computed(
    () => Boolean(paper.value) && actualCount.value !== expectedCount.value,
  )
  const selectedIdSet = computed(
    () => new Set((paper.value?.items ?? []).map((i) => i.question_id)),
  )
  const canEdit = computed(() => paper.value?.status === 'DRAFT')

  // ══════════════════════════════════════════════════
  //  加载
  // ══════════════════════════════════════════════════

  async function load(paperId: number): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const p = await api.getPaper(paperId)
      paper.value = p
      // ★ 只在「还没编辑过」时用服务端的配置覆盖本地，
      //   否则会把老师正在填的内容冲掉。
      if (!configDirty.value) {
        const cfg = p.compose_config ?? { sections: [] }
        config.value = {
          sections: (cfg.sections ?? []).length
            ? cfg.sections.map((s) => ({ ...s }))
            : [emptySection()],
          knowledge_nodes: cfg.knowledge_nodes ?? [],
          weak_quota: cfg.weak_quota,
          exclude_recent_days: cfg.exclude_recent_days,
        }
        selectedNodeIds.value = [...(cfg.knowledge_nodes ?? [])]
      }
      lastResult.value = null
      lastGaps.value = []
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      paper.value = null
    } finally {
      loading.value = false
    }
  }

  async function loadCandidates(): Promise<void> {
    if (!paper.value) return
    candidateLoading.value = true
    try {
      const d = await api.listCandidates({
        paperId: paper.value.id,
        types: candidateFilters.value.types,
        difficultyBands: candidateFilters.value.difficultyBands,
        // ★ 知识点筛选（后端 `node_id`，可多选）
        nodeIds: candidateFilters.value.nodeIds,
        page: candidatePage.value,
        pageSize: candidatePageSize.value,
      })
      candidates.value = d.items
      candidateTotal.value = d.total
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      candidates.value = []
      candidateTotal.value = 0
    } finally {
      candidateLoading.value = false
    }
  }

  // ══════════════════════════════════════════════════
  //  配置编辑
  // ══════════════════════════════════════════════════

  function markDirty(): void {
    configDirty.value = true
  }

  function addSection(): void {
    // ★ 默认给一个**不重复**的题型：连着加两个单选大题多半是误操作
    const used = new Set(config.value.sections.map((s) => s.type))
    const next =
      Object.keys(api.QUESTION_TYPE_LABEL).find((t) => !used.has(t)) ??
      'SINGLE_CHOICE'
    config.value.sections.push(emptySection(next))
    markDirty()
  }

  function removeSection(idx: number): void {
    if (config.value.sections.length <= 1) return
    config.value.sections.splice(idx, 1)
    markDirty()
  }

  /**
   * 把配置存回试卷（`PATCH /papers/{id}/`）。
   *
   * ★ 只存配置，**不改卷面题** —— 改完配置要显式点「按规则组卷」
   *   才会重新抽题。这样老师可以先调参、看清楚了再动手。
   *
   * ★★ 依赖后端的 `PaperUpdateSerializer.compose_config` 字段。
   *    那个字段**原来缺失**，导致 `PATCH` 返回 200 却静默忽略配置
   *    （老师改完点组卷、拿到旧配置的结果，界面还显示「已保存」）。
   *    已在 `verify_paper_api.py` 的 P10b 补了回归断言
   *    「配置真的变了」——只断言 200 是抓不到静默忽略的。
   *
   * ★ 定稿后后端会拒绝（400）：配置是「这份卷子怎么来的」的记录。
   */
  async function saveConfig(): Promise<boolean> {
    if (!paper.value) return false
    saving.value = true
    error.value = ''
    try {
      const cfg: ComposeConfig = {
        sections: config.value.sections.map((s) => ({
          ...s,
          count: Number(s.count) || 0,
          score_each: Number(s.score_each) || 0,
          difficulty_range: [
            Number(s.difficulty_range?.[0] ?? 1),
            Number(s.difficulty_range?.[1] ?? 5),
          ] as [number, number],
        })),
        knowledge_nodes: selectedNodeIds.value,
      }
      const updated = await api.updatePaper(paper.value.id, {
        compose_config: cfg,
      })
      paper.value = updated
      config.value = cfg
      configDirty.value = false
      return true
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      return false
    } finally {
      saving.value = false
    }
  }

  // ══════════════════════════════════════════════════
  //  自动组卷
  // ══════════════════════════════════════════════════

  /**
   * 按规则组卷。
   *
   * 返回 `'ok' | 'gaps' | 'error'`：
   *   `ok`    组卷成功且无缺口
   *   `gaps`  **业务分支**：候选不足，缺口清单已填入 `lastGaps`
   *   `error` 真失败（配置非法、网络等），`error` 有值
   *
   * ★★ 为什么不用 try/catch 让调用方自己判：
   *    「候选不足」是一条**需要老师决策的正常路径**，不是异常。
   *    把它和真失败混在一起，界面上就没法给出不同引导。
   */
  async function compose(allowPartial = false): Promise<'ok' | 'gaps' | 'error'> {
    if (!paper.value) return 'error'
    saving.value = true
    error.value = ''
    lastGaps.value = []
    try {
      const r = await api.composeByRule(paper.value.id, {
        allow_partial: allowPartial,
      })
      lastResult.value = r
      if (r.paper) paper.value = r.paper
      else paper.value = await api.getPaper(paper.value.id)
      if (r.gaps?.length) {
        lastGaps.value = r.gaps
        return 'gaps'
      }
      return 'ok'
    } catch (e) {
      if (e instanceof BusinessBranch) {
        const d = e.data as { gaps?: ComposeResult['gaps'] }
        lastGaps.value = d?.gaps ?? []
        error.value = e.message
        return 'gaps'
      }
      error.value = String((e as Error)?.message ?? e)
      return 'error'
    } finally {
      saving.value = false
    }
  }

  // ══════════════════════════════════════════════════
  //  手动加题 / 删题
  // ══════════════════════════════════════════════════

  async function addQuestions(
    ids: number[],
    sectionTitle: string,
    scoreEach: number,
  ): Promise<boolean> {
    if (!paper.value || !ids.length) return false
    saving.value = true
    error.value = ''
    try {
      paper.value = await api.addItems(
        paper.value.id,
        ids.map((id) => ({
          question_id: id,
          score: scoreEach,
          section_title: sectionTitle,
        })),
        sectionTitle,
      )
      return true
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      return false
    } finally {
      saving.value = false
    }
  }

  async function removeQuestion(itemId: number): Promise<boolean> {
    if (!paper.value) return false
    saving.value = true
    error.value = ''
    try {
      paper.value = await api.removeItem(paper.value.id, itemId)
      return true
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      return false
    } finally {
      saving.value = false
    }
  }

  // ══════════════════════════════════════════════════
  //  定稿
  // ══════════════════════════════════════════════════

  async function refreshFinalizeCheck(): Promise<void> {
    if (!paper.value) return
    try {
      finalizeInfo.value = await api.finalizeCheck(paper.value.id)
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      finalizeInfo.value = null
    }
  }

  async function finalize(force = false, reason = ''): Promise<boolean> {
    if (!paper.value) return false
    saving.value = true
    error.value = ''
    try {
      paper.value = await api.finalizePaper(paper.value.id, { force, reason })
      await refreshFinalizeCheck()
      return true
    } catch (e) {
      error.value = String((e as Error)?.message ?? e)
      return false
    } finally {
      saving.value = false
    }
  }

  function reset(): void {
    paper.value = null
    config.value = { sections: [emptySection()] }
    selectedNodeIds.value = []
    configDirty.value = false
    lastResult.value = null
    lastGaps.value = []
    finalizeInfo.value = null
    candidates.value = []
    candidateTotal.value = 0
    candidatePage.value = 1
    error.value = ''
  }

  return {
    paper,
    loading,
    saving,
    error,
    config,
    selectedNodeIds,
    configDirty,
    lastResult,
    lastGaps,
    candidates,
    candidateTotal,
    candidatePage,
    candidatePageSize,
    candidateLoading,
    candidateFilters,
    finalizeInfo,
    expectedCount,
    expectedScore,
    actualCount,
    actualScore,
    countMismatch,
    selectedIdSet,
    canEdit,
    load,
    loadCandidates,
    markDirty,
    addSection,
    removeSection,
    saveConfig,
    compose,
    addQuestions,
    removeQuestion,
    refreshFinalizeCheck,
    finalize,
    reset,
  }
})
