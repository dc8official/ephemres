<template>
  <!-- What ephemres-snmpd knows about this endpoint (v3.3.6 build plan 8.3): poll status, what
       the device answers (its profile), its interfaces with up/down state, and interface events
       (up, down, added, removed, renumbered, reboot). Hidden when the endpoint has no SNMP. -->
  <div v-if="data && data.credential_id" class="snmp-panel">
    <PanelHeader title="SNMP" :hint="hint">
      <template v-if="isAdmin" #actions>
        <button type="button" class="btn-secondary btn-small" :disabled="busy" @click="probe"
                title="Read the whole device again at its next turn (profile, names, speeds)">
          {{ probeAsked ? 'Probe requested' : 'Probe now' }}
        </button>
      </template>
    </PanelHeader>

    <div class="snmp-badges">
      <span class="snmp-badge" :class="statusTone">{{ statusLabel }}</span>
      <span class="snmp-badge muted">Set: {{ data.credential_name }}</span>
      <span v-if="state && state.last_ok_at" class="snmp-badge muted">Last answer {{ formatTime(state.last_ok_at) }}</span>
      <span v-if="state && state.last_reboot_at" class="snmp-badge warn">Rebooted {{ formatTime(state.last_reboot_at) }}</span>
      <span v-if="profile && profile.vendor" class="snmp-badge muted">{{ profile.vendor }}</span>
    </div>
    <p v-if="state && state.last_error && state.status !== 'ok'" class="snmp-error">{{ state.last_error }}</p>
    <p v-if="!state" class="snmp-note">Waiting for the first poll (within one poll interval, once SNMP is on in Settings).</p>

    <p v-if="profile" class="snmp-note">
      Answers: {{ answered.join(', ') || 'nothing yet' }}<span v-if="profile.unanswered && profile.unanswered.length">;
      never answered: {{ profile.unanswered.join(', ') }}</span>.
    </p>

    <table v-if="data.interfaces.length" class="snmp-table">
      <thead><tr><th>#</th><th>Interface</th><th>Description</th><th class="num">Speed</th><th>Status</th></tr></thead>
      <tbody>
        <tr v-for="i in data.interfaces" :key="i.if_index">
          <td class="font-mono tnum">{{ i.if_index }}</td>
          <td>{{ i.if_name || i.if_descr }}</td>
          <td class="muted">{{ i.if_alias || '' }}</td>
          <td class="num tnum">{{ formatSpeed(i.speed_mbps) }}</td>
          <td>
            <span class="dot" :class="i.oper_status === 'up' ? 'up' : i.admin_status === 'down' ? 'off' : 'down'"></span>
            {{ i.admin_status === 'down' ? 'shut down' : (i.oper_status || '—') }}
          </td>
        </tr>
      </tbody>
    </table>

    <!-- LLDP/CDP neighbours (v3.3.6 step 3). Monitored ones link to their page; the others
         can be added as endpoints, pre-filled. Matched ones place switches on the map. -->
    <template v-if="data.neighbours && data.neighbours.length">
      <h4 class="snmp-sub">Neighbours</h4>
      <table class="snmp-table">
        <thead><tr><th>Local port</th><th>Neighbour</th><th>Its port</th><th>Address</th><th></th></tr></thead>
        <tbody>
          <tr v-for="(n, i) in data.neighbours" :key="i">
            <td>{{ n.local_port || (n.local_if_index ? `if${n.local_if_index}` : '—') }}</td>
            <td>
              <router-link v-if="n.matched_endpoint_id" :to="`/endpoints/${n.matched_endpoint_id}`">{{ n.matched_hostname || n.remote_name }}</router-link>
              <span v-else>{{ n.remote_name || n.remote_chassis || 'unnamed' }}</span>
              <span class="muted small"> {{ n.protocol === 'cdp' ? 'CDP' : 'LLDP' }}</span>
            </td>
            <td class="muted">{{ n.remote_port || '' }}</td>
            <td class="font-mono small">{{ (n.remote_addresses || []).join(', ') }}</td>
            <td class="num">
              <span v-if="n.matched_endpoint_id" class="muted small" :title="`Matched by ${n.matched_by}`">monitored</span>
              <router-link v-else-if="isAdmin && (n.remote_addresses || []).length" class="small"
                           :to="{ path: '/', query: { add: '1', ip: n.remote_addresses[0], hostname: n.remote_name || '' } }">
                Add as endpoint
              </router-link>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="data.events.length">
      <h4 class="snmp-sub">Interface events</h4>
      <ul class="snmp-events">
        <li v-for="(e, n) in data.events.slice(0, 15)" :key="n">
          <span class="muted tnum">{{ formatTime(e.at) }}</span>
          <strong>{{ EVENT_LABELS[e.kind] || e.kind }}</strong>
          <span v-if="e.if_name">{{ e.if_name }}</span>
          <span v-if="e.detail" class="muted">({{ e.detail }})</span>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import PanelHeader from './ui/PanelHeader.vue'
