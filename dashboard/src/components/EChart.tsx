import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

/** Thin wrapper: one ECharts instance per div, resized with its container. */
export function EChart({ option, height }: { option: echarts.EChartsCoreOption; height: number }) {
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!el.current) return
    chart.current = echarts.init(el.current, undefined, { renderer: 'canvas' })
    const ro = new ResizeObserver(() => chart.current?.resize())
    ro.observe(el.current)
    return () => { ro.disconnect(); chart.current?.dispose(); chart.current = null }
  }, [])

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true, lazyUpdate: true })
  }, [option])

  return <div ref={el} style={{ width: '100%', height }} />
}
