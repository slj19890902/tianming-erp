<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useDateFormat, useNow } from '@vueuse/core'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Refresh, Lock, SwitchButton, Checked } from '@element-plus/icons-vue'
import { useTabsStore } from './stores/tabs'
import { useAuthStore } from './stores/auth'
import { authApi } from './api/client'
import { currentFormalFrame } from './utils/formalOrderEntry'
import FormalWorkspaceView from './views/FormalWorkspaceView.vue'
import { navigationRequestId, parseShellUi, type ShellUi } from './utils/unifiedNavigation'

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
const shellUi = ref<ShellUi | null>(null)
const pendingCommand = ref<{ requestId: string; key: string } | null>(null)
let commandTimer: ReturnType<typeof setTimeout> | undefined
const primaryItems = computed(() => shellUi.value?.items.filter(item => !item.more) || [])
const moreItems = computed(() => shellUi.value?.items.filter(item => item.more) || [])
const moreActive = computed(() => moreItems.value.find(item => item.active))
const toolsBusy = computed(() => !!pendingCommand.value || (isFormalWorkspace.value && (!shellUi.value || shellUi.value.busy)))
const toolsHint = computed(() => shellUi.value?.busy ? '请先完成或关闭当前表单，等待当前操作完成' : '')
const accountName = computed(() => shellUi.value?.name || authStore.displayName())
const accountRole = computed(() => shellUi.value?.role || ({ admin: '管理员', boss: '老板', sales: '业务', finance: '财务', workshop: '车间', delivery_picker: '送货拿货员' }[authStore.user?.role || ''] || ''))
watch(() => route.path, () => { shellUi.value = null; pendingCommand.value = null; clearTimeout(commandTimer) })
function formalFrame() {
  return currentFormalFrame(document, route.path)
}
function receiveFormalNavigation(event: MessageEvent) {
  if (event.origin !== location.origin || event.source !== formalFrame()) return
  if (event.data?.type === 'tianming-unified-command-result-v1') {
    if (!pendingCommand.value || event.data.requestId !== pendingCommand.value.requestId || event.data.key !== pendingCommand.value.key) return
    const messages: Record<string, string> = {
      done: '', busy: '请先完成或关闭当前表单，等待当前操作完成。',
      denied: '此入口当前不可用，请核对登录状态和权限。', failed: '操作未能完成，请查看工作区提示后重试。',
    }
    if (typeof event.data.status !== 'string' || !Object.hasOwn(messages, event.data.status)) return
    clearTimeout(commandTimer)
    pendingCommand.value = null
    if (messages[event.data.status]) ElMessage.warning(messages[event.data.status])
    return
  }
  if (event.data?.type !== 'tianming-formal-navigation-v1') return
  shellUi.value = event.data.ready === true ? parseShellUi(event.data.shellUi, authStore.user?.id) : null
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
onBeforeUnmount(() => { window.removeEventListener('message', receiveFormalNavigation); clearTimeout(commandTimer) })

function runCommand(key: string) {
  const ui = shellUi.value
  const frame = formalFrame()
  if (!ui || !frame || pendingCommand.value) return
  const requestId = navigationRequestId()
  pendingCommand.value = { key, requestId }
  frame.postMessage({ type: 'tianming-unified-command-v1', key, requestId, actorId: ui.actorId, generation: ui.generation }, location.origin)
  commandTimer = setTimeout(() => {
    pendingCommand.value = null
    ElMessage.warning('操作尚未确认，请先查看当前工作区结果；不会自动重复操作。')
  }, 30000)
}

function accountAction(key: string) {
  mobileMenuOpen.value = false
  if (isFormalWorkspace.value) { runCommand(key); return }
  if (key === 'action:password') openPasswordDialog()
  if (key === 'action:logout') void handleLogout()
}

function openApprovalLink(event: MouseEvent) {
  if (toolsBusy.value) event.preventDefault()
  else mobileMenuOpen.value = false
}

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

function openMenu(menu: MenuItem) {
  mobileMenuOpen.value = false
  if (isFormalWorkspace.value) {
    runCommand('menu:' + menu.name)
    return
  }
  tabsStore.openTab({ name: menu.name, title: menu.title, path: menu.path })
  void router.push(menu.path)
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
    <div v-else class="erp-shell" :class="{ 'shell-large': shellUi?.uiMode === 'large' }" @keydown.esc="mobileMenuOpen = false">
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
        <time class="tm-mono time">{{ nowText }}</time>
        <nav class="module-navigation" :aria-label="shellUi?.label || '当前模块'">
          <span v-if="shellUi?.flow" class="flow-label">按顺序做</span>
          <template v-for="(item, index) in primaryItems" :key="item.key">
            <span v-if="shellUi?.flow && index" class="flow-arrow" aria-hidden="true">→</span>
            <button type="button" class="module-link" :class="{ active: item.active }" :aria-current="item.active ? 'page' : undefined"
              :disabled="toolsBusy" :title="toolsHint" @click="runCommand(item.key)">{{ item.label }}</button>
          </template>
          <el-dropdown v-if="moreItems.length" trigger="click" @command="runCommand">
            <button type="button" class="module-link more-link" :class="{ active: !!moreActive }" :disabled="toolsBusy" :title="toolsHint">
              {{ moreActive?.label || '更多' }}<el-icon><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-for="item in moreItems" :key="item.key" :command="item.key" :disabled="toolsBusy">{{ item.label }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <span v-if="!shellUi" class="module-placeholder">{{ isFormalWorkspace ? '正在连接工作区…' : String(route.meta.title || '业务工作台') }}</span>
        </nav>
        <el-popover v-if="shellUi?.overview" placement="bottom-end" :width="260" trigger="click">
          <template #reference><button type="button" class="overview-button">{{ shellUi.overview.floor }} 地图概况
            <span v-if="shellUi.overview.unlocated + shellUi.overview.conflicts" class="overview-alert">需处理 {{ shellUi.overview.unlocated + shellUi.overview.conflicts }}</span>
            <el-icon><ArrowDown /></el-icon></button></template>
          <div class="overview-details"><strong>仓库当前楼层状态</strong>
            <span>有效批次 <b>{{ shellUi.overview.lots }}</b></span><span>占用库位 <b>{{ shellUi.overview.occupied }}</b></span>
            <span>地图库位 <b>{{ shellUi.overview.locations }}</b></span><span>待定位成品 <b>{{ shellUi.overview.unlocated }}</b></span>
            <span>柱子冲突 <b>{{ shellUi.overview.conflicts }}</b></span></div>
        </el-popover>
      </header>

      <section class="erp-body" :class="{ 'mobile-menu-open': mobileMenuOpen }">
        <button v-if="menuGroups.length && mobileMenuOpen" class="mobile-menu-backdrop"
          type="button" aria-label="关闭业务菜单遮罩" @click="mobileMenuOpen = false"></button>
        <aside id="erp-business-menu" class="erp-menu" aria-label="业务导航">
          <button class="mobile-menu-close" type="button" @click="mobileMenuOpen = false">收起业务菜单</button>
          <div class="menu-scroll">
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
                :disabled="isFormalWorkspace && toolsBusy"
                :title="toolsHint"
                @click="openMenu(menu)"
              >
                <span class="menu-icon">{{ menu.icon }}</span>
                <span>{{ menu.title }}</span>
                <span v-if="(isFormalWorkspace ? formalActive === menu.name : route.path === menu.path)" class="menu-glow"></span>
              </button>
            </template>
          </template>
          </div>
          <section class="account-tools" aria-label="账号与常用工具">
            <div class="account-identity"><span class="user-avatar">{{ accountName.slice(0, 1) || '用' }}</span>
              <div class="account-text"><strong>{{ accountName }} <span>· {{ accountRole }}</span></strong><small>{{ shellUi?.username || authStore.user?.username }}</small></div>
            </div>
            <div v-if="shellUi" class="display-mode" role="group" aria-label="显示模式">
              <span>显示模式</span><div class="mode-options">
                <button v-for="mode in (['standard', 'large'] as const)" :key="mode" type="button" :aria-pressed="shellUi.uiMode === mode"
                  :disabled="!!pendingCommand || shellUi.uiModeSaving" @click="runCommand('ui:' + mode)">{{ mode === 'standard' ? '标准' : '大字' }}</button>
              </div>
            </div>
            <div class="account-actions" :title="toolsHint">
              <a v-if="shellUi?.canApprove" href="/static/business-approvals.html" target="_blank" rel="noopener noreferrer"
                :aria-disabled="toolsBusy" :tabindex="toolsBusy ? -1 : 0" title="在新标签打开，保留当前工作区" @click="openApprovalLink"><el-icon><Checked /></el-icon>{{ shellUi.approvalLabel }}</a>
              <button v-if="shellUi" type="button" :disabled="toolsBusy" @click="accountAction('action:refresh')"><el-icon><Refresh /></el-icon>刷新</button>
              <button type="button" :disabled="toolsBusy" @click="accountAction('action:password')"><el-icon><Lock /></el-icon>修改密码</button>
              <button type="button" class="logout-action" :disabled="toolsBusy" @click="accountAction('action:logout')"><el-icon><SwitchButton /></el-icon>退出登录</button>
            </div>
          </section>
        </aside>

        <main class="erp-main">
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
  gap: 24px;
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
  flex-shrink: 0;
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

.time {
  color: var(--tm-text-dim);
  font-size: 12px;
  white-space: nowrap;
  flex-shrink: 0;
}

.module-navigation { display:flex; align-items:center; gap:6px; flex:1; min-width:0; white-space:nowrap; }
.module-link { min-height:38px; border:1px solid var(--tm-line); border-radius:7px; padding:8px 16px; background:var(--tm-bg-2); color:var(--tm-text-dim); font:inherit; font-size:14px; font-weight:600; cursor:pointer; }
.module-link:hover { background:var(--tm-table-hover-bg); color:var(--tm-selected-text); }
.module-link.active { background:var(--tm-accent-gradient); color:var(--tm-on-accent); }
.more-link { display:flex; align-items:center; gap:6px; }
.flow-label, .module-placeholder { font-size:13px; color:var(--tm-text-faint); margin-right:6px; }
.flow-arrow { font-size:14px; color:var(--tm-text-faint); }
.overview-button { display:flex; align-items:center; gap:7px; border:1px solid var(--tm-line); border-radius:7px; background:var(--tm-bg-2); min-height:36px; padding:6px 10px; color:var(--tm-text-dim); font:inherit; font-size:12px; cursor:pointer; white-space:nowrap; }
.overview-alert { color:#92400e; background:#fff3d6; border-radius:10px; padding:2px 6px; }
.overview-details { display:flex; flex-direction:column; gap:10px; }
.overview-details span { display:flex; justify-content:space-between; }
.erp-shell button:focus-visible { outline:2px solid var(--tm-accent-a); outline-offset:2px; }
.erp-shell button:disabled { cursor:default; opacity:.6; }

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
  padding: 14px 12px 12px;
  background: var(--tm-sidebar-bg);
  border-right: 1px solid var(--tm-line);
  display:flex;
  flex-direction:column;
  min-height:0;
}
.menu-scroll { flex:1; min-height:0; overflow:auto; }
.account-tools { flex-shrink:0; padding:14px 0 0; border-top:1px solid var(--tm-line); margin-top:10px; }
.account-identity { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
.account-identity .user-avatar { width:32px; height:32px; flex-shrink:0; }
.account-text { min-width:0; }
.account-text strong { display:block; font-size:13px; font-weight:600; color:var(--tm-text); line-height:1.5; overflow-wrap:anywhere; }
.account-text strong span { color:var(--tm-text-dim); font-weight:400; }
.account-text small { display:block; color:var(--tm-text-faint); font-size:11px; }
.display-mode { display:flex; align-items:center; justify-content:space-between; gap:6px; font-size:12px; color:var(--tm-text-dim); margin-bottom:10px; }
.mode-options { display:flex; padding:2px; background:var(--tm-bg-2); border:1px solid var(--tm-line); border-radius:7px; }
.mode-options button { background:transparent; color:var(--tm-text-dim); padding:4px 9px; border:0; border-radius:4px; font:inherit; cursor:pointer; }
.mode-options button[aria-pressed="true"] { background:var(--tm-accent-gradient); color:var(--tm-on-accent); }
.account-actions { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
.account-actions button, .account-actions a { min-height:34px; padding:6px 4px; display:flex; justify-content:center; align-items:center; gap:5px; font:inherit; font-size:12px; border:1px solid var(--tm-line); border-radius:6px; color:var(--tm-text-dim); background:var(--tm-bg-2); cursor:pointer; text-decoration:none; }
.account-actions button:hover, .account-actions a:hover { border-color:var(--tm-accent-border); color:var(--tm-selected-text); background:var(--tm-selected-surface); }
.account-actions a[aria-disabled="true"] { cursor:default; opacity:.6; }
.account-actions a:focus-visible { outline:2px solid var(--tm-accent-a); outline-offset:2px; }
.account-actions .logout-action { color:#a83f48; }
.shell-large .module-link, .shell-large .menu-button { font-size:17px; }
.shell-large .account-actions button, .shell-large .account-actions a, .shell-large .account-text strong { font-size:14px; }
.shell-large .module-link { padding-inline:13px; }

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

.work-panel {
  flex: 1;
  min-height: 0;
  padding: 14px;
  overflow: auto;
}
.formal-work-panel { overflow: hidden; padding:0; }
.persistent-formal-workspace { height:100%; min-height:0; display:flex; flex-direction:column; }
/* The original business iframe remains attached while the navigation drawer
   changes visibility. Small screens use the complete content width. */
@media (max-width: 960px) {
  .erp-header { height: auto; min-height: 64px; padding: 6px 10px; gap: 8px; flex-wrap:wrap; }
  .mobile-menu-toggle, .mobile-menu-close {
    display: block; min-height: 42px; padding: 6px 10px;
    border: 1px solid var(--tm-line); border-radius: 7px;
    color: var(--tm-text); background: var(--tm-bg-2); cursor: pointer;
    flex-shrink: 0; font: inherit;
  }
  .mobile-menu-close { width: 100%; margin-bottom: 10px; }
  .erp-menu { display: none; position: absolute; inset: 0 auto 0 0; z-index: 21; width: min(280px, 85%); }
  .mobile-menu-open .erp-menu { display: flex; }
  .mobile-menu-open .mobile-menu-backdrop {
    display: block; position: absolute; inset: 0; z-index: 20;
    border: 0; padding: 0; background: rgba(15, 23, 42, 0.28);
  }
  .brand { min-width: 0; flex: 1; gap: 8px; }
  .time { display:none; }
  .module-navigation { order:5; flex-basis:100%; overflow-x:auto; padding:2px 0; }
  .module-link { padding:7px 11px; }
  .overview-button { font-size:11px; }
  .work-panel { padding: 4px; }
  .formal-work-panel { padding:0; }
}
@media (min-width:961px) and (max-width:1400px) {
  .erp-header { gap:14px; padding-inline:16px; }
  .module-link { padding-inline:12px; }
  .shell-large .module-link { padding-inline:10px; }
}
@media (max-width: 640px) {
  .v2-badge { display: none; }
  .brand-logo { width: 34px; height: 34px; }
  .brand-title { font-size: 15px; letter-spacing: 0; }
  .brand-subtitle { font-size: 10px; }
  .mobile-menu-toggle { padding: 6px 8px; font-size: 13px; }
}
</style>
