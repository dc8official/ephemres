<template>
  <div id="app">
    <Toast />
    <ConfirmDialog />
    <header class="app-header" v-if="showNav">
      <div class="header-inner">
        <div class="brand">
          <span class="brand-name">Ephemres</span>
          <span class="brand-version">{{ appVersion || 'v3.3.2.v' }}</span>
        </div>
        <nav class="header-nav" aria-label="Main Navigation">
          <RouterLink to="/" class="nav-link">Dashboard</RouterLink>
          <RouterLink to="/topology" class="nav-link">Topology Map</RouterLink>
          <RouterLink to="/reports" class="nav-link">Reports & SLA</RouterLink>
          <RouterLink to="/bandwidth" class="nav-link">Bandwidth</RouterLink>
          <RouterLink to="/settings" class="nav-link" v-if="isAdmin">Admin Settings</RouterLink>
        </nav>
        <div class="header-actions">
          <button 
            class="theme-toggle" 
            @click="toggleTheme"
            :title="isDark ? 'Switch to light mode' : 'Switch to dark mode'"
            :aria-label="isDark ? 'Switch to light mode' : 'Switch to dark mode'"
          >
            {{ isDark ? '☀' : '☾' }}
          </button>
          <span class="user-badge" v-if="currentUser">
            {{ currentUser }} <span class="role-tag" v-if="isAdmin">(ADMIN)</span>
          </span>
          <button class="btn-sign-out" @click="handleLogout">
            Sign Out
          </button>
        </div>
      </div>
    </header>

    <!-- Accessible live region for real-time announcements -->
    <div class="sr-only" aria-live="polite" aria-atomic="true">
      {{ liveAnnouncement }}
    </div>

    <main class="app-main">
      <RouterView />
    </main>

    <!-- Forced Password Change Modal -->
    <div v-if="displayPasswordModal" class="modal-overlay">
      <div class="modal-card">
        <div class="modal-header">
          <h3>Initial Setup — Password Reset Required</h3>
        </div>
        <form @submit.prevent="executeChangePassword" class="modal-form">
          <div class="alert-info warning-alert">
            For security reasons, you are required to change your default or temporary password before continuing.
          </div>
          
          <div v-if="changePasswordError" class="alert-error" role="alert">
            {{ changePasswordError }}
          </div>

          <div class="form-group">
            <label for="current-password">Current Password *</label>
            <input 
              id="current-password"
              name="current-password"
              type="password"
              autocomplete="current-password"
              v-model="changePasswordForm.old_password" 
              placeholder="Enter current password" 
              required 
              :disabled="changePasswordLoading"
            />
          </div>
          <div class="form-group">
            <label for="new-password">New Password *</label>
            <input 
              id="new-password"
              name="new-password"
              type="password"
              autocomplete="new-password"
              v-model="changePasswordForm.new_password" 
              placeholder="Enter new password (min 8 chars)" 
              required 
              :disabled="changePasswordLoading"
            />
          </div>
          <div class="form-group">
            <label for="confirm-password">Confirm New Password *</label>
            <input 
              id="confirm-password"
              name="confirm-password"
              type="password"
              autocomplete="new-password"
              v-model="changePasswordForm.confirm_password" 
              placeholder="Confirm new password" 
              required 
              :disabled="changePasswordLoading"
            />
          </div>
          <div class="modal-actions">
            <button type="submit" class="btn-primary full-width-btn" :disabled="changePasswordLoading">
              {{ changePasswordLoading ? 'Updating...' : 'Update Password & Sign In' }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute, RouterLink, RouterView } from 'vue-router'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'
import { useToast } from 'primevue/usetoast'
import { logout, changePassword, getVersion } from './services/api.js'
import { currentUser, isAdmin, mustChangePassword, loadUserFromStorage, setUserState, clearUserState } from './services/auth.js'
import { useSSE } from './composables/useSSE.js'

const router = useRouter()
const route = useRoute()
const toast = useToast()
const isDark = ref(true)
const appVersion = ref('v3.3.2.v')
const liveAnnouncement = ref('')
const { subscribe } = useSSE()
let unsubscribeSSE = null

