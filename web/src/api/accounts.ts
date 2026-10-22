/**
 * 账号管理 API（教师账号授权）。
 *
 * ★★ 三级账户（2026-10-21 规则改版）之后，学校平台账户的职责收窄成
 *    **「只授权教师账号」** —— 学校不再统一建班级，班级由教师自建。
 *    所以这个页面是学校账户的**主界面**，而不是「设置」里的附页。
 *
 * 接口（全部已有，本文件只是包一层类型）：
 *   GET    /api/v1/accounts/users/                    列表
 *   POST   /api/v1/accounts/users/                    建账号
 *   PATCH  /api/v1/accounts/users/{id}/               改资料 / 角色
 *   DELETE /api/v1/accounts/users/{id}/               停用（软删）
 *   POST   /api/v1/accounts/users/{id}/reset-password/
 *   POST   /api/v1/accounts/users/{id}/transfer/      离职资产移交
 */

import { http } from './http'
import type { Paged } from './paper'

/** 列表里的账号。★ `role` 是数据库值，显示名用 `role_label`。 */
export interface AccountUser {
  id: number
  username: string
  real_name: string
  phone: string
  role: string
  /** 后端算好的三级显示名：系统管理员 / 学校平台账户 / 教师账户 */
  role_label?: string
  level?: number
  org_id: number | null
  org_name?: string
  is_active?: boolean
  created_at?: string
  last_login_at?: string | null
}

export interface AccountListParams {
  keyword?: string
  role?: string
  isActive?: boolean
  page?: number
  pageSize?: number
}

/** 账号列表（分页）。 */
export async function listAccounts(
  params: AccountListParams = {},
): Promise<Paged<AccountUser>> {
  return http.get<Paged<AccountUser>>('/api/v1/accounts/users/', {
    keyword: params.keyword,
    role: params.role,
    is_active: params.isActive === undefined
      ? undefined
      : (params.isActive ? '1' : '0'),
    page: params.page,
    page_size: params.pageSize,
  })
}

export interface CreateAccountInput {
  username: string
  password: string
  real_name?: string
  phone?: string
  role?: string
}

/**
 * 建账号。
 *
 * ★ 学校账户只能建 `TEACHER` —— 建同级/上级会被后端拒绝
 *   （`can_create_role`）。前端不预先拦，因为「为什么不能建」
 *   这句话由后端给更准确（它知道当前有几个人、上限是多少）。
 */
export async function createAccount(
  input: CreateAccountInput,
): Promise<AccountUser> {
  return http.post<AccountUser>('/api/v1/accounts/users/', input)
}

export async function updateAccount(
  id: number,
  input: { real_name?: string; phone?: string; role?: string },
): Promise<AccountUser> {
  return http.patch<AccountUser>(`/api/v1/accounts/users/${id}/`, input)
}

/** 停用（软删）。★ 不能停用自己、不能停用高于自己等级的人。 */
export async function disableAccount(
  id: number, reason = '',
): Promise<AccountUser> {
  return http.post<AccountUser>(`/api/v1/accounts/users/${id}/disable/`, {
    reason,
  })
}

/**
 * 重置密码。
 *
 * ★ 接口收 `new_password` 且**不回显** —— 由管理员自己设定并当面告知。
 *   不要在这里臆造「返回随机密码」：后端没有这个行为，
 *   前端显示一个不存在的密码比不显示更糟。
 */
export async function resetPassword(
  id: number, newPassword: string,
): Promise<null> {
  return http.post<null>(
    `/api/v1/accounts/users/${id}/reset-password/`,
    { new_password: newPassword },
  )
}

/** 离职移交：把源账号的教学资产整体转给接收人。 */
export async function transferAssets(
  id: number, toUserId: number, reason = '',
): Promise<{ counts: Record<string, number>; total: number }> {
  return http.post<{ counts: Record<string, number>; total: number }>(
    `/api/v1/accounts/users/${id}/transfer/`,
    { to_user_id: toUserId, reason },
  )
}
