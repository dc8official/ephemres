// One set of thresholds for coloring uptime, utilization and health values.
//
// Tones map to the --status-text-* tokens: 'up' | 'warn' | 'down' | 'unknown'.
// Not yet wired in: DashboardView, ReportsView and EndpointDetailView each use their own
// thresholds today (e.g. utilization turns red at 80 % on Bandwidth, 85 % on Endpoint Detail).
// v3.4 Phase 1 moves them onto these helpers.

export const DEFAULT_SLA_TARGET = 99.9

// Uptime meets the target -> 'up'; within one point below it -> 'warn'; lower -> 'down'.
// null/undefined (not measured, e.g. passive exporters) -> 'unknown', never a red 0 %.
export function uptimeTone(pct, target = DEFAULT_SLA_TARGET) {
  if (pct === null || pct === undefined || Number.isNaN(Number(pct))) return 'unknown'
  const value = Number(pct)
  if (value >= target) return 'up'
  if (value >= target - 1) return 'warn'
  return 'down'
}

// Link utilization: below 60 % -> 'up', below 80 % -> 'warn', otherwise 'down'.
// null (link speed not configured) -> 'unknown'.
export function utilizationTone(pct) {
  if (pct === null || pct === undefined || Number.isNaN(Number(pct))) return 'unknown'
  const value = Number(pct)
  if (value >= 80) return 'down'
  if (value >= 60) return 'warn'
  return 'up'
}

// Probe health score (share of successful probes in a cycle).
export function healthTone(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return 'unknown'
  const value = Number(score)
  if (value >= 95) return 'up'
  if (value >= 70) return 'warn'
  return 'down'
}

export function toneVar(tone) {
  return `var(--status-text-${tone || 'unknown'})`
}
