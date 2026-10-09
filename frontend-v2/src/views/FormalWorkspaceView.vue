<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { parseFormalOrderEntry } from '../utils/formalOrderEntry'
import { useTabsStore } from '../stores/tabs'
import { useAuthStore } from '../stores/auth'
const frame = ref<HTMLIFrameElement | null>(null)
const props = defineProps<{ workspacePage: string; workspacePath: string }>()
const route = useRoute()
const router = useRouter()
const tabs = useTabsStore()
const auth = useAuthStore()
// Snapshot each kept workspace's own entry; switching another route must not
// reload its unfinished legacy form.
const page = props.workspacePage
const routePath = props.workspacePath
const active = computed(() => route.path === routePath)
const source = '/frontend-v2/formal-workspace?embedded=1&frontend_shell=1&unified_navigation=1&page=' + encodeURIComponent(page)
const entryMessage = ref('')
const connectionError = ref('')
const connected = ref(false)
const frameRevision = ref(0)
let connectionTimer: ReturnType<typeof setTimeout> | undefined
function armConnection() {
  clearTimeout(connectionTimer)
  connectionTimer = setTimeout(() => { if (!connected.value) connectionError.value = '工作区连接尚未完成。可以先重新连接；若页面未加载，可重新加载工作区。' }, 20000)
}
function retryConnection() {
  connectionError.value = ''
  connect(true)
  armConnection()
}
function reloadWorkspace() {
  const child = frame.value?.contentWindow as (Window & {ERPFrontendReliability?:{state:()=>{dirty:boolean;saving:boolean;uncertain:boolean}}}) | null
  let state
  try {state = child?.ERPFrontendReliability?.state()} catch { /* failed frame */ }
  if (tabs.dirtyPaths[routePath] || state?.dirty || state?.saving || state?.uncertain) {
    connectionError.value = '当前工作区有未保存内容或待核对的提交，请先继续处理；重新连接会保留当前内容。'
    return
  }
  connectionError.value = ''; connected.value = false; navigationReady = false
  frameRevision.value++; armConnection()
}
let navigationReady = false
let pending: ReturnType<typeof parseFormalOrderEntry> = null
const sentRequests = new Set<string>()
let responseTimer: ReturnType<typeof setTimeout> | undefined
function connect(retrySession = false) {
  navigationReady = false
  frame.value?.contentWindow?.postMessage({type:'tianming-formal-shell-v1',unifiedNavigation:true,active:active.value,retrySession}, location.origin)
}
function sendEntry() {
  if (!active.value || page !== 'orders' || !navigationReady) return
  const entry = parseFormalOrderEntry(route.query.order_entry, route.query.order_request)
  if (!entry || sentRequests.has(entry.requestId)) return
  sentRequests.add(entry.requestId)
  pending = entry
  entryMessage.value = '正在打开原业务入口…'
  frame.value?.contentWindow?.postMessage({type:'tianming-formal-order-entry-v1', ...entry}, location.origin)
  clearTimeout(responseTimer)
  responseTimer = setTimeout(() => {
    if (pending?.requestId === entry.requestId) entryMessage.value = '入口尚未确认，请在下方完整业务区核对；不会自动重复打开。'
  }, 15000)
}
function receive(event: MessageEvent) {
  if (event.origin !== location.origin || event.source !== frame.value?.contentWindow) return
  if (event.data?.type === 'tianming-formal-bridge-ready-v1') { connect(); return }
  if (event.data?.type === 'tianming-formal-draft-state-v1') {
    if (event.data.actorId === auth.user?.id && Number.isSafeInteger(event.data.generation)
      && ['dirty','saving','uncertain'].every(key => typeof event.data[key] === 'boolean')) {
      tabs.markDirty(routePath,event.data.dirty || event.data.saving || event.data.uncertain)
    }
    return
  }
  if (event.data?.type === 'tianming-formal-orders-changed-v1') {
    if (page === 'orders' && navigationReady && Number.isSafeInteger(event.data.actorId) && event.data.actorId === auth.user?.id) {
      window.dispatchEvent(new CustomEvent('tianming-orders-changed', {detail:{actorId:event.data.actorId}}))
    }
    return
  }
  if (event.data?.type === 'tianming-formal-navigation-v1') {
    if (event.data.sessionUnavailable === true) connectionError.value = '暂时无法确认工作区的登录状态，请检查网络后点“重新连接”，无需修改密码。'
    if (typeof event.data.draftOpen === 'boolean') tabs.markDirty(routePath, event.data.draftOpen)
    navigationReady = event.data.ready === true
    if (navigationReady) {connected.value = true; connectionError.value = ''; clearTimeout(connectionTimer)}
    sendEntry()
    return
  }
  if (event.data?.type !== 'tianming-formal-order-entry-result-v1' || !pending) return
  if (event.data.requestId !== pending.requestId || event.data.action !== pending.action) return
  const messages: Record<string, string> = {
    opened: '', busy: '当前有未完成的表单或请求，请先在下方继续处理，再打开其他入口。',
    denied: '当前账号或页面不能打开此入口，请核对权限。', failed: '入口未能打开，请使用下方原业务按钮继续。',
  }
  if (typeof event.data.status !== 'string' || !Object.hasOwn(messages, event.data.status)) return
  entryMessage.value = messages[event.data.status] || ''
  clearTimeout(responseTimer)
  const requestId = pending.requestId
  pending = null
  if (active.value && route.query.order_request === requestId) {
    const query = { ...route.query }
    delete query.order_entry
    delete query.order_request
    void router.replace({ path: routePath, query })
  }
}
watch(active, () => frame.value?.contentWindow?.postMessage({type:'tianming-formal-shell-v1',unifiedNavigation:true,active:active.value}, location.origin))
watch(() => [route.path, route.query.order_entry, route.query.order_request], sendEntry)
onMounted(() => {window.addEventListener('message', receive); armConnection()})
onBeforeUnmount(() => { clearTimeout(connectionTimer); clearTimeout(responseTimer); window.removeEventListener('message', receive) })
</script>
<template>
  <div v-if="connectionError" class="entry-message" role="alert">{{ connectionError }}
    <button type="button" @click="retryConnection">重新连接（保留内容）</button>
    <button type="button" @click="reloadWorkspace">重新加载工作区</button>
  </div>
  <p v-if="entryMessage" class="entry-message" role="status">{{ entryMessage }}</p>
  <iframe :key="frameRevision" ref="frame" :data-formal-route="routePath" class="formal-workspace"
    :src="source" title="正式版完整业务工作区" @load="connect()" @error="connectionError='工作区加载失败，请检查网络后重新加载。'" />
</template>
<style scoped>
.formal-workspace { display:block; width:100%; flex:1; min-height:0; border:0; border-radius:10px; background:#fff; }
.entry-message button { margin:4px 8px; padding:6px 10px; cursor:pointer; border:1px solid #cbd5e1; border-radius:4px; background:white; }
.entry-message { margin:0 0 8px; padding:8px 12px; background:#fff7ed; color:#9a3412; border-radius:6px; }
</style>