const noNavRoutes = ['/login', '/change-password']
const showNav = computed(() => !noNavRoutes.includes(route.path))

const displayPasswordModal = computed(() => {
  return !!currentUser.value && !!mustChangePassword.value && route.path !== '/login'
})

const changePasswordLoading = ref(false)
const changePasswordError = ref(null)
const changePasswordForm = ref({
  old_password: '',
  new_password: '',
  confirm_password: ''
})

onMounted(async () => {
  const saved = localStorage.getItem('theme') || 'dark'
  if (saved === 'dark') {
    isDark.value = true
    document.documentElement.classList.add('dark')
  } else {
    isDark.value = false
    document.documentElement.classList.remove('dark')
  }
  
  loadUserFromStorage()

  try {
    const res = await getVersion()
    if (res.data?.data?.version) {
      appVersion.value = `v${res.data.data.version.replace(/^v/, '')}`
    }
  } catch (err) {
    appVersion.value = 'v3.3.2.v'
  }

  // Global SSE listener for accessibility screen reader announcements
  unsubscribeSSE = subscribe((e) => {
    if (!e.data) return
    try {
      const payload = JSON.parse(e.data)
      if (payload.type === 'STATE_TRANSITION') {
        const state = payload.detailed_state || payload.operational_state || payload.current_state || 'UPDATED'
        liveAnnouncement.value = `Network update: Endpoint state transitioned to ${state}`
      } else if (payload.type === 'NODE_STATE_CHANGE') {
        const state = payload.new_state || payload.operational_state || payload.current_state || 'UPDATED'
        liveAnnouncement.value = `Topology update: Node status is now ${state}`
      }
    } catch (err) {}
  })
})

onUnmounted(() => {
  if (unsubscribeSSE) unsubscribeSSE()
})

function toggleTheme() {
  isDark.value = !isDark.value
  if (isDark.value) {
    document.documentElement.classList.add('dark')
    localStorage.setItem('theme', 'dark')
  } else {
    document.documentElement.classList.remove('dark')
    localStorage.setItem('theme', 'light')
  }
}

async function executeChangePassword() {
  if (changePasswordForm.value.new_password.length < 8) {
    changePasswordError.value = 'New password must be at least 8 characters long.'
    return
  }
  if (changePasswordForm.value.new_password !== changePasswordForm.value.confirm_password) {
    changePasswordError.value = 'New password and confirmation do not match.'
    return
  }

  changePasswordLoading.value = true
  changePasswordError.value = null
  try {
    await changePassword({
      old_password: changePasswordForm.value.old_password,
      new_password: changePasswordForm.value.new_password
    })
    
    const existing = loadUserFromStorage() || {}
    setUserState({
      username: existing.username || '',
      role: existing.role || '',
      must_change_password: false
    })

    changePasswordForm.value = {
      old_password: '',
      new_password: '',
      confirm_password: ''
    }

    toast.add({
      severity: 'success',
      summary: 'Password Updated',
      detail: 'Password updated successfully! You now have full access to the platform.',
      life: 3500,
    })
  } catch (err) {
    console.error('Failed to change password:', err)
    changePasswordError.value = err.response?.data?.detail || 'Failed to update password. Verify current password.'
  } finally {
    changePasswordLoading.value = false
  }
}

async function handleLogout() {
  try { 
    await logout() 
  } catch (err) {
    console.error('Logout error:', err)
  }
  clearUserState()
  router.push('/login')
}
</script>

<style>
/* ── Reset ── */
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; padding: 0; }
a { text-decoration: none; color: inherit; }
button { cursor: pointer; border: none; background: none; }

/* ── Screen Reader Live Announcement ── */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}

