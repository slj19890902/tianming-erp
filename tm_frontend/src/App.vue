<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useDateFormat, useNow } from '@vueuse/core'
import { useTabsStore } from './stores/tabs'
import type { TabPaneName } from 'element-plus'

const router = useRouter()
const route = useRoute()
const tabsStore = useTabsStore()
const nowText = useDateFormat(useNow(), 'YYYY-MM-DD HH:mm:ss')
const currentUser = ref('管理员')
const isMobileReceive = computed(() => route.path === '/mobile-receive')

const menus = [
  { title: '首页工作台', path: '/', name: 'dashboard' },
  { title: '基础资料', path: '/master-data', name: 'master-data' },
  { title: '订单生产', path: '/orders', name: 'orders' },
  { title: '报料工作台', path: '/requisitions', name: 'requisitions' },
  { title: '超级K列报料台', path: '/excel-requisitions', name: 'excel-requisitions' },
  { title: '出货回签', path: '/deliveries', name: 'deliveries' },
  { title: '月结对账', path: '/statements', name: 'statements' },
  { title: '智能派工单', path: '/print-demo', name: 'print-demo' },
  { title: '扫码状态面板', path: '/scan-demo', name: 'scan-demo' },
]

const activePath = computed({
  get: () => tabsStore.activePath,
  set: (value: string) => {
    const target = tabsStore.tabs.find((item) => item.path === value)
    if (target) router.push(target.path)
  },
})

function openMenu(menu: (typeof menus)[number]) {
  tabsStore.openTab(menu)
  router.push(menu.path)
}

function closeTab(name: TabPaneName) {
  const path = String(name)
  tabsStore.closeTab(path)
  if (route.path === path) router.push(tabsStore.activePath)
}

watch(
  () => route.fullPath,
  () => {
    const title = String(route.meta.title || '业务页面')
    tabsStore.openTab({
      name: String(route.name || route.path),
      title,
      path: route.path,
    })
  },
  { immediate: true },
)

onMounted(() => {
  document.title = '天明包装 ERP'
})
</script>

<template>
  <el-config-provider size="large">
    <RouterView v-if="isMobileReceive" />
    <div v-else class="erp-shell">
      <header class="erp-header">
        <div class="brand">
          <div class="brand-logo">天</div>
          <div>
            <div class="brand-title">天明包装 ERP</div>
            <div class="brand-subtitle">局域网 Web 管理系统</div>
          </div>
        </div>
        <div class="header-status">
          <span>当前用户：{{ currentUser }}</span>
          <span>系统时间：{{ nowText }}</span>
        </div>
      </header>

      <section class="erp-body">
        <aside class="erp-menu">
          <div class="menu-group-title">常用入口</div>
          <button
            v-for="menu in menus"
            :key="menu.path"
            class="menu-button"
            :class="{ active: route.path === menu.path }"
            type="button"
            @click="openMenu(menu)"
          >
            {{ menu.title }}
          </button>

          <div class="menu-group-title">业务模块</div>
          <button class="menu-button ghost" type="button" @click="router.push('/orders')">客户订单管理</button>
          <button class="menu-button ghost" type="button" @click="router.push('/requisitions')">报料工作台</button>
          <button class="menu-button ghost" type="button" @click="router.push('/excel-requisitions')">超级K列报料台</button>
          <button class="menu-button ghost" type="button" @click="router.push('/mobile-receive')">纸板入仓</button>
          <button class="menu-button ghost" type="button" @click="router.push('/deliveries')">送货回单</button>
          <button class="menu-button ghost" type="button" @click="router.push('/statements')">月结对账</button>
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
  </el-config-provider>
</template>

<style scoped>
.erp-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: #d7e3f1;
}

.erp-header {
  height: 78px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 22px;
  color: #fff;
  background: linear-gradient(180deg, #1f4f80 0%, #0c3159 100%);
  border-bottom: 3px solid #f2b84b;
}

.brand {
  display: flex;
  gap: 14px;
  align-items: center;
}

.brand-logo {
  width: 48px;
  height: 48px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  color: #0c3159;
  background: #ffd166;
  font-size: 30px;
  font-weight: 900;
}

.brand-title {
  font-size: 28px;
  font-weight: 900;
  letter-spacing: 1px;
}

.brand-subtitle {
  margin-top: 2px;
  color: #dbeafe;
  font-size: 16px;
}

.header-status {
  display: flex;
  gap: 24px;
  align-items: center;
  font-size: 18px;
  font-weight: 700;
}

.erp-body {
  flex: 1;
  min-height: 0;
  display: flex;
}

.erp-menu {
  width: 238px;
  padding: 14px 12px;
  background: #eef5fb;
  border-right: 2px solid #9fb7d2;
}

.menu-group-title {
  margin: 12px 6px 8px;
  color: #36516f;
  font-size: 15px;
  font-weight: 900;
}

.menu-button {
  width: 100%;
  height: 48px;
  margin-bottom: 8px;
  padding: 0 14px;
  text-align: left;
  border: 1px solid #8aa8c8;
  border-radius: 6px;
  color: #12385f;
  background: #fff;
  font-size: 18px;
  font-weight: 800;
  cursor: pointer;
}

.menu-button.active {
  color: #fff;
  border-color: #174a7c;
  background: #1f5d96;
}

.menu-button.ghost {
  color: #53697f;
  background: #f8fbfe;
}

.erp-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.work-tabs {
  padding: 8px 12px 0;
  background: #cad9eb;
}

.work-panel {
  flex: 1;
  min-height: 0;
  padding: 14px;
  overflow: auto;
}
</style>
