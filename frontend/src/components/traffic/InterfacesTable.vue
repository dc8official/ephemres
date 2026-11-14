<template>
  <!-- The Interfaces table (build plan 7.5 step 6, review I1-I3): one device's interfaces, or
       every router's in scope (with Site and Device columns). "Now" is the last five complete
       minutes; the utilisation column is the window's average / P95 / busiest minute and says
       so. Status comes from SNMP where the router is polled. Sortable by any column. -->
  <div class="table-responsive">
    <table class="dense-table interactive-table" :aria-label="multiDevice ? 'Interfaces of the routers in scope' : 'Interfaces of the device'">
      <thead>
        <tr>
          <th scope="col" v-for="col in columns" :key="col.key" :class="col.cls" :style="col.style" :aria-sort="ariaSort(col)">
            <button v-if="col.sort" type="button" class="th-sort" @click="sortBy(col)">
              {{ col.label }}<span class="sort-mark" aria-hidden="true">{{ sortMark(col) }}</span>
            </button>
            <span v-else>{{ col.label }}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in visibleRows"
          :key="`${item.exporter_id}-${item.interface_idx}`"
          :class="{
            'row-selected': !multiDevice && selectedIdx === item.interface_idx,
            'row-shut': item.status === 'shut_down',
          }"
          class="cursor-pointer"
          tabindex="0"
          @click="$emit('select', item)"
          @keydown.enter="$emit('select', item)"
        >
          <template v-if="multiDevice">
            <td class="text-muted">{{ item.site || '—' }}</td>
            <td class="font-mono text-xs nowrap">{{ item.exporter_hostname }}</td>
          </template>
          <td v-else class="font-mono tnum">#{{ item.interface_idx }}</td>
          <td class="name-cell">
            <div class="name-cell-inner">
              <span class="font-bold">{{ item.name }}</span>
              <span v-if="multiDevice" class="text-muted text-xs tnum">#{{ item.interface_idx }}</span>
              <span v-if="item.role === 'wan'" class="badge-wan" title="WAN link: counted in edge totals">WAN</span>
              <button
                v-if="isAdmin"
                type="button"
                class="btn-row-edit"
                :aria-label="`Configure ${item.name} on ${item.exporter_hostname || 'this device'}`"
                title="Configure this interface"
                @click.stop="$emit('edit', item)"
              >✎</button>
            </div>
          </td>
          <td class="text-right tnum text-muted nowrap">
            <span v-if="item.speed_configured !== false">{{ formatSpeed(item.speed_mbps) }}</span>
            <span v-else class="speed-unset" title="Set the link speed to see utilization">Not set</span>
          </td>
          <td class="text-right tnum ingress-text nowrap">{{ formatBps(item.now_in_bps) }}</td>
          <td class="text-right tnum egress-text nowrap">{{ formatBps(item.now_out_bps) }}</td>
          <td>
            <!-- No speed: the window's average rates instead of a bar against a guessed speed. -->
            <span v-if="item.utilization_percentage == null" class="text-muted text-xs tnum">
              avg {{ formatBps(item.in_bps) }} in / {{ formatBps(item.out_bps) }} out
            </span>
            <div v-else class="util-cell">
              <div class="util-bar-bg">
                <div
                  class="util-bar-fill"
                  :class="utilClass(item.utilization_percentage)"
                  :style="{ width: `${Math.min(100, item.utilization_percentage)}%` }"
                ></div>
                <span
                  v-if="item.p95_utilization_percentage != null"
                  class="util-marker util-marker-p95"
                  :style="{ left: `${Math.min(100, item.p95_utilization_percentage)}%` }"
                  :title="`95th percentile: ${item.p95_utilization_percentage.toFixed(1)}%`"
                ></span>
                <span
                  v-if="item.peak_utilization_percentage != null"
                  class="util-marker util-marker-peak"
                  :style="{ left: `${Math.min(100, item.peak_utilization_percentage)}%` }"
                  :title="`Busiest minute: ${item.peak_utilization_percentage.toFixed(1)}%`"
                ></span>
              </div>
              <span class="util-text tnum" :class="utilClass(item.p95_utilization_percentage ?? item.utilization_percentage)">
                {{ item.utilization_percentage.toFixed(1) }}<span class="util-sub">
                  / {{ (item.p95_utilization_percentage ?? 0).toFixed(1) }} / {{ (item.peak_utilization_percentage ?? 0).toFixed(1) }}%</span>
              </span>
            </div>
          </td>
          <td class="text-xs">
            <span
              :class="item.traffic_source === 'snmp' ? 'badge-wan badge-snmp' : 'text-muted'"
              :title="item.traffic_source === 'snmp' ? 'From the device\'s SNMP counters (exact)' : 'From flow records (sampled)'"
            >{{ item.traffic_source === 'snmp' ? 'SNMP' : 'Flows' }}</span>
          </td>
          <td>
            <span class="if-status" :class="statusInfo(item).cls" :title="statusInfo(item).title">{{ statusInfo(item).text }}</span>
            <span v-if="downText(item)" class="if-down text-xs" :title="downTitle(item)">{{ downText(item) }}</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length" class="text-center text-muted">{{ emptyText }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="hiddenCount > 0 || showAll" class="if-more">
      <span class="text-xs text-muted">{{ showAll ? `All ${rows.length} interfaces` : `WAN links and the ${MAX_OTHERS} busiest other interfaces` }}</span>
      <button type="button" class="btn-secondary btn-small" @click="showAll = !showAll">
        {{ showAll ? 'Show fewer' : `Show all ${rows.length}` }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { formatBps } from '../../utils/format.js'

// Across devices: every WAN link, then this many of the busiest others (review I1).
const MAX_OTHERS = 20

const props = defineProps({
  rows: { type: Array, default: () => [] },
  multiDevice: { type: Boolean, default: false },
  isAdmin: { type: Boolean, default: false },
  selectedIdx: { type: Number, default: null },
  windowLabel: { type: String, default: '' },
  // Server timezone name: down times are shown in it, like the rest of the page.
  timezone: { type: String, default: '' },
  emptyText: { type: String, default: 'No interfaces in this window.' },
})
defineEmits(['select', 'edit'])

const sortKey = ref(null)
const sortDir = ref(1)
const showAll = ref(false)

const STATUS_RANK = { down: 0, not_answering: 1, unknown: 2, up: 3, traffic_seen: 4, idle: 5, shut_down: 6 }

const columns = computed(() => {
  const cols = []
  if (props.multiDevice) {
    cols.push({ key: 'site', label: 'Site', sort: (r) => (r.site || '￿').toLowerCase() })
    cols.push({ key: 'device', label: 'Device', sort: (r) => (r.exporter_hostname || '').toLowerCase() })
  } else {
    cols.push({ key: 'idx', label: 'Index', style: 'width: 70px;', sort: (r) => r.interface_idx })
  }
  cols.push({ key: 'name', label: 'Interface', sort: (r) => (r.name || '').toLowerCase() })
  cols.push({ key: 'speed', label: 'Speed', cls: 'text-right', sort: (r) => (r.speed_configured === false ? -1 : r.speed_mbps) })
  cols.push({ key: 'now_in', label: 'Now in', cls: 'text-right', sort: (r) => r.now_in_bps })
  cols.push({ key: 'now_out', label: 'Now out', cls: 'text-right', sort: (r) => r.now_out_bps })
  cols.push({
    key: 'util',
    label: `Last ${props.windowLabel}: avg / P95 / peak`,
    style: 'width: 290px;',
    sort: (r) => (r.utilization_percentage ?? -1),
  })
  cols.push({ key: 'source', label: 'Source', sort: (r) => r.traffic_source })
  cols.push({ key: 'status', label: 'Status', sort: (r) => STATUS_RANK[r.status] ?? 9 })
  return cols
})

function sortBy(col) {
  if (sortKey.value === col.key) {
    // Ascending, descending, then back to the default order.
    if (sortDir.value === 1) sortDir.value = -1
    else sortKey.value = null
  } else {
    sortKey.value = col.key
    sortDir.value = 1
  }
}
const ariaSort = (col) => (sortKey.value === col.key ? (sortDir.value === 1 ? 'ascending' : 'descending') : col.sort ? 'none' : undefined)
const sortMark = (col) => (sortKey.value === col.key ? (sortDir.value === 1 ? ' ▲' : ' ▼') : '')

// The API's order across devices: WAN links (busiest first), the busiest others, shut-down last.
const capped = computed(() => {
  if (!props.multiDevice || showAll.value) return props.rows
  let others = 0
  return props.rows.filter((r) => r.role === 'wan' || ++others <= MAX_OTHERS)
})
const hiddenCount = computed(() => props.rows.length - capped.value.length)

const visibleRows = computed(() => {
  const col = columns.value.find((c) => c.key === sortKey.value)
  if (!col) return capped.value
  return [...capped.value].sort((a, b) => {
    const x = col.sort(a)
    const y = col.sort(b)
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir.value
  })
})

function formatSpeed(mbps) {
  if (!mbps) return 'Not set'
  if (mbps >= 1000 && mbps % 1000 === 0) return `${mbps / 1000} Gb/s`
  return `${mbps} Mb/s`
}

function utilClass(u) {
  if (u >= 80) return 'util-danger'
  if (u >= 60) return 'util-warning'
  return 'util-healthy'
}

function statusInfo(item) {
  switch (item.status) {
    case 'up': return { text: 'Up', cls: 'st-up', title: 'SNMP: the port is up' }
    case 'down': return { text: 'Down', cls: 'st-down', title: 'SNMP: the port is down' }
    case 'shut_down': return { text: 'Shut down', cls: 'st-muted', title: 'Disabled on the router (admin status down)' }
    case 'not_answering': return { text: 'SNMP not answering', cls: 'st-warn', title: "The router doesn't reply to SNMP: the port's state is unknown" }
    case 'unknown': return { text: 'Unknown', cls: 'st-muted', title: 'SNMP reports another state for the port' }
    case 'traffic_seen': return { text: 'Traffic seen', cls: 'st-up', title: 'No SNMP for this port: flows showed traffic in the window' }
    default: return { text: 'Idle', cls: 'st-muted', title: 'No SNMP for this port, and no flows in the window' }
  }
}

function fmt(iso, opts) {
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: props.timezone || undefined }).format(new Date(iso))
  } catch {
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso))
  }
}
const when = (iso) => fmt(iso, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })

