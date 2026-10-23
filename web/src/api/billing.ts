/**
 * 积分与凭证 API（U27）。
 *
 * 接口一览（都挂在 `/api/v1/billing/` 下）：
 *
 *     GET    account/            我的余额快照
 *     GET    ledger/             我的流水（分页 + 方向/账期筛选）
 *     GET    settings/           积分规则（所有人可读）
 *     PUT    settings/           改规则（★ 学校平台账户）
 *     POST   grants/run/         手动发放本月配额（★ 学校平台账户，幂等）
 *     POST   adjust/             人工调账（★ 学校平台账户）
 *     GET    org/accounts/       全校账户概览（★ 学校平台账户）
 *     GET    reconcile/          对账结果（★ 学校平台账户）
 *     GET    keys/               我的 Key（★ 教师及以上）
 *     POST   keys/               新增/覆盖 Key
 *     PATCH  keys/{id}/          启用 / 停用
 *     DELETE keys/{id}/          删除
 *     POST   keys/{id}/test/     真发一次最小请求
 *
 * ★★ 三条前端纪律
 *
 *   1. **余额只读 `account/`，绝不自己 `sum(流水)`。**
 *      与个人中心、与对账脚本用的是同一个后端函数（`account_snapshot`）。
 *      ★ 自己加总看着"更实时"，但分页只返回当前 20 条，
 *        加出来的是"这 20 条之和"，不是余额。这种错最致命：
 *        它看起来像个精确的数字。
 *
 *   2. **Key 明文只在提交那一刻存在。**
 *      后端只回 `key_masked`（`****末四位`），前端不缓存明文、
 *      不回填输入框、不在列表里显示。
 *
 *   3. **`kek_source === 'derived'` 要在界面上明说。**
 *      它意味着服务器没配 `ZF_SECRET_KEK`，加密密钥是从 `SECRET_KEY`
 *      派生的——换 `SECRET_KEY` 会让所有已保存的 Key 一起解不开。
 *      这不是"配置洁癖"，是「某天所有人的 Key 突然失效」的根因。
 */

import type { Paged } from './paper'
import type { CreditSnapshot } from './profile'
import { http } from './http'

// ══════════════════════════════════════════════════════════════
//  流水
// ══════════════════════════════════════════════════════════════

/** 流水方向。★ 与后端 `LedgerDirection` 逐字一致。 */
export type LedgerDirection = 'GRANT' | 'HOLD' | 'SETTLE' | 'REFUND' | 'ADJUST'

/**
 * 方向 → 中文标签。
 *
 * ★ 刻意区分「系统扣费」与「人工调账」：
 *   合并成「收入/支出」后，月底谁也说不清钱花在哪，
 *   而 `GRANT`（学校发）与 `ADJUST`（管理员补）的追问对象完全不同。
 */
export const DIRECTION_LABEL: Record<LedgerDirection, string> = {
  GRANT: '发放',
  HOLD: '预扣',
  SETTLE: '结算',
  REFUND: '回滚',
  ADJUST: '调账',
}

/** 流水行（对齐 `CreditLedgerSerializer`）。金额一律字符串，前端不做算术。 */
export interface LedgerRow {
  id: number
  direction: LedgerDirection
  scene: string
  /** 变动额：负数为扣减 */
  delta: string
  /** 变动后余额：★ 用它就能逐笔核对，不必自己累加 */
  balance_after: string
  /** 账期 `YYYY-MM` */
  period: string
  ref_type: string
  ref_id: number | null
  remark: string
  username: string | null
  created_at: string
}

export interface LedgerQuery {
  direction?: LedgerDirection | ''
  period?: string
  page?: number
  page_size?: number
}

/**
 * 场景码 → 中文。
 *
 * ★ **未知场景一律回原码，不要显示成「其他」**。
 *   流水是核对用的，出现一个没见过的 `scene` 恰恰说明
 *   「有新任务接进计费了，得去看一眼」——把它统一显示成「其他」
 *   等于把它藏起来，而账对不平的时候就靠这些线索。
 *
 * ★ 这里只做**展示**映射，不参与任何计算；新增计费场景时
 *   即使忘了补这里，也只是显示原码，不会算错。
 */
