import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi, type AuthUser } from '../api/client'

/** 登录态：后端 session 以 httpOnly cookie 维持，前端只存用户信息 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const permissions = ref<string[]>([])
  const initialized = ref(false)
  const restoreError = ref('')
  let generation = 0

  const displayName = () => user.value?.real_name || user.value?.username || ''
  const hasPermission = (code: string) => permissions.value.includes(code)

  async function login(username: string, password: string, rememberMe: boolean) {
    const current = ++generation
    const result = await authApi.login({ username, password, remember_me: rememberMe })
    if (current !== generation) return result.user
    restoreError.value = ''
    initialized.value = true
    user.value = result.user
    permissions.value = result.permissions || []
    return result.user
  }

  async function logout() {
    const current = ++generation
    restoreError.value = ''
    try {
      await authApi.logout()
    } catch {
      // 即便后端登出失败，本地态也必须清理
    } finally {
      if (current === generation) {
        user.value = null
        permissions.value = []
      }
    }
  }

  /** 应用启动 / 路由守卫时恢复登录态 */
  async function restore() {
    const current = ++generation
    restoreError.value = ''
    try {
      const data = await authApi.me()
      if (current !== generation) return user.value
      user.value = data.user
      permissions.value = data.permissions || []
    } catch (error) {
      if (current !== generation) return user.value
      user.value = null
      permissions.value = []
      const status = (error as {status?: number})?.status
      if (status !== 401) restoreError.value = 'ERP 服务暂时未能响应。请检查网络后重试连接，无需修改密码。'
    } finally {
      if (current === generation) initialized.value = true
    }
    return user.value
  }

  /** 被 API 层 401 回调时调用 */
  function handleUnauthorized() {
    ++generation
    restoreError.value = ''
    user.value = null
    permissions.value = []
  }

  return { user, permissions, initialized, restoreError, displayName, hasPermission, login, logout, restore, handleUnauthorized }
})
