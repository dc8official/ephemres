// Plain-text summaries of chart data for screen readers (rendered in a visually hidden element
// next to each chart). Canvas charts carry no text of their own.

function formatWhen(ms) {
  return new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// One sentence for a numeric series: average, peak (with time) and latest value.
// `points` are { t: epoch ms, v: number | null }; nulls (gaps) are ignored.
export function summarizeSeries(label, points, format = (v) => String(v)) {
  const valid = points.filter((p) => p.v !== null && p.v !== undefined && !Number.isNaN(p.v))
  if (valid.length === 0) return `${label}: no data.`
  const avg = valid.reduce((sum, p) => sum + p.v, 0) / valid.length
  const peak = valid.reduce((best, p) => (p.v > best.v ? p : best), valid[0])
  const latest = valid[valid.length - 1]
  return `${label}: average ${format(avg)}, peak ${format(peak.v)} at ${formatWhen(peak.t)}, latest ${format(latest.v)}.`
}

// Share of a period spent in each state, largest first: "UP 92%, UP-UNSTABLE 8%".
export function summarizeDurations(segments) {
  const totals = new Map()
  let all = 0
  for (const seg of segments) {
    const ms = Math.max(0, seg.endMs - seg.startMs)
    totals.set(seg.state, (totals.get(seg.state) || 0) + ms)
    all += ms
  }
  if (all === 0) return 'No state data for this period.'
  return [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([state, ms]) => `${state} ${Math.round((ms / all) * 100)}%`)
    .join(', ')
}