/* ── CSS Variables Design System ── */
:root {
  --font-display: 'Exo 2', var(--font-sans);
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;

  --bg-app: #f5f5f5;
  --bg-surface: #ffffff;
  --bg-surface-hover: #f9f9f9;
  --bg-surface-selected: #f0f0f0;
  --border-color: #d0d0d0;
  --border-color-strong: #c0c0c0;
  --text-primary: #111111;
  --text-secondary: #444444;
  --text-muted: #666666;
  --text-inverse: #ffffff;
  --accent: #111111;
  --accent-hover: #333333;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  --shadow-hover: 0 4px 12px rgba(0, 0, 0, 0.12);
  --radius-sm: 4px;
  --radius: 8px;
  --radius-lg: 12px;
  --radius-full: 9999px;

  --text-xs: 0.75rem;     /* 12px */
  --text-sm: 0.8125rem;   /* 13px */
  --text-base: 0.875rem;  /* 14px */
  --text-lg: 1rem;        /* 16px */
  --text-xl: 1.15rem;     /* 18px */
  --text-2xl: 1.5rem;     /* 24px */
  
  /* Semantic Status Variables */
  --status-up-color: #16a34a;
  --status-warn-color: #b45309;
  --status-down-color: #dc2626;
  
  /* Status Colors */
  --color-up: #16a34a;
  --color-up-bg: rgba(22, 163, 74, 0.1);
  --color-up-unstable: #d97706;
  --color-up-unstable-bg: rgba(217, 119, 6, 0.1);
  --color-down-unstable: #ea580c;
  --color-down-unstable-bg: rgba(234, 88, 12, 0.1);
  --color-down: #dc2626;
  --color-down-bg: rgba(220, 38, 38, 0.1);
  --color-unknown: #6b7280;
  --color-unknown-bg: rgba(107, 114, 128, 0.1);

  --canvas-bg: var(--bg-app);
  --card-bg: var(--bg-surface);
  --card-border: var(--border-color);

  /* ── Network Topology & Canvas Design System Tokens (Light Mode Default) ── */
  --topo-bg: var(--bg-surface);
  --topo-border: var(--border-color);

  --topo-node-root-bg: #2563eb;
  --topo-node-root-border: #1d4ed8;
  --topo-node-root-text: #ffffff;

  --topo-node-subnet-bg: #eef2ff;
  --topo-node-subnet-border: #6366f1;
  --topo-node-subnet-text: #1e1b4b;

  --topo-node-transit-bg: #f1f5f9;
  --topo-node-transit-border: #64748b;
  --topo-node-transit-text: #0f172a;

  --topo-node-up-bg: #ecfdf5;
  --topo-node-up-border: #16a34a;
  --topo-node-up-text: #064e3b;

  --topo-node-unstable-bg: #fffbeb;
  --topo-node-unstable-border: #d97706;
  --topo-node-unstable-text: #78350f;

  --topo-node-down-bg: #fef2f2;
  --topo-node-down-border: #dc2626;
  --topo-node-down-text: #7f1d1d;

  --topo-node-failure-bg: #fee2e2;
  --topo-node-failure-border: #dc2626;
  --topo-node-failure-text: #7f1d1d;

  --topo-node-inferred-bg: #fef2f2;
  --topo-node-inferred-border: #ef4444;
  --topo-node-inferred-text: #991b1b;

  --topo-edge-stroke: #94a3b8;
  --topo-edge-highlight: #2563eb;
}

/* ── Focus Outlines for Keyboard Accessibility (WCAG 2.1 AA) ── */
:focus-visible {
  outline: 2px solid #2563EB;
  outline-offset: 2px;
}
html.dark :focus-visible {
  outline: 2px solid #60A5FA;
  outline-offset: 2px;
}

