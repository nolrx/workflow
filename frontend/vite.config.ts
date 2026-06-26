import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: true,
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true,
      },
      // Session-bound deployed preview of generated frontend projects (served by
      // the backend at the top level, mirroring the nginx /preview proxy in prod).
      '/preview': {
        target: 'http://localhost:5001',
        changeOrigin: true,
      },
      // Deployed full-stack backend proxy (mirrors nginx /app/<pid>/api proxy).
      '/app': {
        target: 'http://localhost:5001',
        changeOrigin: true,
      },
    },
  },
})
