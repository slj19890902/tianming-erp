<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useDateFormat, useNow } from '@vueuse/core'
import { ElMessage, ElMessageBox, type TabPaneName } from 'element-plus'
import { useTabsStore } from './stores/tabs'
import { useAuthStore } from './stores/auth'
import { authApi } from './api/client'

const router = useRouter()
const route = useRoute()
const tabsStore = useTabsStore()
const authStore = useAuthStore()
const nowText = useDateFormat(useNow(), 'YYYY-MM-DD HH:mm:ss')

const isLogin = computed(() => route.path === '/login')
const isMobileReceive = computed(() => route.path === '/mobile-receive')

interface MenuItem {
  title: string
  path: string
  name: string
  icon: string
  group: string
}

const menus: MenuItem[] = [
  { title: '首页工作台', path: '/', name: 'dashboard', icon: '◈', group: '总览' },
  { title: '基础资料', path: '/master-data', name: 'master-data', icon: '▤', group: '主数据' },
  { title: '订单生产', path: '/orders', name: 'orders', icon: '✎', group: '产销' },
  { title: '报料工作台', path: '/requisitions', name: 'requisitions', icon: '⛁', group: '产销' },
  { title: '超级K列报料台', path: '/excel-requisitions', name: 'excel-requisitions', icon: '▦', group: '产销' },
  { title: '出货回签', path: '/deliveries', name: 'deliveries', icon: '➤', group: '产销' },
  { title: '月结对账', path: '/statements', name: 'statements', icon: '₿', group: '财务' },
  { title: '移动收料', path: '/mobile-receive', name: 'mobile-receive', icon: '◉', group: '现场' },
]

const demoMenus: MenuItem[] = [
  { title: '智能派工单', path: '/print-demo', name: 'print-demo', icon: '⎙', group: '演示工具' },
  { title: '扫码状态面板', path: '/scan-demo', name: 'scan-demo', icon: '☰', group: '演示工具' },
]

const menuGroups = computed(() => {
  const groups: { title: string; items: MenuItem[] }[] = []
  const push = (title: string, items: MenuItem[]) => {
    if (items.length) groups.push({ title, items })
  }
  push('总览', menus.filter((m) => m.group === '总览'))
  push('主数据', menus.filter((m) => m.group === '主数据'))
  push('产销', menus.filter((m) => m.group === '产销'))
  push('财务', menus.filter((m) => m.group === '财务'))
  push('现场', menus.filter((m) => m.group === '现场'))
  push('演示工具', demoMenus)
  return groups
})

const activePath = computed({
  get: () => tabsStore.activePath,
  set: (value: string) => {
    const target = tabsStore.tabs.find((item) => item.path === value)
    if (target) void router.push(target.path)
  },
})

function openMenu(menu: MenuItem) {
  tabsStore.openTab({ name: menu.name, title: menu.title, path: menu.path })
  void router.push(menu.path)
}

function closeTab(name: TabPaneName) {
  const path = String(name)
  tabsStore.closeTab(path)
  if (route.path === path) void router.push(tabsStore.activePath)
}

const passwordDialog = ref(false)
const passwordForm = ref({ current_password: '', new_password: '', confirm: '' })
const passwordSaving = ref(false)

function openPasswordDialog() {
  passwordForm.value = { current_password: '', new_password: '', confirm: '' }
  passwordDialog.value = true
}

async function submitPassword() {
  if (passwordForm.value.new_password !== passwordForm.value.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  // 与后端 _validate_new_password 一致：≥10 位且同时包含字母和数字
  const value = passwordForm.value.new_password
  if (value.length < 10 || !/[A-Za-z]/.test(value) || !/\d/.test(value)) {
    ElMessage.warning('新密码至少 10 位，且必须同时包含字母和数字')
    return
  }
  passwordSaving.value = true
  try {
    await authApi.changePassword({
      current_password: passwordForm.value.current_password,
      new_password: passwordForm.value.new_password,
    })
    ElMessage.success('密码已修改，下次登录生效')
    passwordDialog.value = false
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '修改密码失败')
  } finally {
    passwordSaving.value = false
  }
}

async function handleLogout() {
  try {
    await ElMessageBox.confirm('确认退出当前账号？', '退出登录', { type: 'warning' })
  } catch {
    return
  }
  await authStore.logout()
  tabsStore.reset()
  void router.replace('/login')
}

onMounted(() => {
  document.title = '天明包装 ERP · v2'
})
</script>

