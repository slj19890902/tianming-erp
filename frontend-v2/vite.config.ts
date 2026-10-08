import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://127.0.0.1:18380'

  return {
    base: '/frontend-v2/',
    plugins: [vue()],
    server: {
      host: '127.0.0.1',
      port: 15380,
      strictPort: true,
      // 本地开发统一走同源 /api 代理，cookie 不跨端口、不直连工厂服务。
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
        },
        '/mobile': {
          target: proxyTarget,
          changeOrigin: true,
        },
        '/static': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
