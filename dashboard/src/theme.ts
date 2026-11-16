import type { LinkId } from './types'

export const C = {
  ink: '#1d1d1f',
  muted: '#6b6b70',
  line: '#e3e3e6',
  panel: '#ffffff',
  page: '#f4f5f7',
  blue: '#005581',      // deck primary
  red: '#c0262d',       // deck accent
  recovered: '#7a5fa8', // data that arrived late (kept on the ship or at the coast, then forwarded)
  up: '#1f8a5b',
  down: '#b23a3a',
}

export const LINK: Record<LinkId, { name: string; short: string; color: string; band: string }> = {
  A: { name: 'Link A · ship ↔ coast', short: 'A ship↔coast', color: '#c98612', band: 'rgba(201,134,18,0.12)' },
  B: { name: 'Link B · coast ↔ shore', short: 'B coast↔shore', color: '#b23a3a', band: 'rgba(178,58,58,0.10)' },
}

export const FONT = "'Pretendard Variable', Pretendard, -apple-system, 'Segoe UI', sans-serif"

export const clock = (ms: number) => new Date(ms).toISOString().slice(11, 19)
