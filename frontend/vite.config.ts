import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ command }) => ({
  base: command === 'serve' ? '/' : '/frontend-assets/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/interviews': 'http://127.0.0.1:8000',
      '/interview-plans': 'http://127.0.0.1:8000',
      '/materials': 'http://127.0.0.1:8000',
      '/question-bank': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/ai': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
}))
