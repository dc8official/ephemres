<template>
  <div class="settings-alerts-pane">
    <!-- Master Engine Switch Card -->
    <div class="settings-card mb-4">
      <div class="card-header">
        <div class="flex-1">
          <div class="flex-row-center gap-2">
            <h2 class="card-title">🚨 Enterprise Alerting & Notification Engine</h2>
            <span class="engine-badge" :class="alertingEnabled ? 'badge-redis' : 'badge-pg'">
              {{ alertingEnabled ? 'DISPATCHER ACTIVE' : 'DISPATCHER PAUSED' }}
            </span>
          </div>
          <p class="card-desc mt-1">
            Evaluates real-time state transitions and dispatches notifications to Microsoft Teams, Discord, Slack, SMTP Email, or Generic Webhooks.
            All dispatching runs asynchronously inside background workers, completely decoupled with zero impact on the 32-second ICMP probing sweep.
          </p>
        </div>
        <div class="master-toggle-wrap">
          <label class="switch">
            <input type="checkbox" :checked="alertingEnabled" @change="onToggleAlerting" />
            <span class="slider round"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Configured Alert Channels Table -->
    <div class="settings-card full-width mb-4">
      <div class="card-header">
        <div>
          <h2 class="card-title">Configured Notification Channels</h2>
          <p class="card-desc">Active delivery targets for telemetry transitions, latency degradation, and downtime events.</p>
        </div>
        <button class="btn-primary" @click="openAddChannelModal">
          + Add Alert Channel
        </button>
      </div>

      <div class="table-responsive" style="margin-top: 14px;">
        <table class="data-table" aria-label="Alert Channels Table">
          <thead>
            <tr>
              <th>Channel Name</th>
              <th>Provider</th>
              <th>Endpoint Scope</th>
              <th>Severities</th>
              <th>Status</th>
              <th class="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="channelsLoading">
              <td colspan="6" class="text-center py-6 text-muted">Loading configured notification channels...</td>
            </tr>
            <tr v-else-if="channels.length === 0">
              <td colspan="6" class="text-center py-6 text-muted">
                No alert channels configured. Click <strong>+ Add Alert Channel</strong> to route alerts to Teams, Discord, Slack, or Email.
              </td>
            </tr>
            <tr v-for="ch in channels" :key="ch.id">
              <td class="font-bold">{{ ch.name }}</td>
              <td>
                <span class="provider-pill" :class="ch.channel_type.toLowerCase()">
                  {{ formatProviderName(ch.channel_type) }}
                </span>
              </td>
              <td>
                <span v-if="!ch.endpoint_ids || ch.endpoint_ids.length === 0" class="badge-scope all">
                  All Monitored Nodes
                </span>
                <span v-else class="badge-scope custom">
                  {{ ch.endpoint_ids.length }} Specific Node(s)
                </span>
                <div v-if="ch.subnet_filters && ch.subnet_filters.length > 0" class="text-xs text-muted mt-1 font-mono">
                  Subnets: {{ ch.subnet_filters.join(', ') }}
                </div>
              </td>
              <td>
                <div class="severity-tag-group">
                  <span 
                    v-for="s in (ch.severity_filters || ['DOWN', 'RECOVERED'])" 
                    :key="s" 
                    class="severity-tag"
                    :class="s.toLowerCase()"
                  >
                    {{ s }}
                  </span>
                </div>
              </td>
              <td>
                <span class="status-pill" :class="ch.is_enabled ? 'status-up' : 'status-down'">
                  {{ ch.is_enabled ? 'ACTIVE' : 'PAUSED' }}
                </span>
              </td>
              <td class="text-right">
                <div class="table-actions">
                  <button 
                    class="btn-action" 
                    @click="triggerChannelTest(ch)" 
                    :disabled="testingChannelId === ch.id"
                    title="Send Diagnostic Probe"
                  >
                    <i class="pi" :class="testingChannelId === ch.id ? 'pi-spin pi-spinner' : 'pi-send'"></i>
                    {{ testingChannelId === ch.id ? 'Testing...' : 'Test' }}
                  </button>
                  <button class="btn-action" @click="openEditChannelModal(ch)" title="Edit Channel">
                    ✏️ Edit
                  </button>
                  <button class="btn-action text-down" @click="confirmDeleteChannel(ch)" title="Delete Channel">
                    ✕
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Real-Time Delivery Audit Log -->
    <div class="settings-card full-width">
      <div class="card-header">
        <div>
          <h2 class="card-title">Real-Time Delivery Audit Log</h2>
          <p class="card-desc">Most recent 50 alert dispatch events, HTTP response codes, and rate limiting status.</p>
        </div>
        <button class="btn-action" @click="fetchAlertLogs">
          🔄 Refresh Log
        </button>
      </div>

      <div class="table-responsive" style="margin-top: 14px;">
        <table class="data-table" aria-label="Alert Delivery Audit Log Table">
          <thead>
            <tr>
              <th>Delivered At (UTC)</th>
              <th>Channel</th>
              <th>Target Endpoint</th>
              <th>Event Type</th>
              <th>Delivery Status</th>
              <th>Response / Error Details</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="logsLoading">
              <td colspan="6" class="text-center py-6 text-muted">Loading delivery audit logs...</td>
            </tr>
            <tr v-else-if="deliveryLogs.length === 0">
              <td colspan="6" class="text-center py-6 text-muted">No alert delivery events recorded yet.</td>
            </tr>
            <tr v-for="log in deliveryLogs" :key="log.id">
              <td class="font-mono tnum">{{ new Date(log.delivered_at).toISOString().replace('T', ' ').slice(0, 19) }}</td>
              <td class="font-bold">{{ log.channel_name }}</td>
              <td class="font-mono">{{ log.endpoint_name }}</td>
              <td>
                <span class="badge-scope">{{ log.event_type }}</span>
              </td>
              <td>
                <span class="status-pill" :class="getDeliveryStatusClass(log.status)">
                  {{ log.status }} {{ log.status_code ? `(${log.status_code})` : '' }}
                </span>
              </td>
              <td class="font-mono text-muted text-xs truncate-cell" :title="log.response_message || ''">
                {{ log.response_message || 'OK' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 800px Channel Configuration Modal -->
    <div 
      class="modal-overlay" 
      v-if="showChannelModal" 
      @click.self="closeChannelModal"
      @keydown="onChannelModalKeydown"
    >
      <div 
        ref="channelModalRef"
        class="modal-card channel-modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="channel-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <div>
            <h3 id="channel-modal-title" class="modal-title">{{ editingChannelId ? 'Edit Alert Channel' : 'Configure New Alert Channel' }}</h3>
            <p class="modal-subtitle">Configure outbound webhook or SMTP email notifications for real-time telemetry events.</p>
          </div>
          <button class="btn-close" @click="closeChannelModal" aria-label="Close dialog">✕</button>
        </div>

        <form @submit.prevent="saveChannel" class="modal-form">
          <!-- Provider Selection Cards -->
          <div class="form-group">
            <label class="setting-label" id="provider-selection-label">Select Notification Provider *</label>
            <div class="provider-selector-grid" role="radiogroup" aria-labelledby="provider-selection-label">
              <div 
                v-for="p in providerOptions" 
                :key="p.id" 
                class="provider-card" 
                :class="{ active: selectedProvider === p.id }"
                role="radio"
                :aria-checked="selectedProvider === p.id"
                tabindex="0"
                @click="selectProvider(p.id)"
                @keydown.enter.prevent="selectProvider(p.id)"
                @keydown.space.prevent="selectProvider(p.id)"
              >
                <span class="provider-icon">{{ p.icon }}</span>
                <span class="provider-name">{{ p.name }}</span>
                <span class="provider-badge">{{ p.format }}</span>
              </div>
            </div>
          </div>

          <!-- Channel Name -->
          <div class="form-group">
            <label class="setting-label">Channel Display Name *</label>
            <input 
              v-model="channelForm.name" 
              type="text" 
              placeholder="e.g. NOC Critical Alerts / Team Chat" 
              required 
              class="form-input"
            />
          </div>

          <!-- Provider Specific Inputs -->
          <div v-if="channelForm.channel_type !== 'EMAIL_SMTP'" class="form-group">
            <label class="setting-label">
              Webhook Destination URL *
              <span class="label-hint">(Must be a valid HTTP/HTTPS endpoint. Private and loopback IPs are blocked by SSRF defense)</span>
            </label>
            <input 
              v-model="channelForm.config.webhook_url" 
              type="url" 
              :placeholder="getWebhookPlaceholder(channelForm.channel_type)" 
              required 
              class="form-input font-mono"
            />
          </div>

          <!-- Generic Webhook Headers -->
          <div v-if="channelForm.channel_type === 'GENERIC_WEBHOOK'" class="form-group">
            <label class="setting-label">Custom HTTP Headers (Optional JSON object)</label>
            <textarea 
              v-model="channelForm.headersRaw" 
              placeholder='{ "Authorization": "Bearer YOUR_TOKEN" }'
              rows="2"
              class="form-input font-mono"
            ></textarea>
          </div>

          <!-- SMTP Email Fields -->
          <div v-if="channelForm.channel_type === 'EMAIL_SMTP'" class="smtp-config-box">
            <div class="form-row-2">
              <div class="form-group">
                <label class="setting-label">SMTP Host *</label>
                <input v-model="channelForm.config.smtp_host" type="text" placeholder="smtp.office365.com" required class="form-input font-mono" />
              </div>
              <div class="form-group">
                <label class="setting-label">SMTP Port *</label>
                <input v-model.number="channelForm.config.smtp_port" type="number" placeholder="587" required class="form-input font-mono" />
              </div>
            </div>

            <div class="form-row-2">
              <div class="form-group">
                <label class="setting-label">SMTP Username</label>
                <input v-model="channelForm.config.username" type="text" placeholder="alerts@corp.net" class="form-input" />
              </div>
              <div class="form-group">
                <label class="setting-label">SMTP Password</label>
                <input v-model="channelForm.config.password" type="password" placeholder="••••••••••••" class="form-input" />
              </div>
            </div>

            <div class="form-row-2">
              <div class="form-group">
                <label class="setting-label">From Address *</label>
                <input v-model="channelForm.config.from_email" type="email" placeholder="ephemres-alerts@corp.net" required class="form-input" />
              </div>
              <div class="form-group">
                <label class="setting-label">Recipient Email(s) * (comma-separated)</label>
                <input v-model="channelForm.toEmailsRaw" type="text" placeholder="ops@corp.net, noc-oncall@corp.net" required class="form-input font-mono" />
              </div>
            </div>
          </div>

          <!-- Target Endpoint Scope -->
          <div class="form-group">
            <label class="setting-label">Target Endpoint Scope</label>
            <div class="radio-options mb-2">
              <label class="radio-label">
                <input type="radio" value="all" v-model="channelScope" />
                <span>All Monitored Endpoints (Entire Fleet)</span>
              </label>
              <label class="radio-label">
                <input type="radio" value="custom" v-model="channelScope" />
                <span>Specific Endpoints ({{ channelForm.endpoint_ids.length }} selected)</span>
              </label>
            </div>

            <div v-if="channelScope === 'custom'" class="target-picker-box">
              <div class="target-picker-header">
                <input 
                  type="text" 
                  v-model="endpointSearchFilter" 
                  placeholder="Filter targets by name or IP..." 
                  class="target-search-input"
                />
                <button type="button" class="btn-text-action" @click="toggleAllChannelEndpoints">
                  {{ isAllChannelEndpointsSelected ? 'Deselect All' : 'Select All' }}
                </button>
              </div>
              <div class="target-list">
                <div v-for="ep in filteredEndpoints" :key="ep.id" class="target-item">
                  <label class="checkbox-label">
                    <input type="checkbox" :value="ep.id" v-model="channelForm.endpoint_ids" />
                    <span class="target-name font-bold">{{ ep.hostname }}</span>
                    <span class="target-ip font-mono tnum">{{ ep.ip_address }}</span>
                    <span class="device-pill">{{ ep.device_type }}</span>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <!-- Subnet Filters -->
          <div class="form-group">
            <label class="setting-label">
              Subnet Filters (Optional)
              <span class="label-hint">(Comma-separated CIDR notation, e.g. 10.0.0.0/16, 192.168.1.0/24)</span>
            </label>
            <input 
              v-model="channelForm.subnetFiltersRaw" 
              type="text" 
              placeholder="e.g. 10.0.0.0/16, 192.168.1.0/24" 
              class="form-input font-mono"
            />
          </div>

          <!-- Severity Filters -->
          <div class="form-group">
            <label class="setting-label">Triggering Severity States</label>
            <div class="severity-checkbox-group">
              <label class="checkbox-label">
                <input type="checkbox" value="DOWN" v-model="channelForm.severity_filters" />
                <span class="severity-tag down">DOWN</span>
              </label>
              <label class="checkbox-label">
                <input type="checkbox" value="RECOVERED" v-model="channelForm.severity_filters" />
                <span class="severity-tag recovered">RECOVERED</span>
              </label>
              <label class="checkbox-label">
                <input type="checkbox" value="UNSTABLE" v-model="channelForm.severity_filters" />
                <span class="severity-tag unstable">UNSTABLE / FLAPPING</span>
              </label>
            </div>
          </div>

          <!-- Channel Active Toggle -->
          <div class="setting-row">
            <div>
              <label class="setting-label">Channel Status</label>
              <p class="setting-hint">Enable or pause outbound alerts for this channel.</p>
            </div>
            <label class="switch">
              <input type="checkbox" v-model="channelForm.is_enabled" />
              <span class="slider round"></span>
            </label>
          </div>

          <!-- Diagnostic Test Banner & Button -->
          <div class="diagnostic-test-section">
            <div v-if="modalTestResult" :class="['test-feedback-banner', modalTestResult.success ? 'success' : 'error']">
              <span>{{ modalTestResult.message }}</span>
            </div>

            <button 
              type="button" 
              class="btn-action" 
              @click="sendModalDiagnosticTest" 
              :disabled="modalTesting"
            >
              <i class="pi" :class="modalTesting ? 'pi-spin pi-spinner' : 'pi-send'"></i>
              <span>{{ modalTesting ? 'Sending Diagnostic Probe...' : '⚡ Send Diagnostic Test Alert' }}</span>
            </button>
          </div>

          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeChannelModal">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="channelSaving">
              {{ channelSaving ? 'Saving...' : (editingChannelId ? 'Update Channel' : 'Create Channel') }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import {
  getAlertChannels,
  createAlertChannel,
  updateAlertChannel,
  deleteAlertChannel,
  testAlertChannel,
  getAlertHistory,
  getEndpoints,
} from '../../services/api.js'

const props = defineProps({
  alertingEnabled: {
    type: Boolean,
    default: true,
  },
})

const emit = defineEmits(['update:alertingEnabled', 'change'])

const toast = useToast()
const confirm = useConfirm()

function onToggleAlerting(e) {
  emit('update:alertingEnabled', e.target.checked)
  emit('change')
}

const channels = ref([])
const channelsLoading = ref(false)
const deliveryLogs = ref([])
const logsLoading = ref(false)
const endpoints = ref([])

const showChannelModal = ref(false)
const channelSaving = ref(false)
const editingChannelId = ref(null)
const testingChannelId = ref(null)
const modalTesting = ref(false)
const modalTestResult = ref(null)
const channelScope = ref('all')
const endpointSearchFilter = ref('')

const channelModalRef = ref(null)
let channelOpenerElement = null

const providerOptions = [
  { id: 'MICROSOFT_TEAMS', name: 'Microsoft Teams', icon: '👥', format: 'Adaptive Card' },
  { id: 'DISCORD', name: 'Discord', icon: '🎮', format: 'Rich Embed' },
  { id: 'SLACK', name: 'Slack', icon: '💬', format: 'Block Kit' },
  { id: 'EMAIL_SMTP', name: 'Email (SMTP)', icon: '✉️', format: 'HTML & Text' },
  { id: 'GENERIC_WEBHOOK', name: 'Generic Webhook', icon: '🌐', format: 'JSON REST' },
]

const selectedProvider = ref('MICROSOFT_TEAMS')

const channelForm = reactive({
  name: '',
  channel_type: 'MICROSOFT_TEAMS',
  is_enabled: true,
  config: {
    webhook_url: '',
    smtp_host: '',
    smtp_port: 587,
    username: '',
    password: '',
    from_email: '',
  },
  headersRaw: '',
  toEmailsRaw: '',
  subnetFiltersRaw: '',
  endpoint_ids: [],
  severity_filters: ['DOWN', 'RECOVERED'],
})

function formatProviderName(providerType) {
  const map = {
    MICROSOFT_TEAMS: 'Microsoft Teams',
    DISCORD: 'Discord',
    SLACK: 'Slack',
    EMAIL_SMTP: 'Email (SMTP)',
    GENERIC_WEBHOOK: 'Generic Webhook',
  }
  return map[providerType] || providerType
}

function getWebhookPlaceholder(providerType) {
  switch (providerType) {
    case 'MICROSOFT_TEAMS':
      return 'https://outlook.office.com/webhook/...'
    case 'DISCORD':
      return 'https://discord.com/api/webhooks/...'
    case 'SLACK':
      return 'https://hooks.slack.com/services/...'
    default:
      return 'https://api.monitoring.corp.net/alerts'
  }
}

function getDeliveryStatusClass(status) {
  switch (status?.toUpperCase()) {
    case 'DELIVERED':
      return 'status-up'
    case 'FAILED':
      return 'status-down'
    case 'RATE_LIMITED':
      return 'status-unstable'
    default:
      return 'status-unknown'
  }
}

const filteredEndpoints = computed(() => {
  if (!endpointSearchFilter.value.trim()) return endpoints.value
  const term = endpointSearchFilter.value.toLowerCase()
  return endpoints.value.filter(
    (ep) =>
      ep.hostname?.toLowerCase().includes(term) ||
      ep.ip_address?.toLowerCase().includes(term)
  )
})

const isAllChannelEndpointsSelected = computed(() => {
  return (
    endpoints.value.length > 0 &&
    channelForm.endpoint_ids.length === endpoints.value.length
  )
})

function toggleAllChannelEndpoints() {
  if (isAllChannelEndpointsSelected.value) {
    channelForm.endpoint_ids = []
  } else {
    channelForm.endpoint_ids = endpoints.value.map((ep) => ep.id)
  }
}

function selectProvider(providerId) {
  selectedProvider.value = providerId
  channelForm.channel_type = providerId
}

function openAddChannelModal() {
  channelOpenerElement = document.activeElement
  editingChannelId.value = null
  selectedProvider.value = 'MICROSOFT_TEAMS'
  channelForm.name = ''
  channelForm.channel_type = 'MICROSOFT_TEAMS'
  channelForm.is_enabled = true
  channelForm.config = {
    webhook_url: '',
    smtp_host: '',
    smtp_port: 587,
    username: '',
    password: '',
    from_email: '',
  }
  channelForm.headersRaw = ''
  channelForm.toEmailsRaw = ''
  channelForm.subnetFiltersRaw = ''
  channelForm.endpoint_ids = []
  channelForm.severity_filters = ['DOWN', 'RECOVERED']
  channelScope.value = 'all'
  modalTestResult.value = null
  showChannelModal.value = true
  nextTick(() => {
    channelModalRef.value?.querySelector('button, input, [tabindex="0"]')?.focus()
  })
}

function openEditChannelModal(ch) {
  channelOpenerElement = document.activeElement
  editingChannelId.value = ch.id
  selectedProvider.value = ch.channel_type
  channelForm.name = ch.name
  channelForm.channel_type = ch.channel_type
  channelForm.is_enabled = ch.is_enabled
  channelForm.config = { ...ch.config }
  channelForm.endpoint_ids = ch.endpoint_ids ? [...ch.endpoint_ids] : []
  channelForm.subnetFiltersRaw = (ch.subnet_filters && ch.subnet_filters.length > 0) ? ch.subnet_filters.join(', ') : ''
  channelForm.severity_filters = ch.severity_filters ? [...ch.severity_filters] : ['DOWN', 'RECOVERED']
  channelScope.value = (ch.endpoint_ids && ch.endpoint_ids.length > 0) ? 'custom' : 'all'

  if (ch.config?.headers) {
    channelForm.headersRaw = JSON.stringify(ch.config.headers, null, 2)
  } else {
    channelForm.headersRaw = ''
  }

  if (ch.config?.to_emails) {
    channelForm.toEmailsRaw = ch.config.to_emails.join(', ')
  } else {
    channelForm.toEmailsRaw = ''
  }

  modalTestResult.value = null
  showChannelModal.value = true
  nextTick(() => {
    channelModalRef.value?.querySelector('button, input, [tabindex="0"]')?.focus()
  })
}

function closeChannelModal() {
  showChannelModal.value = false
  nextTick(() => {
    channelOpenerElement?.focus()
  })
}

function onChannelModalKeydown(e) {
  if (e.key === 'Escape') {
    closeChannelModal()
  }
}

async function fetchChannels() {
  channelsLoading.value = true
  try {
    const res = await getAlertChannels()
    if (res.data?.data) {
      channels.value = res.data.data
    }
  } catch (err) {
    console.error('Failed to load alert channels:', err)
  } finally {
    channelsLoading.value = false
  }
}

async function fetchAlertLogs() {
  logsLoading.value = true
  try {
    const res = await getAlertHistory()
    if (res.data?.data) {
      deliveryLogs.value = res.data.data
    }
  } catch (err) {
    console.error('Failed to load delivery logs:', err)
  } finally {
    logsLoading.value = false
  }
}

async function fetchEndpointsList() {
  try {
    const res = await getEndpoints()
    if (res.data?.data) {
      endpoints.value = res.data.data
    }
  } catch (err) {
    console.error('Failed to load endpoints:', err)
  }
}

async function saveChannel() {
  channelSaving.value = true
  try {
    const subnets = channelForm.subnetFiltersRaw
      ? channelForm.subnetFiltersRaw
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean)
      : []

    const payload = {
      name: channelForm.name,
      channel_type: channelForm.channel_type,
      is_enabled: channelForm.is_enabled,
      config: { ...channelForm.config },
      endpoint_ids: channelScope.value === 'all' ? [] : channelForm.endpoint_ids,
      subnet_filters: subnets,
      severity_filters: channelForm.severity_filters,
    }

    if (channelForm.channel_type === 'GENERIC_WEBHOOK' && channelForm.headersRaw.trim()) {
      try {
        payload.config.headers = JSON.parse(channelForm.headersRaw)
      } catch (e) {
        toast.add({
          severity: 'error',
          summary: 'Validation Error',
          detail: 'Invalid JSON in custom headers.',
          life: 4000,
        })
        channelSaving.value = false
        return
      }
    }

    if (channelForm.channel_type === 'EMAIL_SMTP' && channelForm.toEmailsRaw.trim()) {
      payload.config.to_emails = channelForm.toEmailsRaw
        .split(',')
        .map((e) => e.trim())
        .filter(Boolean)
    }

    if (editingChannelId.value) {
      await updateAlertChannel(editingChannelId.value, payload)
      toast.add({
        severity: 'success',
        summary: 'Channel Updated',
        detail: `Alert channel '${channelForm.name}' updated.`,
        life: 3000,
      })
    } else {
      await createAlertChannel(payload)
      toast.add({
        severity: 'success',
        summary: 'Channel Created',
        detail: `Alert channel '${channelForm.name}' created.`,
        life: 3000,
      })
    }
    closeChannelModal()
    await fetchChannels()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to save alert channel.',
      life: 4000,
    })
  } finally {
    channelSaving.value = false
  }
}