html.dark {
  --bg-app: #0d0d0d;
  --bg-surface: #161616;
  --bg-surface-hover: #1f1f1f;
  --bg-surface-selected: #262626;
  --border-color: #333333;
  --border-color-strong: #444444;
  --text-primary: #f0f0f0;
  --text-secondary: #b5b5b5;
  --text-muted: #808080;
  --text-inverse: #111111;
  --accent: #f0f0f0;
  --accent-hover: #cccccc;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
  --shadow-hover: 0 4px 12px rgba(0, 0, 0, 0.6);
  
  --status-up-color: #4ade80;
  --status-warn-color: #f59e0b;
  --status-down-color: #f87171;
  --color-unknown: #808080;
  --color-unknown-bg: rgba(128, 128, 128, 0.15);

  /* Status Colors (Dark Mode Calibrated) */
  --color-up: #4ade80;
  --color-up-bg: rgba(74, 222, 128, 0.15);
  --color-up-unstable: #f59e0b;
  --color-up-unstable-bg: rgba(245, 158, 11, 0.15);
  --color-down-unstable: #fb923c;
  --color-down-unstable-bg: rgba(251, 146, 60, 0.15);
  --color-down: #f87171;
  --color-down-bg: rgba(248, 113, 113, 0.15);

  /* ── Network Topology & Canvas Design System Tokens (Dark Mode) ── */
  --topo-bg: var(--bg-surface);
  --topo-border: var(--border-color);

  --topo-node-root-bg: #1d4ed8;
  --topo-node-root-border: #3b82f6;
  --topo-node-root-text: #ffffff;

  --topo-node-subnet-bg: #1e293b;
  --topo-node-subnet-border: #6366f1;
  --topo-node-subnet-text: #e0e7ff;

  --topo-node-transit-bg: #374151;
  --topo-node-transit-border: #6b7280;
  --topo-node-transit-text: #f3f4f6;

  --topo-node-up-bg: #064e3b;
  --topo-node-up-border: #10b981;
  --topo-node-up-text: #ecfdf5;

  --topo-node-unstable-bg: #78350f;
  --topo-node-unstable-border: #f59e0b;
  --topo-node-unstable-text: #fef3c7;

  --topo-node-down-bg: #7f1d1d;
  --topo-node-down-border: #ef4444;
  --topo-node-down-text: #fee2e2;

  --topo-node-failure-bg: #991b1b;
  --topo-node-failure-border: #f97316;
  --topo-node-failure-text: #fee2e2;

  --topo-node-inferred-bg: #7f1d1d;
  --topo-node-inferred-border: #dc2626;
  --topo-node-inferred-text: #fca5a5;

  --topo-edge-stroke: #475569;
  --topo-edge-highlight: #60a5fa;
}

/* ── Base ── */
body {
  font-family: var(--font-sans);
  background-color: var(--bg-app);
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.5;
  transition: background-color 0.2s, color 0.2s;
}

/* ── Typography & Headings ── */
h1, h2, h3, h4, h5, h6,
.page-title,
.section-title {
  font-family: var(--font-display);
}

.tnum, table, th, td, .metric-value, .kpi-value, .sla-badge {
  font-variant-numeric: tabular-nums;
}

#app { min-height: 100vh; display: flex; flex-direction: column; }

/* ── Header ── */
.app-header {
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-color);
  position: sticky;
  top: 0;
  z-index: 100;
}

.header-inner {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 24px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 16px;
  color: var(--text-primary);
}

.brand-name {
  font-family: var(--font-display);
  font-size: 1.2rem;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.brand-version {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-muted);
  background: var(--bg-surface-selected);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
}

.header-nav {
  display: flex;
  gap: 4px;
  flex: 1;
}

.nav-link {
  padding: 6px 12px;
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  transition: color 0.15s, background-color 0.15s;
}

.nav-link:hover {
  color: var(--text-primary);
  background: var(--bg-surface-hover);
}

