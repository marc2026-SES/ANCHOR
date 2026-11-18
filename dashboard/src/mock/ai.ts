// Demo AI results in the shapes of the AI spec ("AI → 대시보드 연동 스펙"), placed inside the demo run.
// Live mode uses what the Shore Monitor reads from the ship CSE instead.
import type { AiInfo, Run } from '../types'

export function demoAi(run: Run): AiInfo {
  const t = (f: number) => run.start + (run.end - run.start) * f
  return {
    state: 'ok', error: null, checkedAt: run.end, source: 'shore',
    status: {
      eventId: 'sts-demo', state: 'online', profile: 'ship', windows: 1234, sirenOn: false, seenAt: null,
      levels: { ENGINE: 'warn', BRIDGE: 'normal', CARGO: 'normal' },
    },
    summary: {
      eventId: 'sum-demo',
      stats: {
        'ENGINE/vibration.vib_rms_g': { mean: 0.009, min: 0.007, max: 0.031, n: 57 },
        'ENGINE/gas.mq2_v': { mean: 0.46, min: 0.44, max: 0.49, n: 29 },
        'BRIDGE/attitude.roll': { mean: -2.4, min: -3.1, max: 1.2, n: 60 },
        'CARGO/climate.temp_c': { mean: 30.8, min: 30.6, max: 30.9, n: 12 },
      },
    },
    anomaly: [
      { eventId: 'anm-demo-2', subsystem: 'ENGINE', level: 'critical', score: 0.91, at: t(0.62),
        topFeatures: [{ feature: 'vibration.vib_rms_g.std', z: 5.2 }, { feature: 'gas.mq2_v.slope', z: 3.1 },
          { feature: 'vibration.vib_peak_g.max', z: 2.4 }], srcContainers: ['vibration', 'gas'] },
      { eventId: 'anm-demo-1', subsystem: 'ENGINE', level: 'warn', score: 0.41, at: t(0.48),
        topFeatures: [{ feature: 'gas.mq2_v.slope', z: 2.6 }, { feature: 'vibration.vib_rms_g.mean', z: 1.8 }],
        srcContainers: ['vibration', 'gas'] },
    ],
    prognosis: [
      { eventId: 'prg-demo', type: 'threshold_predicted', channel: 'ENGINE/gas.mq2_v', threshold: 1.0, value: 0.46,
        slopePerMin: 0.08, etaS: 420, at: t(0.55) },
    ],
    linkForecast: [
      { eventId: 'lnk-demo', type: 'loss_predicted', mode: 'lte', routeMode: 'coastal', etaS: 600, at: t(0.3) },
    ],
  }
}