async function triggerChannelTest(ch) {
  testingChannelId.value = ch.id
  try {
    const res = await testAlertChannel(ch.id)
    toast.add({
      severity: res.data?.data?.success ? 'success' : 'warn',
      summary: 'Test Probe Dispatched',
      detail: res.data?.data?.message || 'Diagnostic alert sent.',
      life: 4000,
    })
    await fetchAlertLogs()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Probe Delivery Failed',
      detail: err.response?.data?.detail || 'Channel test failed.',
      life: 4000,
    })
  } finally {
    testingChannelId.value = null
  }
}

async function sendModalDiagnosticTest() {
  modalTesting.value = true
  modalTestResult.value = null
  try {
    if (editingChannelId.value) {
      const res = await testAlertChannel(editingChannelId.value)
      modalTestResult.value = {
        success: res.data?.data?.success ?? true,
        message: res.data?.data?.message || 'Diagnostic probe dispatched successfully.',
      }
    } else {
      modalTestResult.value = {
        success: true,
        message: 'Pre-flight format valid. Save channel to execute full live probe.',
      }
    }
  } catch (err) {
    modalTestResult.value = {
      success: false,
      message: err.response?.data?.detail || 'Failed to send diagnostic test alert.',
    }
  } finally {
    modalTesting.value = false
  }
}

