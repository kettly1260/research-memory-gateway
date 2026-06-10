import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  base: '/admin/',
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/admin/api': {
        target: 'http://127.0.0.1:8788',
        changeOrigin: true,
      },
    },
  },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