import { getEndpointSnmp, requestSnmpProbe } from '../services/api.js'
import { isAdmin } from '../services/auth.js'

const props = defineProps({ endpointId: { type: String, required: true } })
const toast = useToast()
const data = ref(null)
const busy = ref(false)
const probeAsked = ref(false)
let timer = null

const STATUS = {
  ok: ['Answering', 'ok'],
  partial: ['Answering (some parts missing)', 'warn'],
  pending: ['Waiting for first poll', 'muted'],
  no_reply: ['No reply', 'down'],
  auth_failed: ['Credentials refused', 'down'],
  error: ['Error', 'down'],
}
const EVENT_LABELS = { up: 'Up', down: 'Down', added: 'Added', removed: 'Removed', renumbered: 'Renumbered', reboot: 'Device rebooted' }
const ANSWER_LABELS = {
  interfaces: 'interfaces', hc_counters: '64-bit counters', addresses: 'addresses', lldp: 'LLDP', cdp: 'CDP',
  hr_uptime: 'host uptime', engine_boots: 'engine boots',
}

const state = computed(() => data.value?.state || null)
const profile = computed(() => state.value?.profile || null)
const statusLabel = computed(() => (STATUS[state.value?.status] || STATUS.pending)[0])
const statusTone = computed(() => (STATUS[state.value?.status] || STATUS.pending)[1])
const answered = computed(() => Object.entries(profile.value?.answers || {})
  .filter(([k, v]) => v && ANSWER_LABELS[k]).map(([k, v]) => (k === 'lldp' ? `LLDP (${v})` : ANSWER_LABELS[k])))
const hint = computed(() => `${data.value?.interfaces.length || 0} interfaces, polled by ephemres-snmpd`)

function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function formatSpeed(mbps) {
  if (!mbps) return '—'
  return mbps >= 1000 ? `${mbps / 1000} Gb/s` : `${mbps} Mb/s`
}

async function load() {
  try {
    const res = await getEndpointSnmp(props.endpointId)
    data.value = res.data?.data || null
    if (probeAsked.value && state.value?.probe_requested_at && state.value?.last_probe_at
        && new Date(state.value.last_probe_at) >= new Date(state.value.probe_requested_at)) {
      probeAsked.value = false
    }
  } catch {
    data.value = null
  }
}

async function probe() {
  busy.value = true
  try {
    await requestSnmpProbe(props.endpointId)
    probeAsked.value = true
    toast.add({ severity: 'info', summary: 'Probe requested', detail: 'The device is read again at its next turn.', life: 3000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Error', detail: err.response?.data?.detail || 'Could not request a probe.', life: 4000 })
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  load()
  // Interface status changes on the poll interval; a light refresh keeps the panel current.
  timer = setInterval(() => { if (!document.hidden) load() }, 30000)
})
onUnmounted(() => clearInterval(timer))
watch(() => props.endpointId, load)
</script>

<style scoped>
.snmp-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.snmp-badge { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--bg-surface-selected); color: var(--text-secondary); }
.snmp-badge.ok { background: rgba(16, 185, 129, 0.15); color: var(--status-text-up, #10b981); }
.snmp-badge.warn { background: rgba(245, 158, 11, 0.15); color: var(--status-text-warn, #f59e0b); }
.snmp-badge.down { background: rgba(239, 68, 68, 0.15); color: var(--status-text-down, #ef4444); }
.snmp-badge.muted { font-weight: 500; color: var(--text-muted); }
.snmp-note { margin: 0 0 10px; font-size: 13px; color: var(--text-muted); }
.snmp-error { margin: 0 0 10px; font-size: 13px; color: var(--status-text-down, #ef4444); }
.snmp-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 12px; }
.snmp-table th { text-align: left; font-size: 12px; color: var(--text-muted); font-weight: 600; padding: 4px 8px; border-bottom: 1px solid var(--border-color); }
.snmp-table td { padding: 5px 8px; border-bottom: 1px solid var(--border-color); }
.num { text-align: right; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--text-muted); }
.dot.up { background: var(--status-text-up, #10b981); }
.dot.down { background: var(--status-text-down, #ef4444); }
.dot.off { background: var(--text-muted); }
.snmp-sub { margin: 4px 0 6px; font-size: 13px; font-weight: 700; color: var(--text-primary); }
.snmp-events { list-style: none; margin: 0; padding: 0; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
.snmp-events li { display: flex; gap: 8px; flex-wrap: wrap; }
.muted { color: var(--text-muted); }
.small { font-size: 12px; }
.font-mono { font-family: var(--font-mono); }
.tnum { font-variant-numeric: tabular-nums; }
</style>
