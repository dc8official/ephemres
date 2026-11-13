<template>
  <!-- The endpoint's route (v3.3.6 build plan 6.4): the path in use now, which root-cause
       analysis compares a failure with, next to its normal path, plus the route-change log.
       A new path counts once two traces 2 minutes apart agree; a path held 24 hours becomes
       normal unless an admin pinned the normal one. -->
  <div class="route-panel">
    <PanelHeader title="Route" :hint="hint">
      <template v-if="isAdmin && status" #actions>
        <button
          v-if="!status.on_normal_path"
          type="button"
          class="btn-secondary btn-small"
          :disabled="busy"
          title="Treat the path in use now as this endpoint's normal path"
          @click="makeNormal(false)"
        >Make current path normal</button>
        <button
          v-if="!status.normal_pinned"
          type="button"
          class="btn-secondary btn-small"
          :disabled="busy"
          title="Make the path in use now normal and keep it so until unpinned"
          @click="makeNormal(true)"
        >Pin as normal</button>
        <button
          v-else
          type="button"
          class="btn-secondary btn-small"
          :disabled="busy"
          title="Let the normal path follow the current one again after 24 hours"
          @click="unpin"
        >Unpin</button>
      </template>
    </PanelHeader>

    <p v-if="loading" class="route-note">Loading route…</p>
    <p v-else-if="!status" class="route-note">No route recorded yet. It is traced when the endpoint is added and on the rolling schedule.</p>
    <template v-else>
      <div class="route-badges">
        <span v-if="status.on_normal_path" class="route-badge ok">On its normal path</span>
        <span v-else class="route-badge warn">
          Alternate path since {{ formatTime(status.current_since) }}
        </span>
        <span v-if="status.normal_pinned" class="route-badge">Normal path pinned</span>
        <span v-if="status.flapping_since" class="route-badge down">Flapping since {{ formatTime(status.flapping_since) }}</span>
        <span v-if="status.pending_hops" class="route-badge">
          New path seen {{ formatTime(status.pending_since) }}, waiting for a second trace
        </span>
        <span class="route-badge muted">Last traced {{ formatTime(status.last_traced_at) }}</span>
      </div>

      <div class="route-columns">
        <div>
          <h4 class="route-col-title">Current path <span class="col-hint">used by root-cause analysis</span></h4>
          <ol class="hop-list">
            <li
              v-for="(h, i) in status.current_hops"
              :key="`c${i}`"
              :class="{ differs: !status.on_normal_path && status.first_diff_hop && i + 1 >= status.first_diff_hop }"
            >
              <span class="hop-num tnum">{{ h.hop ?? i + 1 }}</span>
              <span class="hop-ip font-mono">{{ h.ip || '* * *' }}</span>
            </li>
          </ol>
        </div>
        <div v-if="!status.on_normal_path">
          <h4 class="route-col-title">Normal path <span class="col-hint">since {{ formatTime(status.normal_since) }}</span></h4>
          <ol class="hop-list faded">
            <li v-for="(h, i) in status.normal_hops" :key="`n${i}`">
              <span class="hop-num tnum">{{ h.hop ?? i + 1 }}</span>
              <span class="hop-ip font-mono">{{ h.ip || '* * *' }}</span>
            </li>
          </ol>
        </div>
      </div>

      <h4 class="route-col-title log-title">Route changes</h4>
      <p v-if="!status.changes.length" class="route-note">No changes recorded.</p>
      <table v-else class="route-log" aria-label="Route changes">
        <thead>
          <tr><th>When</th><th>What</th><th>Noticed by</th><th>Details</th></tr>
        </thead>
        <tbody>
          <tr v-for="(c, i) in status.changes" :key="i">
            <td class="tnum nowrap">{{ formatTime(c.changed_at) }}</td>
            <td class="nowrap">{{ KIND_LABELS[c.kind] || c.kind }}</td>
            <td class="nowrap text-muted">{{ TRIGGER_LABELS[c.trigger] || c.trigger || '—' }}</td>
            <td>{{ c.detail || (c.first_diff_hop ? `From hop ${c.first_diff_hop}` : '') }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import PanelHeader from './ui/PanelHeader.vue'
import { getEndpointRoute, setNormalPath, unpinNormalPath } from '../services/api.js'
import { isAdmin } from '../services/auth.js'
import { useSSE } from '../composables/useSSE.js'

const props = defineProps({
  endpointId: { type: String, required: true },
})

const toast = useToast()
const status = ref(null)
const loading = ref(true)
const busy = ref(false)

const KIND_LABELS = {
  path_changed: 'Path changed',
  back_to_normal: 'Back on normal path',
  became_normal: 'Became normal path',
  made_normal: 'Made normal',
  pinned: 'Pinned as normal',
  unpinned: 'Unpinned',
  flapping: 'Flapping',
}
const TRIGGER_LABELS = {
  added: 'Endpoint added',
  manual: 'Admin',
  scheduled: 'Schedule',
  recovery: 'Recovery',
  route_hint: 'Flow data',
  transit: 'Transit device',
  ttl: 'Reply TTL',
  confirm: 'Confirming trace',
}

const hint = computed(() => {
  if (!status.value) return 'The path traffic takes to this endpoint'
  return status.value.normal_pinned
    ? 'Path in use and the pinned normal path'
    : 'Path in use; a path held 24 hours becomes the normal one'
})

function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

async function load() {
  try {
    const res = await getEndpointRoute(props.endpointId)
    status.value = res.data?.data || null
  } catch (err) {
    console.error('Failed to load route:', err)
  } finally {
    loading.value = false
  }
}

async function makeNormal(pinned) {
  busy.value = true
  try {
    await setNormalPath(props.endpointId, pinned)
    toast.add({ severity: 'success', summary: pinned ? 'Pinned' : 'Normal path set', detail: 'The current path is now the normal path.', life: 3000 })
    await load()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Error', detail: err.response?.data?.detail || 'Failed to set the normal path.', life: 4000 })
  } finally {
    busy.value = false
  }
}

async function unpin() {
  busy.value = true
  try {
    await unpinNormalPath(props.endpointId)
    await load()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Error', detail: err.response?.data?.detail || 'Failed to unpin.', life: 4000 })
  } finally {
    busy.value = false
  }
}

// A route change anywhere reloads this panel (the event names no single endpoint reliably).
const { subscribe } = useSSE()
let unsubscribe = null
onMounted(() => {
  load()
  unsubscribe = subscribe((e) => {
    try {
      const p = JSON.parse(e.data)
      if (p?.type === 'TOPOLOGY_UPDATED') load()
    } catch {
      // not JSON: not ours
    }
  })
})
onUnmounted(() => unsubscribe && unsubscribe())
watch(() => props.endpointId, () => {
  loading.value = true
  load()
})
</script>

<style scoped>
.route-note {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
}

.route-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
}