.nav-link.router-link-active {
  color: var(--text-primary);
  font-weight: 600;
  background: var(--bg-surface-selected);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.theme-toggle {
  font-size: 16px;
  padding: 6px 10px;
  border-radius: var(--radius);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  transition: all 0.15s ease;
}

.theme-toggle:hover {
  color: var(--text-primary);
  background: var(--bg-surface-hover);
}

.user-badge {
  font-size: 13px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.role-tag {
  font-size: 11px;
  color: #3B82F6;
  font-weight: 700;
}

.btn-sign-out {
  padding: 6px 12px;
  border-radius: var(--radius);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  transition: all 0.15s ease;
}

.btn-sign-out:hover {
  color: var(--text-primary);
  background: var(--bg-surface-hover);
}

/* ── Main Layout ── */
.app-main {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  width: 100%;
  flex: 1;
  box-sizing: border-box;
}

/* ── Unified Status Pill System (Inherited by all cards, tables, and views) ── */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border-radius: var(--radius-sm, 4px);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-family: var(--font-mono, monospace);
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

/* Status States */
.status-up {
  background: var(--color-up-bg);
  color: var(--color-up);
  border: 1px solid rgba(22, 163, 74, 0.35);
}
.status-up .status-dot {
  background: var(--color-up);
  box-shadow: 0 0 6px rgba(22, 163, 74, 0.7);
}

.status-unstable {
  background: var(--color-up-unstable-bg);
  color: var(--color-up-unstable);
  border: 1px solid rgba(217, 119, 6, 0.35);
}
.status-unstable .status-dot {
  background: var(--color-up-unstable);
  box-shadow: 0 0 6px rgba(217, 119, 6, 0.7);
}

.status-down {
  background: var(--color-down-bg);
  color: var(--color-down);
  border: 1px solid rgba(220, 38, 38, 0.35);
}
.status-down .status-dot {
  background: var(--color-down);
  box-shadow: 0 0 6px rgba(220, 38, 38, 0.7);
}

.status-unknown {
  background: var(--color-unknown-bg);
  color: var(--color-unknown);
  border: 1px solid rgba(107, 114, 128, 0.35);
}
.status-unknown .status-dot {
  background: var(--color-unknown);
}

/* ── Centralized Button System ── */
.btn-primary {
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid transparent;
  font-weight: 600;
  font-size: 0.8125rem;
  padding: 0.5rem 0.875rem;
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.375rem;
  transition: opacity 0.15s ease, transform 0.15s ease;
  font-family: var(--font-sans);
}
.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
  transform: translateY(-1px);
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  background: transparent;
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  font-weight: 600;
  font-size: 0.8125rem;
  padding: 0.5rem 0.875rem;
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.375rem;
  transition: background-color 0.15s ease, border-color 0.15s ease;
  font-family: var(--font-sans);
}
.btn-secondary:hover:not(:disabled) {
  background: var(--bg-surface-hover);
  border-color: var(--border-color-strong);
}

.btn-danger {
  background: var(--color-down);
  color: #ffffff;
  border: none;
  font-weight: 600;
  font-size: 0.8125rem;
  padding: 0.5rem 0.875rem;
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  transition: opacity 0.15s ease;
}
.btn-danger:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-small {
  padding: 0.3rem 0.6rem;
  font-size: 0.75rem;
}

/* ── Centralized Data Table System ── */
.table-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  overflow: hidden;
  box-shadow: var(--shadow);
}

.table-responsive {
  width: 100%;
  overflow-x: auto;
}

.dense-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 0.8125rem;
}

.dense-table th {
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  font-weight: 600;
  padding: 0.625rem 0.875rem;
  border-bottom: 1px solid var(--border-color);
  text-transform: uppercase;
  font-size: 0.6875rem;
  letter-spacing: 0.05em;
  user-select: none;
}

.dense-table td {
  padding: 0.625rem 0.875rem;
  border-bottom: 1px solid var(--border-color);
  color: var(--text-primary);
  vertical-align: middle;
}

.dense-table tr:last-child td {
  border-bottom: none;
}

.clickable-row {
  cursor: pointer;
  transition: background 0.1s ease;
}

.clickable-row:hover td {
  background: var(--bg-surface-hover);
}

.row-selected td {
  background: var(--bg-surface-selected);
}

.sortable-th {
  cursor: pointer;
}
.sortable-th:hover {
  color: var(--text-primary);
}

/* ── Centralized Modal System ── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1000;
  padding: 1rem;
}

.modal-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  box-shadow: var(--shadow-hover);
  width: 100%;
  max-width: 520px;
  max-height: 90vh;
  overflow-y: auto;
  padding: 1.5rem;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1.25rem;
}

.modal-header h3 {
  margin: 0;
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--text-primary);
}

.modal-form {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.form-input, .form-select, input[type="text"], input[type="password"] {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  font-family: inherit;
}

.form-input:focus, .form-select:focus, input:focus {
  border-color: var(--accent);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.625rem;
  margin-top: 1.5rem;
}

.btn-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0.25rem;
  border-radius: var(--radius-sm, 4px);
}
.btn-close:hover {
  color: var(--text-primary);
}

.full-width-btn {
  width: 100%;
}

.tnum {
  font-feature-settings: "tnum";
  font-variant-numeric: tabular-nums;
}
</style>
