import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Saat development (npm run dev), request /api diteruskan ke backend FastAPI di port 8000
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
})
