import type { LinkChange, LinkId } from '../types'
import { LINK } from '../theme'

/** down intervals of each link, for shading charts */
export function downIntervals(links: LinkChange[], end: number): { link: LinkId; from: number; to: number }[] {
  const out: { link: LinkId; from: number; to: number }[] = []
  const open: Partial<Record<LinkId, number>> = {}
  for (const c of links) {
    if (!c.up && open[c.link] === undefined) open[c.link] = c.t
    if (c.up && open[c.link] !== undefined) { out.push({ link: c.link, from: open[c.link]!, to: c.t }); delete open[c.link] }
  }
  for (const link of Object.keys(open) as LinkId[]) out.push({ link, from: open[link]!, to: end })
  return out
}

export function bands(links: LinkChange[], end: number) {
  return downIntervals(links, end).map(d => [
    { xAxis: d.from, itemStyle: { color: LINK[d.link].band } }, { xAxis: d.to },
  ])
}