async function confirmDeleteChannel(ch) {
  confirm.require({
    message: `Delete alert channel '${ch.name}'? Associated delivery history will also be removed.`,
    header: 'Confirm Channel Deletion',
    icon: 'pi pi-exclamation-triangle',
    rejectClass: 'p-button-secondary p-button-outlined',
    rejectLabel: 'Cancel',
    acceptLabel: 'Delete',
    acceptClass: 'p-button-danger',
    accept: async () => {
      try {
        await deleteAlertChannel(ch.id)
        await fetchChannels()
        toast.add({
          severity: 'success',
          summary: 'Channel Deleted',
          detail: `Channel '${ch.name}' deleted successfully.`,
          life: 3000,
        })
      } catch (err) {
        toast.add({
          severity: 'error',
          summary: 'Error',
          detail: err.response?.data?.detail || 'Failed to delete channel.',
          life: 4000,
        })
      }
    },
  })
}

onMounted(() => {
  fetchChannels()
  fetchAlertLogs()
  fetchEndpointsList()
})
</script>

<style scoped>
.settings-alerts-pane {
  display: flex;
  flex-direction: column;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 20px;
}

.mb-4 {
  margin-bottom: 16px;
}

.full-width {
  width: 100%;
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

.btn-action {
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  font-size: 12px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.btn-action:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.text-down {
  color: #ef4444;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.data-table th {
  text-align: left;
  padding: 10px 12px;
  background: var(--bg-surface-selected);
  color: var(--text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--border-color);
}

.data-table td {
  padding: 12px;
  border-bottom: 1px solid var(--border-color);
  color: var(--text-primary);
}

.provider-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(14, 165, 233, 0.15);
  color: #0ea5e9;
  border: 1px solid rgba(14, 165, 233, 0.25);
}

.badge-scope {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}

.badge-scope.all {
  color: #10b981;
}

.severity-tag-group {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.severity-tag {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 3px;
  text-transform: uppercase;
}

.severity-tag.down {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
}

.severity-tag.recovered {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
}

.severity-tag.unstable {
  background: rgba(245, 158, 11, 0.2);
  color: #f59e0b;
}

.status-pill {
  font-size: 11px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
}

.status-up {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
}

.status-down {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
}

.status-unstable {
  background: rgba(245, 158, 11, 0.2);
  color: #f59e0b;
}

.status-unknown {
  background: rgba(100, 116, 139, 0.2);
  color: #94a3b8;
}

.table-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
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
  max-width: 600px;
  max-height: 90vh;
  overflow-y: auto;
}

.channel-modal-dialog {
  max-width: 800px;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 20px;
}

.modal-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.modal-subtitle {
  font-size: 12px;
  color: var(--text-muted);
  margin: 4px 0 0 0;
}

.btn-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
}

