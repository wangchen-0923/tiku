/**
 * 认证 API 与用户类型。
 *
 * ★ 端点前缀是 `/api/v1/auth/`（不是 `/api/v1/accounts/auth/`）——
 *   见 05 文档 1.3 节。后端两组路径都通，但前端只走前者。
 */

import type { Role } from '@/constants/permissions'
import { http, tokenStore } from './http'

// ★ Role 的唯一定义在 `@/constants/permissions` ——
//   在这里再写一份联合类型，加一级角色时必然漏掉一处。
export type { Role }

/** ★ 与后端 `UserSerializer` 对齐；**绝不含 `password`**。 */
export interface User {
  id: number
  username: string
  real_name: string
  phone: string
  role: Role
  /** 三级显示名（后端算，唯一来源）：系统管理员 / 学校平台账户 / 教师账户 */
  role_label?: string
  /** 角色等级 0/10/20/30 */
  level?: number
  org_id: number | null
  org_name?: string
  is_active: boolean
}

export interface LoginResult {
  access_token: string
  refresh_token: string
  expires_in: number
  user: User
}

export async function login(
  username: string,
  password: string,
): Promise<LoginResult> {
  const data = await http.post<LoginResult>(
    '/api/v1/auth/login/',
    { username, password },
    { anonymous: true },
  )
  tokenStore.set(data.access_token, data.refresh_token)
  return data
}

export async function me(): Promise<User> {
  return http.get<User>('/api/v1/auth/me/')
}

export function logout(): void {
  // ★ 后端目前没有 logout 端点（JWT 是无状态的，刷新令牌黑名单
  //   依赖 Redis jti，已记为「刷新轮换延后到二期」）。
  //   所以前端退出只需清本地令牌——这是**如实行为**，不是偷懒。
  tokenStore.clear()
}
