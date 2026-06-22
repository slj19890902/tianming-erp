import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import VxeUITable from 'vxe-table'
import { VxePager } from 'vxe-pc-ui'
import 'element-plus/dist/index.css'
import 'vxe-pc-ui/lib/style.css'
import 'vxe-table/lib/style.css'
import './style.css'
import App from './App.vue'
import { router } from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn, size: 'large' })
app.use(VxeUITable)
app.use(VxePager)

app.mount('#app')
