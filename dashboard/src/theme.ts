import type { LinkId } from './types'

export const C = {
  ink: '#dfeaf2',
  muted: '#8fa6b5',
  line: 'rgba(126, 180, 214, 0.16)',
  panel: '#0e2233',
  page: '#081722',
  blue: '#3f9ad0',      // delivered live
  red: '#d9604f',       // accent / link B
  recovered: '#b273c8', // data that arrived late (kept on the ship or at the coast, then forwarded)
  up: '#2fae76',
  down: '#d9604f',
}

export const LINK: Record<LinkId, { name: string; short: string; color: string; band: string }> = {
  A: { name: 'Link A · ship ↔ coast', short: 'A ship↔coast', color: '#bd8c37', band: 'rgba(189,140,55,0.14)' },
  B: { name: 'Link B · coast ↔ shore', short: 'B coast↔shore', color: '#d9604f', band: 'rgba(217,96,79,0.12)' },
}

export const FONT = "'Pretendard Variable', Pretendard, -apple-system, 'Segoe UI', sans-serif"

export const clock = (ms: number) => new Date(ms).toISOString().slice(11, 19)
