import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the UI runs on Vite (5173) and proxies the API to FastAPI (8470).
// `npm run build` writes web/dist, which FastAPI serves directly.
export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on the LAN too, so phones and other computers can use the dev server.
    host: true,
    proxy: { '/api': 'http://127.0.0.1:8470' },
  },
})
