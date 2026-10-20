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
import MobileLayout from './mobile/MobileLayout.vue'

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
  { path: '/mobile-receive', redirect: '/mobile/receive' },
  { path: '/mobile', component: MobileLayout, meta: { mobile: true }, children: [
    { path: '', name: 'mobile-home', component: () => import('./mobile/MobileHome.vue'), meta: { title: '天明包装', mobile: true } },
    { path: 'query', name: 'mobile-query', component: () => import('./mobile/MobileQuery.vue'), meta: { title: '现场查询', mobile: true } },
    { path: 'products/:id', name: 'mobile-product', component: () => import('./mobile/MobileProduct.vue'), meta: { title: '产品详情', mobile: true } },
    { path: 'recognize', name: 'mobile-recognize', component: () => import('./mobile/MobileRecognize.vue'), meta: { title: '认纸板', mobile: true } },
    { path: 'warehouse', name: 'mobile-warehouse', component: () => import('./mobile/MobileWarehouse.vue'), meta: { title: '仓库地图', mobile: true } },
    { path: 'receive', name: 'mobile-receive', component: MobileReceiveView, meta: { title: '收料', mobile: true } },
    { path: 'more', name: 'mobile-more', component: () => import('./mobile/MobileMore.vue'), meta: { title: '更多', mobile: true } },
  ] },
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
  if (to.path === '/' && window.matchMedia('(max-width: 760px)').matches) return { path: '/mobile' }
  return true
})

/** 每次进入业务页时同步标签页（登录页不进标签） */
router.afterEach((to) => {
  if (to.meta.public === true || to.meta.mobile === true) return
  const tabs = useTabsStore()
  tabs.openTab({
    name: String(to.name || to.path),
    title: String(to.meta.title || '业务页面'),
    path: to.path,
  })
})
