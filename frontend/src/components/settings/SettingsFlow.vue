<template>
  <div class="settings-flow-pane">
    <!-- Performance & Caching Engine Card -->
    <div class="settings-card">
      <div class="card-header">
        <h2 class="card-title">⚡ Performance & Caching Engine</h2>
        <span class="engine-badge" :class="settings.performanceMode ? 'badge-faststore' : 'badge-pg'">
          {{ settings.performanceMode ? 'FASTSTORE ACCELERATED' : 'POSTGRESQL NATIVE' }}
        </span>
      </div>
      <p class="card-desc">
        Accelerate session lookups and real-time event broadcasting using in-memory FastStore caching, with zero-downtime PostgreSQL fallback.
      </p>

      <div class="setting-row">
        <div>
          <label class="setting-label">Memory Acceleration Driver</label>
          <p class="setting-hint">When enabled, user session tokens and pub/sub events are routed through FastStore.</p>
        </div>
        <div class="driver-toggle">
          <button 
            type="button" 
            class="btn-toggle-option" 
            :class="{ active: !settings.performanceMode }"
            @click="setPerformanceMode(false)"
          >
            Standard (PostgreSQL)
          </button>
          <button 
            type="button" 
            class="btn-toggle-option" 
            :class="{ active: settings.performanceMode }"
            @click="setPerformanceMode(true)"
          >
            Accelerated (FastStore)
          </button>
        </div>
      </div>
    </div>

    <!-- Flow Telemetry Ingestion Card -->
    <div class="settings-card mt-4">
      <div class="card-header">
        <div class="flex-1">
          <div class="flex-row-center gap-2">
            <h2 class="card-title">🌊 Network Flow Telemetry Engine (NetFlow & IPFIX)</h2>
            <span class="engine-badge" :class="settings.flowIngestionEnabled ? 'badge-faststore' : 'badge-pg'">
              {{ settings.flowIngestionEnabled ? 'FLOW INGESTION ACTIVE' : 'FLOW INGESTION DISABLED' }}
            </span>
          </div>
          <p class="card-desc mt-1">
            Ingests raw NetFlow v5, NetFlow v9, and IPFIX datagrams into FastStore Stream wire buffers, correlating with monitored endpoints and batching into TimescaleDB rollups.
          </p>
        </div>
        <div class="master-toggle-wrap">
          <label class="switch">
            <input type="checkbox" :checked="settings.flowIngestionEnabled" @change="toggleFlowIngestion" />
            <span class="slider round"></span>
          </label>
        </div>
      </div>

      <!-- Prerequisite Banner when FastStore is disabled -->
      <div v-if="!settings.performanceMode" class="alert-banner alert-warning mt-3" role="alert">
        <div class="flex-row-center gap-2">
          <span>⚠️ FastStore Memory Acceleration is required to buffer raw packet streams. Please activate FastStore first.</span>
        </div>
        <button type="button" class="btn-primary btn-small ml-3" @click="activateFastStoreDriver">
          Activate FastStore Driver
        </button>
      </div>

      <div v-if="settings.flowIngestionEnabled" class="flow-config-fields mt-3">
        <div class="setting-row">
          <div>
            <label class="setting-label">NetFlow Listen Port (v5 / v9)</label>
            <p class="setting-hint">UDP port for Cisco, Fortinet, Mikrotik NetFlow streams.</p>
          </div>
          <input 
            type="number" 
            v-model.number="settings.flowNetflowPort" 
            class="setting-input font-mono tnum" 
            min="1024" 
            max="65535" 
            @input="emitChange"
          />
        </div>

        <div class="setting-row">
          <div>
            <label class="setting-label">IPFIX Listen Port (v10)</label>
            <p class="setting-hint">UDP port for standard RFC 7011 IPFIX / Juniper J-Flow.</p>
          </div>
          <input 
            type="number" 
            v-model.number="settings.flowIpfixPort" 
            class="setting-input font-mono tnum" 
            min="1024" 
            max="65535" 
            @input="emitChange"
          />
        </div>

        <div class="setting-row">
          <div>
            <label class="setting-label">Sampling Multiplier Override</label>
            <p class="setting-hint">Hardware sampling scale factor (1 = 1:1 unsampled, e.g. 1000 for 1:1000 sampling).</p>
          </div>
          <input 
            type="number" 
            v-model.number="settings.flowSamplingMultiplier" 
            class="setting-input font-mono tnum" 
            min="1" 
            max="100000" 
            @input="emitChange"
          />
        </div>

        <div class="setting-row pt-2">
          <div>
            <label class="setting-label">Engine Diagnostic Preflight (STAB-05)</label>
            <p class="setting-hint">Verify FastStore streams, Linux kernel receive buffer (rmem_max >= 2MB), and UDP socket port bindings.</p>
          </div>
          <button type="button" class="btn-action" @click="openPreflightModal">
            🔍 Run Diagnostic Preflight
          </button>
        </div>
      </div>
    </div>

    <!-- Flow Preflight Modal (STAB-05) -->
    <div v-if="showPreflightModal" class="modal-overlay" @click.self="showPreflightModal = false">
      <div class="modal-card">
        <div class="modal-header">
          <h3>⚡ Flow Ingestion Diagnostic Preflight (STAB-05)</h3>
          <button class="btn-close" @click="showPreflightModal = false">✕</button>
        </div>
        <div class="modal-form">
          <div v-if="preflightChecking" class="loading-state p-4 text-center">
            <p class="mt-2 text-sm">Verifying FastStore streams, kernel rmem_max, and UDP socket bindings...</p>
          </div>
          <div v-else>
            <div :class="preflightResult?.ready ? 'alert-info success-alert' : 'alert-error'" role="alert">
              <strong>{{ preflightResult?.ready ? 'Preflight Verification Passed' : 'Prerequisite Check Failed' }}</strong>
              <p class="mt-1 text-sm">{{ preflightResult?.message }}</p>
            </div>

            <div class="preflight-details mt-3 text-sm">
              <div class="preflight-item">
                <span>FastStore Engine Connection: </span>
                <span class="font-bold font-mono" :class="(preflightResult?.faststore_connected ?? preflightResult?.redis_connected) ? 'text-success' : 'text-danger'">
                  {{ (preflightResult?.faststore_connected ?? preflightResult?.redis_connected) ? 'CONNECTED' : 'UNREACHABLE' }}
                </span>
              </div>
              <div class="preflight-item mt-1">
                <span>FastStore Protocol Version: </span>
                <span class="font-bold font-mono">
                  {{ preflightResult?.faststore_version || preflightResult?.redis_version || 'N/A' }} (FastStore WAL)
                </span>
              </div>
              <div class="preflight-item mt-1">
                <span>Wire Buffer Stream Write (XADD): </span>
                <span class="font-bold font-mono" :class="preflightResult?.stream_write_success ? 'text-success' : 'text-danger'">
                  {{ preflightResult?.stream_write_success ? 'SUPPORTED' : 'FAILED' }}
                </span>
              </div>
              <!-- STAB-05 Kernel Socket Buffer -->
              <div class="preflight-item mt-1">
                <span>Kernel Socket Buffer (rmem_max): </span>
                <span class="font-bold font-mono tnum" :class="preflightResult?.rmem_max_supported ? 'text-success' : 'text-warning'">
                  {{ preflightResult?.rmem_max != null ? `${preflightResult.rmem_max} B` : 'Verified' }}
                  {{ preflightResult?.rmem_max_supported ? '(>= 2MB Recommended)' : '(< 2MB Recommended)' }}
                </span>
              </div>
              <!-- STAB-05 UDP Socket Bind -->
              <div class="preflight-item mt-1">
                <span>UDP Socket Availability: </span>
                <span class="font-bold font-mono" :class="preflightResult?.udp_ports_available ? 'text-success' : 'text-danger'">
                  {{ preflightResult?.udp_ports_available ? 'PORTS AVAILABLE' : `CONFLICT ON: ${preflightResult?.port_conflicts?.join(', ')}` }}
                </span>
              </div>
            </div>

            <p v-if="!preflightResult?.ready" class="text-xs text-muted mt-3">
              Flow telemetry buffering requires active FastStore embedded memory storage in <code>/run/ephemres</code> and unbound UDP ports. If needed, verify permissions (<code>sudo chown -R ephemres:ephemres /run/ephemres</code>) and ensure port conflicts are resolved.
            </p>
          </div>

          <div class="modal-actions">
            <button class="btn-secondary" @click="showPreflightModal = false">Close</button>
            <button
              v-if="!preflightResult?.ready"
              class="btn-primary"
              @click="runPreflightCheck"
              :disabled="preflightChecking"
            >
              {{ preflightChecking ? 'Testing...' : 'Retry Preflight' }}
            </button>
            <button
              v-else
              class="btn-primary"
              @click="confirmFlowActivation"
            >
              Activate Flow Telemetry
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import { testFlowPreflight } from '../../services/api.js'