.route-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
}

.route-badge.ok {
  background: rgba(16, 185, 129, 0.15);
  color: var(--status-text-up, #10b981);
}

.route-badge.warn {
  background: rgba(245, 158, 11, 0.15);
  color: var(--status-text-warn, #f59e0b);
}

.route-badge.down {
  background: rgba(239, 68, 68, 0.15);
  color: var(--status-text-down, #ef4444);
}

.route-badge.muted {
  font-weight: 500;
  color: var(--text-muted);
}

.route-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}

.route-col-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
}

.col-hint {
  font-weight: 500;
  font-size: 12px;
  color: var(--text-muted);
}

.log-title {
  margin-top: 18px;
}

.hop-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.hop-list li {
  display: flex;
  gap: 10px;
  padding: 3px 0;
  font-size: 13px;
  color: var(--text-primary);
}

.hop-list li.differs .hop-ip {
  color: var(--status-text-warn, #f59e0b);
}

.hop-list.faded li {
  color: var(--text-muted);
}

.hop-num {
  width: 1.5em;
  text-align: right;
  color: var(--text-muted);
}

.route-log {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.route-log th {
  text-align: left;
  padding: 6px 8px;
  color: var(--text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--border-color);
}

.route-log td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-color);
  color: var(--text-primary);
  vertical-align: top;
}

.nowrap {
  white-space: nowrap;
}

.tnum {
  font-variant-numeric: tabular-nums;
}

.font-mono {
  font-family: var(--font-mono, monospace);
}
</style>
