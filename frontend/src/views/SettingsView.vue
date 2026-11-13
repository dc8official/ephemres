<template>
  <div class="settings-view">
    <!-- Header Toolbar -->
    <div class="view-header">
      <div>
        <h1 class="page-title">Platform Administration & Governance</h1>
        <p class="page-sub">Accounts and sign-in, route discovery, data collection, alerts, and your organization</p>
      </div>
      <div class="header-actions">
        <!-- One save for every plain setting on every tab (v3.3.6 layout). Lists (users, alert
             channels, renames) still save when their own dialog is confirmed. -->
        <button
          class="btn-primary"
          @click="saveAllSettings"
          :disabled="saving || !isAnyDirty"
        >
          {{ saving ? 'Saving...' : (isAnyDirty ? '💾 Save Changes *' : '✓ Saved') }}
        </button>
      </div>
    </div>

    <!-- Alert / Toast Banner -->
    <div v-if="alertMessage" :class="['alert-banner', alertType]">
      <span>{{ alertMessage }}</span>
      <button class="btn-close" @click="alertMessage = null">✕</button>
    </div>

    <!-- Tabs in the v3.3.6 order, agreed with Kenneth on 2026-11-12. A dot marks unsaved changes. -->
    <div class="settings-tabs-nav" role="tablist">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        role="tab"
        :aria-selected="activeTab === tab.key"
        @click="switchTab(tab.key)"
      >
        <span class="tab-icon">{{ tab.icon }}</span>
        <span class="tab-label">{{ tab.label }}</span>
        <span v-if="tabDirty[tab.key]" class="tab-dirty-dot" title="Unsaved changes"></span>
      </button>
    </div>

    <div class="settings-content">
      <SettingsUsers
        v-if="activeTab === 'users'"
        :currentUser="currentUser"
        :settings="settings"
        @change="markDirty"
      />

      <SettingsDiscovery
        v-if="activeTab === 'discovery'"
        :settings="settings"
        @change="markDirty"
      />

      <SettingsFlow
        v-if="activeTab === 'data'"
        :settings="settings"
        :saving="saving"
        @change="markDirty"
      />
      <!-- SNMP poller (v3.3.6): the switch and interval join the shared Save bar; credential
           sets save when their dialog is confirmed, like users and alert channels. -->
      <SettingsSnmp v-if="activeTab === 'data'" :settings="settings" />

      <SettingsAlerts
        v-if="activeTab === 'alerts'"
        v-model:alertingEnabled="settings.alertingEnabled"
        :settings="settings"
        @change="markDirty"
      />

      <!-- Kept mounted (v-show) so unsaved branding survives switching tabs until it is saved
           or discarded from the shared bar. -->
      <div v-show="activeTab === 'organization'">
        <SettingsOrganization ref="organizationRef" @dirty-change="(v) => (brandingDirty = v)" />
        <SettingsLabels />
      </div>
    </div>

    <!-- Sticky Dirty-State Banner -->
    <transition name="slide-up">
      <div v-if="isAnyDirty" class="sticky-dirty-banner">
        <div class="dirty-banner-inner">
          <div class="dirty-message">
            <span class="dirty-icon">⚠️</span>
            <span>You have unsaved configuration changes.</span>
          </div>
          <div class="dirty-actions">
            <button class="btn-discard" @click="discardChanges" :disabled="saving">
              Discard Changes
            </button>
            <button class="btn-primary" @click="saveAllSettings" :disabled="saving">
              {{ saving ? 'Saving Changes...' : 'Save Changes' }}
            </button>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { getSettings, updateSettings } from '../services/api.js'
import { currentUser, loadUserFromStorage } from '../services/auth.js'

import SettingsAlerts from '../components/settings/SettingsAlerts.vue'
import SettingsFlow from '../components/settings/SettingsFlow.vue'
import SettingsSnmp from '../components/settings/SettingsSnmp.vue'
import SettingsDiscovery from '../components/settings/SettingsDiscovery.vue'
import SettingsUsers from '../components/settings/SettingsUsers.vue'
import SettingsOrganization from '../components/settings/SettingsOrganization.vue'
import SettingsLabels from '../components/settings/SettingsLabels.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()

const TABS = [
  { key: 'users', icon: '👥', label: 'Users & access' },
  { key: 'discovery', icon: '🌐', label: 'Discovery & topology' },
  { key: 'data', icon: '📡', label: 'Data collection' },
  { key: 'alerts', icon: '🔔', label: 'Alerts' },
  { key: 'organization', icon: '🏢', label: 'Organization' },
]
const TAB_KEYS = TABS.map((t) => t.key)
// Links and bookmarks from before v3.3.6 use the old tab names.
const OLD_TAB_KEYS = { performance: 'data', security: 'discovery' }

function tabFromQuery(raw) {
  const key = OLD_TAB_KEYS[raw] || raw
  return TAB_KEYS.includes(key) ? key : null
}

const activeTab = ref(
  route.path === '/users' ? 'users' : (tabFromQuery(route.query.tab?.toString()) || 'users')
)

function switchTab(tab) {
  activeTab.value = tab
  router.replace({ query: { ...route.query, tab } })
}

