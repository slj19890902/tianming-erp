import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useTabsStore } from './stores/tabs'
import LoginView from './views/LoginView.vue'
import { formalOrderEntryQuery } from './utils/formalOrderEntry'

const DashboardView = () => import('./views/FormalWorkspaceView.vue')
const MasterDataView = () => import('./views/MasterDataView.vue')
const OrderView = () => import('./views/OrderView.vue')
const OrdersWorkspaceView = () => import('./views/OrdersWorkspaceView.vue')
const RequisitionView = () => import('./views/RequisitionView.vue')
const DeliveryView = () => import('./views/DeliveryView.vue')
const StatementView = () => import('./views/StatementView.vue')
const MobileReceiveView = () => import('./views/MobileReceiveView.vue')
const WarehouseView = () => import('./views/WarehouseView.vue')

export const routes: RouteRecordRaw[] = [
  {path:'/connection',name:'connection',component:()=>import('./views/ConnectionView.vue'),meta:{public:true,title:'重新连接'}},
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { title: '登录', public: true },
  },
  { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '首页', formalCompatibility: true, formalPage: 'dashboard' } },
  { path: '/orders', name: 'formal-orders', component: DashboardView, meta: { title: '订单', permission: 'orders.view', formalCompatibility: true, formalPage: 'orders' } },
  { path: '/requisitions', name: 'formal-requisitions', component: DashboardView, meta: { title: '报料', permission: 'requisition.view', formalCompatibility: true, formalPage: 'requisition' } },
  { path: '/deliveries', name: 'formal-deliveries', component: DashboardView, meta: { title: '送货与回单', permission: 'deliveries.view', formalCompatibility: true, formalPage: 'deliveries' } },
  { path: '/warehouse', name: 'formal-warehouse', component: DashboardView, meta: { title: '仓库', permission: 'warehouse.view', formalCompatibility: true, formalPage: 'warehouse' } },
  { path: '/statements', name: 'formal-finance', component: DashboardView, meta: { title: '对账与开票', permission: 'finance.view', formalCompatibility: true, formalPage: 'finance' } },
  { path: '/master-data', name: 'formal-master-data', component: DashboardView, meta: { title: '主数据', permissionAny: ['customers.view', 'products.view'], formalCompatibility: true, formalPage: 'customers' } },
  { path: '/orders/new', redirect: () => ({ path: '/orders', query: formalOrderEntryQuery('new') }) },
  { path: '/review/master-data', name: 'master-data', component: MasterDataView, meta: { title: '基础资料', permissionAny: ['customers.view', 'products.view'] } },
  { path: '/review/orders', name: 'orders', component: OrdersWorkspaceView, meta: { title: '订单列表', permission: 'orders.view' } },
  { path: '/review/orders/new', redirect: () => ({ path: '/orders', query: formalOrderEntryQuery('new') }) },
  { path: '/review/orders/native-new', name: 'order-new', component: OrderView, meta: { title: '原生录单待核对', permission: 'orders.create' } },
  { path: '/review/requisitions', name: 'requisitions', component: RequisitionView, meta: { title: '报料工作台', permission: 'requisition.view' } },
  { path: '/excel-requisitions', redirect: '/requisitions' },
  { path: '/review/deliveries', name: 'deliveries', component: DeliveryView, meta: { title: '送货与回单', permission: 'deliveries.view' } },
  { path: '/review/warehouse', name: 'warehouse', component: WarehouseView, meta: { title: '库存与拿货', permission: 'warehouse.view' } },
  { path: '/review/statements', name: 'statements', component: StatementView, meta: { title: '月结对账', permission: 'finance.view' } },
  {
    path: '/mobile-receive',
    name: 'mobile-receive',
    component: MobileReceiveView,
    meta: { title: '移动收料', permission: 'incoming.view' },
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

/** 路由守卫：未登录一律先去 /login，登录后按 redirect 回跳 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.initialized) {
    await auth.restore()
  }
  if (to.path === '/connection') return true
  if (auth.restoreError) return {path:'/connection',query:{redirect:to.fullPath}}
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
  const permission = typeof to.meta.permission === 'string' ? to.meta.permission : ''
  if (permission && !auth.hasPermission(permission)) return '/'
  const any = Array.isArray(to.meta.permissionAny) ? to.meta.permissionAny.map(String) : []
  if (any.length && !any.some((code) => auth.hasPermission(code))) return '/'
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
