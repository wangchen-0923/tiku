/**
 * Pinia store：认证状态。
 *
 * ★★ 一个刻意的设计：**`user` 为 `null` 不代表「未登录」**，
 *   而是「还不知道」。三种状态必须分开：
 *
 *     access 令牌    user      含义                动作
 *     ────────────  ────────  ──────────────────  ──────────────
 *     无            null      确定未登录          跳登录页
 *     有            null      **还没校验**        先调 /auth/me/
 *     有            对象      已登录              放行
 *
 *   把「还不知道」并进「未登录」会导致**刷新页面被踢回登录页**
 *   （令牌明明还有效），是这类应用最常见的糟糕体验。
 *   所以 `bootstrap()` 只在有令牌时才去校验，并且**只跑一次**。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { login as apiLogin, logout as apiLogout, me as apiMe } from '@/api/auth'
import type { User } from '@/api/auth'
import { ApiError, Unauthorized, tokenStore } from '@/api/http'
import { ROLE_LABEL, can as canDo, levelOf } from '@/constants/permissions'
import type { Capability, Role } from '@/constants/permissions'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  /** 是否已尝试过用本地令牌恢复会话 */
  const resolved = ref(false)
  const loading = ref(false)
  const error = ref('')

  const hasToken = computed(() => Boolean(tokenStore.access))
  /** 真的已登录（令牌 + 用户都拿到了） */
  const isAuthenticated = computed(() => Boolean(user.value))
  /** 确定未登录（尝试过恢复，但没有有效令牌） */
  const isAnonymous = computed(() => resolved.value && !user.value)

  /** 三级显示名：系统管理员 / 学校平台账户 / 教师账户。
   *
   * ★ 优先用**后端**返回的 `role_label`：角色改名只改后端一处，
   *   前端不必跟着发版。`ROLE_LABEL` 只在老接口没有该字段时兜底。
   */
  const roleLabel = computed(() => {
    const u = user.value
    if (!u) return ''
    return u.role_label || ROLE_LABEL[u.role as Role] || ''
  })

  /** 角色等级：0 学生 / 10 教师 / 20 学校平台账户 / 30 系统管理员 */
  const level = computed(
    () => user.value?.level ?? levelOf(user.value?.role),
  )

  /** 是否具备某能力。★ 真正的拦截在后端，这里只用于菜单与按钮显隐。 */
  function can(cap: Capability): boolean {
    return canDo(user.value?.role, cap)
  }

  async function login(username: string, password: string): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const data = await apiLogin(username, password)
      user.value = data.user
      resolved.value = true
    } catch (e) {
      // ★ 后端对「密码错」与「用户不存在」返回**同一个**提示
      //   （AUTH.INVALID_CREDENTIALS），这是刻意的防枚举设计。
      //   前端**不要**自作聪明地区分，否则就绕过了这个防护。
      error.value =
        e instanceof ApiError ? e.message : '登录失败，请稍后重试'
      throw e
    } finally {
      loading.value = false
    }
  }

  /**
   * 用本地令牌恢复会话。**幂等，只会真正跑一次**。
   */
  async function bootstrap(): Promise<void> {
    if (resolved.value || loading.value) return
    if (!tokenStore.access) {
      resolved.value = true
      return
    }
    loading.value = true
    try {
      user.value = await apiMe()
    } catch (e) {
      if (e instanceof Unauthorized) {
        user.value = null
      } else {
        // ★ 网络故障**不等于**未登录：不要把令牌清掉，
        //   否则用户从弱网回来就得重新登录。
        error.value = e instanceof ApiError ? e.message : '会话校验失败'
      }
    } finally {
      resolved.value = true
      loading.value = false
    }
  }

  function logout(): void {
    apiLogout()
    user.value = null
    resolved.value = true
  }

  return {
    user,
    resolved,
    loading,
    error,
    hasToken,
    isAuthenticated,
    isAnonymous,
    roleLabel,
    level,
    can,
    login,
    bootstrap,
    logout,
  }
})
