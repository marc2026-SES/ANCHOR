import { readFileSync } from 'node:fs'
import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'

// Live mode (?live) reads the Shore Monitor API (server/shore_monitor.py) through /api.
// ANCHOR_MONITOR: where the monitor API listens (default: same machine, port 8795, plain http on loopback).
// A monitor on another machine listens on https with a token:
//   ANCHOR_MONITOR=https://<host>:8795 ANCHOR_MONITOR_CA=<team ca.crt> ANCHOR_MONITOR_TOKEN_FILE=<file>
const target = process.env.ANCHOR_MONITOR || 'http://127.0.0.1:8795'
const caFile = process.env.ANCHOR_MONITOR_CA
const tokenFile = process.env.ANCHOR_MONITOR_TOKEN_FILE
const token = tokenFile ? readFileSync(tokenFile, 'utf8').trim() : ''
const url = new URL(target)
if (url.protocol === 'https:' && !caFile) throw new Error('ANCHOR_MONITOR is https: set ANCHOR_MONITOR_CA to the team ca.crt')
if (token && url.protocol !== 'https:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
  throw new Error('a monitor token must not travel over plain http: use https for a remote monitor')
}

const proxy: Record<string, ProxyOptions> = {
  '/api': {
    // https: verified against the team's CA, never switched off
    target: caFile
      ? { protocol: url.protocol, host: url.hostname, port: Number(url.port || 443), ca: readFileSync(caFile) }
      : target,
    secure: true,
    changeOrigin: true,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    // Read only. The dev server listens on the LAN (host: true) and every proxied request reaches the
    // monitor from 127.0.0.1, so POST /api/runs (start / close a run) would otherwise be open to the LAN.
    // Start and close runs with curl on the monitor machine instead.
    bypass: req => (req.method === 'GET' || req.method === 'HEAD' ? undefined : false),
  },
}

// Own port so it does not collide with other Vite apps on 5173.
// strictPort: fail with an error instead of silently moving to another port.
export default defineConfig({
  plugins: [react()],
  server: { port: 5180, strictPort: true, host: true, proxy },
  preview: { port: 5181, strictPort: true, proxy },
})
