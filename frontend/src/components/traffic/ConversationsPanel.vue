<template>
  <section class="conversations-panel table-card">
    <div class="panel-pad">
      <PanelHeader :title="title" :hint="hintText">
        <template #actions>
          <SegmentedControl
            v-model="quick"
            :options="quickOptions"
            aria-label="Conversation filter"
            size="sm"
          />
          <SegmentedControl v-model="sort" :options="sortOptions" aria-label="Sort conversations" size="sm" />
          <input
            v-model="searchText"
            class="form-input search-input"
            placeholder="IP address or port"
            aria-label="Filter by IP address or port"
            @keydown.enter="applySearch"
            @blur="applySearch"
          />
        </template>
      </PanelHeader>

      <div v-if="activeChips.length" class="filter-chips" aria-label="Active filters">
        <span v-for="chip in activeChips" :key="chip.key" class="filter-chip">
          {{ chip.label }} <strong class="font-mono tnum">{{ chip.value }}</strong>
          <button type="button" class="chip-remove" :aria-label="`Remove ${chip.label} filter`" @click="chip.clear()">✕</button>
        </span>
        <button type="button" class="btn-text clear-all" @click="clearAll">Clear filters</button>
      </div>
      <p v-if="searchError" class="search-error" role="alert">{{ searchError }}</p>
    </div>

    <div class="table-responsive">
      <table class="dense-table" :aria-label="title" :aria-busy="loading">
        <thead>
          <tr>
            <th>Source</th>
            <th>Destination</th>
            <th>Site</th>
            <th v-if="showSeen">First / last seen</th>
            <th>Protocol</th>
            <th>Service port</th>
            <th>Profile &amp; avg packet</th>
            <th>Flags &amp; QoS</th>
            <th class="text-right">Volume</th>
            <th class="text-right">Flows</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in items" :key="`${c.src_ip}-${c.dst_ip}-${c.protocol}-${c.dst_port}`">
            <td>
              <button type="button" class="ip-link font-mono tnum" :title="`Show conversations of ${c.src_ip}`" @click="setIp(c.src_ip)">{{ stripMask(c.src_ip) }}</button>
              <span v-if="c.src_hostname" class="text-muted text-xs block">{{ c.src_hostname }}</span>
            </td>
            <td>
              <button type="button" class="ip-link font-mono tnum" :title="`Show conversations of ${c.dst_ip}`" @click="setIp(c.dst_ip)">{{ stripMask(c.dst_ip) }}</button>
              <span v-if="c.dst_hostname" class="text-muted text-xs block">{{ c.dst_hostname }}</span>
            </td>
            <!-- Where it was seen (review C1): the sites of the routers that reported it, the
                 routers themselves on hover. On Security the router and, when it has only one,
                 its WAN link are spelled out (review S1). -->
            <td>
              <span :title="routersTitle(c)">{{ siteText(c) }}</span>
              <span v-if="!showSeen && c.routers?.length" class="sr-only">, {{ routersTitle(c) }}</span>
              <span v-if="showSeen && c.routers?.length" class="text-muted text-xs block">{{ routerLine(c) }}</span>
            </td>
            <td v-if="showSeen" class="nowrap">
              <span class="tnum block" :title="seenTitle">{{ when(c.first_seen) }}</span>
              <span class="tnum text-muted text-xs block">to {{ when(c.last_seen) }}</span>
            </td>
            <td><span class="status-pill status-unknown">{{ c.protocol }}</span></td>
            <td>
              <button
                type="button"
                class="tag port-tag"
                :class="portTone(c.dst_port)"
                :title="`Show conversations on port ${c.dst_port}`"
                @click="setPort(c.dst_port)"
              >{{ formatPortLabel(c.dst_port, c.service_name) }}</button>
            </td>
            <td>
              <span class="tag" :class="profileTone(c.traffic_profile)">{{ c.traffic_profile }}</span>
              <span class="text-xs text-muted tnum font-mono block">{{ c.avg_packet_size }} B/pkt · {{ formatNumber(Math.round(c.packets_per_flow)) }} pkts/flow</span>
            </td>
            <td>
              <div class="tag-group">
                <!-- Flags only where they explain something: every normal TCP row read
                     "FIN SYN PSH ACK" (review C4). -->
                <span
                  v-if="c.protocol === 'TCP' && c.anomaly_hint && c.tcp_flags_labels.length"
                  class="tag tone-neutral font-mono"
                  :title="`TCP flags 0x${(c.tcp_flags || 0).toString(16).padStart(2, '0')}`"
                >{{ c.tcp_flags_labels.join(' ') }}</span>
                <span v-if="c.anomaly_hint === 'SYN_ONLY'" class="tag tone-down" title="SYN without ACK over the whole window: possible scan">SYN only</span>
                <span v-else-if="c.anomaly_hint === 'TCP_RST'" class="tag tone-warn" title="RST without FIN: refused or reset connections">RST</span>
                <button
                  v-if="c.dscp > 0"
                  type="button"
                  class="tag"
                  :class="c.dscp === 46 ? 'tone-up' : 'tone-info'"
                  :title="`Show conversations marked ${c.dscp_label} (DSCP ${c.dscp})`"
                  @click="setDscp(c.dscp, c.dscp_label)"
                >{{ c.dscp_label }}</button>
              </div>
            </td>
            <td class="text-right tnum font-bold">{{ formatBytes(c.total_bytes) }}</td>
            <td class="text-right tnum text-muted">{{ formatNumber(c.flow_count) }}</td>
          </tr>
          <tr v-if="!loading && items.length === 0">
            <td :colspan="showSeen ? 10 : 9" class="text-center text-muted empty-row">
              {{ loadError || (counts.all === 0 && !activeChips.length ? `No conversations in the last ${window}.` : 'No conversations match these filters.') }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager panel-pad">
      <span class="text-muted text-sm tnum">
        <template v-if="total">{{ offset + 1 }}–{{ Math.min(offset + items.length, total) }} of {{ formatNumber(total) }}</template>
        <template v-else>0 of 0</template>
      </span>
      <div class="pager-buttons">
        <button type="button" class="btn-secondary btn-small" :disabled="offset === 0 || loading" @click="go(-1)">← Previous</button>
        <button type="button" class="btn-secondary btn-small" :disabled="offset + pageSize >= total || loading" @click="go(1)">Next →</button>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { getConversations } from '../../services/api.js'
import { formatBytes, formatNumber, formatPortLabel } from '../../utils/format.js'
import PanelHeader from '../ui/PanelHeader.vue'
import SegmentedControl from '../ui/SegmentedControl.vue'

// Conversations filtered, counted and paged on the server (GET /bandwidth/conversations).
// `ip`, `port` and `dscp` are two-way bound so a parent (talker list, app chips, QoS list) can set
// them and the panel can clear them. `quick` presets the triage filter (e.g. 'anomalies').
const props = defineProps({
  window: { type: String, required: true },
  // The Traffic page scope: the shared filter plus an optional device (api.trafficScopeParams).
  scope: { type: [Object, String], default: null },
  endpointId: { type: String, default: null },
  ip: { type: String, default: null },
  port: { type: Number, default: null },
  dscp: { type: Number, default: null },
  dscpLabel: { type: String, default: '' },
  initialQuick: { type: String, default: 'all' },
  initialSort: { type: String, default: 'bytes' },
  title: { type: String, default: 'Conversations' },
  hint: { type: String, default: '' },
  pageSize: { type: Number, default: 25 },
  // Security's table: a first / last seen column, and the router (with its WAN link when it
  // has one) under the site (review S1).
  showSeen: { type: Boolean, default: false },
  // Server timezone name: times are shown in it, like the rest of the page.
  timezone: { type: String, default: '' },
  refreshKey: { type: Number, default: 0 },
})
const emit = defineEmits(['update:ip', 'update:port', 'update:dscp', 'update:dscpLabel'])

const quick = ref(props.initialQuick)
const sort = ref(props.initialSort)
const offset = ref(0)
const items = ref([])
const counts = ref({ all: 0, anomalies: 0, qos: 0, bulk: 0 })
const total = ref(0)
const loading = ref(false)
const loadError = ref('')
const searchText = ref('')
const searchError = ref('')
let requestSeq = 0

const quickOptions = computed(() => [
  { value: 'all', label: `All (${formatNumber(counts.value.all)})` },
  { value: 'anomalies', label: `Anomalies (${formatNumber(counts.value.anomalies)})` },
  { value: 'qos', label: `QoS tagged (${formatNumber(counts.value.qos)})` },
  { value: 'bulk', label: `Bulk (${formatNumber(counts.value.bulk)})`, title: 'Large transfers in full-size packets: seen for at least 30 minutes at 5 Mb/s or more (backups, replication)' },
])
const sortOptions = [
  { value: 'bytes', label: 'Volume' },
  { value: 'flows', label: 'Flows' },
  { value: 'packets', label: 'Packets' },
]

const hintText = computed(() => props.hint || `Last ${props.window} · grouped by source, destination, protocol and service port`)

const activeChips = computed(() => {
  const chips = []
  if (props.ip) chips.push({ key: 'ip', label: 'Address', value: stripMask(props.ip), clear: () => emit('update:ip', null) })
  if (props.port !== null && props.port !== undefined) {
    // The name comes with the rows; until they load the chip shows the number.
    const name = items.value.find((c) => c.dst_port === Number(props.port))?.service_name
    chips.push({ key: 'port', label: 'Port', value: formatPortLabel(props.port, name), clear: () => emit('update:port', null) })
  }
  if (props.dscp !== null && props.dscp !== undefined) {
    chips.push({
      key: 'dscp',
      label: 'QoS',
      value: props.dscpLabel || `DSCP ${props.dscp}`,
      clear: () => { emit('update:dscp', null); emit('update:dscpLabel', '') },
    })
  }
  return chips
})

// One name per site; a router with no site stands for itself.
function siteText(c) {
  const names = [...new Set((c.routers || []).map((r) => r.site || r.hostname || 'Unknown router'))]
  return names.length ? names.join(', ') : '—'
}

function routerName(r) {
  return r.hostname || 'an unregistered router'
}

// "Seen by abj-rtr1 (Abuja) via Glo MPLS, hq-rtr1 (Lagos HQ)". The bytes are counted once.
function routersTitle(c) {
  const parts = (c.routers || []).map((r) =>
    `${routerName(r)}${r.site ? ` (${r.site})` : ''}${r.wan_link ? ` via ${r.wan_link}` : ''}`)
  return parts.length ? `Seen by ${parts.join(', ')}` : ''
}

function routerLine(c) {
  return (c.routers || []).map((r) => `${routerName(r)}${r.wan_link ? ` · ${r.wan_link}` : ''}`).join(', ')
}

// Windows over 2 hours read the hourly tier: within an hour, when it started and stopped is
// not kept.
const seenTitle = computed(() => (props.window === '1h' ? 'To the minute' : 'To the hour: the first hour it was seen in, and the end of the last'))

// A weekday alone is ambiguous once the window covers a week ("Sat 08:06 to Sat 08:05" at 7d
// was last Saturday to today), so 7d and longer add the date.
function when(iso) {
  if (!iso) return '—'
  const withDate = ['7d', '30d', '1y'].includes(props.window)
  const opts = withDate
    ? { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }
    : { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: props.timezone || undefined }).format(new Date(iso))
  } catch {
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso))
  }
}

