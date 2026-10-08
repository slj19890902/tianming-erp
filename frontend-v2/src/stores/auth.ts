import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi, type AuthUser } from '../api/client'

/** 登录态：后端 session 以 httpOnly cookie 维持，前端只存用户信息 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const permissions = ref<string[]>([])
  const initialized = ref(false)

  const displayName = () => user.value?.real_name || user.value?.username || ''
  const hasPermission = (code: string) => permissions.value.includes(code)

  async function login(username: string, password: string, rememberMe: boolean) {
    const result = await authApi.login({ username, password, remember_me: rememberMe })
    user.value = result.user
    permissions.value = result.permissions || []
    return result.user
  }

  async function logout() {
    try {
      await authApi.logout()
    } catch {
      // 即便后端登出失败，本地态也必须清理
    } finally {
      user.value = null
      permissions.value = []
    }
  }

  /** 应用启动 / 路由守卫时恢复登录态 */
  async function restore() {
    try {
      const data = await authApi.me()
      user.value = data.user
      permissions.value = data.permissions || []
    } catch {
      user.value = null
      permissions.value = []
    } finally {
      initialized.value = true
    }
    return user.value
  }

  /** 被 API 层 401 回调时调用 */
  function handleUnauthorized() {
    user.value = null
    permissions.value = []
  }

  return { user, permissions, initialized, displayName, hasPermission, login, logout, restore, handleUnauthorized }
})
