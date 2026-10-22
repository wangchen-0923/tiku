/**
 * Pinia store：作业（任务发布）状态。
 *
 * ★★ 一个刻意的设计：**详情与名单分开加载**。
 *
 *     `GET /assignments/{id}/`           详情 + `stats`（轻）
 *     `GET /assignments/{id}/students/`  名单（45 人 × 每行若干字段）
 *
 *   老师的操作大多只需要详情（改时间、看交卷率）；
 *   只有在「核对谁没交」时才需要名单。合并成一个请求会让
 *   每次打开发布页都拉 45 行——而其中 90% 的操作用不到。
 *
 * ★★ `publish()` 的失败要在界面上**能被照做**：
 *    - 未绑定账号（400） → 列出学号，引导去名册绑定
 *    - 试卷未定稿（409） → 引导回组卷页定稿
 *    - 缺答题卡模板（409）→ 引导选模板
 *   这些都不是「操作失败」，而是「还差一步」。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/http'
import * as api from '@/api/assignment'
import type {
  AssignmentDetail,
  AssignmentStudentRow,
  CreateAssignmentBody,
} from '@/api/assignment'

export const useAssignmentStore = defineStore('assignment', () => {
  const detail = ref<AssignmentDetail | null>(null)
  const students = ref<AssignmentStudentRow[]>([])
  /** 后端报告的全班人数（★ 与 `students.length` 不等时说明没拉全） */
  const studentsTotal = ref(0)
  const loading = ref(false)
  const studentsLoading = ref(false)
  const saving = ref(false)
  const error = ref('')
  /** 发布失败时需要老师**照做**的提示（不是一句「失败」） */
  const publishHint = ref<{ text: string; items: string[] } | null>(null)

  const canPublish = computed(() => detail.value?.status === 'DRAFT')
  const canClose = computed(() => detail.value?.status === 'PUBLISHED')
  const canArchive = computed(
    () => detail.value?.status === 'CLOSED',
  )
  const stats = computed(() => detail.value?.stats)
  const submittedRatio = computed(() =>
    Math.round((stats.value?.submitted_ratio ?? 0) * 100),
  )
  /** 发布了才有扫码入口 */
  const entryUrl = computed(() =>
    detail.value?.publish_token && detail.value.status !== 'DRAFT'
      ? api.studentEntryUrl(detail.value.publish_token)
      : '',
  )

  async function load(id: number): Promise<void> {
    loading.value = true
    error.value = ''
    publishHint.value = null
    try {
      detail.value = await api.getAssignment(id)
    } catch (e) {
      error.value = e instanceof Error ? e.message : '加载失败'
      detail.value = null
    } finally {
      loading.value = false
    }
  }

  /**
   * 拉名单。
   *
   * ★★ `students` 是**全量**（api 层自动翻页），不是后端的第一页：
   *   页面上的四种快捷筛选都是本地算的，只拿 20 条就会「少人」。
   */
  async function loadStudents(status?: string): Promise<void> {
    if (!detail.value) return
    studentsLoading.value = true
    try {
      const d = await api.listAssignmentStudents(detail.value.id, status)
      students.value = d.items
      studentsTotal.value = d.total || d.items.length
    } catch (e) {
      error.value = e instanceof Error ? e.message : '名单加载失败'
      students.value = []
      studentsTotal.value = 0
    } finally {
      studentsLoading.value = false
    }
  }

  async function save(body: Partial<CreateAssignmentBody>): Promise<boolean> {
    if (!detail.value) return false
    saving.value = true
    error.value = ''
    try {
      // ★ 时间窗/可见性这类**非结构性**设置现在可以改了
      //   （后端 2026-10-17 起提供 `PATCH /assignments/{id}/`，
      //   白名单字段见 `api.updateAssignment`）。
      //   ★ 但乱序开关与「换卷/换班/换通道」仍然只能重建任务 ——
      //     乱序在发布时已逐生冻结，改它会让卡上题号与系统题号对不上。
      const patch: api.AssignmentSettings = {}
      if (body.start_at !== undefined) patch.start_at = body.start_at
      if (body.end_at !== undefined) patch.end_at = body.end_at
      if (body.answer_visibility !== undefined) {
        patch.answer_visibility = body.answer_visibility
      }
      if (body.score_visibility !== undefined) {
        patch.score_visibility = body.score_visibility
      }
      if (body.allow_late !== undefined) patch.allow_late = body.allow_late
      if (body.pass_ratio !== undefined) patch.pass_ratio = body.pass_ratio
      if (body.auto_practice !== undefined) {
        patch.auto_practice = body.auto_practice
      }
      if (body.auto_grade_on_submit !== undefined) {
        patch.auto_grade_on_submit = body.auto_grade_on_submit
      }
      if (body.shuffle_questions !== undefined) {
        patch.shuffle_questions = body.shuffle_questions
      }
      if (body.shuffle_options !== undefined) {
        patch.shuffle_options = body.shuffle_options
      }
      if (body.sheet_template_id !== undefined) {
        patch.sheet_template_id = body.sheet_template_id
      }
      if (!Object.keys(patch).length) {
        error.value = '没有可修改的设置（试卷、班级请重建任务）'
        return false
      }
      detail.value = await api.updateAssignment(detail.value.id, patch)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '保存失败'
      return false
    } finally {
      saving.value = false
    }
  }

  /**
   * 改单个设置开关（作业详情页的即时开关用）。
   *
   * ★ 与 `save()` 的区别：这个直接发 PATCH，不做「先改本地再保存」的
   *   假象——失败时状态**回滚**，界面上不会留下「看起来改了」的假象。
   */
  async function updateSettings(
    patch: api.AssignmentSettings,
  ): Promise<boolean> {
    if (!detail.value) return false
    saving.value = true
    try {
      detail.value = await api.updateAssignment(detail.value.id, patch)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '保存失败'
      await load(detail.value.id)
      return false
    } finally {
      saving.value = false
    }
  }

  /**
   * 发布。返回 `'ok' | 'need_bind' | 'error'`。
   *
   * ★★ 返回三态而不是布尔：`need_bind`（未绑定账号）与 `error`
   *    需要**完全不同**的界面引导——前者是「去名册绑账号」，
   *    后者是「重试」。合成布尔就只能给一句含糊的提示。
   */
  async function publish(): Promise<'ok' | 'need_bind' | 'error'> {
    if (!detail.value) return 'error'
    saving.value = true
    error.value = ''
    publishHint.value = null
    try {
      await api.publishAssignment(detail.value.id)
      await load(detail.value.id)
      await loadStudents()
      return 'ok'
    } catch (e) {
      if (e instanceof ApiError) {
        // ★ 用 `ApiError.extra()` 安全读取结构化附加数据
        //   （后端把 `ZfError.extra` 放在响应的 `data` 里）。
        const nos = e.extra<string[]>('unbound_student_nos')
        if (nos?.length) {
          // ★ 这是「还差一步」，不是失败
          publishHint.value = {
            text: `名单里有 ${nos.length} 名学生未绑定系统账号，无法发布。`,
            items: nos,
          }
          return 'need_bind'
        }
        const validation = e.extra<{
          blocking?: Array<{ message: string }>
        }>('validation')
        const diffs = validation?.blocking ?? []
        if (diffs.length) {
          publishHint.value = {
            text: '答题卡与试卷不一致，发布被阻断：',
            items: diffs.map((d) => d.message),
          }
          error.value = e.message
          return 'need_bind'
        }
        error.value = e.message
        publishHint.value = { text: e.message, items: [] }
        return 'error'
      }
      error.value = e instanceof Error ? e.message : '发布失败'
      return 'error'
    } finally {
      saving.value = false
    }
  }

  async function close(): Promise<boolean> {
    if (!detail.value) return false
    saving.value = true
    try {
      detail.value = await api.closeAssignment(detail.value.id)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '关闭作答失败'
      return false
    } finally {
      saving.value = false
    }
  }

  async function archive(): Promise<boolean> {
    if (!detail.value) return false
    saving.value = true
    try {
      detail.value = await api.archiveAssignment(detail.value.id)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '归档失败'
      return false
    } finally {
      saving.value = false
    }
  }

  async function markAbsent(studentIds: number[], reason = ''): Promise<boolean> {
    if (!detail.value) return false
    saving.value = true
    try {
      await api.markAbsent(detail.value.id, studentIds, reason)
      await loadStudents()
      await load(detail.value.id)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '标记缺考失败'
      return false
    } finally {
      saving.value = false
    }
  }

  function reset(): void {
    detail.value = null
    students.value = []
    studentsTotal.value = 0
    error.value = ''
    publishHint.value = null
  }

  return {
    detail,
    students,
    studentsTotal,
    loading,
    studentsLoading,
    saving,
    error,
    publishHint,
    canPublish,
    canClose,
    canArchive,
    stats,
    submittedRatio,
    entryUrl,
    load,
    loadStudents,
    save,
    updateSettings,
    publish,
    close,
    archive,
    markAbsent,
    reset,
  }
})