watch(
  () => route.query.tab,
  (newTab) => {
    const key = tabFromQuery(newTab?.toString())
    if (key) activeTab.value = key
  }
)

const saving = ref(false)
const alertMessage = ref(null)
const alertType = ref('alert-success')

// No sessionTimeout here since v3.3.6: each account has its own idle timeout (Users tab). The
// API still accepts session_timeout for older clients, but nothing reads it any more.
const settings = reactive({
  performanceMode: false,
  l2AutoBypass: true,
  lockoutThreshold: '5',
  alertingEnabled: true,
  flowIngestionEnabled: false,
  flowNetflowPort: 2055,
  flowIpfixPort: 4739,
  flowSamplingMultiplier: 1,
  discoveryParallel: 2,
  rediscoveryHours: 24,
  trafficAlertsEnabled: false,
  trafficAlertThresholdPct: 80,
  trafficAlertMinutes: 15,
  snmpEnabled: false,
  snmpPollSeconds: 60,
})

const savedSettings = ref(null)

function markDirty() {
  // Trigger reactive update if needed
}

const isAlertsDirty = computed(() => {
  if (!savedSettings.value) return false
  return (
    settings.alertingEnabled !== savedSettings.value.alertingEnabled ||
    settings.trafficAlertsEnabled !== savedSettings.value.trafficAlertsEnabled ||
    settings.trafficAlertThresholdPct !== savedSettings.value.trafficAlertThresholdPct ||
    settings.trafficAlertMinutes !== savedSettings.value.trafficAlertMinutes
  )
})

const isFlowDirty = computed(() => {
  if (!savedSettings.value) return false
  return (
    settings.performanceMode !== savedSettings.value.performanceMode ||
    settings.flowIngestionEnabled !== savedSettings.value.flowIngestionEnabled ||
    settings.flowNetflowPort !== savedSettings.value.flowNetflowPort ||
    settings.flowIpfixPort !== savedSettings.value.flowIpfixPort ||
    settings.flowSamplingMultiplier !== savedSettings.value.flowSamplingMultiplier ||
    settings.snmpEnabled !== savedSettings.value.snmpEnabled ||
    settings.snmpPollSeconds !== savedSettings.value.snmpPollSeconds
  )
})

const isUsersDirty = computed(() => {
  if (!savedSettings.value) return false
  return String(settings.lockoutThreshold) !== String(savedSettings.value.lockoutThreshold)
})

const isDiscoveryDirty = computed(() => {
  if (!savedSettings.value) return false
  return (
    settings.l2AutoBypass !== savedSettings.value.l2AutoBypass ||
    settings.discoveryParallel !== savedSettings.value.discoveryParallel ||
    settings.rediscoveryHours !== savedSettings.value.rediscoveryHours
  )
})

// Branding lives in its own component and API (organization settings); it reports here.
const organizationRef = ref(null)
const brandingDirty = ref(false)

const tabDirty = computed(() => ({
  users: isUsersDirty.value,
  discovery: isDiscoveryDirty.value,
  data: isFlowDirty.value,
  alerts: isAlertsDirty.value,
  organization: brandingDirty.value,
}))

const isSettingsDirty = computed(
  () => isAlertsDirty.value || isFlowDirty.value || isUsersDirty.value || isDiscoveryDirty.value
)
const isAnyDirty = computed(() => isSettingsDirty.value || brandingDirty.value)

function discardChanges() {
  if (brandingDirty.value) organizationRef.value?.discard()
  if (!savedSettings.value) return
  Object.assign(settings, JSON.parse(JSON.stringify(savedSettings.value)))
  toast.add({
    severity: 'info',
    summary: 'Changes Discarded',
    detail: 'Settings reverted to saved configuration.',
    life: 3000,
  })
}

// Ask before leaving the page with unsaved changes.
onBeforeRouteLeave((to, from, next) => {
  if (isAnyDirty.value) {
    const confirmLeave = window.confirm(
      'You have unsaved configuration changes. Are you sure you want to discard them and leave this page?'
    )
    if (!confirmLeave) {
      next(false)
      return
    }
  }
  next()
})

