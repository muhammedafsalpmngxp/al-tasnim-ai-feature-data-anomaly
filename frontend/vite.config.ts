import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the backend, so the browser only ever talks to one origin
// during development. That keeps CORS out of the picture entirely while developing, and means
// VITE_API_BASE only has to be set for a deployment where the two are served separately.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/api': {
        target: 'http://localhost:8100',
        changeOrigin: true,
      },
    },
  },
})
