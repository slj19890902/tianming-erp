import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useTabsStore } from './stores/tabs'
import DashboardView from './views/DashboardView.vue'
import LoginView from './views/LoginView.vue'
import PrintDemo from './views/PrintDemo.vue'
import ScanDemo from './views/ScanDemo.vue'
import MasterDataView from './views/MasterDataView.vue'
import OrderView from './views/OrderView.vue'
import RequisitionView from './views/RequisitionView.vue'
import ExcelRequisitionView from './views/ExcelRequisitionView.vue'
import DeliveryView from './views/DeliveryView.vue'
import StatementView from './views/StatementView.vue'
import MobileReceiveView from './views/MobileReceiveView.vue'

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { title: '登录', public: true },
  },
  { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '首页工作台' } },
  { path: '/master-data', name: 'master-data', component: MasterDataView, meta: { title: '基础资料' } },
  { path: '/orders', name: 'orders', component: OrderView, meta: { title: '订单生产' } },
  { path: '/requisitions', name: 'requisitions', component: RequisitionView, meta: { title: '报料工作台' } },
  {
    path: '/excel-requisitions',
    name: 'excel-requisitions',
    component: ExcelRequisitionView,
    meta: { title: '超级K列报料台' },
  },
  { path: '/deliveries', name: 'deliveries', component: DeliveryView, meta: { title: '出货回签' } },
  { path: '/statements', name: 'statements', component: StatementView, meta: { title: '月结对账' } },
  {
    path: '/mobile-receive',
    name: 'mobile-receive',
    component: MobileReceiveView,
    meta: { title: '移动收料' },
  },
  // 演示页：二级入口，不挂主菜单
  { path: '/print-demo', name: 'print-demo', component: PrintDemo, meta: { title: '智能派工单' } },
  { path: '/scan-demo', name: 'scan-demo', component: ScanDemo, meta: { title: '扫码状态面板' } },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

/** 路由守卫：未登录一律先去 /login，登录后按 redirect 回跳 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.initialized) {
    await auth.restore()
  }
  const isPublic = to.meta.public === true
  if (isPublic) {
    if (auth.user) {
      const redirect = typeof to.query.redirect === 'string' ? to.query.redirect : '/'
      return redirect.startsWith('/') ? redirect : '/'
    }
    return true
  }
  if (!auth.user) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  return true
})

/** 每次进入业务页时同步标签页（登录页不进标签） */
router.afterEach((to) => {
  if (to.meta.public === true) return
  const tabs = useTabsStore()
  tabs.openTab({
    name: String(to.name || to.path),
    title: String(to.meta.title || '业务页面'),
    path: to.path,
  })
})
