import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Own port so it does not collide with other Vite apps on 5173.
// strictPort: fail with an error instead of silently moving to another port.
export default defineConfig({
  plugins: [react()],
  server: { port: 5180, strictPort: true, host: true },
  preview: { port: 5181, strictPort: true },
})
