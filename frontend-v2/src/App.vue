<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useDateFormat, useNow } from '@vueuse/core'
import { ElMessage, ElMessageBox, type TabPaneName, type TabsPaneContext } from 'element-plus'
import { useTabsStore } from './stores/tabs'
import { useAuthStore } from './stores/auth'
import { authApi } from './api/client'
import { currentFormalFrame } from './utils/formalOrderEntry'
import FormalWorkspaceView from './views/FormalWorkspaceView.vue'

const router = useRouter()
const route = useRoute()
const tabsStore = useTabsStore()
const authStore = useAuthStore()
const nowText = useDateFormat(useNow(), 'YYYY-MM-DD HH:mm:ss')
const mobileMenuOpen = ref(false)
watch(() => route.path, () => { mobileMenuOpen.value = false })

const isLogin = computed(() => route.path === '/login')
const isMobileReceive = computed(() => route.path === '/mobile-receive')
const isFormalWorkspace = computed(() => route.meta.formalCompatibility === true)
const formalWorkspaces = computed(() => tabsStore.tabs.flatMap(tab => {
  const target = router.resolve(tab.path)
  return target.meta.formalCompatibility === true
    ? [{ path: tab.path, page: String(target.meta.formalPage || 'dashboard') }]
    : []
}))

interface MenuItem {
  title: string
  path: string
  name: string
  icon: string
  group: string
  permission?: string
  permissionAny?: string[]
  external?: boolean
}

const menus: MenuItem[] = [
  { title: '首页工作台', path: '/', name: 'dashboard', icon: '◈', group: '总览' },
  { title: '基础资料', path: '/master-data', name: 'master-data', icon: '▤', group: '主数据', permissionAny: ['customers.view', 'products.view'] },
  { title: '订单列表', path: '/orders', name: 'orders', icon: '✎', group: '产销', permission: 'orders.view' },
  { title: '报料工作台', path: '/requisitions', name: 'requisitions', icon: '⛁', group: '产销', permission: 'requisition.view' },
  { title: '预送货', path: '/static/index.html?page=deliveries', name: 'pre-delivery', icon: '⇢', group: '产销', permission: 'deliveries.execute', external: true },
  { title: '送货与回单', path: '/deliveries', name: 'deliveries', icon: '➤', group: '产销', permission: 'deliveries.view' },
  { title: '库存与拿货', path: '/warehouse', name: 'warehouse', icon: '▥', group: '仓库', permission: 'warehouse.view' },
  { title: '仓库地图', path: '/warehouse.html', name: 'warehouse-map', icon: '⌖', group: '仓库', permission: 'warehouse.view', external: true },
  { title: '月结对账', path: '/statements', name: 'statements', icon: '₿', group: '财务', permission: 'finance.view' },
  { title: '移动收料', path: '/mobile-receive', name: 'mobile-receive', icon: '◉', group: '现场', permission: 'incoming.view' },
]

// In compatibility mode, the original page resolves role, permission and
// personal menu order. The shell only renders that safe navigation projection.
const formalMenus = ref<MenuItem[]>([])
const formalActive = ref('')
function formalFrame() {
  return currentFormalFrame(document, route.path)
}
function receiveFormalNavigation(event: MessageEvent) {
  if (event.origin !== location.origin || event.source !== formalFrame()) return
  if (event.data?.type !== 'tianming-formal-navigation-v1') return
  if (event.data.authenticated === false) {
    formalMenus.value = []
    authStore.handleUnauthorized()
    tabsStore.reset()
    void router.replace('/login')
    return
  }
  if (!Array.isArray(event.data.menus)) return
  formalMenus.value = event.data.menus.filter((m: { key?: unknown; label?: unknown }) =>
    typeof m.key === 'string' && /^[a-z0-9_:-]+$/i.test(m.key) && typeof m.label === 'string'
  ).map((m: { key: string; label: string }) => ({
    name: m.key, title: m.label, path: m.key, icon: '◈', group: '业务中心'
  }))
  formalActive.value = String(event.data.active || '')
  const activeMenu = formalMenus.value.find(menu => menu.name === formalActive.value)
  const activeTab = tabsStore.tabs.find(tab => tab.path === route.path)
  if (isFormalWorkspace.value && activeMenu && activeTab) activeTab.title = activeMenu.title
}
onMounted(() => window.addEventListener('message', receiveFormalNavigation))
onBeforeUnmount(() => window.removeEventListener('message', receiveFormalNavigation))

const menuGroups = computed(() => {
  if (isFormalWorkspace.value) return formalMenus.value.length ? [{ title: '业务中心', items: formalMenus.value }] : []
  const groups: { title: string; items: MenuItem[] }[] = []
  const push = (title: string, items: MenuItem[]) => {
    if (items.length) groups.push({ title, items })
  }
  const visible = menus.filter((menu) =>
    (!menu.permission || authStore.hasPermission(menu.permission)) &&
    (!menu.permissionAny || menu.permissionAny.some((code) => authStore.hasPermission(code))),
  )
  push('总览', visible.filter((m) => m.group === '总览'))
  push('主数据', visible.filter((m) => m.group === '主数据'))
  push('产销', visible.filter((m) => m.group === '产销'))
  push('库存', visible.filter((m) => m.group === '仓库'))
  push('财务', visible.filter((m) => m.group === '财务'))
  push('现场', visible.filter((m) => m.group === '现场'))
  return groups
})

