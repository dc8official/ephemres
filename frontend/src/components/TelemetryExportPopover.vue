<template>
  <div class="export-popover-container">
    <button 
      class="btn-export" 
      @click="open = !open" 
      :title="'Export telemetry data slice'"
      aria-haspopup="dialog"
      :aria-expanded="open"
    >
      📥 Export Telemetry
    </button>

    <div v-if="open" class="export-dropdown" role="dialog" aria-label="Export Telemetry">
      <div class="dropdown-header">
        <h4 class="dropdown-title">Export Telemetry Slice</h4>
        <button class="btn-dropdown-close" @click="open = false">✕</button>
      </div>

      <div class="dropdown-body">
        <!-- Format Selection -->
        <div class="control-group">
          <label class="control-label">Export Format</label>
          <div class="toggle-group">
            <button 
              class="toggle-btn" 
              :class="{ active: format === 'csv' }" 
              @click="format = 'csv'"
            >
              CSV Spreadsheet
            </button>
            <button 
              class="toggle-btn" 
              :class="{ active: format === 'json' }" 
              @click="format = 'json'"
            >
              JSON Data
            </button>
          </div>
        </div>

        <!-- Time Range Selection -->
        <div class="control-group">
          <label class="control-label">Time Window</label>
          <select v-model="timeRange" class="select-input">
            <option value="1h">Last 1 Hour</option>
            <option value="6h">Last 6 Hours</option>
            <option value="24h">Last 24 Hours</option>
            <option value="7d">Last 7 Days</option>
          </select>
        </div>

        <!-- Scope -->
        <div class="control-group">
          <label class="control-label">Scope</label>
          <div class="scope-info">
            {{ endpoints?.length || 0 }} endpoints in current view
          </div>
        </div>
      </div>

      <div class="dropdown-footer">
        <button 
          class="btn-execute-export" 
          @click="executeExport" 
          :disabled="exporting || !endpoints || endpoints.length === 0"
        >
          {{ exporting ? 'Generating Export...' : `Download ${format.toUpperCase()}` }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
  endpoints: {
    type: Array,
    default: () => []
  }
})

const open = ref(false)
const format = ref('csv')
const timeRange = ref('24h')
const exporting = ref(false)

function executeExport() {
  exporting.value = true
  try {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
    const filename = `ephemres-telemetry-${timeRange.value}-${timestamp}.${format.value}`

    let content = ''
    let mimeType = ''

    if (format.value === 'json') {
      mimeType = 'application/json'
      const exportData = {
        exported_at: new Date().toISOString(),
        time_range: timeRange.value,
        total_endpoints: props.endpoints.length,
        endpoints: props.endpoints.map(ep => ({
          id: ep.id,
          hostname: ep.hostname,
          ip_address: ep.ip_address,
          status: ep.current_detailed_state || ep.current_operational_state,
          health_score: ep.current_health_score,
          uptime_percentage: ep.uptime_percentage,
          avg_rtt_ms: ep.avg_rtt_ms,
          last_seen: ep.last_seen
        }))
      }
      content = JSON.stringify(exportData, null, 2)
    } else {
      mimeType = 'text/csv;charset=utf-8;'
      const headers = ['Hostname', 'IP_Address', 'Status', 'Health_Score', 'Uptime_Percent', 'Avg_RTT_ms', 'Last_Seen']
      const rows = props.endpoints.map(ep => [
        `"${ep.hostname || ''}"`,
        `"${ep.ip_address || ''}"`,
        `"${ep.current_detailed_state || ep.current_operational_state || ''}"`,
        ep.current_health_score !== undefined ? ep.current_health_score : '',
        ep.uptime_percentage !== undefined ? ep.uptime_percentage : '',
        ep.avg_rtt_ms !== undefined ? ep.avg_rtt_ms : '',
        `"${ep.last_seen || ''}"`
      ])
      content = [headers.join(','), ...rows.map(r => r.join(','))].join('\n')
    }

    const blob = new Blob([content], { type: mimeType })
    const link = document.createElement('a')
    const url = URL.createObjectURL(blob)
    link.setAttribute('href', url)
    link.setAttribute('download', filename)
    link.style.visibility = 'hidden'
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)

    open.value = false
  } catch (err) {
    console.error('Export error:', err)
  } finally {
    exporting.value = false
  }
}
</script>

<style scoped>
.export-popover-container {
  position: relative;
  display: inline-block;
}

.btn-export {
  background: var(--bg-surface);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  padding: 0.45rem 0.85rem;
  border-radius: var(--radius-sm, 6px);
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  transition: all 0.15s ease;
}

.btn-export:hover {
  background: var(--bg-surface-hover);
  border-color: var(--border-color-strong);
}

.export-dropdown {
  position: absolute;
  top: calc(100% + 0.5rem);
  right: 0;
  width: 300px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  box-shadow: var(--shadow-hover);
  z-index: 50;
  padding: 1rem;
  color: var(--text-primary);
}

.dropdown-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border-color);
}

.dropdown-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--text-primary);
}

.btn-dropdown-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 1rem;
}

.dropdown-body {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.control-group {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.control-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.toggle-group {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.35rem;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  padding: 0.2rem;
  border-radius: var(--radius-sm, 6px);
}

.toggle-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.35rem 0.5rem;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}

.toggle-btn.active {
  background: var(--accent);
  color: var(--text-inverse);
}

.select-input {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 0.45rem 0.6rem;
  border-radius: var(--radius-sm, 6px);
  font-size: 0.85rem;
  outline: none;
}

.select-input:focus {
  border-color: var(--border-color-strong);
}

.scope-info {
  font-size: 0.8rem;
  color: var(--text-muted);
  font-feature-settings: 'tnum';
}

.dropdown-footer {
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--border-color);
}

.btn-execute-export {
  width: 100%;
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  padding: 0.5rem 0.8rem;
  border-radius: var(--radius-sm, 6px);
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
  transition: opacity 0.15s;
}

.btn-execute-export:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-execute-export:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
