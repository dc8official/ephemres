<template>
  <div class="settings-view">
    <!-- Header Toolbar -->
    <div class="view-header">
      <div>
        <h1 class="page-title">Platform Administration & Governance</h1>
        <p class="page-sub">Configure enterprise alerting channels, performance acceleration, security policies, and access control</p>
      </div>
      <div class="header-actions">
        <button 
          v-if="activeTab !== 'alerts' && activeTab !== 'users' && activeTab !== 'organization'" 
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

    <!-- Spacious 4-Tab Navigation Strip with Dirty Badges -->
    <div class="settings-tabs-nav" role="tablist">
      <button 
        type="button" 
        class="tab-btn" 
        :class="{ active: activeTab === 'alerts' }" 
        @click="switchTab('alerts')"
        role="tab"
        :aria-selected="activeTab === 'alerts'"
      >
        <span class="tab-icon">🔔</span>
        <span class="tab-label">Alert Channels</span>
        <span v-if="isAlertsDirty" class="tab-dirty-dot" title="Unsaved changes"></span>
      </button>

      <button 
        type="button" 
        class="tab-btn" 
        :class="{ active: activeTab === 'performance' }" 
        @click="switchTab('performance')"
        role="tab"
        :aria-selected="activeTab === 'performance'"
      >
        <span class="tab-icon">⚡</span>
        <span class="tab-label">Performance & Telemetry</span>
        <span v-if="isFlowDirty" class="tab-dirty-dot" title="Unsaved changes"></span>
      </button>

      <button 
        type="button" 
        class="tab-btn" 
        :class="{ active: activeTab === 'security' }" 
        @click="switchTab('security')"
        role="tab"
        :aria-selected="activeTab === 'security'"
      >
        <span class="tab-icon">🛡️</span>
        <span class="tab-label">Security & Discovery</span>
        <span v-if="isSecurityDirty" class="tab-dirty-dot" title="Unsaved changes"></span>
      </button>

      <button 
        type="button" 
        class="tab-btn" 
        :class="{ active: activeTab === 'users' }" 
        @click="switchTab('users')"
        role="tab"
        :aria-selected="activeTab === 'users'"
      >
        <span class="tab-icon">👥</span>
        <span class="tab-label">User Governance</span>
      </button>

      <button 
        type="button" 
        class="tab-btn" 
        :class="{ active: activeTab === 'organization' }" 
        @click="switchTab('organization')"
        role="tab"
        :aria-selected="activeTab === 'organization'"
      >
        <span class="tab-icon">🏢</span>
        <span class="tab-label">Organization & Branding</span>
      </button>
    </div>

    <!-- Tab Panels Container -->
    <div class="settings-content">
      <!-- TAB 1: Enterprise Alert Channels -->
      <SettingsAlerts 
        v-if="activeTab === 'alerts'" 
        v-model:alertingEnabled="settings.alertingEnabled"
        @change="markDirty"
      />

      <!-- TAB 2: Performance & Telemetry (NetFlow + Cache) -->
      <SettingsFlow 
        v-if="activeTab === 'performance'" 
        :settings="settings"
        :saving="saving"
        @change="markDirty"
      />

      <!-- TAB 3: Security & Discovery -->
      <SettingsSecurity 
        v-if="activeTab === 'security'" 
        :settings="settings"
        @change="markDirty"
      />

      <!-- TAB 4: User Account Governance -->
      <SettingsUsers 
        v-if="activeTab === 'users'" 
        :currentUser="currentUser"
      />

      <!-- TAB 5: Organization Profile & Report Branding -->
      <SettingsOrganization 
        v-if="activeTab === 'organization'" 
      />
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
import SettingsSecurity from '../components/settings/SettingsSecurity.vue'
import SettingsUsers from '../components/settings/SettingsUsers.vue'
import SettingsOrganization from '../components/settings/SettingsOrganization.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()

const activeTab = ref(
  route.path === '/users' ? 'users' : (route.query.tab?.toString() || 'alerts')
)

function switchTab(tab) {
  activeTab.value = tab
  router.replace({ query: { ...route.query, tab } })
}

watch(
  () => route.query.tab,
  (newTab) => {
    if (newTab && ['alerts', 'performance', 'security', 'users', 'organization'].includes(newTab)) {
      activeTab.value = newTab
    }
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
})

const savedSettings = ref(null)

function markDirty() {
  // Trigger reactive update if needed
}

const isAlertsDirty = computed(() => {
  if (!savedSettings.value) return false
  return settings.alertingEnabled !== savedSettings.value.alertingEnabled
})

const isFlowDirty = computed(() => {
  if (!savedSettings.value) return false
  return (
    settings.performanceMode !== savedSettings.value.performanceMode ||
    settings.flowIngestionEnabled !== savedSettings.value.flowIngestionEnabled ||
    settings.flowNetflowPort !== savedSettings.value.flowNetflowPort ||
    settings.flowIpfixPort !== savedSettings.value.flowIpfixPort ||
    settings.flowSamplingMultiplier !== savedSettings.value.flowSamplingMultiplier
  )
})

const isSecurityDirty = computed(() => {
  if (!savedSettings.value) return false
  return (
    settings.l2AutoBypass !== savedSettings.value.l2AutoBypass ||
    String(settings.lockoutThreshold) !== String(savedSettings.value.lockoutThreshold)
  )
})

const isAnyDirty = computed(() => {
  return isAlertsDirty.value || isFlowDirty.value || isSecurityDirty.value
})

function discardChanges() {
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
    }

    await updateSettings(payload)
    savedSettings.value = JSON.parse(JSON.stringify(settings))
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