<template>
  <el-config-provider>
    <RouterView v-if="isLogin || isMobileReceive" />
    <div v-else class="erp-shell">
      <header class="erp-header">
        <div class="brand">
          <div class="brand-logo">天</div>
          <div>
            <div class="brand-title">天明包装 <span class="v2-badge">ERP · v2</span></div>
            <div class="brand-subtitle">局域网 Web 管理系统</div>
          </div>
        </div>
        <div class="header-status">
          <span class="tm-mono time">{{ nowText }}</span>
          <el-dropdown trigger="click">
            <span class="user-chip">
              <span class="user-avatar">{{ authStore.displayName().slice(0, 1) || '用' }}</span>
              <span>{{ authStore.displayName() }}</span>
              <span class="role-tag">{{ authStore.user?.role }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openPasswordDialog">修改密码</el-dropdown-item>
                <el-dropdown-item divided @click="handleLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <section class="erp-body">
        <aside class="erp-menu">
          <template v-for="group in menuGroups" :key="group.title">
            <div class="menu-group-title">{{ group.title }}</div>
            <button
              v-for="menu in group.items"
              :key="menu.path"
              class="menu-button"
              :class="{ active: route.path === menu.path }"
              type="button"
              @click="openMenu(menu)"
            >
              <span class="menu-icon">{{ menu.icon }}</span>
              <span>{{ menu.title }}</span>
              <span v-if="route.path === menu.path" class="menu-glow"></span>
            </button>
          </template>
        </aside>

        <main class="erp-main">
          <el-tabs
            v-model="activePath"
            type="card"
            class="work-tabs"
            @tab-remove="closeTab"
          >
            <el-tab-pane
              v-for="tab in tabsStore.tabs"
              :key="tab.path"
              :label="tab.title"
              :name="tab.path"
              :closable="tab.path !== '/'"
            />
          </el-tabs>

          <div class="work-panel">
            <RouterView />
          </div>
        </main>
      </section>
    </div>

    <el-dialog v-model="passwordDialog" title="修改密码" width="440px">
      <el-form label-width="90px">
        <el-form-item label="当前密码">
          <el-input v-model="passwordForm.current_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="passwordForm.new_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="passwordForm.confirm" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="passwordSaving" @click="submitPassword">保存</el-button>
      </template>
    </el-dialog>
  </el-config-provider>
</template>

<style scoped>
.erp-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.erp-header {
  height: 64px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  background: linear-gradient(180deg, rgba(16, 26, 46, 0.96), rgba(11, 18, 32, 0.96));
  border-bottom: 1px solid var(--tm-line-strong);
  backdrop-filter: blur(12px);
  position: relative;
  z-index: 10;
}

.erp-header::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  background: var(--tm-accent-gradient);
  opacity: 0.85;
  box-shadow: var(--tm-glow);
}

.brand {
  display: flex;
  gap: 12px;
  align-items: center;
}

.brand-logo {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #04121f;
  background: var(--tm-accent-gradient);
  box-shadow: var(--tm-glow);
  font-size: 22px;
  font-weight: 900;
}

.brand-title {
  font-size: 17px;
  font-weight: 900;
  letter-spacing: 1px;
  color: #f0f6ff;
  display: flex;
  align-items: center;
  gap: 8px;
}

.v2-badge {
  font-size: 11px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 20px;
  color: #04121f;
  background: var(--tm-accent-gradient);
  letter-spacing: 0;
}

.brand-subtitle {
  margin-top: 1px;
  color: var(--tm-text-faint);
  font-size: 12px;
}

.header-status {
  display: flex;
  gap: 18px;
  align-items: center;
}

.time {
  color: var(--tm-text-dim);
  font-size: 13px;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px 6px 6px;
  border: 1px solid var(--tm-line);
  border-radius: 24px;
  background: rgba(22, 35, 60, 0.6);
  cursor: pointer;
  font-size: 14px;
  color: var(--tm-text);
}

.user-chip:hover {
  border-color: var(--tm-accent-a);
}

.user-avatar {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--tm-accent-gradient);
  color: #04121f;
  font-weight: 900;
  font-size: 14px;
}

.role-tag {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 12px;
  background: rgba(34, 211, 238, 0.14);
  border: 1px solid rgba(34, 211, 238, 0.4);
  color: #a5f3fc;
}

.erp-body {
  flex: 1;
  min-height: 0;
  display: flex;
}

.erp-menu {
  width: 216px;
  flex-shrink: 0;
  padding: 14px 12px 20px;
  background: rgba(11, 18, 32, 0.72);
  border-right: 1px solid var(--tm-line);
  overflow-y: auto;
}

.menu-group-title {
  margin: 14px 8px 8px;
  color: var(--tm-text-faint);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 2px;
}

.menu-group-title:first-child {
  margin-top: 2px;
}

.menu-button {
  position: relative;
  width: 100%;
  min-height: 42px;
  margin-bottom: 6px;
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  text-align: left;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--tm-text-dim);
  background: transparent;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.16s ease;
}

.menu-button:hover {
  color: var(--tm-text);
  background: rgba(56, 189, 248, 0.08);
  border-color: var(--tm-line);
}

.menu-button.active {
  color: #eaf6ff;
  background: linear-gradient(135deg, rgba(34, 211, 238, 0.18), rgba(59, 130, 246, 0.14));
  border-color: rgba(34, 211, 238, 0.45);
  box-shadow: var(--tm-glow);
}

.menu-icon {
  width: 22px;
  text-align: center;
  font-size: 15px;
  color: var(--tm-accent-a);
}

.menu-glow {
  margin-left: auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--tm-accent-a);
  box-shadow: 0 0 8px var(--tm-accent-a);
}

.erp-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.work-tabs {
  padding: 10px 14px 0;
}

.work-tabs :deep(.el-tabs__header) {
  border-bottom: 1px solid var(--tm-line);
  margin-bottom: 0;
}

.work-tabs :deep(.el-tabs--card > .el-tabs__header .el-tabs__item) {
  background: rgba(16, 26, 46, 0.6);
  border: 1px solid var(--tm-line);
  color: var(--tm-text-dim);
}

.work-tabs :deep(.el-tabs--card > .el-tabs__header .el-tabs__item.is-active) {
  background: linear-gradient(180deg, rgba(34, 211, 238, 0.16), rgba(59, 130, 246, 0.08));
  border-bottom-color: transparent;
  color: #a5f3fc;
  font-weight: 700;
}

.work-panel {
  flex: 1;
  min-height: 0;
  padding: 14px;
  overflow: auto;
}
</style>