function duration(seconds) {
  const m = Math.max(1, Math.round(seconds / 60))
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`
}

// Down time within the window (review I2): "down 40 min, Thu 14:00".
function downText(item) {
  const spans = item.down_spans || []
  if (!spans.length) return ''
  if (spans.length === 1) {
    const s = spans[0]
    return s.end ? `down ${duration(item.down_seconds)}, ${when(s.start)}` : `down since ${when(s.start)}`
  }
  return `down ${spans.length} times, ${duration(item.down_seconds)} in all`
}
function downTitle(item) {
  return (item.down_spans || [])
    .map((s) => `Down ${when(s.start)} to ${s.end ? when(s.end) : 'now'}`)
    .join('\n')
}
</script>

<style scoped>
/* Moved from BandwidthView (its styles are scoped, so they don't reach this component). */
.speed-unset {
  color: var(--status-text-warn);
  font-weight: 600;
}
.ingress-text {
  color: #10b981;
}
.egress-text {
  color: #0ea5e9;
}
.interactive-table tr.cursor-pointer {
  cursor: pointer;
  transition: background-color 0.15s ease;
}
.interactive-table tr.row-selected {
  background: rgba(99, 102, 241, 0.15) !important;
  border-left: 3px solid #6366f1;
}
.util-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.util-bar-bg {
  position: relative;
  flex: 1;
  height: 6px;
  background: var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 3px;
}
.util-marker {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 12px;
  transform: translateX(-1px);
  background: var(--text-secondary);
}
.util-marker-peak {
  top: -6px;
  width: 0;
  height: 0;
  background: none;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid var(--status-text-down);
  transform: translateX(-4px);
}
.util-sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 500;
}
.util-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}
.util-bar-fill.util-healthy {
  background: #10b981;
}
.util-bar-fill.util-warning {
  background: #f59e0b;
}
.util-bar-fill.util-danger {
  background: #ef4444;
}
.util-text {
  font-size: 0.75rem;
  font-weight: 700;
  min-width: 45px;
  text-align: right;
  white-space: nowrap;
}
.util-text.util-healthy {
  color: #10b981;
}
.util-text.util-warning {
  color: #f59e0b;
}
.util-text.util-danger {
  color: #ef4444;
}
.badge-wan.badge-snmp {
  color: #0ea5e9;
}
.badge-wan {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.02em;
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}
.name-cell-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.btn-row-edit {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 2px 6px;
  border-radius: 4px;
  transition: all 0.15s ease;
}
.btn-row-edit:hover {
  background: var(--bg-surface-hover);
  color: var(--accent-primary, #2563eb);
}

/* Hostnames and rates read badly broken over two lines. */
td.nowrap {
  white-space: nowrap;
}
.th-sort {
  all: unset;
  cursor: pointer;
  font: inherit;
  color: inherit;
  white-space: nowrap;
}
.th-sort:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.sort-mark {
  font-size: 0.7em;
}
.row-shut td {
  opacity: 0.55;
}
.if-status {
  font-size: var(--text-xs);
  font-weight: 600;
}
.if-down {
  display: block;
  color: var(--status-text-down);
}
.st-up {
  color: var(--status-text-up);
}
.st-down {
  color: var(--status-text-down);
}
.st-warn {
  color: var(--status-text-warn);
}
.st-muted {
  color: var(--text-muted);
}
.if-more {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.75rem;
  padding: 0.5rem 0.75rem;
}
</style>