const props = defineProps({
  settings: {
    type: Object,
    required: true,
  },
  saving: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits(['change'])

const toast = useToast()

const showPreflightModal = ref(false)
const preflightChecking = ref(false)
const preflightResult = ref(null)

function emitChange() {
  emit('change')
}

function setPerformanceMode(val) {
  if (!val && props.settings.flowIngestionEnabled) {
    toast.add({
      severity: 'warn',
      summary: 'Action Blocked',
      detail: 'Cannot disable FastStore Memory Acceleration while Flow Ingestion is active. Please disable Flow Ingestion first.',
      life: 5000,
    })
    return
  }
  props.settings.performanceMode = val
  emitChange()
}

async function activateFastStoreDriver() {
  props.settings.performanceMode = true
  emitChange()
  showPreflightModal.value = true
  await runPreflightCheck()
}

async function toggleFlowIngestion(e) {
  const targetState = e.target.checked
  if (targetState) {
    e.target.checked = false
    if (!props.settings.performanceMode) {
      toast.add({
        severity: 'warn',
        summary: 'Prerequisite Required',
        detail: 'Network Flow Telemetry requires FastStore Memory Acceleration. Please activate FastStore first.',
        life: 5000,
      })
      return
    }
    showPreflightModal.value = true
    await runPreflightCheck()
  } else {
    props.settings.flowIngestionEnabled = false
    emitChange()
  }
}

function openPreflightModal() {
  showPreflightModal.value = true
  runPreflightCheck()
}

async function runPreflightCheck() {
  preflightChecking.value = true
  preflightResult.value = null
  try {
    const res = await testFlowPreflight()
    if (res.data?.data) {
      preflightResult.value = res.data.data
    }
  } catch (err) {
    preflightResult.value = {
      redis_connected: false,
      redis_version: null,
      redis_version_supported: false,
      faststore_connected: false,
      faststore_version: null,
      faststore_version_supported: false,
      stream_write_success: false,
      rmem_max_supported: false,
      udp_ports_available: false,
      ready: false,
      message: err.response?.data?.detail || 'Preflight API request failed.',
    }
  } finally {
    preflightChecking.value = false
  }
}

function confirmFlowActivation() {
  props.settings.flowIngestionEnabled = true
  showPreflightModal.value = false
  emitChange()
  toast.add({
    severity: 'success',
    summary: 'Flow Telemetry Activated',
    detail: 'NetFlow & IPFIX ingestion enabled. Remember to save settings.',
    life: 4000,
  })
}
</script>

<style scoped>
.settings-flow-pane {
  display: flex;
  flex-direction: column;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 20px;
}

.mt-4 {
  margin-top: 16px;
}

.mt-3 {
  margin-top: 12px;
}

.mt-1 {
  margin-top: 4px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.card-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.card-desc {
  font-size: 13px;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 4px 0 0 0;
}

.flex-1 {
  flex: 1;
}

.flex-row-center {
  display: flex;
  align-items: center;
}

.gap-2 {
  gap: 8px;
}

.engine-badge {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 4px;
}

.badge-faststore,
.badge-redis {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.badge-pg {
  background: rgba(100, 116, 139, 0.2);
  color: #94a3b8;
  border: 1px solid rgba(100, 116, 139, 0.3);
}

.setting-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 0;
  border-bottom: 1px solid var(--border-color);
}

.setting-row:last-child {
  border-bottom: none;
}

.setting-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 4px 0;
}

.setting-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin: 0;
}

.setting-input {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 13px;
  width: 140px;
  text-align: right;
}

.setting-input:focus {
  border-color: #0ea5e9;
  outline: none;
}

.driver-toggle {
  display: flex;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 3px;
}

.btn-toggle-option {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-toggle-option.active {
  background: #0ea5e9;
  color: #ffffff;
}

.master-toggle-wrap {
  display: flex;
  align-items: center;
}

.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: rgba(255, 255, 255, 0.15);
  transition: 0.2s;
  border-radius: 24px;
}

.slider:before {
  position: absolute;
  content: "";
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background-color: white;
  transition: 0.2s;
  border-radius: 50%;
}

input:checked + .slider {
  background-color: #10b981;
}

input:checked + .slider:before {
  transform: translateX(20px);
}

.alert-banner {
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.alert-warning {
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid rgba(245, 158, 11, 0.3);
  color: #f59e0b;
}

.btn-action {
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  font-size: 12px;
  font-weight: 600;
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
}

.btn-action:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.modal-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color-strong);
  border-radius: 10px;
  padding: 24px;
  width: 100%;
  max-width: 550px;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.modal-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
}

.btn-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
}

.alert-info {
  background: rgba(16, 185, 129, 0.15);
  border: 1px solid rgba(16, 185, 129, 0.3);
  color: #10b981;
  padding: 10px 14px;
  border-radius: 6px;
}

.alert-error {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #ef4444;
  padding: 10px 14px;
  border-radius: 6px;
}

.preflight-details {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 12px;
}

.preflight-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.text-success {
  color: #10b981;
}

.text-danger {
  color: #ef4444;
}

.text-warning {
  color: #f59e0b;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}

.tnum {
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
}
</style>
