<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore(), route = useRoute(), router = useRouter()
const busy = ref(false)
async function retry() {
  if (busy.value) return
  busy.value = true
  try {
    await auth.restore()
    if (auth.restoreError) return
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
    await router.replace(auth.user ? target : {path:'/login',query:{redirect:target}})
  } finally { busy.value = false }
}
</script>
<template>
  <main class="connection-recovery" role="status">
    <h1>暂时无法确认登录状态</h1>
    <p>{{ auth.restoreError || '请重新连接 ERP 服务。' }}</p>
    <p>连接恢复后会返回您原来打开的页面。</p>
    <button type="button" :disabled="busy" @click="retry">{{ busy ? '正在重新连接…' : '重试连接' }}</button>
  </main>
</template>
<style scoped>
.connection-recovery{max-width:560px;margin:15vh auto;padding:28px;background:#fff;border:1px solid #cbd5e1;border-radius:12px}
h1{font-size:24px}button{padding:10px 24px;border:0;border-radius:6px;background:#155eef;color:#fff;cursor:pointer}button:disabled{opacity:.6}
</style>