// Only a user tab click may navigate. Initial pane registration can emit a
// model update for the home pane before the intended route pane is registered.
function activateTab(pane: TabsPaneContext) {
  activateTabPath(String(pane.props.name))
}
function activateTabPath(path: string) {
  const target = tabsStore.tabs.find((item) => item.path === path)
  if (target && target.path !== route.path) void router.push(target.path)
}

function openMenu(menu: MenuItem) {
  mobileMenuOpen.value = false
  if (isFormalWorkspace.value) {
    formalFrame()?.postMessage({ type: 'tianming-formal-menu-v1', key: menu.name }, location.origin)
    return
  }
  tabsStore.openTab({ name: menu.name, title: menu.title, path: menu.path })
  void router.push(menu.path)
}

async function closeTab(name: TabPaneName) {
  const path = String(name)
  if (tabsStore.dirtyPaths[path]) {
    try {
      await ElMessageBox.confirm('此页有未保存的输入，关闭后会丢弃。确认关闭？', '丢弃工作草稿', {
        type: 'warning', confirmButtonText: '关闭并丢弃', cancelButtonText: '继续编辑',
      })
    } catch { return }
  }
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
  passwordSaving.value = true
  try {
    await authApi.changePassword({
      current_password: passwordForm.value.current_password,
      new_password: passwordForm.value.new_password,
    })
    ElMessage.success('密码已修改，所有旧会话已失效，请重新登录')
    passwordDialog.value = false
    passwordForm.value = { current_password: '', new_password: '', confirm: '' }
    authStore.handleUnauthorized()
    tabsStore.reset()
    await router.replace('/login')
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
    <div v-else class="erp-shell" @keydown.esc="mobileMenuOpen = false">
      <header class="erp-header">
        <button v-if="menuGroups.length" class="mobile-menu-toggle" type="button"
          :aria-label="mobileMenuOpen ? '关闭业务菜单' : '打开业务菜单'"
          :aria-expanded="mobileMenuOpen" aria-controls="erp-business-menu"
          @click="mobileMenuOpen = !mobileMenuOpen">☰ 菜单</button>
        <div class="brand">
          <img class="brand-logo" :src="'/static/assets/tianming-brand-symbol.png'" alt="天明包装" />
          <div>
            <div class="brand-title">天明包装 <span class="v2-badge">ERP · v2</span></div>
            <div class="brand-subtitle">TIANMING PACKAGING</div>
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

      <section class="erp-body" :class="{ 'mobile-menu-open': mobileMenuOpen }">
        <button v-if="menuGroups.length && mobileMenuOpen" class="mobile-menu-backdrop"
          type="button" aria-label="关闭业务菜单遮罩" @click="mobileMenuOpen = false"></button>
        <aside v-if="menuGroups.length" id="erp-business-menu" class="erp-menu" aria-label="业务导航">
          <button class="mobile-menu-close" type="button" @click="mobileMenuOpen = false">收起业务菜单</button>
          <template v-for="group in menuGroups" :key="group.title">
            <div class="menu-group-title">{{ group.title }}</div>
            <template v-for="menu in group.items" :key="menu.path">
              <a
                v-if="menu.external"
                class="menu-button menu-external-link"
                @click="mobileMenuOpen = false"
                :href="menu.path"
                target="_blank"
                rel="noopener noreferrer"
                title="在新标签打开"
                :aria-label="menu.title + '（在新标签打开）'"
              >
                <span class="menu-icon">{{ menu.icon }}</span>
                <span>{{ menu.title }}</span>
                <span class="menu-external-mark" aria-hidden="true">↗</span>
              </a>
              <button
                v-else
                class="menu-button"
                :class="{ active: (isFormalWorkspace ? formalActive === menu.name : route.path === menu.path) }"
                type="button"
                @click="openMenu(menu)"
              >
                <span class="menu-icon">{{ menu.icon }}</span>
                <span>{{ menu.title }}</span>
                <span v-if="(isFormalWorkspace ? formalActive === menu.name : route.path === menu.path)" class="menu-glow"></span>
              </button>
            </template>
          </template>
        </aside>

        <main class="erp-main">
          <el-tabs
            :model-value="tabsStore.activePath"
            type="card"
            class="work-tabs"
            @tab-remove="closeTab"
            @tab-click="activateTab"
          >
            <el-tab-pane
              v-for="tab in tabsStore.tabs"
              :key="tab.path"
              :label="tab.title"
              :name="tab.path"
              :closable="tab.path !== '/'"
            ><template #label><button type="button" class="work-tab-label" @click.stop="activateTabPath(tab.path)">{{ tab.title }}</button></template></el-tab-pane>
          </el-tabs>

          <div class="work-panel" :class="{ 'formal-work-panel': isFormalWorkspace }">
            <!-- Keep iframe documents attached: moving them through KeepAlive's
                 detached cache destroys their browser document and draft state. -->
            <div v-for="workspace in formalWorkspaces" :key="tabsStore.keyFor(workspace.path)"
              v-show="route.path === workspace.path" class="persistent-formal-workspace">
              <FormalWorkspaceView :workspace-page="workspace.page" :workspace-path="workspace.path" />
            </div>
            <RouterView v-slot="{ Component, route: currentRoute }">
              <KeepAlive :max="20">
                <component v-if="currentRoute.meta.formalCompatibility !== true" :is="Component" :key="tabsStore.keyFor(currentRoute.path)" />
              </KeepAlive>
            </RouterView>
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
  height: 100vh;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.erp-header {
  height: 64px;
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  background: var(--tm-header-bg);
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
  width: 46px;
  height: 46px;
  object-fit: contain;
  flex-shrink: 0;
}

.brand-title {
  font-size: 17px;
  font-weight: 900;
  letter-spacing: 1px;
  color: var(--tm-title);
  display: flex;
  align-items: center;
  gap: 8px;
}

.v2-badge {
  font-size: 11px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 20px;
  color: var(--tm-on-accent);
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
  background: var(--tm-bg-2);
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
  color: var(--tm-on-accent);
  font-weight: 900;
  font-size: 14px;
}

.role-tag {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 12px;
  background: var(--tm-accent-soft);
  border: 1px solid var(--tm-accent-border);
  color: var(--tm-selected-text);
}

.mobile-menu-toggle, .mobile-menu-close, .mobile-menu-backdrop { display: none; }

.erp-body {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}

.erp-menu {
  width: 216px;
  flex-shrink: 0;
  padding: 14px 12px 20px;
  background: var(--tm-sidebar-bg);
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

.menu-external-link {
  text-decoration: none;
}

.menu-external-mark {
  margin-left: auto;
  font-size: 12px;
}

.menu-button:hover {
  color: var(--tm-text);
  background: var(--tm-table-hover-bg);
  border-color: var(--tm-line);
}

.menu-button.active {
  color: var(--tm-selected-text);
  background: var(--tm-selected-surface);
  border-color: var(--tm-accent-border);
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
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.work-tabs {
  flex-shrink: 0;
  padding: 10px 14px 0;
}

.work-tabs :deep(.el-tabs__header) {
  border-bottom: 1px solid var(--tm-line);
  margin-bottom: 0;
}

.work-tabs :deep(.el-tabs--card > .el-tabs__header .el-tabs__item) {
  background: var(--tm-bg-2);
  border: 1px solid var(--tm-line);
  color: var(--tm-text-dim);
}

.work-tabs :deep(.el-tabs--card > .el-tabs__header .el-tabs__item.is-active) {
  background: var(--tm-selected-surface);
  border-bottom-color: transparent;
  color: var(--tm-selected-text);
  font-weight: 700;
}

.work-panel {
  flex: 1;
  min-height: 0;
  padding: 14px;
  overflow: auto;
}
.work-tab-label {border:0;padding:0;background:transparent;color:inherit;font:inherit;cursor:pointer;min-height:28px}
.formal-work-panel { overflow: hidden; }
.persistent-formal-workspace { height:100%; min-height:0; display:flex; flex-direction:column; }
/* The original business iframe remains attached while the navigation drawer
   changes visibility. Small screens use the complete content width. */
@media (max-width: 960px) {
  .erp-header { height: auto; min-height: 64px; padding: 6px 10px; gap: 10px; }
  .mobile-menu-toggle, .mobile-menu-close {
    display: block; min-height: 42px; padding: 6px 10px;
    border: 1px solid var(--tm-line); border-radius: 7px;
    color: var(--tm-text); background: var(--tm-bg-2); cursor: pointer;
    flex-shrink: 0; font: inherit;
  }
  .mobile-menu-close { width: 100%; margin-bottom: 10px; }
  .erp-menu { display: none; position: absolute; inset: 0 auto 0 0; z-index: 21; width: min(280px, 85%); }
  .mobile-menu-open .erp-menu { display: block; }
  .mobile-menu-open .mobile-menu-backdrop {
    display: block; position: absolute; inset: 0; z-index: 20;
    border: 0; padding: 0; background: rgba(15, 23, 42, 0.28);
  }
  .brand { min-width: 0; flex: 1; gap: 8px; }
  .header-status { gap: 0; flex-shrink: 0; }
  .header-status .time { display: none; }
  .work-tabs { padding: 6px 4px 0; }
  .work-panel { padding: 4px; }
}
@media (max-width: 640px) {
  .v2-badge, .user-avatar { display: none; }
  .brand-logo { width: 34px; height: 34px; }
  .brand-title { font-size: 15px; letter-spacing: 0; }
  .brand-subtitle { font-size: 10px; }
  .user-chip { max-width: 160px; padding: 6px 8px; gap: 4px; font-size: 12px; }
  .mobile-menu-toggle { padding: 6px 8px; font-size: 13px; }
}
</style>