export const SCENE_LABEL: Record<string, string> = {
  // 人工 / 系统动作
  monthly_grant: '月度发放',
  manual_adjust: '人工调账',
  // AI 任务（与 `apps.ai.routing.ALL_TASKS` 对齐）
  ocr_question: '题目识别',
  extract_figures: '配图提取',
  verify_figure_binding: '配图核对',
  polish_stem: '题干润色',
  gen_explanation: '解析生成',
  design_distractors: '干扰项设计',
  calibrate_difficulty: '难度校准',
  tag_question: '知识点标注',
  generate_question: 'AI 生题',
  kb_cite: '知识引用',
  embed_text: '向量化',
  parse_kb_document: '知识文档解析',
}

export function sceneLabel(scene: string): string {
  return SCENE_LABEL[scene] || scene || '—'
}

export async function getLedger(
  query: LedgerQuery = {},
): Promise<Paged<LedgerRow>> {
  return http.get<Paged<LedgerRow>>('/api/v1/billing/ledger/', {
    direction: query.direction || undefined,
    period: query.period || undefined,
    page: query.page,
    page_size: query.page_size,
  })
}

export async function getAccount(): Promise<CreditSnapshot> {
  return http.get<CreditSnapshot>('/api/v1/billing/account/')
}

// ══════════════════════════════════════════════════════════════
//  积分规则 / 发放 / 调账 / 对账（学校平台账户）
// ══════════════════════════════════════════════════════════════

export type SettingField =
  | 'credit_yuan_per_point'
  | 'credit_monthly_quota_teacher'
  | 'credit_monthly_quota_org_admin'
  | 'credit_low_balance_threshold'

/** 单项设置：`value` + `source`（`org` = 学校自己配过，`platform` = 用平台默认）。 */
export interface SettingItem {
  value: string
  source: 'org' | 'platform'
}

export type CreditSettings = Record<SettingField, SettingItem>

/**
 * 设置项的中文名与说明。
 *
 * ★ 说明列写的是「这项改变会影响什么」，不是「这是什么」——
 *   管理员要知道的是后果，不是名词解释。
 */
export const SETTING_META: Record<
  SettingField,
  { label: string; hint: string }
> = {
  credit_yuan_per_point: {
    label: '积分单价',
    hint: '1 积分折合多少元。改它只影响新产生的费用，历史流水不重算。',
  },
  credit_monthly_quota_teacher: {
    label: '教师月配额',
    hint: '每月发给每位教师的积分。不结转，月初重新发放。',
  },
  credit_monthly_quota_org_admin: {
    label: '学校账户月配额',
    hint: '每月发给学校平台账户的积分。',
  },
  credit_low_balance_threshold: {
    label: '低余额告警阈值',
    hint: '余额低于此值时，个人中心与积分页提示「额度将尽」。',
  },
}

export async function getSettings(): Promise<{ settings: CreditSettings }> {
  return http.get<{ settings: CreditSettings }>('/api/v1/billing/settings/')
}

export async function putSettings(
  payload: Partial<Record<SettingField, string>>,
): Promise<{ settings: CreditSettings }> {
  return http.put<{ settings: CreditSettings }>(
    '/api/v1/billing/settings/',
    payload,
  )
}

export interface GrantRunResult {
  period: string
  granted_users: number
  skipped_users: number
  total_credits: string
}

/**
 * 手动发放月配额。
 *
 * ★ 幂等：同一账期重复点不会双发，跳过的数量在 `skipped_users` 里。
 *   所以按钮不用"发过就置灰"——那种前端状态一旦刷新就丢，
 *   反而会让人以为"没发过"。
 */
export async function runGrant(payload: {
  period?: string
} = {}): Promise<GrantRunResult> {
  return http.post<GrantRunResult>('/api/v1/billing/grants/run/', payload)
}