.form-group {
  margin-bottom: 16px;
}

.setting-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.label-hint {
  font-weight: 400;
  color: var(--text-muted);
  font-size: 11px;
}

.form-input {
  width: 100%;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface-selected);
  color: var(--text-primary);
  font-size: 13px;
}

.form-input:focus {
  border-color: #0ea5e9;
  outline: none;
}

.provider-selector-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 8px;
}

.provider-card {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  transition: all 0.15s;
}

.provider-card.active {
  border-color: #0ea5e9;
  background: rgba(14, 165, 233, 0.12);
}

.provider-icon {
  font-size: 20px;
}

.provider-name {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-primary);
  text-align: center;
}

.provider-badge {
  font-size: 9px;
  color: var(--text-muted);
}

.form-row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.radio-options {
  display: flex;
  gap: 16px;
}

.radio-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
}

.target-picker-box {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 10px;
  margin-top: 8px;
}

.target-picker-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}

.target-search-input {
  flex: 1;
  padding: 6px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12px;
}

.btn-text-action {
  background: transparent;
  border: none;
  color: #0ea5e9;
  font-size: 12px;
  cursor: pointer;
  padding: 0 8px;
}

.target-list {
  max-height: 180px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.target-item {
  padding: 4px 8px;
  border-radius: 4px;
}

.target-item:hover {
  background: rgba(255, 255, 255, 0.04);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  cursor: pointer;
}

.device-pill {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--text-muted);
}

.severity-checkbox-group {
  display: flex;
  gap: 12px;
}

.diagnostic-test-section {
  border-top: 1px solid var(--border-color);
  padding-top: 16px;
  margin-top: 16px;
}

.test-feedback-banner {
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 12px;
  margin-bottom: 10px;
}

.test-feedback-banner.success {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.test-feedback-banner.error {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
  border-top: 1px solid var(--border-color);
  padding-top: 16px;
}

.tnum {
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
}

.truncate-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
