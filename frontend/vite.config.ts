import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    // Bind the local dev server on IPv4 explicitly.  Node may otherwise
    // resolve `localhost` to ::1 on Windows while the browser/extension uses
    // 127.0.0.1, which looks like a dead page and makes every API action
    // appear to be unsent.
    host: '127.0.0.1',
    strictPort: true,
    proxy: {
      // The backend binds IPv4 loopback.  An explicit address avoids Windows
      // resolving localhost to ::1 first and reporting a misleading proxy
      // Network Error while the IPv4 service is healthy.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  // 生产构建：输出到 frontend/static（由 FastAPI 直接挂载，单端口部署）
  base: './',
  build: {
    outDir: 'static',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Keep framework/UI upgrades cacheable and prevent the application
        // chunk from growing whenever a page adds a component.
        manualChunks: {
          'vue-vendor': ['vue', 'vue-router', 'pinia'],
          'ui-vendor': ['element-plus', '@element-plus/icons-vue'],
          'http-vendor': ['axios'],
        },
      },
    },
  },
})