async function loadSettings() {
  try {
    const res = await getSettings()
    if (res.data?.data) {
      const d = res.data.data
      settings.performanceMode = Boolean(d.performance_mode ?? d.performanceMode ?? false)
      settings.l2AutoBypass = Boolean(d.l2_auto_bypass ?? d.l2AutoBypass ?? true)
      settings.lockoutThreshold = String(d.lockout_threshold ?? d.lockoutThreshold ?? 5)
      settings.alertingEnabled = Boolean(d.alerting_enabled ?? d.alertingEnabled ?? true)
      settings.flowIngestionEnabled = Boolean(d.flow_ingestion_enabled ?? d.flowIngestionEnabled ?? false)
      settings.flowNetflowPort = Number(d.flow_netflow_port ?? d.flowNetflowPort ?? 2055)
      settings.flowIpfixPort = Number(d.flow_ipfix_port ?? d.flowIpfixPort ?? 4739)
      settings.flowSamplingMultiplier = Number(d.flow_sampling_multiplier ?? d.flowSamplingMultiplier ?? 1)
      settings.discoveryParallel = Number(d.discovery_parallel ?? d.discoveryParallel ?? 2)
      settings.rediscoveryHours = Number(d.rediscovery_hours ?? d.rediscoveryHours ?? 24)
      settings.trafficAlertsEnabled = Boolean(d.traffic_alerts_enabled ?? d.trafficAlertsEnabled ?? false)
      settings.trafficAlertThresholdPct = Number(d.traffic_alert_threshold_pct ?? d.trafficAlertThresholdPct ?? 80)
      settings.trafficAlertMinutes = Number(d.traffic_alert_minutes ?? d.trafficAlertMinutes ?? 15)
      settings.snmpEnabled = Boolean(d.snmp_enabled ?? d.snmpEnabled ?? false)
      settings.snmpPollSeconds = Number(d.snmp_poll_seconds ?? d.snmpPollSeconds ?? 60)

      savedSettings.value = JSON.parse(JSON.stringify(settings))
    }
  } catch (err) {
    console.error('Failed to load system settings:', err)
  }
}

async function saveAllSettings() {
  saving.value = true
  alertMessage.value = null
  try {
    const payload = {
      performance_mode: settings.performanceMode,
      l2_auto_bypass: settings.l2AutoBypass,
      lockout_threshold: parseInt(settings.lockoutThreshold, 10),
      alerting_enabled: settings.alertingEnabled,
      flow_ingestion_enabled: settings.flowIngestionEnabled,
      flow_netflow_port: parseInt(settings.flowNetflowPort, 10),
      flow_ipfix_port: parseInt(settings.flowIpfixPort, 10),
      flow_sampling_multiplier: parseInt(settings.flowSamplingMultiplier, 10),
      discovery_parallel: settings.discoveryParallel,
      rediscovery_hours: settings.rediscoveryHours,
      traffic_alerts_enabled: settings.trafficAlertsEnabled,
      traffic_alert_threshold_pct: settings.trafficAlertThresholdPct,
      traffic_alert_minutes: settings.trafficAlertMinutes,
      snmp_enabled: settings.snmpEnabled,
      snmp_poll_seconds: settings.snmpPollSeconds,
    }

    // Both parts save from the one bar; each only when it has changes.
    if (isSettingsDirty.value) {
      await updateSettings(payload)
      savedSettings.value = JSON.parse(JSON.stringify(settings))
    }
    if (brandingDirty.value) await organizationRef.value?.save()
    alertMessage.value = 'Platform configuration successfully saved and synchronized.'
    alertType.value = 'alert-success'
    toast.add({
      severity: 'success',
      summary: 'Settings Saved',
      detail: 'Platform settings updated and broadcasted across daemons.',
      life: 3000,
    })
  } catch (err) {
    alertMessage.value = err.response?.data?.detail || 'Failed to update system settings.'
    alertType.value = 'alert-error'
    toast.add({
      severity: 'error',
      summary: 'Save Failed',
      detail: err.response?.data?.detail || 'Failed to update settings.',
      life: 5000,
    })
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadUserFromStorage()
  loadSettings()
})
</script>

<style scoped>
.settings-view {
  padding: 24px 32px 80px 32px;
  max-width: 1440px;
  margin: 0 auto;
  position: relative;
}

.view-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 4px 0;
}

.page-sub {
  font-size: 13px;
  color: var(--text-muted);
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.alert-banner {
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.alert-success {
  background: rgba(16, 185, 129, 0.15);
  border: 1px solid rgba(16, 185, 129, 0.3);
  color: #10b981;
}

.alert-error {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #ef4444;
}

.btn-close {
  background: transparent;
  border: none;
  color: inherit;
  font-size: 16px;
  cursor: pointer;
}

/* 4-Tab Navigation Strip */
.settings-tabs-nav {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--border-color);
  margin-bottom: 24px;
}

.tab-btn {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 600;
  padding: 10px 16px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  position: relative;
  transition: all 0.15s ease;
}

.tab-btn:hover {
  color: var(--text-primary);
}

.tab-btn.active {
  color: #0ea5e9;
  border-bottom-color: #0ea5e9;
}

.tab-dirty-dot {
  width: 8px;
  height: 8px;
  background-color: #f59e0b;
  border-radius: 50%;
  display: inline-block;
  box-shadow: 0 0 6px rgba(245, 158, 11, 0.6);
}

.settings-content {
  margin-top: 16px;
}

.btn-discard {
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  font-weight: 600;
  font-size: 13px;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}

.btn-discard:hover:not(:disabled) {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

/* Sticky Bottom Dirty State Banner */
.sticky-dirty-banner {
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 999;
  width: calc(100% - 64px);
  max-width: 900px;
}

.dirty-banner-inner {
  background: var(--bg-surface);
  border: 1px solid #f59e0b;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4), 0 0 12px rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  padding: 12px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.dirty-message {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.dirty-actions {
  display: flex;
  gap: 10px;
}

.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.25s ease-out;
}

.slide-up-enter-from,
.slide-up-leave-to {
  opacity: 0;
  transform: translate(-50%, 20px);
}
</style>
