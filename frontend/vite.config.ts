import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev-only proxy: the frontend calls relative `/api/...` paths and Vite forwards them to
// the FastAPI backend, so nothing here needs a CORS setup in development. In production
// the two are typically served from the same origin behind one reverse proxy (see
// README/DEPLOY.md); if they aren't, set VITE_API_BASE_URL at build time instead.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})
