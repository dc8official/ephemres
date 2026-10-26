// One classification of an endpoint's current state for KPI tiles, filters, cards and reports.
//
//   'UP' | 'UNSTABLE' | 'DOWN' -> actively monitored with a known state
//   'NO_STATUS'                -> passive flow exporter, paused, monitoring disabled, or no probe
//                                 result yet; never counted as UP and left out of SLA averages

const NOT_MONITORED_STATES = new Set(['PASSIVE', 'PAUSED', 'MAINTENANCE'])

export function isMonitored(ep) {
  if (!ep || ep.monitoring_enabled === false) return false
  const state = String(ep.current_detailed_state || ep.current_operational_state || '').toUpperCase()
  return !NOT_MONITORED_STATES.has(state)
}

export function endpointStateBucket(ep) {
  if (!isMonitored(ep)) return 'NO_STATUS'
  const op = String(ep.current_operational_state || '').toUpperCase()
  const det = String(ep.current_detailed_state || op).toUpperCase()
  if (det.includes('UNSTABLE')) return 'UNSTABLE'
  if (det === 'UP') return 'UP'
  if (det === 'DOWN' || op === 'DOWN') return 'DOWN'
  if (op === 'UP') return 'UP'
  return 'NO_STATUS'
}

// 24h uptime as a number, or null when it does not apply (not monitored) or is missing.
// 0 % stays 0 %: it must never be read as "no data".
export function uptimeValue(ep, field = 'uptime_percentage_24h') {
  if (!isMonitored(ep)) return null
  const raw = ep?.[field]
  if (raw === null || raw === undefined || raw === '') return null
  const value = Number.parseFloat(raw)
  return Number.isNaN(value) ? null : value
}

// Mean uptime of the endpoints that have one; null when none do.
export function meanUptime(endpoints, field = 'uptime_percentage_24h') {
  const values = endpoints.map((ep) => uptimeValue(ep, field)).filter((v) => v !== null)
  if (values.length === 0) return null
  return values.reduce((sum, v) => sum + v, 0) / values.length
}
