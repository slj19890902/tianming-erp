<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, Key } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useTabsStore } from '../stores/tabs'
import { authApi, ApiError } from '../api/client'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const tabsStore = useTabsStore()

const form = ref({
  username: localStorage.getItem('tm_erp_username') || '',
  password: '',
  remember_me: true,
})
const loading = ref(false)

// 首次登录强制改密
const forcePassword = ref(false)
const passwordForm = ref({ new_password: '', confirm: '' })
const passwordSaving = ref(false)

function redirectTarget() {
  const redirect = route.query.redirect
  return typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/'
}

async function handleLogin() {
  if (!form.value.username.trim() || !form.value.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const user = await authStore.login(
      form.value.username.trim(),
      form.value.password,
      form.value.remember_me,
    )
    if (form.value.remember_me) localStorage.setItem('tm_erp_username', form.value.username.trim())
    else localStorage.removeItem('tm_erp_username')
    tabsStore.reset()
    if (user.must_change_password) {
      forcePassword.value = true
      ElMessage.warning('首次登录请先修改初始密码')
      return
    }
    ElMessage.success(`欢迎回来，${user.real_name || user.username}`)
    await router.replace(redirectTarget())
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      ElMessage.error('用户名或密码错误')
    } else {
      ElMessage.error(error instanceof Error ? error.message : '登录失败，请检查后端服务')
    }
  } finally {
    loading.value = false
  }
}

async function submitForcePassword() {
  if (passwordForm.value.new_password !== passwordForm.value.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  // 与后端 _validate_new_password 一致：≥10 位且同时包含字母和数字
  const value = passwordForm.value.new_password
  if (value.length < 10 || !/[A-Za-z]/.test(value) || !/\d/.test(value)) {
    ElMessage.warning('新密码至少 10 位，且必须同时包含字母和数字')
    return
  }
  passwordSaving.value = true
  try {
    await authApi.changePassword({
      current_password: form.value.password,
      new_password: passwordForm.value.new_password,
    })
    ElMessage.success('密码已更新')
    forcePassword.value = false
    await router.replace(redirectTarget())
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '修改密码失败')
  } finally {
    passwordSaving.value = false
  }
}

onMounted(() => {
  document.title = '登录 · 天明包装 ERP v2'
})
</script>

<template>
  <div class="login-page tm-grid-bg">
    <div class="glow-orb orb-a"></div>
    <div class="glow-orb orb-b"></div>

    <div class="login-card tm-card">
      <div class="login-brand">
        <div class="brand-logo">天</div>
        <div>
          <h1>天明包装 ERP <span class="v2-badge">v2</span></h1>
          <p>局域网 Web 管理系统 · 深色科技版</p>
        </div>
      </div>

      <el-form v-if="!forcePassword" class="login-form" @submit.prevent="handleLogin">
        <el-form-item>
          <el-input
            v-model="form.username"
            size="large"
            placeholder="用户名"
            autocomplete="username"
            :prefix-icon="User"
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            placeholder="密码"
            autocomplete="current-password"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <div class="login-options">
          <el-checkbox v-model="form.remember_me">记住用户名</el-checkbox>
        </div>
        <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="handleLogin">
          登 录
        </el-button>
        <p class="tm-muted tip">登录态由后端 session 维持；忘记密码请联系管理员重置。</p>
      </el-form>

      <div v-else class="force-password">
        <h2><el-icon><Key /></el-icon> 首次登录请修改密码</h2>
        <p class="tm-muted">检测到初始密码，为保障账号安全请先设置新密码。</p>
        <el-form label-width="86px">
          <el-form-item label="新密码">
            <el-input v-model="passwordForm.new_password" type="password" show-password size="large" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="passwordForm.confirm" type="password" show-password size="large" />
          </el-form-item>
        </el-form>
        <el-button type="primary" size="large" class="login-btn" :loading="passwordSaving" @click="submitForcePassword">
          保存并进入系统
        </el-button>
      </div>
    </div>

    <footer class="login-foot tm-mono">TIANMING PACKAGING · ERP v2 · LAN EDITION</footer>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  position: relative;
  overflow: hidden;
}

.glow-orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  pointer-events: none;
}

.orb-a {
  width: 480px;
  height: 480px;
  left: 8%;
  top: 6%;
  background: rgba(34, 211, 238, 0.16);
}

.orb-b {
  width: 560px;
  height: 560px;
  right: 6%;
  bottom: 4%;
  background: rgba(59, 130, 246, 0.18);
}

.login-card {
  width: 420px;
  max-width: calc(100vw - 40px);
  padding: 34px 34px 26px;
  position: relative;
  z-index: 1;
}

.login-brand {
  display: flex;
  gap: 14px;
  align-items: center;
  margin-bottom: 26px;
}

.brand-logo {
  width: 52px;
  height: 52px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  color: #04121f;
  background: var(--tm-accent-gradient);
  box-shadow: var(--tm-glow);
  font-size: 28px;
  font-weight: 900;
  flex-shrink: 0;
}

.login-brand h1 {
  margin: 0;
  font-size: 21px;
  color: #f0f6ff;
  display: flex;
  align-items: center;
  gap: 8px;
}

.v2-badge {
  font-size: 11px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 20px;
  color: #04121f;
  background: var(--tm-accent-gradient);
}

.login-brand p {
  margin: 4px 0 0;
  color: var(--tm-text-dim);
  font-size: 13px;
}

.login-form :deep(.el-form-item) {
  margin-bottom: 16px;
}

.login-options {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 2px 0 16px;
}

.login-btn {
  width: 100%;
  height: 46px;
  font-size: 16px;
  letter-spacing: 6px;
}

.tip {
  margin-top: 16px;
  text-align: center;
  font-size: 12px;
}

.force-password h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  color: #f0f6ff;
  margin: 0 0 8px;
}

.force-password p {
  margin: 0 0 18px;
}

.login-foot {
  position: absolute;
  bottom: 22px;
  color: var(--tm-text-faint);
  font-size: 11px;
  letter-spacing: 3px;
}
</style>
