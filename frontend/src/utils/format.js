// Shared number formatters for traffic and telemetry values.
//
// These are the BandwidthView implementations. EndpointDetailView still carries its own copies,
// which differ slightly (kbps without a decimal, no TB step); v3.4 Phase 1 moves it onto these.

export function formatBps(bps) {
  if (!bps || bps <= 0) return '0 bps'
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gbps`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbps`
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)} kbps`
  return `${Math.round(bps)} bps`
}

export function formatPps(pps) {
  if (!pps || pps <= 0) return '0 pps'
  if (pps >= 1e6) return `${(pps / 1e6).toFixed(2)} Mpps`
  if (pps >= 1e3) return `${(pps / 1e3).toFixed(1)} kpps`
  return `${Math.round(pps)} pps`
}

export function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B'
  if (bytes >= 1e12) return `${(bytes / 1e12).toFixed(2)} TB`
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(2)} MB`
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(1)} KB`
  return `${bytes} B`
}

export function formatNumber(num) {
  if (!num) return '0'
  return new Intl.NumberFormat().format(num)
}

const PORT_LABELS = {
  443: 'HTTPS',
  80: 'HTTP',
  53: 'DNS',
  22: 'SSH',
  123: 'NTP',
  161: 'SNMP',
  2055: 'NetFlow',
  4739: 'IPFIX',
}

export function formatPortLabel(port) {
  const name = PORT_LABELS[Number(port)]
  return name ? `${Number(port)} / ${name}` : `${port}`
}
