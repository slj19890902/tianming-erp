import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
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
    path: '/',
    name: 'dashboard',
    component: DashboardView,
    meta: { title: '首页工作台' },
  },
  {
    path: '/print-demo',
    name: 'print-demo',
    component: PrintDemo,
    meta: { title: '智能派工单' },
  },
  {
    path: '/scan-demo',
    name: 'scan-demo',
    component: ScanDemo,
    meta: { title: '扫码状态面板' },
  },
  {
    path: '/master-data',
    name: 'master-data',
    component: MasterDataView,
    meta: { title: '基础资料' },
  },
  {
    path: '/orders',
    name: 'orders',
    component: OrderView,
    meta: { title: '订单生产' },
  },
  {
    path: '/requisitions',
    name: 'requisitions',
    component: RequisitionView,
    meta: { title: '报料工作台' },
  },
  {
    path: '/excel-requisitions',
    name: 'excel-requisitions',
    component: ExcelRequisitionView,
    meta: { title: '超级K列报料台' },
  },
  {
    path: '/deliveries',
    name: 'deliveries',
    component: DeliveryView,
    meta: { title: '出货回签' },
  },
  {
    path: '/statements',
    name: 'statements',
    component: StatementView,
    meta: { title: '月结对账' },
  },
  {
    path: '/mobile-receive',
    name: 'mobile-receive',
    component: MobileReceiveView,
    meta: { title: '移动收料' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
