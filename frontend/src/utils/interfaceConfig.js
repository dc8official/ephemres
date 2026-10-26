// Rows for the interface editor (Traffic → Interfaces and Endpoint Detail) and the payload for
// PUT /bandwidth/endpoints/{id}/interfaces. Both editors replace the whole alias map, so every
// field must survive a round trip: an unset link speed stays unset and the role is kept.

export const SPEED_PRESETS = [10, 100, 1000, 10000, 40000, 100000]

export function editRow(index, name, speedMbps, role) {
  const speed = Number(speedMbps) > 0 ? Number(speedMbps) : null
  return {
    index,
    name,
    speed_mbps: speed,
    preset: speed === null ? 'unset' : SPEED_PRESETS.includes(speed) ? speed : 'custom',
    role: role === 'wan' ? 'wan' : 'lan',
  }
}

// Stored aliases are either a legacy name string or { name, speed_mbps, role }.
export function rowsFromAliases(aliases) {
  return Object.entries(aliases || {}).map(([idx, val]) => {
    if (typeof val === 'string') return editRow(Number(idx) || idx, val, null, 'lan')
    const entry = val && typeof val === 'object' ? val : {}
    return editRow(Number(idx) || idx, entry.name || `if${idx}`, entry.speed_mbps, entry.role)
  })
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
    const entry = { name: String(row.name || '').trim() || `if${idx}`, role: row.role === 'wan' ? 'wan' : 'lan' }
    if (row.preset !== 'unset' && Number(row.speed_mbps) > 0) entry.speed_mbps = Number(row.speed_mbps)
    payload[idx] = entry
  }
  return payload
}
