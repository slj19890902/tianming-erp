import type { App } from 'vue'
import VxeUITable from 'vxe-table'
import { VxePager } from 'vxe-pc-ui'
import 'vxe-pc-ui/lib/style.css'
import 'vxe-table/lib/style.css'
export function installReviewTables(app: App) {
  app.use(VxeUITable)
  app.use(VxePager)
}
