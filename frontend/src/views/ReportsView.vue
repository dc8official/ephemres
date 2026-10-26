<template>
  <div class="reports-view">
    <!-- Header Toolbar -->
    <div class="reports-toolbar">
      <div class="toolbar-left">
        <h1 class="page-title">Fleet Reports & SLA Console</h1>
        <p class="page-sub">Comprehensive fleet availability, outage incident ledger, and telemetry exports</p>
      </div>
      <div class="toolbar-right">
        <button 
          class="btn-secondary" 
          @click="loadAllReports" 
          :disabled="loading"
        >
          <span>{{ loading ? 'Refreshing...' : '↻ Refresh Data' }}</span>
        </button>
        <button 
          v-if="isOperatorOrAdmin"
          class="btn-primary" 
          @click="openExportModal" 
          :disabled="exporting || endpoints.length === 0"
        >
          <i class="pi" :class="exporting ? 'pi-spin pi-spinner' : 'pi-download'" style="margin-right: 0.5rem;"></i>
          <span>{{ exporting ? 'Exporting...' : '📥 Export Telemetry (CSV)' }}</span>
        </button>
        <button 
          v-if="isOperatorOrAdmin"
          class="btn-primary btn-pdf-export" 
          @click="openPdfModal" 
          :disabled="generatingPdf || endpoints.length === 0"
        >
          <i class="pi" :class="generatingPdf ? 'pi-spin pi-spinner' : 'pi-file-pdf'" style="margin-right: 0.5rem;"></i>
          <span>{{ generatingPdf ? 'Generating PDF...' : '📄 Generate PDF Report' }}</span>
        </button>
      </div>
    </div>

    <!-- Analysis Period Toolbar -->
    <div class="period-toolbar">
      <div class="period-pills">
        <button 
          class="pill-btn" 
          :class="{ active: filterRange === '24h' }" 
          @click="setRange('24h')"
        >
          24 Hours
        </button>
        <button 
          class="pill-btn" 
          :class="{ active: filterRange === '7d' }" 
          @click="setRange('7d')"
        >
          7 Days
        </button>
        <button 
          class="pill-btn" 
          :class="{ active: filterRange === '30d' }" 
          @click="setRange('30d')"
        >
          30 Days
        </button>
        <button 
          class="pill-btn" 
          :class="{ active: filterRange === 'custom' }" 
          @click="setRange('custom')"
        >
          Custom Range
        </button>
      </div>

      <!-- Custom Range Inline Inputs -->
      <div v-if="filterRange === 'custom'" class="custom-range-row">
        <div class="date-group">
          <label>Start (UTC)</label>
          <input type="datetime-local" v-model="customStart" class="input-datetime" />
        </div>
        <div class="date-group">
          <label>End (UTC)</label>
          <input type="datetime-local" v-model="customEnd" class="input-datetime" />
        </div>
        <button class="btn-query" @click="loadAllReports" :disabled="loading">
          Apply Range
        </button>
      </div>
    </div>

    <!-- Error Alert -->
    <div v-if="error" class="alert-error" role="alert">
      {{ error }}
    </div>

    <!-- Fleet KPI Strip -->
    <div class="kpi-strip">
      <div class="kpi-card">
        <span class="kpi-label">Mean uptime</span>
        <span class="kpi-value tnum" :style="{ color: toneVar(fleetSla === null ? 'unknown' : uptimeTone(fleetSla)) }">
          {{ fleetSla === null ? '—' : `${fleetSla.toFixed(2)}%` }}
        </span>
        <span class="kpi-sub">Monitored targets · target {{ DEFAULT_SLA_TARGET }}%</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">Active Targets</span>
        <span class="kpi-value tnum">{{ activeEndpointsCount }} / {{ endpoints.length }}</span>
        <span class="kpi-sub">Monitored nodes</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">Total Outages</span>
        <span class="kpi-value tnum" :class="totalIncidentCount > 0 ? 'text-down' : 'text-up'">
          {{ totalIncidentCount }}
        </span>
        <span class="kpi-sub">Service disruptions</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">Total Fleet Downtime</span>
        <span class="kpi-value tnum" :class="totalDowntimeSeconds > 0 ? 'text-down' : ''">
          {{ formatDuration(totalDowntimeSeconds) }}
        </span>
        <span class="kpi-sub">Cumulative outage duration</span>
      </div>
    </div>

    <!-- Fleet Availability & SLA Table -->
    <div class="table-card">
      <div class="table-card-header">
        <div>
          <h3>Fleet Availability & SLA Performance</h3>
          <p class="table-sub">Individual uptime percentage, outage incident counts, and operational duration</p>
        </div>
        <div class="table-search-box">
          <input 
            type="text" 
            v-model="searchQuery" 
            placeholder="Search by hostname or IP..." 
            class="search-input"
          />
        </div>
      </div>

      <div class="table-responsive">
        <table class="dense-table" aria-label="Fleet Availability Table">
          <thead>
            <tr>
              <th @click="toggleSort('hostname')" class="sortable-th">
                Target / Hostname 
                <span class="sort-icon">{{ sortKey === 'hostname' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th>Device Type</th>
              <th @click="toggleSort('operational_state')" class="sortable-th">
                Current State
                <span class="sort-icon">{{ sortKey === 'operational_state' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th @click="toggleSort('uptime_percentage')" class="sortable-th">
                Uptime SLA (%)
                <span class="sort-icon">{{ sortKey === 'uptime_percentage' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th @click="toggleSort('incident_count')" class="sortable-th">
                Incidents
                <span class="sort-icon">{{ sortKey === 'incident_count' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th @click="toggleSort('uptime_seconds')" class="sortable-th">
                UP Duration
                <span class="sort-icon">{{ sortKey === 'uptime_seconds' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th @click="toggleSort('downtime_seconds')" class="sortable-th">
                DOWN Duration
                <span class="sort-icon">{{ sortKey === 'downtime_seconds' ? (sortAsc ? '▲' : '▼') : '↕' }}</span>
              </th>
              <th class="text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading && reportRows.length === 0">
              <td colspan="8" class="table-empty">
                <div class="spinner"></div>
                <p>Computing fleet telemetry and compiling SLA summaries...</p>
              </td>
            </tr>
            <tr v-else-if="filteredReportRows.length === 0">
              <td colspan="8" class="table-empty">
                No endpoints match the query or filter criteria.
              </td>
            </tr>
            <tr v-for="row in sortedReportRows" :key="row.id">
              <td>
                <div class="host-col">
                  <router-link :to="`/endpoints/${row.id}`" class="host-name">
                    {{ row.hostname }}
                  </router-link>
                  <span class="host-ip tnum">{{ row.ip_address }}</span>
                </div>
              </td>
              <td>
                <span class="device-pill">{{ row.device_type }}</span>
              </td>
              <td>
                <span class="status-pill" :class="getStateClass(row.detailed_state || row.operational_state)">
                  <span class="status-dot"></span>
                  {{ row.detailed_state || row.operational_state }}
                </span>
              </td>
              <td>
                <span class="sla-badge tnum" :class="getSlaClass(row.uptime_percentage)">
                  {{ row.uptime_percentage != null ? row.uptime_percentage.toFixed(2) + '%' : '—' }}
                </span>
              </td>
              <!-- Incident and duration figures only apply to ICMP-monitored endpoints. -->
              <td class="tnum" :class="row.incident_count > 0 ? 'text-down' : ''">
                {{ row.monitoring_enabled === false ? '—' : row.incident_count }}
              </td>
              <td class="tnum font-mono">
                {{ row.monitoring_enabled === false ? '—' : formatDuration(row.uptime_seconds) }}
              </td>
              <td class="tnum font-mono" :class="row.downtime_seconds > 0 ? 'text-down font-bold' : ''">
                {{ row.monitoring_enabled === false ? '—' : formatDuration(row.downtime_seconds) }}
              </td>
              <td class="text-right">
                <router-link :to="`/endpoints/${row.id}`" class="btn-inspect">
                  Inspect →
                </router-link>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Interactive CSV Export Customizer Modal -->
    <div v-if="showExportModal" class="modal-backdrop" @click.self="closeExportModal" @keydown="onExportModalKeydown">
      <div 
        ref="exportModalRef" 
        class="modal-dialog" 
        role="dialog" 
        aria-modal="true" 
        aria-labelledby="export-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <div class="modal-title-group">
            <h2 id="export-modal-title" class="modal-title">📊 Configure Telemetry CSV Export</h2>
            <p class="modal-subtitle">Select endpoint scope, time window, and data columns for telemetry export</p>
          </div>
          <button class="btn-close" @click="closeExportModal" aria-label="Close modal">✕</button>
        </div>

        <div class="modal-body">
          <!-- Section 1: Target Scope -->
          <div class="form-section">
            <label class="section-label">Target Scope</label>
            <div class="radio-options">
              <label class="radio-label">
                <input type="radio" value="all" v-model="exportScope" />
                <span>All Monitored Endpoints ({{ endpoints.length }} nodes)</span>
              </label>
              <label class="radio-label">
                <input type="radio" value="selected" v-model="exportScope" />
                <span>Custom Target Selection ({{ exportSelectedIds.length }} selected)</span>
              </label>
            </div>

            <!-- Target checklist if custom selection -->
            <div v-if="exportScope === 'selected'" class="target-picker-box">
              <div class="target-picker-header">
                <label class="checkbox-label font-bold">
                  <input type="checkbox" :checked="isAllExportSelected" @change="toggleSelectAllExport" />
                  <span>Select All Targets</span>
                </label>
                <span class="target-count">{{ exportSelectedIds.length }} / {{ endpoints.length }}</span>
              </div>
              <div class="target-list">
                <div v-for="ep in endpoints" :key="ep.id" class="target-item">
                  <label class="checkbox-label">
                    <input type="checkbox" :value="ep.id" v-model="exportSelectedIds" />
                    <span class="target-name font-bold">{{ ep.hostname }}</span>
                    <span class="target-ip font-mono tnum">{{ ep.ip_address }}</span>
                    <span class="device-pill">{{ ep.device_type }}</span>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <!-- Section 2: Time Window -->
          <div class="form-section">
            <label class="section-label">Export Time Window</label>
            <div class="period-pills">
              <button class="pill-btn" :class="{ active: exportRange === '24h' }" @click="exportRange = '24h'">24 Hours</button>
              <button class="pill-btn" :class="{ active: exportRange === '7d' }" @click="exportRange = '7d'">7 Days</button>
              <button class="pill-btn" :class="{ active: exportRange === '30d' }" @click="exportRange = '30d'">30 Days</button>
              <button class="pill-btn" :class="{ active: exportRange === 'custom' }" @click="exportRange = 'custom'">Custom Range</button>
            </div>
            <div v-if="exportRange === 'custom'" class="custom-range-row mt-2">
              <div class="date-group">
                <label>Start (UTC)</label>
                <input type="datetime-local" v-model="exportCustomStart" class="input-datetime" />
              </div>
              <div class="date-group">
                <label>End (UTC)</label>
                <input type="datetime-local" v-model="exportCustomEnd" class="input-datetime" />
              </div>
            </div>
          </div>

          <!-- Section 3: Telemetry Schema Preview & Column Customizer -->
          <div class="form-section">
            <div class="column-customizer-header">
              <label class="section-label">Included CSV Columns ({{ selectedColumns.length }}/{{ availableColumns.length }})</label>
              <div class="column-quick-actions">
                <button type="button" class="btn-text-action" @click="selectAllColumns">Select All</button>
                <span class="action-divider">•</span>
                <button type="button" class="btn-text-action" @click="resetToStandardColumns">Reset to Standard</button>
              </div>
            </div>
            <div class="column-grid">
              <div 
                v-for="col in availableColumns" 
                :key="col.id" 
                class="column-custom-item" 
                :class="{ locked: col.locked, active: isColumnSelected(col.id) }"
                @click="toggleColumn(col)"
              >
                <div class="column-item-check">
                  <input 
                    v-if="col.locked" 
                    type="checkbox" 
                    checked 
                    disabled 
                    aria-disabled="true"
                    :aria-label="`${col.label} (Required locked field)`"
                  />
                  <input 
                    v-else 
                    type="checkbox" 
                    :checked="isColumnSelected(col.id)" 
                    :aria-label="col.label"
                    @click.stop="toggleColumn(col)"
                  />
                </div>
                <span class="column-item-label">{{ col.label }}</span>
                <span v-if="col.locked" class="locked-badge">LOCKED</span>
              </div>
            </div>
          </div>
        </div>

        <div class="modal-footer">
          <button class="btn-secondary" @click="closeExportModal" :disabled="exporting">
            Cancel
          </button>
          <button class="btn-primary" @click="triggerExport" :disabled="exporting || (exportScope === 'selected' && exportSelectedIds.length === 0)">
            <i class="pi" :class="exporting ? 'pi-spin pi-spinner' : 'pi-download'" style="margin-right: 0.5rem;"></i>
            <span>{{ exporting ? 'Generating CSV...' : '📥 Download CSV Export' }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Interactive PDF Report Generator Modal -->
    <div v-if="showPdfModal" class="modal-backdrop" @click.self="closePdfModal" @keydown="onPdfModalKeydown">
      <div 
        ref="pdfModalRef" 
        class="modal-dialog modal-dialog-lg" 
        role="dialog" 
        aria-modal="true" 
        aria-labelledby="pdf-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <div class="modal-title-group">
            <h2 id="pdf-modal-title" class="modal-title">📄 Generate PDF Audit Report</h2>
            <p class="modal-subtitle">Compile network telemetry, SLA metrics, and bandwidth data into an immutable, print-ready document</p>
          </div>
          <button class="btn-close" @click="closePdfModal" aria-label="Close modal">✕</button>
        </div>

        <div class="modal-body">
          <!-- Section 1: Template Selection -->
          <div class="form-section">
            <label class="section-label">Select Report Template</label>
            <div class="template-cards-grid">
              <div 
                class="template-card" 
                :class="{ active: pdfTemplate === 'availability' }"
                @click="pdfTemplate = 'availability'"
                tabindex="0"
                role="button"
                @keydown.enter.space="pdfTemplate = 'availability'"
              >
                <div class="template-card-header">
                  <span class="template-badge badge-avail">Template A</span>
                  <input type="radio" value="availability" v-model="pdfTemplate" />
                </div>
                <h3 class="template-card-title">Availability & SLA Performance</h3>
                <p class="template-card-desc">
                  Fleet availability matrix, SLA percentages, downtime duration, and outage incident ledger with RCA findings.
                </p>
                <div class="template-features">
                  <span>✓ Multi-Endpoint SLA</span>
                  <span>✓ Incident Ledger</span>
                  <span>✓ Root Cause Analysis</span>
                </div>
              </div>

              <div 
                class="template-card" 
                :class="{ active: pdfTemplate === 'bandwidth' }"
                @click="pdfTemplate = 'bandwidth'"
                tabindex="0"
                role="button"
                @keydown.enter.space="pdfTemplate = 'bandwidth'"
              >
                <div class="template-card-header">
                  <span class="template-badge badge-bw">Template B</span>
                  <input type="radio" value="bandwidth" v-model="pdfTemplate" />
                </div>
                <h3 class="template-card-title">Bandwidth & Interface Capacity</h3>
                <p class="template-card-desc">
                  Flow exporter routers summary, per-interface traffic volume, line-speed utilization %, and top conversations.
                </p>
                <div class="template-features">
                  <span>✓ Exporter Routers</span>
                  <span>✓ Interface Capacity %</span>
                  <span>✓ Top Conversations</span>
                </div>
              </div>

              <div 
                class="template-card" 
                :class="{ active: pdfTemplate === 'master' }"
                @click="pdfTemplate = 'master'"
                tabindex="0"
                role="button"
                @keydown.enter.space="pdfTemplate = 'master'"
              >
                <div class="template-card-header">
                  <span class="template-badge badge-master">Template C</span>
                  <input type="radio" value="master" v-model="pdfTemplate" />
                </div>
                <h3 class="template-card-title">Executive Master Audit Dossier</h3>
                <p class="template-card-desc">
                  Comprehensive 4-part synthesis merging availability, SLA, outage events, flow exporter routers, and interface bandwidth.
                </p>
                <div class="template-features">
                  <span>✓ All-In-One Synthesis</span>
                  <span>✓ Complete Ledger</span>
                  <span>✓ Forensic Profiles</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Section 2: Target Scope -->
          <div class="form-section">
            <label class="section-label">Target Scope</label>
            <div class="radio-options">
              <label class="radio-label">
                <input type="radio" value="all" v-model="pdfScope" />
                <span>All Monitored Endpoints & Exporters ({{ endpoints.length }} nodes)</span>
              </label>
              <label class="radio-label">
                <input type="radio" value="selected" v-model="pdfScope" />
                <span>Custom Target Selection ({{ pdfSelectedIds.length }} selected)</span>
              </label>
            </div>

            <!-- Target checklist if custom selection -->
            <div v-if="pdfScope === 'selected'" class="target-picker-box">
              <div class="target-picker-header">
                <label class="checkbox-label font-bold">
                  <input type="checkbox" :checked="isAllPdfSelected" @change="toggleSelectAllPdf" />
                  <span>Select All Targets</span>
                </label>
                <span class="target-count">{{ pdfSelectedIds.length }} / {{ endpoints.length }}</span>
              </div>
              <div class="target-list">
                <div v-for="ep in endpoints" :key="ep.id" class="target-item">
                  <label class="checkbox-label">
                    <input type="checkbox" :value="ep.id" v-model="pdfSelectedIds" />
                    <span class="target-name font-bold">{{ ep.hostname }}</span>
                    <span class="target-ip font-mono tnum">{{ ep.ip_address }}</span>
                    <span class="device-pill">{{ ep.device_type }}</span>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <!-- Section 3: Time Window -->
          <div class="form-section">
            <label class="section-label">Report Period</label>
            <div class="period-pills">
              <button type="button" class="pill-btn" :class="{ active: pdfRange === '24h' }" @click="pdfRange = '24h'">24 Hours</button>
              <button type="button" class="pill-btn" :class="{ active: pdfRange === '7d' }" @click="pdfRange = '7d'">7 Days</button>
              <button type="button" class="pill-btn" :class="{ active: pdfRange === '30d' }" @click="pdfRange = '30d'">30 Days</button>
              <button type="button" class="pill-btn" :class="{ active: pdfRange === 'custom' }" @click="pdfRange = 'custom'">Custom Range</button>
            </div>
            <div v-if="pdfRange === 'custom'" class="custom-range-row mt-2">
              <div class="date-group">
                <label>Start (UTC)</label>
                <input type="datetime-local" v-model="pdfCustomStart" class="input-datetime" />
              </div>
              <div class="date-group">
                <label>End (UTC)</label>
                <input type="datetime-local" v-model="pdfCustomEnd" class="input-datetime" />
              </div>
            </div>
          </div>

          <!-- Section 4: Incident Ledger Options -->
          <div class="form-section" v-if="pdfTemplate !== 'bandwidth'">
            <label class="section-label">Incident Ledger Options</label>
            <label class="checkbox-label">
              <input type="checkbox" v-model="pdfIncludeRca" />
              <span>Include Root Cause Analysis (RCA) diagnostic telemetry in outage incident ledger</span>
            </label>
          </div>
        </div>

        <div class="modal-footer">
          <button type="button" class="btn-secondary" @click="closePdfModal" :disabled="generatingPdf">
            Cancel
          </button>
          <button 
            type="button" 
            class="btn-primary btn-pdf-export" 
            @click="triggerPdfExport" 
            :disabled="generatingPdf || (pdfScope === 'selected' && pdfSelectedIds.length === 0)"
          >
            <i class="pi" :class="generatingPdf ? 'pi-spin pi-spinner' : 'pi-file-pdf'" style="margin-right: 0.5rem;"></i>
            <span>{{ generatingPdf ? 'Compiling & Rendering PDF...' : '📄 Generate & Download PDF' }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { DEFAULT_SLA_TARGET, toneVar, uptimeTone } from '../utils/statusTone.js'
import { useToast } from 'primevue/usetoast'
import { getFleetSummary, exportBatchTelemetry, generatePdfReport } from '../services/api.js'
import { isOperatorOrAdmin, loadUserFromStorage } from '../services/auth.js'

const toast = useToast()
const exportModalRef = ref(null)
let exportOpenerElement = null

const loading = ref(false)
const exporting = ref(false)
const error = ref(null)

const endpoints = ref([])
const reportRows = ref([])
const searchQuery = ref('')

const filterRange = ref('24h')
const customStart = ref(new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 16))
const customEnd = ref(new Date().toISOString().slice(0, 16))

const sortKey = ref('uptime_percentage')
const sortAsc = ref(true)

function setRange(range) {
  filterRange.value = range
  if (range !== 'custom') {
    loadAllReports()
  }
}

function getQueryRange() {
  const now = new Date()
  let start = ''
  let end = now.toISOString()

  if (filterRange.value === '24h') {
    start = new Date(now.getTime() - 24 * 3600 * 1000).toISOString()
  } else if (filterRange.value === '7d') {
    start = new Date(now.getTime() - 7 * 24 * 3600 * 1000).toISOString()
  } else if (filterRange.value === '30d') {
    start = new Date(now.getTime() - 30 * 24 * 3600 * 1000).toISOString()
  } else {
    start = new Date(customStart.value).toISOString()
    end = new Date(customEnd.value).toISOString()
  }
  return { start, end }
}

async function loadAllReports() {
  loading.value = true
  error.value = null
  const { start, end } = getQueryRange()

  try {
    const res = await getFleetSummary(start, end)
    const summaryData = res.data?.data || {}
    const epList = summaryData.endpoints || []
    endpoints.value = epList

    reportRows.value = epList.map((ep) => ({
      id: ep.id,
      hostname: ep.hostname,
      ip_address: ep.ip_address,
      device_type: ep.device_type || 'SERVER',
      operational_state: ep.operational_state || 'UP',
      detailed_state: ep.detailed_state || 'UP',
      monitoring_enabled: ep.monitoring_enabled,
      // null when not measured (monitoring off or no data): never shown or averaged as 100 %.
      uptime_percentage: ep.monitoring_enabled !== false && ep.uptime_percentage != null ? Number(ep.uptime_percentage) : null,
      incident_count: ep.incident_count || 0,
      uptime_seconds: ep.uptime_seconds || 0,
      downtime_seconds: ep.downtime_seconds || 0,
      total_seconds: ep.total_seconds || 0,
    }))
  } catch (err) {
    console.error('Failed to load fleet reports:', err)
    error.value = err.response?.data?.detail || 'Failed to assemble fleet report metrics.'
  } finally {
    loading.value = false
  }
}

const fleetSla = computed(() => {
  const values = reportRows.value.map((r) => r.uptime_percentage).filter((v) => v !== null && v !== undefined)
  if (values.length === 0) return null
  return values.reduce((acc, v) => acc + v, 0) / values.length
})

const activeEndpointsCount = computed(() => {
  return reportRows.value.filter(r => r.monitoring_enabled).length
})

const totalIncidentCount = computed(() => {
  return reportRows.value.reduce((acc, r) => acc + (r.incident_count || 0), 0)
})

const totalDowntimeSeconds = computed(() => {
  return reportRows.value.reduce((acc, r) => acc + (r.downtime_seconds || 0), 0)
})

const filteredReportRows = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return reportRows.value
  return reportRows.value.filter(r => 
    r.hostname.toLowerCase().includes(q) ||
    r.ip_address.toLowerCase().includes(q) ||
    r.device_type.toLowerCase().includes(q)
  )
})

const sortedReportRows = computed(() => {
  const rows = [...filteredReportRows.value]
  rows.sort((a, b) => {
    let valA = a[sortKey.value]
    let valB = b[sortKey.value]

    // Missing values sort last in both directions.
    const aMissing = valA === null || valA === undefined
    const bMissing = valB === null || valB === undefined
    if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1
    if (typeof valA === 'string') {
      return sortAsc.value ? valA.localeCompare(valB) : valB.localeCompare(valA)
    }
    return sortAsc.value ? valA - valB : valB - valA
  })
  return rows
})

function toggleSort(key) {
  if (sortKey.value === key) {
    sortAsc.value = !sortAsc.value
  } else {
    sortKey.value = key
    sortAsc.value = true
  }
}

const SLA_CLASS_BY_TONE = { up: 'sla-good', warn: 'sla-warn', down: 'sla-bad', unknown: 'sla-unknown' }

function getSlaClass(val) {
  return SLA_CLASS_BY_TONE[uptimeTone(val)]
}

function getStateClass(st) {
  if (!st) return 'status-unknown'
  const s = st.toUpperCase()
  if (s === 'UP') return 'status-up'
  if (s.includes('UNSTABLE')) return 'status-unstable'
  if (s === 'DOWN') return 'status-down'
  return 'status-unknown'
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '0s'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60

  const parts = []
  if (d > 0) parts.push(`${d}d`)
  if (h > 0) parts.push(`${h}h`)
  if (m > 0) parts.push(`${m}m`)
  if (s > 0 && d === 0 && h === 0) parts.push(`${s}s`)
  return parts.join(' ') || `${seconds}s`
}

const showExportModal = ref(false)
const exportScope = ref('all')
const exportSelectedIds = ref([])
const exportRange = ref('24h')
const exportCustomStart = ref(new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 16))
const exportCustomEnd = ref(new Date().toISOString().slice(0, 16))

const availableColumns = [
  { id: 'Hostname', label: 'Hostname', locked: true },
  { id: 'IP_Address', label: 'IP Address', locked: true },
  { id: 'Endpoint_ID', label: 'Endpoint ID', locked: false },
  { id: 'Device_Type', label: 'Device Type', locked: false },
  { id: 'Timestamp', label: 'Timestamp UTC', locked: false },
  { id: 'Operational_State', label: 'Operational State', locked: false },
  { id: 'Detailed_State', label: 'Detailed State', locked: false },
  { id: 'Packet_Success_Rate', label: 'Health Score / Packet Loss %', locked: false },
  { id: 'Avg_RTT_ms', label: 'Avg Latency (RTT ms)', locked: false },
]

const standardColumns = ['Hostname', 'IP_Address', 'Device_Type', 'Timestamp', 'Operational_State', 'Detailed_State', 'Packet_Success_Rate', 'Avg_RTT_ms']
const selectedColumns = ref([...standardColumns])

function isColumnSelected(colId) {
  return selectedColumns.value.includes(colId)
}

function toggleColumn(col) {
  if (col.locked) return
  if (isColumnSelected(col.id)) {
    selectedColumns.value = selectedColumns.value.filter(c => c !== col.id)
  } else {
    selectedColumns.value.push(col.id)
  }
}

function selectAllColumns() {
  selectedColumns.value = availableColumns.map(c => c.id)
}

function resetToStandardColumns() {
  selectedColumns.value = [...standardColumns]
}

const isAllExportSelected = computed(() => {
  return endpoints.value.length > 0 && exportSelectedIds.value.length === endpoints.value.length
})

function toggleSelectAllExport() {
  if (isAllExportSelected.value) {
    exportSelectedIds.value = []
  } else {
    exportSelectedIds.value = endpoints.value.map(e => e.id)
  }
}

function openExportModal() {
  exportOpenerElement = document.activeElement
  exportScope.value = 'all'
  exportRange.value = filterRange.value
  exportSelectedIds.value = endpoints.value.map(e => e.id)
  showExportModal.value = true
  nextTick(() => {
    if (exportModalRef.value) {
      const first = exportModalRef.value.querySelector('button, input')
      first?.focus()
    }
  })
}

function closeExportModal() {
  showExportModal.value = false
  nextTick(() => {
    exportOpenerElement?.focus()
  })
}

function onExportModalKeydown(e) {
  if (e.key === 'Escape') {
    closeExportModal()
    return
  }
  if (e.key !== 'Tab' || !exportModalRef.value) return
  const focusables = exportModalRef.value.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )
  if (!focusables.length) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

async function triggerExport() {
  const targetIds = exportScope.value === 'all'
    ? endpoints.value.map(ep => ep.id)
    : exportSelectedIds.value

  if (targetIds.length === 0) return

  exporting.value = true
  const now = new Date()
  let start = ''
  let end = now.toISOString()

  if (exportRange.value === '24h') {
    start = new Date(now.getTime() - 24 * 3600 * 1000).toISOString()
  } else if (exportRange.value === '7d') {
    start = new Date(now.getTime() - 7 * 24 * 3600 * 1000).toISOString()
  } else if (exportRange.value === '30d') {
    start = new Date(now.getTime() - 30 * 24 * 3600 * 1000).toISOString()
  } else {
    start = new Date(exportCustomStart.value).toISOString()
    end = new Date(exportCustomEnd.value).toISOString()
  }

  try {
    const res = await exportBatchTelemetry(targetIds, start, end, selectedColumns.value)
    const blob = new Blob([res.data], { type: 'text/csv' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `fleet_telemetry_report_${exportRange.value}_${Date.now()}.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    closeExportModal()
    toast.add({
      severity: 'success',
      summary: 'Export Complete',
      detail: 'Telemetry CSV downloaded successfully.',
      life: 3000,
    })
  } catch (err) {
    console.error('CSV export failed:', err)
    toast.add({
      severity: 'error',
      summary: 'Export Failed',
      detail: 'Failed to generate batch CSV export.',
      life: 4000,
    })
  } finally {
    exporting.value = false
  }
}

// -------------------------------------------------------------
// Interactive PDF Report Generator State & Actions
// -------------------------------------------------------------
const showPdfModal = ref(false)
const pdfModalRef = ref(null)
let pdfOpenerElement = null
const generatingPdf = ref(false)
const pdfTemplate = ref('availability') // 'availability', 'bandwidth', 'master'
const pdfScope = ref('all')
const pdfSelectedIds = ref([])
const pdfRange = ref('24h')
const pdfCustomStart = ref(new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 16))
const pdfCustomEnd = ref(new Date().toISOString().slice(0, 16))
const pdfIncludeRca = ref(true)

const isAllPdfSelected = computed(() => {
  return endpoints.value.length > 0 && pdfSelectedIds.value.length === endpoints.value.length
})

function toggleSelectAllPdf() {
  if (isAllPdfSelected.value) {
    pdfSelectedIds.value = []
  } else {
    pdfSelectedIds.value = endpoints.value.map(e => e.id)
  }
}

function openPdfModal() {
  pdfOpenerElement = document.activeElement
  pdfScope.value = 'all'
  pdfRange.value = filterRange.value
  pdfSelectedIds.value = endpoints.value.map(e => e.id)
  showPdfModal.value = true
  nextTick(() => {
    if (pdfModalRef.value) {
      const first = pdfModalRef.value.querySelector('button, input')
      first?.focus()
    }
  })
}

function closePdfModal() {
  showPdfModal.value = false
  nextTick(() => {
    pdfOpenerElement?.focus()
  })
}

function onPdfModalKeydown(e) {
  if (e.key === 'Escape') {
    closePdfModal()
    return
  }
  if (e.key !== 'Tab' || !pdfModalRef.value) return
  const focusables = pdfModalRef.value.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )
  if (!focusables.length) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

async function triggerPdfExport() {
  const targetIds = pdfScope.value === 'all'
    ? endpoints.value.map(ep => ep.id)
    : pdfSelectedIds.value

  if (targetIds.length === 0) return

  generatingPdf.value = true
  const now = new Date()
  let start = ''
  let end = now.toISOString()

  if (pdfRange.value === '24h') {
    start = new Date(now.getTime() - 24 * 3600 * 1000).toISOString()
  } else if (pdfRange.value === '7d') {
    start = new Date(now.getTime() - 7 * 24 * 3600 * 1000).toISOString()
  } else if (pdfRange.value === '30d') {
    start = new Date(now.getTime() - 30 * 24 * 3600 * 1000).toISOString()
  } else {
    start = new Date(pdfCustomStart.value).toISOString()
    end = new Date(pdfCustomEnd.value).toISOString()
  }

  try {
    const res = await generatePdfReport({
      template_type: pdfTemplate.value,
      endpoint_ids: targetIds,
      start_date: start,
      end_date: end,
      include_rca: pdfIncludeRca.value,
    })

    const blob = new Blob([res.data], { type: 'application/pdf' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    const templateName = pdfTemplate.value.toUpperCase()
    link.download = `Ephemres_${templateName}_Report_${pdfRange.value}_${Date.now()}.pdf`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)

    closePdfModal()
    toast.add({
      severity: 'success',
      summary: 'PDF Report Ready',
      detail: `Template ${templateName} generated and downloaded successfully.`,
      life: 4000,
    })
  } catch (err) {
    console.error('Failed to generate PDF report:', err)
    let errorDetail = 'Failed to generate PDF audit report.'
    if (err.response?.data) {
      if (err.response.data instanceof Blob) {
        try {
          const text = await err.response.data.text()
          const json = JSON.parse(text)
          errorDetail = json.detail || errorDetail
        } catch {
          // ignore
        }
      } else if (err.response.data.detail) {
        errorDetail = err.response.data.detail
      }
    }
    toast.add({
      severity: 'error',
      summary: 'PDF Generation Failed',
      detail: errorDetail,
      life: 5000,
    })
  } finally {
    generatingPdf.value = false
  }
}

onMounted(() => {
  loadUserFromStorage()
  loadAllReports()
})
</script>

<style scoped>
.reports-view {
  padding: 1.5rem 2rem;
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.reports-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1rem;
}

.page-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
  letter-spacing: -0.02em;
}

.page-sub {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin-top: 0.25rem;
}

.toolbar-right {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}

.period-toolbar {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
}

.period-pills {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.pill-btn {
  padding: 0.375rem 0.875rem;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8125rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}

.pill-btn:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.pill-btn.active {
  background: var(--text-primary);
  color: var(--bg-app);
  border-color: var(--text-primary);
}

.custom-range-row {
  display: flex;
  gap: 1rem;
  align-items: flex-end;
  flex-wrap: wrap;
  padding-top: 0.5rem;
  border-top: 1px solid var(--border-color);
}

.date-group {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.date-group label {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-weight: 600;
}

.input-datetime {
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  border-radius: var(--radius-sm, 6px);
  padding: 0.375rem 0.625rem;
  font-size: 0.8125rem;
}

.btn-query {
  padding: 0.375rem 0.875rem;
  background: var(--text-primary);
  color: var(--bg-app);
  border: none;
  border-radius: var(--radius-sm, 6px);
  font-weight: 600;
  font-size: 0.8125rem;
  cursor: pointer;
}

.kpi-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.kpi-card {
  padding: 1.25rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.kpi-label {
  font-size: 0.8125rem;
  color: var(--text-muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.kpi-value {
  font-size: 1.75rem;
  font-weight: 800;
  color: var(--text-primary);
  letter-spacing: -0.03em;
}

.kpi-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.text-accent {
  color: #38bdf8;
}

.text-up {
  color: var(--status-up-color, #16a34a);
}

.text-down {
  color: var(--status-down-color, #dc2626);
}

.table-card-header {
  padding: 1.25rem 1.5rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1rem;
  border-bottom: 1px solid var(--border-color);
}

.table-card-header h3 {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.table-sub {
  font-size: 0.8125rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
}

.search-input {
  width: 260px;
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  border-radius: var(--radius-sm, 6px);
  padding: 0.45rem 0.75rem;
  font-size: 0.8125rem;
}

.sort-icon {
  font-size: 0.6875rem;
  margin-left: 0.25rem;
  opacity: 0.6;
}

.host-col {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
}

.host-name {
  font-weight: 600;
  color: var(--text-primary);
  text-decoration: none;
}

.host-name:hover {
  text-decoration: underline;
}

.host-ip {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}

.device-pill {
  font-size: 0.6875rem;
  font-weight: 600;
  padding: 0.15rem 0.4rem;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 4px);
  color: var(--text-secondary);
  text-transform: uppercase;
}

.sla-badge {
  font-weight: 700;
  padding: 0.2rem 0.5rem;
  border-radius: var(--radius-sm, 4px);
}

.sla-good {
  color: var(--status-text-up);
  background: var(--color-up-bg);
}

.sla-warn {
  color: var(--status-text-warn);
  background: var(--color-up-unstable-bg);
}

.sla-bad {
  color: var(--status-text-down);
  background: var(--color-down-bg);
}

.sla-unknown {
  color: var(--status-text-unknown);
  background: var(--color-unknown-bg);
}

.btn-inspect {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-decoration: none;
  padding: 0.3rem 0.6rem;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 4px);
  transition: all 0.15s ease;
}

.btn-inspect:hover {
  background: var(--text-primary);
  color: var(--bg-app);
}

.table-empty {
  text-align: center;
  padding: 3rem 1rem !important;
  color: var(--text-muted);
}

.font-mono {
  font-family: var(--font-mono, monospace);
}

.font-bold {
  font-weight: 700;
}

.text-right {
  text-align: right;
}

/* ── Modal Customizer Styles ── */
.modal-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  padding: 1rem;
}

.modal-dialog {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  width: 100%;
  max-width: 820px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 16px 36px rgba(0, 0, 0, 0.5);
  animation: modalIn 0.15s ease-out;
}

@keyframes modalIn {
  from { opacity: 0; transform: scale(0.98); }
  to { opacity: 1; transform: scale(1); }
}

.modal-header {
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.modal-title {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.modal-subtitle {
  font-size: 0.8125rem;
  color: var(--text-muted);
  margin: 0.25rem 0 0 0;
}

.btn-close {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 1.125rem;
  cursor: pointer;
  padding: 0.25rem;
  line-height: 1;
}

.btn-close:hover {
  color: var(--text-primary);
}

.modal-body {
  padding: 1.25rem 1.5rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.form-section {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.section-label {
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}

.radio-options {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.radio-label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.875rem;
  color: var(--text-primary);
  cursor: pointer;
}

.target-picker-box {
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 6px);
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 180px;
}

.target-picker-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border-color);
}

.target-count {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}

.target-list {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.target-item {
  padding: 0.25rem 0.375rem;
  border-radius: 4px;
}

.target-item:hover {
  background: var(--bg-surface-selected);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  color: var(--text-primary);
  cursor: pointer;
  width: 100%;
}

.target-name {
  color: var(--text-primary);
}

.target-ip {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.mt-2 {
  margin-top: 0.5rem;
}

.column-customizer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.25rem;
}

.column-quick-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.btn-text-action {
  background: transparent;
  border: none;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-primary, #3b82f6);
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.btn-text-action:hover {
  color: var(--color-primary-hover, #60a5fa);
}

.action-divider {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.column-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.5rem;
}

.column-custom-item {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.5rem 0.75rem;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  user-select: none;
  transition: border-color 0.15s, background-color 0.15s;
}

.column-custom-item:hover:not(.locked) {
  border-color: var(--color-primary, #3b82f6);
}

.column-custom-item.active {
  border-color: var(--border-color-strong, #52525b);
}

.column-custom-item.locked {
  background: rgba(255, 255, 255, 0.03);
  cursor: not-allowed;
  opacity: 0.85;
}

.column-item-check {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 1rem;
}

.locked-icon {
  font-size: 0.75rem;
}

.column-item-label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--text-primary);
  flex: 1;
}

.locked-badge {
  font-size: 0.625rem;
  font-weight: 700;
  padding: 0.125rem 0.375rem;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--text-muted);
  letter-spacing: 0.05em;
}

.modal-footer {
  padding: 1rem 1.5rem;
  border-top: 1px solid var(--border-color);
  display: flex;
  justify-content: flex-end;
  gap: 0.75rem;
}

.modal-dialog-lg {
  max-width: 840px;
}

.btn-pdf-export {
  background: linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);
  border-color: #4f46e5;
}

.btn-pdf-export:hover:not(:disabled) {
  background: linear-gradient(135deg, #4338ca 0%, #2563eb 100%);
}

.template-cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.875rem;
  margin-top: 0.5rem;
}

.template-card {
  padding: 1rem;
  background: var(--bg-surface-selected, rgba(255, 255, 255, 0.02));
  border: 1px solid var(--border-color, #27272a);
  border-radius: var(--radius-md, 6px);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  transition: all 0.2s ease;
  user-select: none;
}

.template-card:hover {
  border-color: var(--color-primary, #3b82f6);
  background: rgba(59, 130, 246, 0.03);
}

.template-card.active {
  border-color: #6366f1;
  background: rgba(99, 102, 241, 0.08);
  box-shadow: 0 0 0 1px #6366f1;
}

.template-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.template-badge {
  font-size: 0.6875rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 0.125rem 0.5rem;
  border-radius: 4px;
}

.badge-avail {
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
}

.badge-bw {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
}

.badge-master {
  background: rgba(168, 85, 247, 0.15);
  color: #c084fc;
}

.template-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
  line-height: 1.3;
}

.template-card-desc {
  font-size: 0.75rem;
  color: var(--text-secondary);
  line-height: 1.4;
  margin: 0;
  flex: 1;
}

.template-features {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.6875rem;
  color: var(--text-muted, #71717a);
  margin-top: 0.25rem;
  padding-top: 0.5rem;
  border-top: 1px solid var(--border-color, #27272a);
}
</style>
