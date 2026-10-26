/**
 * 异步任务查询 API（`/api/v1/tasks/`）。
 *
 * ★★ 异步化的价值**全在这个模块**。
 *
 *   「点完按钮，界面转圈，然后什么都不知道」比同步慢更糟 ——
 *   同步至少知道「没跑完是因为它还在跑」。有了这三个端点，界面才能
 *   回答老师真正关心的三个问题：
 *
 *       跑完了吗（`is_terminal`）
 *       跑到哪了（`percent` / `done` / `total`）
 *       为什么没成（`error` / `result.failed`）
 *
 * ★ 可见范围（与 `/api/v1/audit/*` 同一口径，后端已收窄）：
 *   L3 系统管理员 列表与明细都 **403**；L2 本校全部；L1 **仅自己发起的**；
 *   别人的任务返回 **404 而不是 403**（403 等于确认 id 存在）。
 *   → 所以前端不需要自己判权限，拿到 404 就是「不是你的」。
 */

import { http } from './http'

export interface Paged<T> {
  items: T[]
  total: number
}

/** 终态：到这几种就不会再变了，轮询必须停。 */
export const TERMINAL_STATUSES = [
  'SUCCESS',
  'PARTIAL',
  'FAILED',
  'CANCELLED',
] as const

export function isTerminal(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** 状态的中文名。★ 后端也给了 `status_label`，这里只作兜底。 */
export const TASK_STATUS_LABEL: Record<string, string> = {
  PENDING: '排队中',
  RUNNING: '处理中',
  SUCCESS: '已完成',
  PARTIAL: '部分完成',
  FAILED: '失败',
  CANCELLED: '已取消',
}

/**
 * 状态的**语气**。
 *
 * ★★ `PARTIAL` 是 warning 而不是 success —— 它意味着「有一部分没成」。
 *   染成绿色会让老师以为全好了，于是不去看失败明细，
 *   而那几道题就是没生成 / 没批改。
 */
export const TASK_STATUS_TONE: Record<
  string,
  'info' | 'success' | 'warning' | 'danger'
> = {
  PENDING: 'info',
  RUNNING: 'info',
  SUCCESS: 'success',
  PARTIAL: 'warning',
  FAILED: 'danger',
  CANCELLED: 'info',
}

/** 列表项（**不含** `params` / `result`：列表页用不到，而且可能很大）。 */
export interface TaskRunBrief {
  id: number
  kind: string
  kind_label: string
  status: string
  status_label: string
  biz_type: string
  biz_id: number | null
  total: number
  done: number
  failed: number
  percent: number
  is_terminal: boolean
  /** 毫秒；未开始时为 null（显示「—」而不是 0） */
  duration_ms: number | null
  celery_task_id: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

/**
 * 明细（多出 `params` / `result` / `error`）。
 *
 * ★ `error` 在**非失败**状态也可能有值：`PARTIAL` 的失败原因就写在这里，
 *   而 `PARTIAL` 恰恰是最需要看原因的状态。
 */
export interface TaskRunDetail extends TaskRunBrief {
  params: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string
  created_by: number | null
}

export async function listTaskRuns(params: {
  kind?: string
  status?: string
  /** 只返回未结束的（页面刷新后恢复轮询用） */
  active?: boolean
  page?: number
  pageSize?: number
} = {}): Promise<Paged<TaskRunBrief>> {
  return http.get<Paged<TaskRunBrief>>('/api/v1/tasks/', {
    kind: params.kind,
    status: params.status,
    active: params.active ? '1' : undefined,
    page: params.page,
    page_size: params.pageSize,
  })
}

/** 轮询用的单条查询。 */
export async function getTaskRun(id: number): Promise<TaskRunDetail> {
  return http.get<TaskRunDetail>(`/api/v1/tasks/${id}/`)
}
