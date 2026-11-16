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
export interface LinkChange { t: number; link: LinkId; up: boolean }

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

export interface Run {
  runId: string
  mode: 'anchor' | 'baseline'
  start: number
  end: number
  /** every event the ship generated (mock: ground truth; live: the ship journal) */
  events: Delivered[]
  links: LinkChange[]
  commands: Command[]
  feed: FeedItem[]
}
