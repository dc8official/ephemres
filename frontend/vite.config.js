import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import { readFileSync } from 'node:fs'

// The release, from package.json (kept equal to backend/app/version.py): the UI shows it before
// the API answers, and the page title carries it. Until v3.3.6 it was typed into each place.
const APP_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version

export default defineConfig({
  plugins: [
    vue(),
    { name: 'app-version-title', transformIndexHtml: (html) => html.replaceAll('%APP_VERSION%', APP_VERSION) },
  ],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules/vis-network') || id.includes('node_modules/vis-data')) {
            return 'vendor-vis'
          }
          if (id.includes('node_modules/chart.js') || id.includes('node_modules/vue-chartjs')) {
            return 'vendor-charts'
          }
          if (id.includes('node_modules/primevue') || id.includes('node_modules/@primevue') || id.includes('node_modules/@primeuix')) {
            return 'vendor-primevue'
          }
          if (id.includes('node_modules/vue/') || id.includes('node_modules/@vue/') || id.includes('node_modules/vue-router')) {
            return 'vendor-core'
          }
        }
      }
    }
  },
})
