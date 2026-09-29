import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // 开发时把 API 和 WebSocket 都代理到本机后端（uvicorn 跑在 :8765）
    // 这样前端代码里一律写相对路径 /api/...，开发与生产完全一致
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        ws: true,
      },
    },
  },

  build: {
    // 产物直接落到后端静态目录 —— 「前后端一体」最终就是这一个服务
    outDir: '../app/static',
    emptyOutDir: true,
  },
})