export async function adjustCredits(payload: {
  user_id?: number | null
  delta: string
  remark: string
}): Promise<LedgerRow> {
  return http.post<LedgerRow>('/api/v1/billing/adjust/', payload)
}

export interface OrgAccountRow {
  account_id: number
  user_id: number | null
  username: string | null
  real_name: string
  role: string | null
  balance: string
  frozen: string
  status: 'ACTIVE' | 'FROZEN'
}

export async function getOrgAccounts(): Promise<{
  items: OrgAccountRow[]
  total: number
}> {
  return http.get<{ items: OrgAccountRow[]; total: number }>(
    '/api/v1/billing/org/accounts/',
  )
}

/**
 * 对账结果。★ 四类问题分开返回，前端也必须分开显示。
 *
 * ★ 只报一句「对账失败」等于什么都没说：`balance_mismatch` 是程序写错了，
 *   `shortfalls` 是要补额度，`stale_holds` 是等结算，`ai_check.diff`
 *   是成本折算对不上 —— 四种处置方式完全不同，混在一起只会让人去找运维。
 *   字段名与后端 `services.reconcile()` 的返回**逐字一致**。
 */
export interface ReconcileResult {
  ok: boolean
  org_id: number | null
  accounts_checked: number
  balance_mismatch: Array<{
    account_id: number
    user_id: number | null
    balance: string
    ledger_sum: string
    diff: string
  }>
  ai_check: {
    holds_checked: number
    holds_unlinked: number
    actual_credits: string
    expected_credits: string
    diff: string
    variance: Array<{
      hold_id: number
      actual: string
      expected: string
      diff: string
    }>
  }
  stale_holds: Array<{
    hold_id: number
    amount: string
    created_at: string
  }>
  shortfalls: Array<{ hold_id: number; shortfall: string }>
}

export async function getReconcile(): Promise<ReconcileResult> {
  return http.get<ReconcileResult>('/api/v1/billing/reconcile/')
}

// ══════════════════════════════════════════════════════════════
//  自定义 API Key
// ══════════════════════════════════════════════════════════════

export type CredentialScope = 'PERSONAL' | 'ORG'
export type CredentialStatus = 'ACTIVE' | 'DISABLED'

export interface ApiKeyRow {
  id: number
  scope: CredentialScope
  provider: string
  base_url: string
  /** `****末四位` —— ★ 永远拿不到明文 */
  key_masked: string
  key_last4: string
  status: CredentialStatus
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string
  created_at: string
}

export interface KeyListResult {
  items: ApiKeyRow[]
  total: number
  /**
   * 加密密钥来源。
   * ★ `derived` = 服务器没配 `ZF_SECRET_KEK`，换 `SECRET_KEY` 会让所有
   *   Key 一起解不开。前端要在页面上明说，别让它只在日志里。
   */
  kek_source: 'env' | 'derived'
}

export async function listKeys(): Promise<KeyListResult> {
  return http.get<KeyListResult>('/api/v1/billing/keys/')
}

export async function createKey(payload: {
  scope: CredentialScope
  provider?: string
  api_key: string
  base_url?: string
}): Promise<ApiKeyRow> {
  return http.post<ApiKeyRow>('/api/v1/billing/keys/', payload)
}

export async function setKeyStatus(
  id: number,
  status: CredentialStatus,
): Promise<ApiKeyRow> {
  return http.patch<ApiKeyRow>(`/api/v1/billing/keys/${id}/`, { status })
}

export async function deleteKey(id: number): Promise<{ deleted: boolean }> {
  return http.del<{ deleted: boolean }>(`/api/v1/billing/keys/${id}/`)
}

export interface KeyTestResult {
  ok: boolean
  message: string
  tested_at: string
}

/**
 * 连通测试。
 *
 * ★ 后端**不走**编排器：测 Key 不该扣积分，也不该因为余额不足失败。
 */
export async function testKey(id: number): Promise<KeyTestResult> {
  return http.post<KeyTestResult>(`/api/v1/billing/keys/${id}/test/`)
}