function stripMask(ip) {
  return String(ip || '').split('/')[0]
}

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const res = await getConversations(props.window, {
      scope: props.scope,
      endpointId: props.endpointId,
      ip: props.ip ? stripMask(props.ip) : null,
      port: props.port,
      dscp: props.dscp,
      quick: quick.value,
      sort: sort.value,
      limit: props.pageSize,
      offset: offset.value,
    })
    if (seq !== requestSeq) return // a newer request superseded this one
    const data = res.data?.data || {}
    items.value = data.items || []
    counts.value = data.counts || { all: 0, anomalies: 0, qos: 0, bulk: 0 }
    total.value = data.total || 0
    loadError.value = ''
  } catch (err) {
    if (seq === requestSeq) {
      console.error('Failed to load conversations:', err)
      // A query that hit the time limit says what to do; don't read as "no conversations".
      items.value = []
      total.value = 0
      loadError.value = err.response?.data?.detail || 'Could not load conversations.'
    }
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function go(direction) {
  offset.value = Math.max(0, offset.value + direction * props.pageSize)
  load()
}

// A specific DSCP class (including CS0) supersedes "any QoS marking"; together they could
// match nothing.
watch(
  () => props.dscp,
  (dscp) => {
    if (dscp !== null && dscp !== undefined && quick.value === 'qos') quick.value = 'all'
  },
  { immediate: true }
)

// Any filter change returns to the first page.
watch(
  () => [props.window, JSON.stringify(props.scope), props.endpointId, props.ip, props.port, props.dscp, quick.value, sort.value, props.refreshKey],
  () => {
    offset.value = 0
    load()
  },
  { immediate: true }
)

const IPV4 = /^(\d{1,3}\.){3}\d{1,3}$/

function applySearch() {
  const q = searchText.value.trim()
  searchError.value = ''
  if (!q) return
  if (/^\d{1,5}$/.test(q) && Number(q) <= 65535) {
    emit('update:port', Number(q))
  } else if (IPV4.test(q) || q.includes(':')) {
    emit('update:ip', q)
  } else {
    searchError.value = `"${q}" is not an IP address or port number.`
    return
  }
  searchText.value = ''
}

function setIp(ip) {
  emit('update:ip', stripMask(ip))
}

function setPort(port) {
  emit('update:port', Number(port))
}

function setDscp(dscp, label) {
  emit('update:dscp', dscp)
  emit('update:dscpLabel', label || '')
}

function clearAll() {
  emit('update:ip', null)
  emit('update:port', null)
  emit('update:dscp', null)
  emit('update:dscpLabel', '')
  quick.value = 'all'
}

function portTone(port) {
  const p = Number(port)
  if (p === 443 || p === 80) return 'tone-info'
  if (p === 53) return 'tone-up'
  if (p === 22) return 'tone-warn'
  return 'tone-neutral'
}

function profileTone(profile) {
  switch (profile) {
    case 'SYN Scan / Probe': return 'tone-down'
    case 'Reset / Refused': return 'tone-warn'
    case 'Bulk Transfer': return 'tone-up'
    case 'Real-Time / VoIP': return 'tone-info'
    default: return 'tone-neutral'
  }
}
</script>

<style scoped>
.panel-pad {
  padding: 1rem 1.25rem;
}

.search-input {
  min-height: var(--control-height-sm);
  width: 11rem;
  font-family: var(--font-mono);
}

.search-error {
  margin: 0.5rem 0 0;
  font-size: var(--text-sm);
  color: var(--status-text-down);
}

.filter-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.125rem 0.25rem 0.125rem 0.625rem;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.chip-remove {
  min-width: var(--target-min);
  min-height: var(--target-min);
  border-radius: var(--radius-full);
  color: var(--text-muted);
}

.chip-remove:hover {
  color: var(--text-primary);
  background: var(--bg-surface-hover);
}

.clear-all {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  text-decoration: underline;
}

.ip-link {
  padding: 0;
  color: var(--text-primary);
  text-decoration: underline dotted var(--text-muted);
  text-underline-offset: 3px;
}

.ip-link:hover {
  text-decoration-color: var(--text-primary);
}

.block {
  display: block;
}

.tag-group {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
}

.tag {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  padding: 1px 6px;
  border: 1px solid currentColor;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
}

button.tag {
  cursor: pointer;
}

.port-tag {
  font-family: var(--font-mono);
}

.tone-up {
  color: var(--status-text-up);
  background: color-mix(in srgb, var(--status-text-up) 10%, transparent);
}

.tone-warn {
  color: var(--status-text-warn);
  background: color-mix(in srgb, var(--status-text-warn) 10%, transparent);
}

.tone-down {
  color: var(--status-text-down);
  background: color-mix(in srgb, var(--status-text-down) 10%, transparent);
}

.tone-info {
  color: var(--status-text-info);
  background: color-mix(in srgb, var(--status-text-info) 10%, transparent);
}

.tone-neutral {
  color: var(--text-secondary);
  border-color: var(--border-color);
  background: var(--bg-surface-hover);
}

.empty-row {
  padding: 1.5rem;
}

.pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  border-top: 1px solid var(--border-color);
}

.pager-buttons {
  display: flex;
  gap: 0.5rem;
}

.nowrap { white-space: nowrap; }
.text-center { text-align: center; }
.text-right { text-align: right; }
.font-bold { font-weight: 600; }
.font-mono { font-family: var(--font-mono); }
.text-muted { color: var(--text-muted); }
.text-xs { font-size: var(--text-xs); }
.text-sm { font-size: var(--text-sm); }
</style>
