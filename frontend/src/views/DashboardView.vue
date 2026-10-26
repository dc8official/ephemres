<template>
  <div class="dashboard">
    <!-- Header Toolbar -->
    <div class="dashboard-toolbar">
      <div class="toolbar-left">
        <h1 class="page-title">Network Dashboard</h1>
        <div class="toolbar-sub-row">
          <span class="page-sub" v-if="!loading && !error">
            {{ endpoints.length }} monitored target{{ endpoints.length !== 1 ? 's' : '' }}
            <span class="separator">·</span>
            Sync: {{ lastRefreshedLabel }}
          </span>
          <span class="sse-indicator" :class="sseConnected ? 'sse-live' : 'sse-connecting'">
            <span class="pulse-dot"></span>
            {{ sseConnected ? 'Live SSE' : 'Reconnecting...' }}
          </span>
        </div>
      </div>
      <div class="toolbar-right">
        <SegmentedControl
          v-model="viewMode"
          aria-label="Endpoint view"
          :options="[{ value: 'grid', label: 'Cards', title: 'Card grid' }, { value: 'table', label: 'Table', title: 'Sortable table' }]"
        />

        <TelemetryExportPopover :endpoints="filteredEndpoints" />

        <button 
          class="btn-secondary" 
          @click="fetchEndpoints" 
          :disabled="loading"
        >
          <span>{{ loading ? 'Refreshing...' : '↻ Refresh' }}</span>
        </button>
        <button 
          v-if="isAdmin" 
          class="btn-primary" 
          @click="openAddDialog"
        >
          + Add Endpoint
        </button>
      </div>
    </div>

    <!-- Global Network Health KPI Strip -->
    <div class="kpi-strip">
      <KpiTile
        label="Total"
        :value="kpiStats.total"
        clickable
        :active="statusFilter === 'ALL'"
        @select="statusFilter = 'ALL'"
      />
      <KpiTile
        label="Up"
        :value="kpiStats.up"
        :tone="kpiStats.up ? 'up' : 'neutral'"
        clickable
        :active="statusFilter === 'UP'"
        @select="toggleStatusFilter('UP')"
      />
      <KpiTile
        label="Unstable"
        :value="kpiStats.unstable"
        :tone="kpiStats.unstable ? 'warn' : 'neutral'"
        clickable
        :active="statusFilter === 'UNSTABLE'"
        @select="toggleStatusFilter('UNSTABLE')"
      />
      <KpiTile
        label="Down"
        :value="kpiStats.down"
        :tone="kpiStats.down ? 'down' : 'neutral'"
        clickable
        :active="statusFilter === 'DOWN'"
        @select="toggleStatusFilter('DOWN')"
      />
      <KpiTile
        label="No status"
        :value="kpiStats.noStatus"
        tone="unknown"
        sub="Passive, paused or not probed yet"
        clickable
        :active="statusFilter === 'NO_STATUS'"
        @select="toggleStatusFilter('NO_STATUS')"
      />
      <KpiTile
        label="Mean uptime (24h)"
        :value="kpiStats.meanUptime"
        :unit="kpiStats.meanUptime === null ? '' : '%'"
        :tone="kpiStats.meanUptime === null ? 'unknown' : uptimeTone(kpiStats.meanUptimeRaw)"
        :sub="kpiStats.uptimeCount ? `${kpiStats.uptimeCount} monitored · target ${DEFAULT_SLA_TARGET}%` : 'No monitored targets'"
      />
    </div>

    <!-- Endpoints Content -->
    <div>
      <div v-if="error" class="alert-error" role="alert">
        {{ error }}
      </div>

      <div v-if="loading && endpoints.length === 0" class="empty-state">
        <div class="spinner"></div>
        <p>Synchronizing real-time network status...</p>
      </div>

      <div v-else-if="!loading && endpoints.length === 0 && !error" class="empty-state">
        <p class="empty-title">No endpoints configured</p>
        <p class="empty-sub">Add your first monitored endpoint to begin uptime tracking.</p>
        <button v-if="isAdmin" class="btn-primary" @click="openAddDialog">
          + Add Endpoint
        </button>
      </div>

      <div v-else-if="filteredEndpoints.length === 0" class="empty-state">
        <p class="empty-title">No endpoints match filter "{{ statusFilterLabel }}"</p>
        <button class="btn-secondary" @click="statusFilter = 'ALL'">Clear Filter</button>
      </div>

      <!-- View 1: Visual Card Grid -->
      <div v-else-if="viewMode === 'grid'" class="endpoint-grid">
        <EndpointCard
          v-for="ep in filteredEndpoints"
          :key="ep.id"
          :endpoint="ep"
          :isAdmin="isAdmin"
          :selected="selectedIdSet.has(ep.id)"
          @select="navigateTo"
          @toggle-select="toggleEndpointSelect"
          @inspect="openInspector"
          @edit="openEditDialog"
          @delete="confirmDeleteEndpoint"
        />
      </div>

      <!-- View 2: Dense Sortable Data Table -->
      <div v-else-if="viewMode === 'table'" class="table-card">
        <div class="table-responsive">
          <table class="dense-table" aria-label="Monitored Endpoints Table">
            <thead>
              <tr>
                <th style="width: 44px;">
                  <label class="select-target">
                    <input
                      type="checkbox"
                      :checked="isAllSelected"
                      @change="toggleSelectAll"
                      aria-label="Select all endpoints"
                    />
                  </label>
                </th>
                <th @click="handleSort('hostname')" class="sortable-th">
                  Hostname {{ sortKey === 'hostname' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('ip_address')" class="sortable-th">
                  IP Address {{ sortKey === 'ip_address' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('device_type')" class="sortable-th">
                  Device Type {{ sortKey === 'device_type' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('detailed_state')" class="sortable-th">
                  State {{ sortKey === 'detailed_state' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('avg_rtt_ms')" class="sortable-th text-right" title="Mean and 95th percentile RTT over the last 24 hours">
                  Latency 24h · p95 {{ sortKey === 'avg_rtt_ms' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('loss_pct')" class="sortable-th text-right" title="Share of probes without a reply over the last 24 hours">
                  Loss 24h {{ sortKey === 'loss_pct' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th title="Hourly mean RTT over the last 24 hours">RTT trend</th>
                <th @click="handleSort('uptime_pct')" class="sortable-th text-right">
                  24h Uptime {{ sortKey === 'uptime_pct' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('last_seen')" class="sortable-th" title="When the current state began">
                  In state since {{ sortKey === 'last_seen' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th class="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              <tr 
                v-for="ep in sortedEndpoints" 
                :key="ep.id"
                class="clickable-row"
                :class="{ 'row-selected': selectedIdSet.has(ep.id) }"
                @click="navigateTo(ep.id)"
              >
                <td @click.stop>
                  <label class="select-target">
                    <input
                      type="checkbox"
                      :checked="selectedIdSet.has(ep.id)"
                      @change="toggleEndpointSelect(ep.id)"
                      :aria-label="`Select ${ep.hostname}`"
                    />
                  </label>
                </td>
                <td class="font-bold">{{ ep.hostname }}</td>
                <td class="font-mono tnum">{{ ep.ip_address }}</td>
                <td><span class="device-tag">{{ ep.device_type }}</span></td>
                <td>
                  <StatusBadge 
                    :status="ep.current_detailed_state || ep.current_operational_state"
                    size="sm"
                  />
                </td>
                <td class="font-mono tnum text-right">
                  <template v-if="probeFor(ep)?.rtt_avg_ms != null">
                    {{ probeFor(ep).rtt_avg_ms.toFixed(1) }} ms
                    <span class="text-muted p95">· {{ probeFor(ep).rtt_p95_ms != null ? probeFor(ep).rtt_p95_ms.toFixed(1) : '—' }}</span>
                  </template>
                  <template v-else>—</template>
                </td>
                <td class="font-mono tnum text-right" :style="{ color: probeFor(ep)?.loss_pct > 0 ? 'var(--status-text-down)' : null }">
                  {{ formatLoss(probeFor(ep)?.loss_pct) }}
                </td>
                <td>
                  <Sparkline
                    :values="probeFor(ep)?.rtt_sparkline || []"
                    :aria-label="`${ep.hostname} hourly RTT over 24 hours`"
                  />
                </td>
                <td class="font-mono tnum text-right font-bold" :style="{ color: toneVar(uptimeTone(uptimeValue(ep))) }">
                  {{ getUptimePct(ep) }}
                </td>
                <td class="font-mono tnum text-muted">
                  {{ isMonitored(ep) ? formatTimeAgo(ep.last_seen) : 'Not monitored' }}
                </td>
                <td class="text-right" @click.stop>
                  <div class="table-actions">
                    <button class="btn-action inspect" @click="openInspector(ep)" title="Quick Diagnostics" :aria-label="`Quick diagnostics for ${ep.hostname}`">🔍</button>
                    <button v-if="isAdmin" class="btn-action" @click="openEditDialog(ep)" title="Edit" :aria-label="`Edit ${ep.hostname}`">✎</button>
                    <button v-if="isAdmin" class="btn-action text-down" @click="confirmDeleteEndpoint(ep.id)" title="Delete" :aria-label="`Delete ${ep.hostname}`">✕</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Selection Contextual Banner -->
      <transition name="fade">
        <div v-if="selectedIds.length > 0" class="selection-banner">
          <div class="banner-content">
            <span class="selection-count font-mono tnum">
              <strong>{{ selectedIds.length }}</strong> target(s) selected
            </span>
            <button 
              class="btn-primary btn-small" 
              :disabled="exporting" 
              @click="exportSelectedCSV"
            >
              {{ exporting ? 'Exporting...' : 'Export Selected CSV' }}
            </button>
            <button class="btn-secondary btn-small" @click="selectedIds = []">
              Clear
            </button>
          </div>
        </div>
      </transition>
    </div>

    <!-- Add / Edit Endpoint Modal -->
    <div v-if="displayDialog" class="modal-overlay" @click.self="displayDialog = false">
      <div class="modal-card">
        <div class="modal-header">
          <h3>{{ isEditing ? 'Modify Monitored Endpoint' : 'Register New Monitored Endpoint' }}</h3>
          <button class="btn-close" @click="displayDialog = false">✕</button>
        </div>
        <form @submit.prevent="saveEndpoint" class="modal-form">
          <div class="form-group">
            <label>Hostname *</label>
            <input 
              class="form-input" 
              v-model="form.hostname" 
              placeholder="e.g. core-router.local" 
              required 
            />
          </div>
          <div class="form-group">
            <label>IP Address *</label>
            <input 
              class="form-input font-mono" 
              v-model="form.ip_address" 
              placeholder="e.g. 192.168.1.1 or 8.8.8.8" 
              required 
              :disabled="isEditing" 
            />
          </div>
          <div class="form-group">
            <label>Device Type *</label>
            <select class="form-select" v-model="form.device_type" required>
              <option v-for="type in deviceTypes" :key="type" :value="type">
                {{ type }}
              </option>
            </select>
          </div>
          <div class="form-group">
            <label>Location (optional)</label>
            <input 
              class="form-input" 
              v-model="form.location" 
              placeholder="e.g. Datacenter rack A5" 
            />
          </div>
          <div class="form-group">
            <label>Description (optional)</label>
            <textarea 
              class="form-input form-textarea" 
              v-model="form.description" 
              rows="3" 
              placeholder="Additional endpoint metadata"
            ></textarea>
          </div>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="displayDialog = false">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="formSaving">
              {{ formSaving ? 'Saving...' : (isEditing ? 'Update Endpoint' : 'Save Endpoint') }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div v-if="displayDeleteDialog" class="modal-overlay" @click.self="displayDeleteDialog = false">
      <div class="modal-card">
        <div class="modal-header">
          <h3>Confirm Deletion</h3>
          <button class="btn-close" @click="displayDeleteDialog = false">✕</button>
        </div>
        <div class="modal-form text-center">
          <p class="modal-alert-text">Are you sure you want to delete this endpoint?</p>
          <p class="warning-subtext">This action will stop active monitoring and is completely irreversible.</p>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="displayDeleteDialog = false">Cancel</button>
            <button type="button" class="btn-danger" :disabled="formSaving" @click="executeDeleteEndpoint">
              {{ formSaving ? 'Deleting...' : 'Delete Host' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Slide-Out Inspector Drawer -->
    <EndpointInspectorDrawer 
      v-model:visible="inspectorVisible" 
      :endpoint="inspectedEndpoint" 
      :isAdmin="isAdmin"
      @run-diagnostics="handleRunDiagnostics"
    />
  </div>
</template>

<script setup>
import { ref, reactive, shallowRef, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { 
  getEndpoints,
  getProbeSummary,
  createEndpoint, 
  updateEndpoint, 
  deleteEndpoint, 
  refreshEndpointBaseline,
  exportBatchTelemetry
} from '../services/api.js'
import { user, isAdmin, loadUserFromStorage, clearUserState } from '../services/auth.js'
import EndpointCard from '../components/EndpointCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import EndpointInspectorDrawer from '../components/EndpointInspectorDrawer.vue'
import TelemetryExportPopover from '../components/TelemetryExportPopover.vue'
import { useSSE } from '../composables/useSSE.js'
import KpiTile from '../components/ui/KpiTile.vue'
import Sparkline from '../components/ui/Sparkline.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'
import { endpointStateBucket, isMonitored, meanUptime, uptimeValue } from '../utils/endpointState.js'
import { DEFAULT_SLA_TARGET, toneVar, uptimeTone } from '../utils/statusTone.js'

const router = useRouter()
const toast = useToast()

const endpoints = ref([])
// 24 h probe statistics per endpoint id (loss, mean/p95 RTT, hourly sparkline).
const probeSummary = shallowRef(new Map())

function probeFor(ep) {
  return isMonitored(ep) ? probeSummary.value.get(ep.id) : undefined
}

function formatLoss(pct) {
  if (pct === null || pct === undefined) return '—'
  if (pct === 0) return '0%'
  return pct < 1 ? `${pct.toFixed(2)}%` : `${pct.toFixed(1)}%`
}
const loading = ref(false)
const error = ref(null)
const lastRefreshed = ref(null)
const { sseConnected, subscribeBatched, onReconnect } = useSSE()
let unsubscribeSSE = null
let unsubscribeReconnect = null

const endpointMap = computed(() => {
  const map = new Map()
  for (const ep of endpoints.value) {
    map.set(ep.id, ep)
  }
  return map
})

const inspectorVisible = ref(false)
const inspectedEndpoint = ref(null)

function openInspector(ep) {
  inspectedEndpoint.value = ep
  inspectorVisible.value = true
}

async function handleRunDiagnostics(ep) {
  if (!ep?.id) return
  try {
    toast.add({
      severity: 'info',
      summary: 'Route Discovery',
      detail: `Initiating traceroute and baseline discovery for ${ep.hostname}...`,
      life: 3000,
    })
    const res = await refreshEndpointBaseline(ep.id)
    toast.add({
      severity: 'success',
      summary: 'Diagnostics Complete',
      detail: res.data?.data?.message || `Baseline refreshed for ${ep.hostname}.`,
      life: 4000,
    })
    await fetchEndpoints()
  } catch (err) {
    console.error('Failed to run diagnostics:', err)
    toast.add({
      severity: 'error',
      summary: 'Diagnostics Failed',
      detail: err.response?.data?.detail || 'Failed to trigger route discovery.',
      life: 5000,
    })
  }
}

// View Mode: 'grid' | 'table'
const viewMode = ref('grid')
const statusFilter = ref('ALL')

// Sorting for table view
const sortKey = ref('hostname')
const sortAsc = ref(true)

const selectedIds = ref([])
const selectedIdSet = computed(() => new Set(selectedIds.value))
const exporting = ref(false)

// Form states
const displayDialog = ref(false)
const displayDeleteDialog = ref(false)
const formSaving = ref(false)
const isEditing = ref(false)
const targetEndpointId = ref(null)

const deviceTypes = ['Server', 'Router', 'Switch', 'Access Point', 'Firewall', 'Printer', 'Other']

const form = ref({
  hostname: '',
  ip_address: '',
  device_type: 'Router',
  location: '',
  description: '',
  monitoring_enabled: true
})

const kpiStats = computed(() => {
  const counts = { UP: 0, UNSTABLE: 0, DOWN: 0, NO_STATUS: 0 }
  endpoints.value.forEach((ep) => {
    counts[endpointStateBucket(ep)]++
  })
  // Mean 24h uptime of actively monitored endpoints only; passive or paused ones have no uptime.
  const mean = meanUptime(endpoints.value)
  const uptimeCount = endpoints.value.filter((ep) => uptimeValue(ep) !== null).length
  return {
    total: endpoints.value.length,
    up: counts.UP,
    unstable: counts.UNSTABLE,
    down: counts.DOWN,
    noStatus: counts.NO_STATUS,
    meanUptimeRaw: mean,
    meanUptime: mean === null ? null : mean.toFixed(2),
    uptimeCount,
  }
})

const STATUS_FILTER_LABELS = { ALL: 'All', UP: 'Up', UNSTABLE: 'Unstable', DOWN: 'Down', NO_STATUS: 'No status' }
const statusFilterLabel = computed(() => STATUS_FILTER_LABELS[statusFilter.value] || statusFilter.value)

function toggleStatusFilter(bucket) {
  statusFilter.value = statusFilter.value === bucket ? 'ALL' : bucket
}

const filteredEndpoints = computed(() => {
  if (statusFilter.value === 'ALL') return endpoints.value
  return endpoints.value.filter((ep) => endpointStateBucket(ep) === statusFilter.value)
})

const sortedEndpoints = computed(() => {
  const key = sortKey.value
  const asc = sortAsc.value ? 1 : -1
  const decorated = filteredEndpoints.value.map((ep) => {
    let sortVal
    if (key === 'detailed_state') {
      sortVal = ep.current_detailed_state || ep.current_operational_state || ep.endpoint_status || ''
    } else if (key === 'avg_rtt_ms') {
      sortVal = probeFor(ep)?.rtt_avg_ms ?? null
    } else if (key === 'loss_pct') {
      sortVal = probeFor(ep)?.loss_pct ?? null
    } else if (key === 'uptime_pct') {
      sortVal = uptimeValue(ep)
    } else if (key === 'last_seen') {
      sortVal = ep.last_seen ? (Date.parse(ep.last_seen) || null) : null
    } else {
      sortVal = ep[key] ?? ''
    }
    return { ep, sortVal }
  })

  decorated.sort((a, b) => {
    // Missing values (no uptime, never seen) sort last in both directions.
    const aMissing = a.sortVal === null || a.sortVal === undefined
    const bMissing = b.sortVal === null || b.sortVal === undefined
    if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1
    if (a.sortVal < b.sortVal) return -1 * asc
    if (a.sortVal > b.sortVal) return 1 * asc
    return 0
  })

  return decorated.map((d) => d.ep)
})

const isAllSelected = computed(() => {
  const set = selectedIdSet.value
  return filteredEndpoints.value.length > 0 && filteredEndpoints.value.every(ep => set.has(ep.id))
})

const toggleSelectAll = () => {
  if (isAllSelected.value) {
    selectedIds.value = []
  } else {
    selectedIds.value = filteredEndpoints.value.map(ep => ep.id)
  }
}

function handleSort(key) {
  if (sortKey.value === key) {
    sortAsc.value = !sortAsc.value
  } else {
    sortKey.value = key
    sortAsc.value = true
  }
}

function getStateClass(st) {
  if (!st) return 'status-unknown'
  const s = st.toUpperCase()
  if (s === 'UP') return 'status-up'
  if (s.includes('UNSTABLE')) return 'status-unstable'
  if (s === 'DOWN') return 'status-down'
  return 'status-unknown'
}

function formatTimeAgo(dateStr) {
  if (!dateStr) return 'never'
  const now = Date.now()
  const past = new Date(dateStr).getTime()
  const diff = Math.floor((now - past) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function getUptimePct(ep) {
  if (!isMonitored(ep)) return '—'
  const value = uptimeValue(ep)
  return value === null ? '—' : `${value.toFixed(1)}%`
}

const toggleEndpointSelect = (id) => {
  const idx = selectedIds.value.indexOf(id)
  if (idx > -1) {
    selectedIds.value.splice(idx, 1)
  } else {
    selectedIds.value.push(id)
  }
}

const exportSelectedCSV = async () => {
  if (selectedIds.value.length === 0) return
  exporting.value = true
  try {
    const now = new Date()
    const endTime = now.toISOString()
    const past = new Date()
    past.setDate(past.getDate() - 7)
    const startTime = past.toISOString()
    
    const response = await exportBatchTelemetry(selectedIds.value, startTime, endTime)
    const blob = new Blob([response.data], { type: 'text/csv' })
    const link = document.createElement('a')
    link.href = window.URL.createObjectURL(blob)
    link.download = `telemetry_export_${Date.now()}.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  } catch (err) {
    console.error('Batch export failed:', err)
    alert('Failed to export CSV. Check server logs.')
  } finally {
    exporting.value = false
  }
}

let isFetching = false
const fetchEndpoints = async (isRetry = false) => {
  if (isFetching) return
  isFetching = true
  loading.value = true
  if (!isRetry) {
    error.value = null
  }
  try {
    const [response, probeRes] = await Promise.all([
      getEndpoints(),
      getProbeSummary(24).catch((err) => {
        console.warn('[DashboardView] Probe summary unavailable:', err)
        return null
      }),
    ])
    endpoints.value = response.data.data || []
    probeSummary.value = new Map((probeRes?.data?.data || []).map((row) => [row.endpoint_id, row]))
    lastRefreshed.value = new Date()
    error.value = null
  } catch (err) {
    if (err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED') {
      return
    }
    if (!isRetry && (!err.response || err.response.status >= 500)) {
      console.warn('[DashboardView] Initial endpoint fetch failed, retrying in 500ms...', err)
      isFetching = false
      await new Promise((resolve) => setTimeout(resolve, 500))
      return fetchEndpoints(true)
    }
    console.error('[DashboardView] Failed to fetch endpoints:', err)
    if (err.response?.status === 401) {
      clearUserState()
      router.push('/login')
    } else {
      error.value = err.response?.data?.detail || err.response?.data?.error?.message || err.message || 'Failed to connect to backend engine.'
    }
  } finally {
    loading.value = false
    isFetching = false
  }
}

const navigateTo = (id) => {
  router.push(`/endpoints/${id}`)
}

const lastRefreshedLabel = computed(() => {
  if (!lastRefreshed.value) return 'never'
  return lastRefreshed.value.toLocaleTimeString()
})

const openAddDialog = () => {
  isEditing.value = false
  form.value = {
    hostname: '',
    ip_address: '',
    device_type: 'Router',
    location: '',
    description: '',
    monitoring_enabled: true
  }
  displayDialog.value = true
}

const openEditDialog = (endpoint) => {
  isEditing.value = true
  targetEndpointId.value = endpoint.id
  form.value = {
    hostname: endpoint.hostname,
    ip_address: endpoint.ip_address,
    device_type: endpoint.device_type,
    location: endpoint.location || '',
    description: endpoint.description || '',
    monitoring_enabled: endpoint.monitoring_enabled
  }
  displayDialog.value = true
}

const saveEndpoint = async () => {
  formSaving.value = true
  try {
    if (isEditing.value) {
      await updateEndpoint(targetEndpointId.value, {
        hostname: form.value.hostname,
        device_type: form.value.device_type,
        location: form.value.location,
        description: form.value.description,
        monitoring_enabled: form.value.monitoring_enabled
      })
    } else {
      await createEndpoint(form.value)
    }
    displayDialog.value = false
    await fetchEndpoints()
  } catch (err) {
    alert(err.response?.data?.detail || 'Failed to save endpoint.')
  } finally {
    formSaving.value = false
  }
}

const confirmDeleteEndpoint = (id) => {
  targetEndpointId.value = id
  displayDeleteDialog.value = true
}

const executeDeleteEndpoint = async () => {
  formSaving.value = true
  try {
    await deleteEndpoint(targetEndpointId.value)
    displayDeleteDialog.value = false
    await fetchEndpoints()
  } catch (err) {
    alert(err.response?.data?.detail || 'Failed to delete endpoint.')
  } finally {
    formSaving.value = false
  }
}

function initSSE() {
  unsubscribeSSE = subscribeBatched((batch) => {
    const latestByEndpoint = new Map()
    for (const payload of batch) {
      const isStateChange = payload?.type === 'STATE_TRANSITION' || payload?.type === 'NODE_STATE_CHANGE'
      const targetId = payload?.endpoint_id || payload?.node_id || payload?.data?.endpoint_id
      if (isStateChange && targetId) {
        latestByEndpoint.set(targetId, payload)
      }
    }
    if (latestByEndpoint.size === 0) return

    const map = endpointMap.value
    for (const [epId, payload] of latestByEndpoint.entries()) {
      const ep = map.get(epId)
      if (ep) {
        const op = payload.operational_state || payload.new_state || payload.current_state || payload.data?.operational_state
        const det = payload.detailed_state || payload.new_state || op || payload.data?.detailed_state
        const rtt = payload.avg_rtt_ms ?? payload.data?.avg_rtt_ms
        const hs = payload.health_score ?? payload.data?.health_score

        if (op) ep.current_operational_state = op
        if (det) ep.current_detailed_state = det
        if (rtt != null) ep.avg_rtt_ms = rtt
        if (hs != null) ep.current_health_score = hs
        ep.current_state = {
          ...ep.current_state,
          operational_state: op || ep.current_state?.operational_state,
          detailed_state: det || ep.current_state?.detailed_state,
          avg_rtt_ms: rtt != null ? rtt : ep.current_state?.avg_rtt_ms,
          health_score: hs != null ? hs : ep.current_state?.health_score,
        }
      }
    }
  })

  unsubscribeReconnect = onReconnect(() => {
    // FE-02: Auto-refetch when SSE reconnects after connection loss
    fetchEndpoints()
  })
}

onMounted(async () => {
  loadUserFromStorage()
  await fetchEndpoints()
  initSSE()
})

onUnmounted(() => {
  if (unsubscribeSSE) unsubscribeSSE()
  if (unsubscribeReconnect) unsubscribeReconnect()
})
</script>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dashboard-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-title {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}

.toolbar-sub-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.page-sub {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
}

.separator {
  margin: 0 4px;
}

.sse-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 9999px;
  font-family: var(--font-mono);
}

.sse-live {
  background: rgba(16, 185, 129, 0.15);
  color: #10B981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.sse-connecting {
  background: rgba(245, 158, 11, 0.15);
  color: #F59E0B;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: pulse 2s infinite ease-in-out;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.85); }
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ── KPI Strip ── */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

@media (min-width: 768px) {
  .kpi-strip {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (min-width: 1100px) {
  .kpi-strip {
    grid-template-columns: repeat(6, 1fr);
  }
}

.text-down { color: var(--status-text-down); }

.p95 {
  font-size: var(--text-xs);
}

/* 24 px hit area around native checkboxes */
.select-target {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: var(--target-min);
  min-height: var(--target-min);
  cursor: pointer;
}

.select-target input {
  width: 16px;
  height: 16px;
  margin: 0;
  cursor: pointer;
  accent-color: var(--accent);
}

.tnum {
  font-feature-settings: "tnum";
  font-variant-numeric: tabular-nums;
}

/* ── Tabs ── */
.dashboard-tabs {
  display: flex;
  border-bottom: 1px solid var(--border-color);
  gap: 8px;
  margin-top: 4px;
}

.tab-btn {
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  border-bottom: 2px solid transparent;
  transition: all 0.15s ease;
  margin-bottom: -1px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.tab-btn:hover {
  color: var(--text-primary);
}

.tab-btn.active {
  color: var(--text-primary);
  border-bottom: 2px solid var(--accent);
}

.tab-badge {
  font-size: 11px;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  padding: 1px 6px;
  border-radius: 9999px;
}

/* ── Grid ── */
.endpoint-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(1, 1fr);
}

@media (min-width: 640px) {
  .endpoint-grid { grid-template-columns: repeat(2, 1fr); }
}

@media (min-width: 1024px) {
  .endpoint-grid { grid-template-columns: repeat(3, 1fr); }
}

@media (min-width: 1280px) {
  .endpoint-grid { grid-template-columns: repeat(4, 1fr); }
}

.device-tag {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  padding: 2px 6px;
  border-radius: 4px;
}

.font-mono { font-family: var(--font-mono); }
.font-bold { font-weight: 600; }
.text-right { text-align: right; }

.clickable-row {
  content-visibility: auto;
  contain-intrinsic-size: 0 44px;
}

.table-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.btn-action {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-action:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

/* ── Selection Banner ── */
.selection-banner {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--bg-surface);
  border: 1px solid var(--accent);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
  border-radius: 8px;
  padding: 12px 24px;
  z-index: 150;
  display: flex;
  align-items: center;
  justify-content: center;
}

.banner-content {
  display: flex;
  align-items: center;
  gap: 16px;
  white-space: nowrap;
}

.empty-state {
  text-align: center;
  padding: 80px 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.empty-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.empty-sub {
  color: var(--text-muted);
  margin: 0;
  font-size: 13px;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 2px solid var(--border-color);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.table-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.btn-action {
  min-width: 28px;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid var(--border-color, #374151);
  color: var(--text-secondary, #9ca3af);
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  transition: all 0.15s;
}

.btn-action:hover {
  background: var(--bg-surface-hover, #1f2937);
  color: var(--text-primary, #ffffff);
}

.btn-action.inspect:hover {
  border-color: #3b82f6;
  color: #3b82f6;
}

.alert-error {
  background: rgba(239, 68, 68, 0.1);
  color: #EF4444;
  border: 1px solid rgba(239, 68, 68, 0.3);
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
}
</style>
