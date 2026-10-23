/**
 * 个人中心 API（U26）。
 *
 * 接口：
 *   GET   /api/v1/accounts/profile/     资料 + 安全 + 资产（一次拿全）
 *   PATCH /api/v1/auth/me/              改姓名 / 手机号
 *   POST  /api/v1/auth/change-password/ 改密码（★ 改完旧令牌立即失效）
 *
 * ★★ 「资产」为什么不自己算
 *
 *   个人中心的积分必须来自后端的 `account_snapshot()`，与
 *   `GET /api/v1/billing/account/` 是**同一个函数**。
 *   前端绝不能自己 `sum(流水)`：两处算法一旦分叉，老师就会看到
 *   「个人中心 320 分、积分页 318 分」，然后开始怀疑是不是扣错了。
 *   这类不一致比算错本身更伤信任。
 */

import type { User } from './auth'
import { http } from './http'

/** 积分快照 —— 字段与后端 `account_snapshot()` 一一对应。 */
export interface CreditSnapshot {
  /** 当前余额（字符串，避免浮点误差；后端是 Decimal） */
  balance: string
  /** 在途冻结 */
  frozen: string
  /** 低余额告警阈值 */
  low_threshold: string
  /** 是否已低于阈值 */
  is_low: boolean
  /** 单价：1 积分 = 多少元 */
  price_yuan_per_point: string
  /** 账期，如 `2026-10` */
  period: string
  /** 本月已消耗 */
  period_used: string
  /**
   * 本月发放额。
   *
   * ★ 加这个字段是为了区分两种「余额为 0」：
   *   一种是**学校还没发**（等发放即可），
   *   一种是**发了但用完了**（要找管理员）。
   *   只看余额会把前者也报成「余额偏低」，让老师白紧张一场。
   */
  period_granted: string
  /** 本月是否已发放（`period_granted > 0`） */
  has_grant: boolean
}

export interface ProfileAssets {
  /**
   * 资产是否可用。
   *
   * ★ `false` 不是错误：系统管理员不挂机构、不参与计费，
   *   或者账本模块暂时不可用，都会是 `false` + `reason`。
   *   页面**不该**把它显示成「加载失败」。
   */
  available: boolean
  reason: string
  credits: CreditSnapshot | null
  /** 自己配的自定义 API Key 数量 */
  credential_count: number
}

export interface ProfileSecurity {
  last_login_at: string | null
  days_since_login: number | null
  last_login_ip: string | null
  can_change_password: boolean
}

export interface ProfilePayload {
  profile: User
  security: ProfileSecurity
  assets: ProfileAssets
}

export async function getProfile(): Promise<ProfilePayload> {
  return http.get<ProfilePayload>('/api/v1/accounts/profile/')
}

/**
 * 改自己的资料。
 *
 * ★ 只传 `real_name` / `phone`：后端序列化器本身就是白名单，
 *   传 `role` / `username` 也不会生效。前端也不传，免得让人
 *   误以为这个表单能改角色。
 */
export async function updateMe(payload: {
  real_name?: string
  phone?: string
}): Promise<User> {
  return http.patch<User>('/api/v1/auth/me/', payload)
}

/**
 * 改密码。
 *
 * ★★ 成功之后**必须**清本地令牌并跳登录页：
 *
 *   后端会让所有旧令牌（访问 + 刷新）立即失效（密码指纹变了），
 *   继续用旧令牌请求只会拿到 401。与其让用户点一下才撞上
 *   「登录已过期」，不如改完就明确告诉他「请用新密码重新登录」。
 */
export async function changePassword(payload: {
  old_password: string
  new_password: string
}): Promise<void> {
  await http.post<null>('/api/v1/auth/change-password/', payload)
}
