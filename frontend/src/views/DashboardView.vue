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
        <!-- Dual View Switcher -->
        <div class="view-switcher">
          <button 
            class="btn-view" 
            :class="{ active: viewMode === 'grid' }" 
            @click="viewMode = 'grid'"
            title="Visual Card Grid"
          >
            ▦ Cards
          </button>
          <button 
            class="btn-view" 
            :class="{ active: viewMode === 'table' }" 
            @click="viewMode = 'table'"
            title="Dense Sortable Data Table"
          >
            ☰ Table
          </button>
        </div>

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
      <div 
        class="kpi-card" 
        :class="{ active: statusFilter === 'ALL' }" 
        @click="statusFilter = 'ALL'"
        role="button"
        tabindex="0"
        @keydown.enter="statusFilter = 'ALL'"
      >
        <span class="kpi-label">Total Monitored</span>
        <span class="kpi-value tnum">{{ kpiStats.total }}</span>
      </div>

      <div 
        class="kpi-card kpi-up" 
        :class="{ active: statusFilter === 'UP' }" 
        @click="statusFilter = statusFilter === 'UP' ? 'ALL' : 'UP'"
        role="button"
        tabindex="0"
        @keydown.enter="statusFilter = statusFilter === 'UP' ? 'ALL' : 'UP'"
      >
        <span class="kpi-label">🟢 UP</span>
        <span class="kpi-value tnum text-up">{{ kpiStats.up }}</span>
      </div>

      <div 
        class="kpi-card kpi-unstable" 
        :class="{ active: statusFilter === 'UNSTABLE' }" 
        @click="statusFilter = statusFilter === 'UNSTABLE' ? 'ALL' : 'UNSTABLE'"
        role="button"
        tabindex="0"
        @keydown.enter="statusFilter = statusFilter === 'UNSTABLE' ? 'ALL' : 'UNSTABLE'"
      >
        <span class="kpi-label">🟡 UNSTABLE</span>
        <span class="kpi-value tnum text-unstable">{{ kpiStats.unstable }}</span>
      </div>

      <div 
        class="kpi-card kpi-down" 
        :class="{ active: statusFilter === 'DOWN' }" 
        @click="statusFilter = statusFilter === 'DOWN' ? 'ALL' : 'DOWN'"
        role="button"
        tabindex="0"
        @keydown.enter="statusFilter = statusFilter === 'DOWN' ? 'ALL' : 'DOWN'"
      >
        <span class="kpi-label">🔴 DOWN</span>
        <span class="kpi-value tnum text-down">{{ kpiStats.down }}</span>
      </div>

      <div class="kpi-card kpi-sla">
        <span class="kpi-label">📈 Fleet SLA (24h)</span>
        <span class="kpi-value tnum text-accent">{{ kpiStats.sla }}%</span>
      </div>
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
        <p class="empty-title">No endpoints match filter "{{ statusFilter }}"</p>
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
                <th style="width: 40px;">
                  <input 
                    type="checkbox" 
                    :checked="isAllSelected" 
                    @change="toggleSelectAll"
                    aria-label="Select all endpoints" 
                  />
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
                <th @click="handleSort('avg_rtt_ms')" class="sortable-th text-right">
                  Avg Latency {{ sortKey === 'avg_rtt_ms' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('packet_loss_pct')" class="sortable-th text-right">
                  Loss % {{ sortKey === 'packet_loss_pct' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('uptime_pct')" class="sortable-th text-right">
                  24h Uptime {{ sortKey === 'uptime_pct' ? (sortAsc ? '▲' : '▼') : '' }}
                </th>
                <th @click="handleSort('last_seen')" class="sortable-th">
                  Last Seen {{ sortKey === 'last_seen' ? (sortAsc ? '▲' : '▼') : '' }}
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
                  <input 
                    type="checkbox" 
                    :checked="selectedIdSet.has(ep.id)" 
                    @change="toggleEndpointSelect(ep.id)"
                    aria-label="Select endpoint" 
                  />
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
                  {{ ep.avg_rtt_ms != null ? ep.avg_rtt_ms.toFixed(1) + ' ms' : (ep.current_state?.avg_rtt_ms != null ? ep.current_state.avg_rtt_ms.toFixed(1) + ' ms' : '—') }}
                </td>
                <td class="font-mono tnum text-right">
                  {{ getLossPct(ep) }}
                </td>
                <td class="font-mono tnum text-right font-bold">
                  {{ getUptimePct(ep) }}
                </td>
                <td class="font-mono tnum text-muted">
                  {{ formatTimeAgo(ep.last_seen) }}
                </td>
                <td class="text-right" @click.stop>
                  <div class="table-actions">
                    <button class="btn-action inspect" @click="openInspector(ep)" title="Quick Diagnostics">🔍</button>
                    <button v-if="isAdmin" class="btn-action" @click="openEditDialog(ep)" title="Edit">✎</button>
                    <button v-if="isAdmin" class="btn-action text-down" @click="confirmDeleteEndpoint(ep.id)" title="Delete">✕</button>
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
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { 
  getEndpoints, 
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

const router = useRouter()
const toast = useToast()

const endpoints = ref([])
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
  const total = endpoints.value.length
  let up = 0
  let unstable = 0
  let down = 0

  endpoints.value.forEach(ep => {
    const op = ep.current_operational_state || ep.endpoint_status || 'UP'
    const det = ep.current_detailed_state || op
    if (det === 'UP' || (op === 'UP' && !det.includes('UNSTABLE'))) {
      up++
    } else if (det === 'UP-UNSTABLE' || det === 'DOWN-UNSTABLE' || op === 'UNSTABLE') {
      unstable++
    } else if (det === 'DOWN' || op === 'DOWN') {
      down++
    } else {
      up++
    }
  })

  const totalSla = endpoints.value.reduce((sum, ep) => sum + (parseFloat(ep.uptime_percentage_24h) || 100.0), 0)
  const fleetSla = total > 0 ? (totalSla / total).toFixed(2) : '100.00'
  return { total, up, unstable, down, sla: fleetSla }
})

const filteredEndpoints = computed(() => {
  if (statusFilter.value === 'ALL') return endpoints.value
  return endpoints.value.filter(ep => {
    const op = ep.current_operational_state || ep.endpoint_status || 'UP'
    const det = ep.current_detailed_state || op
    if (statusFilter.value === 'UP') return det === 'UP' || (op === 'UP' && !det.includes('UNSTABLE'))
    if (statusFilter.value === 'UNSTABLE') return det === 'UP-UNSTABLE' || det === 'DOWN-UNSTABLE' || op === 'UNSTABLE'
    if (statusFilter.value === 'DOWN') return det === 'DOWN' || op === 'DOWN'
    return true
  })
})

const sortedEndpoints = computed(() => {
  const key = sortKey.value
  const asc = sortAsc.value ? 1 : -1
  const decorated = filteredEndpoints.value.map((ep) => {
    let sortVal
    if (key === 'detailed_state') {
      sortVal = ep.current_detailed_state || ep.current_operational_state || ep.endpoint_status || ''
    } else if (key === 'avg_rtt_ms') {
      sortVal = ep.avg_rtt_ms ?? (ep.current_state?.avg_rtt_ms ?? 99999)
    } else if (key === 'packet_loss_pct') {
      sortVal = ep.failed_count ?? (ep.current_state?.failed_count ?? 0)
    } else if (key === 'uptime_pct') {
      sortVal = parseFloat(ep.uptime_percentage_24h) || 100
    } else if (key === 'last_seen') {
      sortVal = ep.last_seen ? (Date.parse(ep.last_seen) || 0) : 0
    } else {
      sortVal = ep[key] ?? ''
    }
    return { ep, sortVal }
  })

  decorated.sort((a, b) => {
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

function getLossPct(ep) {
  const succ = ep.success_count ?? ep.current_state?.success_count ?? 5
  const fail = ep.failed_count ?? ep.current_state?.failed_count ?? 0
  const tot = succ + fail
  if (tot === 0) return '0%'
  return `${Math.round((fail / tot) * 100)}%`
}

function getUptimePct(ep) {
  if (ep.uptime_percentage_24h != null) {
    const val = parseFloat(ep.uptime_percentage_24h)
    return !isNaN(val) ? `${val.toFixed(1)}%` : '100.0%'
  }
  const hs = ep.current_health_score ?? ep.current_state?.health_score
  if (hs != null) return `${hs.toFixed(1)}%`
  return '100.0%'
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
    const response = await getEndpoints()
    endpoints.value = response.data.data || []
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
      if (payload?.type === 'STATE_TRANSITION' && payload.endpoint_id) {
        latestByEndpoint.set(payload.endpoint_id, payload)
      }
    }
    if (latestByEndpoint.size === 0) return

    const map = endpointMap.value
    for (const [epId, payload] of latestByEndpoint.entries()) {
      const ep = map.get(epId)
      if (ep) {
        ep.current_operational_state = payload.operational_state
        ep.current_detailed_state = payload.detailed_state
        ep.avg_rtt_ms = payload.avg_rtt_ms
        ep.current_health_score = payload.health_score
        ep.current_state = {
          ...ep.current_state,
          operational_state: payload.operational_state,
          detailed_state: payload.detailed_state,
          avg_rtt_ms: payload.avg_rtt_ms,
          health_score: payload.health_score,
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

.view-switcher {
  display: flex;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 2px;
  gap: 2px;
}

.btn-view {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  padding: 6px 12px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.btn-view.active {
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

/* ── KPI Strip ── */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

@media (min-width: 768px) {
  .kpi-strip {
    grid-template-columns: repeat(5, 1fr);
  }
}

.kpi-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
  user-select: none;
}

.kpi-card:hover {
  border-color: var(--border-color-strong);
  background: var(--bg-surface-hover);
}

.kpi-card.active {
  border-color: var(--text-primary);
  background: var(--bg-surface-selected);
  box-shadow: 0 0 0 1px var(--text-primary);
}

.kpi-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.kpi-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
}

.text-up { color: #10B981; }
.text-unstable { color: #F59E0B; }
.text-down { color: #EF4444; }
.text-accent { color: #3B82F6; }

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
