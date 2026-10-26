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
            <td><span class="status-pill status-unknown">{{ c.protocol }}</span></td>
            <td>
              <button
                type="button"
                class="tag port-tag"
                :class="portTone(c.dst_port)"
                :title="`Show conversations on port ${c.dst_port}`"
                @click="setPort(c.dst_port)"
              >{{ formatPortLabel(c.dst_port) }}</button>
            </td>
            <td>
              <span class="tag" :class="profileTone(c.traffic_profile)">{{ c.traffic_profile }}</span>
              <span class="text-xs text-muted tnum font-mono block">{{ c.avg_packet_size }} B/pkt · {{ c.packets_per_flow }} pkts/flow</span>
            </td>
            <td>
              <div class="tag-group">
                <span
                  v-if="c.protocol === 'TCP' && c.tcp_flags_labels.length"
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
            <td colspan="8" class="text-center text-muted empty-row">
              {{ counts.all === 0 && !activeChips.length ? `No conversations in the last ${window}.` : 'No conversations match these filters.' }}
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
  exporterId: { type: String, default: null },
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
const searchText = ref('')
const searchError = ref('')
let requestSeq = 0

const quickOptions = computed(() => [
  { value: 'all', label: `All (${formatNumber(counts.value.all)})` },
  { value: 'anomalies', label: `Anomalies (${formatNumber(counts.value.anomalies)})` },
  { value: 'qos', label: `QoS tagged (${formatNumber(counts.value.qos)})` },
  { value: 'bulk', label: `Bulk (${formatNumber(counts.value.bulk)})` },
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
    chips.push({ key: 'port', label: 'Port', value: formatPortLabel(props.port), clear: () => emit('update:port', null) })
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

function stripMask(ip) {
  return String(ip || '').split('/')[0]
}

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const res = await getConversations(props.window, {
      exporterId: props.exporterId,
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
  } catch (err) {
    if (seq === requestSeq) console.error('Failed to load conversations:', err)
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
  () => [props.window, props.exporterId, props.endpointId, props.ip, props.port, props.dscp, quick.value, sort.value, props.refreshKey],
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

.text-center { text-align: center; }
.text-right { text-align: right; }
.font-bold { font-weight: 600; }
.font-mono { font-family: var(--font-mono); }
.text-muted { color: var(--text-muted); }
.text-xs { font-size: var(--text-xs); }
.text-sm { font-size: var(--text-sm); }
</style>
