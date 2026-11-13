// Rows for the interface editor (Traffic → Interfaces and Endpoint Detail) and the payload for
// PUT /bandwidth/endpoints/{id}/interfaces. Both editors replace the whole alias map, so every
// field must survive a round trip: an unset link speed stays unset and the role is kept.

export const SPEED_PRESETS = [10, 100, 1000, 10000, 40000, 100000]

// A WAN link's traffic alert rule (v3.3.6): follow the default in Settings → Alerts, its own
// threshold and minutes, or off. Kept on every row so a save never drops it.
export function alertRule(alert) {
  const a = alert && typeof alert === 'object' ? alert : {}
  const mode = ['default', 'custom', 'off'].includes(a.mode) ? a.mode : 'default'
  return { mode, threshold_pct: Number(a.threshold_pct) || 90, minutes: Number(a.minutes) || 15 }
}

export function editRow(index, name, speedMbps, role, alert = null) {
  const speed = Number(speedMbps) > 0 ? Number(speedMbps) : null
  return {
    index,
    name,
    speed_mbps: speed,
    preset: speed === null ? 'unset' : SPEED_PRESETS.includes(speed) ? speed : 'custom',
    role: role === 'wan' ? 'wan' : 'lan',
    alert: alertRule(alert),
  }
}

// Stored aliases are either a legacy name string or { name, speed_mbps, role, alert }.
export function rowsFromAliases(aliases) {
  return Object.entries(aliases || {}).map(([idx, val]) => {
    if (typeof val === 'string') return editRow(Number(idx) || idx, val, null, 'lan')
    const entry = val && typeof val === 'object' ? val : {}
    return editRow(Number(idx) || idx, entry.name || `if${idx}`, entry.speed_mbps, entry.role, entry.alert)
  })
}

// A row from the interface telemetry API (v3.3.6). Only values an admin typed go into the
// fields; the device's own name and speed from SNMP ride along as snmpName / snmpSpeed for the
// editors to show as greyed defaults. Filling the fields with them would store them as manual
// values on save, and a manual value hides later SNMP changes.
export function rowFromTelemetry(item) {
  const manualName = (item.name_source ?? 'manual') === 'manual' ? item.name : ''
  const manualSpeed = (item.speed_source ?? (item.speed_configured === false ? 'default' : 'manual')) === 'manual'
    ? item.speed_mbps : null
  const row = editRow(Number(item.interface_idx) || 1, manualName || '', manualSpeed, item.role, item.alert)
  row.snmpName = item.snmp_name || null
  row.snmpSpeed = item.snmp_speed_mbps || null
  return row
}

export function formatMbps(mbps) {
  if (!mbps) return ''
  return mbps >= 1000 ? `${mbps / 1000} Gbps` : `${mbps} Mbps`
}

export function applyPreset(row) {
  if (row.preset === 'unset') row.speed_mbps = null
  else if (row.preset !== 'custom') row.speed_mbps = Number(row.preset)
  else if (!(Number(row.speed_mbps) > 0)) row.speed_mbps = 1000
}

export function aliasesPayload(rows) {
  const payload = {}
  for (const row of rows) {
    const idx = String(row.index).trim()
    if (!idx) continue
    // No name typed: none is stored, so the device's SNMP name (or "Interface N") shows.
    const entry = { role: row.role === 'wan' ? 'wan' : 'lan' }
    const name = String(row.name || '').trim()
    if (name) entry.name = name
    if (row.preset !== 'unset' && Number(row.speed_mbps) > 0) entry.speed_mbps = Number(row.speed_mbps)
    // Only a non-default rule is stored; "default" is what a missing rule means.
    const alert = alertRule(row.alert)
    if (alert.mode === 'off') entry.alert = { mode: 'off' }
    else if (alert.mode === 'custom') entry.alert = { mode: 'custom', threshold_pct: alert.threshold_pct, minutes: alert.minutes }
    payload[idx] = entry
  }
  return payload
}
