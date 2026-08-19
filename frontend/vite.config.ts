import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  // 生产构建：输出到 frontend/static（由 FastAPI 直接挂载，单端口部署）
  base: './',
  build: {
    outDir: 'static',
    emptyOutDir: true,
  },
})
