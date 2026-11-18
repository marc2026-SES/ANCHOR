// Rolling numbers: when a displayed value changes, tween the numeric part instead of jumping.
// Works on strings like "5,463", "2.3 s", "100%"; anything without a number just swaps.
import { useEffect, useRef, useState } from 'react'

const NUM = /-?[\d,]+(?:\.\d+)?/

function parse(text: string) {
  const m = text.match(NUM)
  if (!m || m.index === undefined) return null
  return {
    value: parseFloat(m[0].replace(/,/g, '')),
    prefix: text.slice(0, m.index),
    suffix: text.slice(m.index + m[0].length),
    decimals: (m[0].split('.')[1] ?? '').length,
    grouped: m[0].includes(','),
  }
}

export function useRolling(text: string, ms = 550): string {
  const [shown, setShown] = useState(text)
  const last = useRef(text)
  const raf = useRef(0)

  useEffect(() => {
    if (text === last.current) return
    const from = parse(last.current)
    const to = parse(text)
    last.current = text
    cancelAnimationFrame(raf.current)
    if (!from || !to || from.value === to.value || !isFinite(from.value) ||
        window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setShown(text)
      return
    }
    const t0 = performance.now()
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / ms)
      const ease = 1 - Math.pow(1 - k, 3)
      const v = from.value + (to.value - from.value) * ease
      const body = to.grouped
        ? Math.round(v).toLocaleString('en-US')
        : v.toFixed(to.decimals)
      setShown(k >= 1 ? text : to.prefix + body + to.suffix)
      if (k < 1) raf.current = requestAnimationFrame(step)
    }
    raf.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf.current)
  }, [text, ms])

  return shown
}

export function RollText({ text }: { text: string }) {
  return <>{useRolling(text)}</>
}
