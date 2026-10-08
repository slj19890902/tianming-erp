import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import VxeUITable from 'vxe-table'
import { VxePager } from 'vxe-pc-ui'
import 'element-plus/dist/index.css'
// 保留 Element Plus 显式 dark class 的变量支持
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'vxe-pc-ui/lib/style.css'
import 'vxe-table/lib/style.css'
import './style.css'
import App from './App.vue'
import { router } from './router'
import { onUnauthorized } from './api/client'
import { useAuthStore } from './stores/auth'
import { useTabsStore } from './stores/tabs'

// 默认浅色由共享 CSS tokens 提供；不强制覆盖已有根节点 class。

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn, size: 'default' })
app.use(VxeUITable)
app.use(VxePager)

// API 层 401 → 清登录态、重置标签页、跳登录
onUnauthorized(() => {
  const auth = useAuthStore()
  const tabs = useTabsStore()
  auth.handleUnauthorized()
  tabs.reset()
  // A guest's initial session restore normally returns 401. The route guard
  // still owns that pending navigation and preserves its intended return URL.
  if (!auth.initialized) return
  const current = router.currentRoute.value
  if (current.path !== '/login') {
    void router.replace({ path: '/login', query: { redirect: current.fullPath } })
  }
})

app.mount('#app')
