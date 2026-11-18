// Data formats of the ANCHOR core (team repo src/anchor, envelope schemaVersion 2).
// The Shore Monitor AE reads the event journals (cse-edge/anchor/events, cse-in/anchor/events)
// and keeps, for every event, the CSE creation time of each copy: coastAt (edge) and shoreAt (shore).
// All times in this app are epoch milliseconds.

export type Device = 'bridge' | 'engine' | 'cargo'
export type Sensor = 'attitude' | 'position' | 'vibration' | 'gas' | 'climate' | 'hatch' | 'commandResults'
export type Kind = 'measurement' | 'incident' | 'commandResult' | 'incidentAck'
export type Priority = 'critical' | 'normal'
/** A = ship ↔ coast, B = coast ↔ shore. They are independent and never need to be up together. */
export type LinkId = 'A' | 'B'

/** One event of an anchor/events journal (src/anchor/events.py). generatedAt is seconds in the CIN, ms here. */
export interface AnchorEvent {
  /** <vesselId>:<deviceId>:<bootId>:<seq> */
  eventId: string
  vesselId: string
  /** bridge | engine | cargo for hardware; ship-core, edge-operator … for incidents and acknowledgements */
  deviceId: string
  /** random per ESP32 boot; seq restarts at 0 with every new bootId */
  bootId: string
  /** one counter per device, shared by all its sensors; null for incidents */
  seq: number | null
  generatedAt: number
  kind: Kind
  sensor?: Sensor
  priority: Priority
  quality?: {
    identity?: 'device-boot-sequence' | 'legacy-resource'
    time?: 'device' | 'cse'
    droppedBefore?: number
    validFix?: boolean
    sensorOk?: boolean
    warmed?: boolean
  }
  payload: Record<string, unknown>
}

/** What the shore keeps per event. null = that copy does not exist (yet). */
export interface Delivered extends AnchorEvent {
  /** CSE creation time of the copy in the coastal journal (custody) */
  coastAt: number | null
  /** CSE creation time of the copy in the shore journal (final) */
  shoreAt: number | null
}

/** One change of a link (core status: links[peer].reachable, observedAt). */
/** source: who saw it. 'inferred' = read from a gap in the arrivals (the core could not report), see infer_outages */
export interface LinkChange { t: number; link: LinkId; up: boolean; source?: string }

/** what a link pill shows; unknown = live mode and no fresh report from the core that uses the link */
export type LinkState = 'up' | 'down' | 'unknown'

/** A shore command (src/anchor/commands.py) and when each step was observed. */
export interface Command {
  commandId: string
  target: string
  intervalMs: number
  durationMs: number
  issuedAt: number
  expiresAt: number
  cachedAtCoast: number | null
  deliveredToShip: number | null
  appliedAt: number | null
}

export type FeedKind = 'link' | 'incident' | 'command' | 'delivery' | 'security'

export interface FeedItem {
  /** when it happened (ship, coast or shore time) */
  t: number
  /** when the shore learned about it; defaults to t */
  seen?: number
  kind: FeedKind
  text: string
}

/** what only the live monitor knows: link state reported by the cores, counts the shore cannot see itself */
export interface LiveInfo {
  links: Record<LinkId, { known: boolean; up: boolean | null; observedAt: number | null; source: string | null }>
  backlog: { generated?: number; heldOnShip?: number; waitingAtCoast?: number; atCoast?: number } | null
  sources: { shore: boolean | null; coast: boolean | null; subscription: boolean | null }
}

export interface Run {
  runId: string
  mode: 'anchor' | 'baseline'
  start: number
  end: number
  /** every event the ship generated (mock: ground truth), live: every event that reached the coast or the shore */
  events: Delivered[]
  links: LinkChange[]
  commands: Command[]
  feed: FeedItem[]
  /** set only for a run read from the Shore Monitor AE while it is still open */
  live?: LiveInfo | null
  /** AI results read from the ship CSE (live), or demo samples */
  ai?: AiInfo | null
  fleet?: FleetInfo | null
}

// ── AI results (<root>/anchorAI/… on the shore CSE copy, else the ship CSE, read by the Shore Monitor;
//    spec: "AI → 대시보드 연동 스펙") ──
export type AiState = 'ok' | 'stale' | 'no_ai' | 'forbidden' | 'ship_unreachable' | 'error' | 'starting'
export type AiLevel = 'normal' | 'warn' | 'critical'
export interface AiStatus {
  eventId: string; state?: string; profile?: string; ts?: string; windows?: number
  levels?: Record<string, AiLevel>; sirenOn?: boolean; seenAt?: number | null
}
export interface AiAnomaly {
  eventId: string; subsystem: string; level: AiLevel; score: number
  topFeatures?: { feature: string; z: number }[]; windowStart?: string; windowEnd?: string; srcContainers?: string[]; at: number
}
export interface AiPrognosis {
  eventId: string; type?: string; channel: string; threshold?: number; value?: number; slopePerMin?: number; etaS: number; ts?: string; at: number
}
export interface AiLinkForecast { eventId: string; type: string; mode?: string; routeMode?: string; etaS?: number; at: number }
export interface AiSummary {
  eventId: string; windowStart?: string; windowEnd?: string
  stats?: Record<string, { mean: number; min: number; max: number; n: number }>
}
// Coastal MEC fleet AI (<edge root>/fleetAI/…): field names are the fleet AI's own, shown generically.
export type FleetState = 'ok' | 'stale' | 'none' | 'forbidden' | 'error' | 'starting'
export interface FleetStatus { eventId?: string; seenAt?: number | null; [key: string]: unknown }
export interface FleetConflict { at: number; eventId?: string; [key: string]: unknown }
export interface FleetInfo {
  state: FleetState; error: string | null; checkedAt: number | null
  status: FleetStatus | null; conflicts: FleetConflict[]
}

export interface AiInfo {
  state: AiState; error: string | null; checkedAt: number | null
  source?: 'shore' | 'ship' | null  // where the last read came from
  status: AiStatus | null; summary: AiSummary | null
  anomaly: AiAnomaly[]; prognosis: AiPrognosis[]; linkForecast: AiLinkForecast[]
}
