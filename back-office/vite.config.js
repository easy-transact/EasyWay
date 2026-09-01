import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // 5173 (defaut Vite) est deja pris par test-ui/ -- cf. config/settings.py
  // CORS_ALLOWED_ORIGINS qui autorise les deux en DEBUG.
  server: { port: 5174 },
})
