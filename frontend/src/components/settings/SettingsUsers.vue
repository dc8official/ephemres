<template>
  <div class="settings-users-pane">
    <div class="settings-card full-width">
      <div class="card-header">
        <div>
          <h2 class="card-title">👥 User Account Governance</h2>
          <p class="card-desc">Manage user credentials, system access roles (ADMIN / VIEWER), and status.</p>
        </div>
        <button class="btn-primary btn-small" @click="openAddUserModal">
          + Add User Account
        </button>
      </div>

      <div class="table-responsive" style="margin-top: 12px;">
        <table class="data-table" aria-label="User Accounts Table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th>Status</th>
              <th>Credential State</th>
              <th>Session</th>
              <th>Last Active Sign-in</th>
              <th class="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="usersLoading">
              <td colspan="7" class="text-center py-6 text-muted">Loading user accounts...</td>
            </tr>
            <tr v-for="u in users" :key="u.id">
              <td class="font-bold">{{ u.username }}</td>
              <td>
                <span class="role-badge" :class="u.role.toLowerCase()">
                  {{ u.role }}
                </span>
              </td>
              <td>
                <span class="status-pill" :class="u.is_active ? 'status-up' : 'status-down'">
                  {{ u.is_active ? 'ACTIVE' : 'DISABLED' }}
                </span>
              </td>
              <td>
                <span v-if="u.must_change_password" class="text-unstable font-bold">
                  ⚡ Reset Pending
                </span>
                <span v-else class="text-up font-bold">
                  ✓ Secure
                </span>
              </td>
              <!-- Idle timeout and session limit of this account (v3.3.6). -->
              <td class="session-cell">
                <span class="tnum">{{ formatMinutes(u.session_idle_minutes) }} idle</span>
                <!-- "1 of 2 sessions": in use against the limit; opens the session list. -->
                <button
                  v-if="u.active_sessions !== null && u.active_sessions !== undefined"
                  type="button"
                  class="session-count tnum"
                  :title="`Show ${u.username}'s sessions`"
                  @click="openSessionsModal(u)"
                >
                  {{ u.active_sessions }} of {{ u.max_sessions }} {{ u.max_sessions === 1 ? 'session' : 'sessions' }}
                </button>
                <span v-else class="session-limit tnum">
                  {{ u.max_sessions }} {{ u.max_sessions === 1 ? 'session' : 'sessions' }}
                </span>
              </td>
              <td class="font-mono tnum">
                {{ u.last_login ? new Date(u.last_login).toLocaleString() : 'Never' }}
              </td>
              <td class="text-right">
                <div class="table-actions">
                  <button class="btn-action" @click="openEditModal(u)" title="Edit role and session settings">✎ Edit</button>
                  <button class="btn-action" @click="openResetPasswordModal(u)" title="Reset Password">🔑 Reset</button>
                  <button 
                    v-if="u.username !== currentUser" 
                    class="btn-action" 
                    @click="toggleUserStatus(u)"
                  >
                    {{ u.is_active ? '🚫 Disable' : '✅ Enable' }}
                  </button>
                  <button 
                    v-if="u.username !== currentUser" 
                    class="btn-action text-down" 
                    @click="confirmDeleteUser(u)"
                  >
                    ✕
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add User Modal -->
    <div 
      class="modal-overlay" 
      v-if="showAddModal" 
      @click.self="closeAddModal"
      @keydown="onAddModalKeydown"
    >
      <div 
        ref="addModalRef"
        class="modal-card" 
        role="dialog" 
        aria-modal="true" 
        aria-labelledby="add-user-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <h3 id="add-user-modal-title">Register New User Account</h3>
          <button class="btn-close" @click="closeAddModal" aria-label="Close dialog">✕</button>
        </div>
        <form @submit.prevent="saveNewUser" class="modal-form">
          <div class="form-group">
            <label>Username *</label>
            <input v-model="userForm.username" type="text" placeholder="username" required class="form-input" />
          </div>
          <div class="form-group">
            <label>Temporary Password</label>
            <input v-model="userForm.password" type="password" placeholder="Leave blank to auto-generate" class="form-input" />
          </div>
          <div class="form-group">
            <label>Account Role *</label>
            <select v-model="userForm.role" class="form-select" @change="fitIdleToRole(userForm)">
              <option value="VIEWER">VIEWER (Read-Only Access)</option>
              <option value="ADMIN">ADMIN (Full Administrative Control)</option>
            </select>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="add-session-idle">Session Timeout</label>
              <select id="add-session-idle" v-model.number="userForm.session_idle_minutes" class="form-select">
                <option v-for="m in idleChoices(userForm.role)" :key="m" :value="m">
                  {{ formatMinutes(m) }}{{ m === defaultIdle(userForm.role) ? ' (default)' : '' }}
                </option>
              </select>
            </div>
            <div class="form-group">
              <label for="add-max-sessions">Session Limit</label>
              <select id="add-max-sessions" v-model.number="userForm.max_sessions" class="form-select">
                <option v-for="n in sessionLimitChoices" :key="n" :value="n">
                  {{ n }} {{ n === 1 ? 'session' : 'sessions' }}
                </option>
              </select>
            </div>
          </div>
          <p class="form-hint">{{ sessionHint(userForm.role) }}</p>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeAddModal">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="userSaving">
              {{ userSaving ? 'Creating...' : 'Register User' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Edit User Modal: role, session timeout and session limit. -->
    <div
      class="modal-overlay"
      v-if="showEditModal"
      @click.self="closeEditModal"
      @keydown="onEditModalKeydown"
    >
      <div
        ref="editModalRef"
        class="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="edit-user-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <h3 id="edit-user-modal-title">Edit {{ targetUser?.username }}</h3>
          <button class="btn-close" @click="closeEditModal" aria-label="Close dialog">✕</button>
        </div>
        <form @submit.prevent="saveEditUser" class="modal-form">
          <div class="form-group">
            <label for="edit-role">Account Role</label>
            <!-- An admin can't change their own role (the API refuses it too). -->
            <select
              id="edit-role"
              v-model="editForm.role"
              class="form-select"
              :disabled="isSelf(targetUser)"
              @change="fitIdleToRole(editForm)"
            >
              <option value="VIEWER">VIEWER (Read-Only Access)</option>
              <option value="ADMIN">ADMIN (Full Administrative Control)</option>
            </select>
            <p v-if="isSelf(targetUser)" class="form-hint">You can't change the role of your own account.</p>
            <p v-else-if="editForm.role !== targetUser?.role" class="form-hint warn">
              Changing the role signs {{ targetUser?.username }} out everywhere.
            </p>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="edit-session-idle">Session Timeout</label>
              <select id="edit-session-idle" v-model.number="editForm.session_idle_minutes" class="form-select">
                <option v-for="m in idleChoices(editForm.role)" :key="m" :value="m">
                  {{ formatMinutes(m) }}{{ m === defaultIdle(editForm.role) ? ' (default)' : '' }}
                </option>
              </select>
            </div>
            <div class="form-group">
              <label for="edit-max-sessions">Session Limit</label>
              <select id="edit-max-sessions" v-model.number="editForm.max_sessions" class="form-select">
                <option v-for="n in sessionLimitChoices" :key="n" :value="n">
                  {{ n }} {{ n === 1 ? 'session' : 'sessions' }}
                </option>
              </select>
            </div>
          </div>
          <p class="form-hint">{{ sessionHint(editForm.role) }} A new timeout applies at once; a new limit from the next sign-in.</p>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeEditModal">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="userSaving">
              {{ userSaving ? 'Saving...' : 'Save Changes' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Sessions Modal: the account's live sessions, with End per session and End all. -->
    <div
      class="modal-overlay"
      v-if="showSessionsModal"
      @click.self="closeSessionsModal"
      @keydown="onSessionsModalKeydown"
    >
      <div
        ref="sessionsModalRef"
        class="modal-card modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sessions-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <h3 id="sessions-modal-title">Sessions of {{ targetUser?.username }}</h3>
          <button class="btn-close" @click="closeSessionsModal" aria-label="Close dialog">✕</button>
        </div>
        <p v-if="sessionsLoading" class="form-hint">Loading sessions...</p>
        <p v-else-if="!sessions.length" class="form-hint">No active sessions.</p>
        <ul v-else class="session-list">
          <li v-for="sess in sessions" :key="sess.id" class="session-row">
            <div class="session-main">
              <span class="session-device" :title="sess.user_agent || ''">
                {{ describeDevice(sess.user_agent) }}
                <span v-if="sess.is_current" class="current-tag">This browser</span>
              </span>
              <span class="session-meta tnum">
                {{ sess.client_ip || 'Address not recorded' }} · signed in {{ formatWhen(sess.created_at) }}
              </span>
              <span class="session-meta tnum">
                Last active {{ formatAgo(sess.last_activity_at) }} · ends {{ formatUntil(sess.ends_at) }} if left alone
              </span>
            </div>
            <button
              v-if="!sess.is_current"
              type="button"
              class="btn-action text-down"
              :disabled="sessionEnding === sess.id"
              @click="endOneSession(sess)"
            >
              {{ sessionEnding === sess.id ? 'Ending...' : 'End' }}
            </button>
          </li>
        </ul>
        <!-- "End all" confirms in place: a second dialog stacked on this one had no backdrop
             of its own and its text ran over this list. -->
        <div v-if="confirmingEndAll" class="modal-actions confirm-row" role="alert">
          <span class="confirm-text">
            {{ isSelf(targetUser)
              ? 'Sign out every other device using your account? This browser stays signed in.'
              : `Sign out every device using '${targetUser?.username}'?` }}
          </span>
          <button type="button" class="btn-secondary" @click="confirmingEndAll = false">Cancel</button>
          <button type="button" class="btn-danger" :disabled="endingAll" @click="endAllSessionsNow">
            {{ endingAll ? 'Ending...' : 'End all' }}
          </button>
        </div>
        <div v-else class="modal-actions">
          <button
            v-if="sessions.some((x) => !x.is_current)"
            type="button"
            class="btn-secondary text-down end-all"
            @click="confirmingEndAll = true"
          >
            End all sessions
          </button>
          <button type="button" class="btn-primary" @click="closeSessionsModal">Close</button>
        </div>
      </div>
    </div>

    <!-- Reset Password Modal -->
    <div 
      class="modal-overlay" 
      v-if="showResetModal" 
      @click.self="closeResetModal"
      @keydown="onResetModalKeydown"
    >
      <div 
        ref="resetModalRef"
        class="modal-card" 
        role="dialog" 
        aria-modal="true" 
        aria-labelledby="reset-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <h3 id="reset-modal-title">Reset Password for {{ targetUser?.username }}</h3>
          <button class="btn-close" @click="closeResetModal" aria-label="Close dialog">✕</button>
        </div>
        <form @submit.prevent="executeResetPassword" class="modal-form">
          <div class="form-group">
            <label>New Temporary Password</label>
            <input v-model="resetPasswordVal" type="password" placeholder="Leave blank to auto-generate" class="form-input" />
          </div>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeResetModal">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="userSaving">
              {{ userSaving ? 'Resetting...' : 'Confirm Reset' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Generated password: the server returns it once, so it is shown until dismissed. -->
    <div
      class="modal-overlay"
      v-if="generatedCredential"
      @keydown="onGeneratedModalKeydown"
    >
      <div
        ref="generatedModalRef"
        class="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="generated-modal-title"
        tabindex="-1"
      >
        <div class="modal-header">
          <h3 id="generated-modal-title">Temporary Password for {{ generatedCredential.username }}</h3>
        </div>
        <div class="modal-form">
          <div class="form-group">
            <label for="generated-password">Temporary Password</label>
            <input
              id="generated-password"
              ref="generatedInputRef"
              :value="generatedCredential.password"
              type="text"
              readonly
              class="form-input generated-password"
              @focus="$event.target.select()"
            />
          </div>
          <p class="generated-note">
            Give this password to {{ generatedCredential.username }} now: it is not shown again.
            They must choose a new one when they sign in.
          </p>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="copyGeneratedPassword">
              {{ generatedCopied ? 'Copied' : 'Copy' }}
            </button>
            <button type="button" class="btn-primary" @click="closeGeneratedModal">Done</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import {
  getUsers,
  getSessionOptions,
  getUserSessions,
  endUserSession,
  endAllUserSessions,
  createUser,
  resetUserPassword,
  updateUser,
  deleteUser,
} from '../../services/api.js'

const props = defineProps({
  currentUser: {
    type: String,
    default: '',
  },
})

const emit = defineEmits(['user-changed'])

const toast = useToast()
const confirm = useConfirm()

const users = ref([])
const usersLoading = ref(false)
const userSaving = ref(false)

const showAddModal = ref(false)
const showResetModal = ref(false)
const targetUser = ref(null)
const resetPasswordVal = ref('')

const addModalRef = ref(null)
let addOpenerElement = null
const resetModalRef = ref(null)
let resetOpenerElement = null

// { username, password } when the server generated a password (the form was left blank). The
// API returns it only in that one response, so it stays on screen until the admin dismisses it.
const generatedCredential = ref(null)
const generatedModalRef = ref(null)
const generatedInputRef = ref(null)
const generatedCopied = ref(false)

function showGeneratedPassword(username, response) {
  const password = response?.data?.data?.generated_password
  if (!password) return
  generatedCopied.value = false
  generatedCredential.value = { username, password }
  nextTick(() => {
    generatedInputRef.value?.focus()
  })
}

async function copyGeneratedPassword() {
  try {
    // Needs HTTPS (or localhost); otherwise the text is selected for a manual copy.
    await navigator.clipboard.writeText(generatedCredential.value.password)
    generatedCopied.value = true
  } catch {
    generatedInputRef.value?.select()
  }
}

function closeGeneratedModal() {
  generatedCredential.value = null
}

function onGeneratedModalKeydown(e) {
  if (e.key === 'Escape') {
    closeGeneratedModal()
  }
}

// Per-account session settings (v3.3.6). The choices come from the API
// (GET /users/session-options) so the form offers exactly what the server accepts; these are
// only the fallback while it loads or if it fails.
const sessionOptions = ref({
  idle_minutes: { ADMIN: [15, 30, 60], VIEWER: [30, 60, 120, 240] },
  default_idle_minutes: { ADMIN: 30, VIEWER: 120 },
  admin_absolute_hours: 12,
  max_sessions: { min: 1, max: 10, default: 2 },
})

async function loadSessionOptions() {
  try {
    const res = await getSessionOptions()
    if (res.data?.data) sessionOptions.value = res.data.data
  } catch (err) {
    console.error('Failed to load session options:', err)
  }
}

function idleChoices(role) {
  return sessionOptions.value.idle_minutes[role] || sessionOptions.value.idle_minutes.VIEWER
}

function defaultIdle(role) {
  return sessionOptions.value.default_idle_minutes[role] ?? idleChoices(role)[0]
}

const sessionLimitChoices = computed(() => {
  const { min, max } = sessionOptions.value.max_sessions
  return Array.from({ length: max - min + 1 }, (_, i) => min + i)
})

// After a role change, keep the timeout if the new role offers it, else the largest choice
// below it (the server does the same when only the role is sent).
function fitIdleToRole(form) {
  const choices = idleChoices(form.role)
  if (choices.includes(form.session_idle_minutes)) return
  const fitting = choices.filter((c) => c <= form.session_idle_minutes)
  form.session_idle_minutes = fitting.length ? Math.max(...fitting) : choices[0]
}

function sessionHint(role) {
  const hours = sessionOptions.value.admin_absolute_hours
  return role === 'ADMIN'
    ? `Signed out after this long without activity, and always ${hours} hours after sign-in. A sign-in past the limit ends the oldest session.`
    : 'Signed out after this long without activity. A sign-in past the limit ends the oldest session.'
}

// "15 min", "1 hour", "4 hours".
function formatMinutes(minutes) {
  const m = Number(minutes)
  if (!Number.isFinite(m) || m <= 0) return '—'
  if (m % 60 === 0) return m === 60 ? '1 hour' : `${m / 60} hours`
  return `${m} min`
}

function isSelf(u) {
  return !!u && u.username === props.currentUser
}

const userForm = reactive({
  username: '',
  password: '',
  role: 'VIEWER',
  session_idle_minutes: 120,
  max_sessions: 2,
})

function openAddUserModal() {
  addOpenerElement = document.activeElement
  userForm.username = ''
  userForm.password = ''
  userForm.role = 'VIEWER'
  userForm.session_idle_minutes = defaultIdle('VIEWER')
  userForm.max_sessions = sessionOptions.value.max_sessions.default
  showAddModal.value = true
  nextTick(() => {
    addModalRef.value?.querySelector('input')?.focus()
  })
}

function closeAddModal() {
  showAddModal.value = false
  nextTick(() => {
    addOpenerElement?.focus()
  })
}

function onAddModalKeydown(e) {
  if (e.key === 'Escape') {
    closeAddModal()
  }
}

const showEditModal = ref(false)
const editModalRef = ref(null)
let editOpenerElement = null
const editForm = reactive({ role: 'VIEWER', session_idle_minutes: 120, max_sessions: 2 })

function openEditModal(u) {
  editOpenerElement = document.activeElement
  targetUser.value = u
  editForm.role = u.role
  editForm.session_idle_minutes = u.session_idle_minutes ?? defaultIdle(u.role)
  editForm.max_sessions = u.max_sessions ?? sessionOptions.value.max_sessions.default
  showEditModal.value = true
  nextTick(() => {
    editModalRef.value?.querySelector('select:not([disabled])')?.focus()
  })
}

function closeEditModal() {
  showEditModal.value = false
  nextTick(() => {
    editOpenerElement?.focus()
  })
}

function onEditModalKeydown(e) {
  if (e.key === 'Escape') {
    closeEditModal()
  }
}

async function saveEditUser() {
  const u = targetUser.value
  if (!u) return
  // Send only what changed: the API refuses a role field for the admin's own account.
  const payload = {}
  if (editForm.role !== u.role) payload.role = editForm.role
  if (editForm.session_idle_minutes !== u.session_idle_minutes) {
    payload.session_idle_minutes = editForm.session_idle_minutes
  }
  if (editForm.max_sessions !== u.max_sessions) payload.max_sessions = editForm.max_sessions
  if (!Object.keys(payload).length) {
    closeEditModal()
    return
  }
  userSaving.value = true
  try {
    await updateUser(u.id, payload)
    closeEditModal()
    toast.add({
      severity: 'success',
      summary: 'User Updated',
      detail: `User '${u.username}' updated.`,
      life: 3000,
    })
    await fetchUsersList()
    emit('user-changed')
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to update user.',
      life: 4000,
    })
  } finally {
    userSaving.value = false
  }
}

// --- Sessions dialog ---------------------------------------------------------------------
const showSessionsModal = ref(false)
const sessionsModalRef = ref(null)
let sessionsOpenerElement = null
const sessions = ref([])
const sessionsLoading = ref(false)
const sessionEnding = ref(null)

async function loadSessions() {
  if (!targetUser.value) return
  sessionsLoading.value = true
  try {
    const res = await getUserSessions(targetUser.value.id)
    sessions.value = res.data?.data || []
  } catch (err) {
    sessions.value = []
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to load sessions.',
      life: 4000,
    })
  } finally {
    sessionsLoading.value = false
  }
}

function openSessionsModal(u) {
  sessionsOpenerElement = document.activeElement
  targetUser.value = u
  sessions.value = []
  confirmingEndAll.value = false
  showSessionsModal.value = true
  loadSessions()
  nextTick(() => {
    sessionsModalRef.value?.focus()
  })
}

function closeSessionsModal() {
  showSessionsModal.value = false
  // Counts in the table may have changed (ended here, or sessions timed out meanwhile).
  fetchUsersList()
  nextTick(() => {
    sessionsOpenerElement?.focus()
  })
}

function onSessionsModalKeydown(e) {
  if (e.key === 'Escape') {
    closeSessionsModal()
  }
}

async function endOneSession(sess) {
  sessionEnding.value = sess.id
  try {
    await endUserSession(targetUser.value.id, sess.id)
    toast.add({ severity: 'success', summary: 'Session Ended', detail: 'That device is signed out.', life: 3000 })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to end the session.',
      life: 4000,
    })
  } finally {
    sessionEnding.value = null
    await loadSessions()
  }
}

const confirmingEndAll = ref(false)
const endingAll = ref(false)

async function endAllSessionsNow() {
  const name = targetUser.value?.username
  endingAll.value = true
  try {
    await endAllUserSessions(targetUser.value.id)
    toast.add({ severity: 'success', summary: 'Sessions Ended', detail: `Sessions of '${name}' ended.`, life: 3000 })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to end sessions.',
      life: 4000,
    })
  } finally {
    endingAll.value = false
    confirmingEndAll.value = false
    await loadSessions()
  }
}

// "Chrome on Windows" from a user agent string; the full string is in the row's tooltip.
// Only a label for telling devices apart, so a rough match is enough.
function describeDevice(ua) {
  if (!ua) return 'Unknown device'
  const browser =
    /Edg\//.test(ua) ? 'Edge'
      : /OPR\//.test(ua) ? 'Opera'
        : /Firefox\//.test(ua) ? 'Firefox'
          : /Chrome\//.test(ua) ? 'Chrome'
            : /Safari\//.test(ua) ? 'Safari'
              : /curl\//i.test(ua) ? 'curl'
                : null
  const os =
    /Windows/.test(ua) ? 'Windows'
      : /iPhone|iPad/.test(ua) ? 'iOS'
        : /Android/.test(ua) ? 'Android'
          : /Mac OS X|Macintosh/.test(ua) ? 'macOS'
            : /CrOS/.test(ua) ? 'ChromeOS'
              : /Linux/.test(ua) ? 'Linux'
                : null
  if (browser && os) return `${browser} on ${os}`
  return browser || os || ua.slice(0, 40)
}

function formatWhen(iso) {
  return iso ? new Date(iso).toLocaleString() : '—'
}

// "just now", "4 min ago", "2 h 5 min ago".
function spanText(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000))
  if (minutes < 1) return null
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return h ? (m ? `${h} h ${m} min` : `${h} h`) : `${m} min`
}

function formatAgo(iso) {
  if (!iso) return '—'
  const text = spanText(Date.now() - new Date(iso).getTime())
  return text ? `${text} ago` : 'just now'
}

function formatUntil(iso) {
  if (!iso) return '—'
  const text = spanText(new Date(iso).getTime() - Date.now())
  return text ? `in ${text}` : 'within a minute'
}

function openResetPasswordModal(u) {
  resetOpenerElement = document.activeElement
  targetUser.value = u
  resetPasswordVal.value = ''
  showResetModal.value = true
  nextTick(() => {
    resetModalRef.value?.querySelector('input')?.focus()
  })
}

function closeResetModal() {
  showResetModal.value = false
  nextTick(() => {
    resetOpenerElement?.focus()
  })
}

function onResetModalKeydown(e) {
  if (e.key === 'Escape') {
    closeResetModal()
  }
}

async function fetchUsersList() {
  usersLoading.value = true
  try {
    const res = await getUsers()
    if (res.data?.data) {
      users.value = res.data.data
    }
  } catch (err) {
    console.error('Failed to load users:', err)
  } finally {
    usersLoading.value = false
  }
}

async function saveNewUser() {
  userSaving.value = true
  try {
    const payload = {
      username: userForm.username,
      role: userForm.role,
      password: userForm.password || undefined,
      session_idle_minutes: userForm.session_idle_minutes,
      max_sessions: userForm.max_sessions,
    }
    const res = await createUser(payload)
    closeAddModal()
    showGeneratedPassword(payload.username, res)
    toast.add({
      severity: 'success',
      summary: 'User Created',
      detail: `User account '${userForm.username}' created successfully.`,
      life: 3000,
    })
    await fetchUsersList()
    emit('user-changed')
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to create user.',
      life: 4000,
    })
  } finally {
    userSaving.value = false
  }
}

async function executeResetPassword() {
  if (!targetUser.value) return
  userSaving.value = true
  try {
    const payload = resetPasswordVal.value ? { password: resetPasswordVal.value } : {}
    const res = await resetUserPassword(targetUser.value.id, payload)
    closeResetModal()
    showGeneratedPassword(targetUser.value.username, res)
    toast.add({
      severity: 'success',
      summary: 'Password Reset',
      detail: `Password for '${targetUser.value.username}' reset successfully.`,
      life: 3000,
    })
    await fetchUsersList()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to reset password.',
      life: 4000,
    })
  } finally {
    userSaving.value = false
  }
}

async function toggleUserStatus(u) {
  const newStatus = !u.is_active
  try {
    await updateUser(u.id, { is_active: newStatus })
    await fetchUsersList()
    toast.add({
      severity: 'success',
      summary: 'Status Updated',
      detail: `User '${u.username}' is now ${newStatus ? 'active' : 'disabled'}.`,
      life: 3000,
    })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: err.response?.data?.detail || 'Failed to update user status.',
      life: 4000,
    })
  }
}

async function confirmDeleteUser(u) {
  confirm.require({
    message: `Permanently revoke and delete user account '${u.username}'?`,
    header: 'Confirm User Revocation',
    icon: 'pi pi-exclamation-triangle',
    rejectClass: 'p-button-secondary p-button-outlined',
    rejectLabel: 'Cancel',
    acceptLabel: 'Delete',
    acceptClass: 'p-button-danger',
    accept: async () => {
      try {
        await deleteUser(u.id)
        await fetchUsersList()
        toast.add({
          severity: 'success',
          summary: 'User Removed',
          detail: `User '${u.username}' has been removed.`,
          life: 3000,
        })
        emit('user-changed')
      } catch (err) {
        toast.add({
          severity: 'error',
          summary: 'Error',
          detail: err.response?.data?.detail || 'Failed to delete user.',
          life: 4000,
        })
      }
    },
  })
}

onMounted(() => {
  loadSessionOptions()
  fetchUsersList()
})
</script>

<style scoped>
.generated-password {
  font-family: var(--font-mono, ui-monospace, monospace);
  letter-spacing: 0.02em;
}

.session-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  white-space: nowrap;
}

.session-limit {
  font-size: 12px;
  color: var(--text-muted);
}

.session-count {
  align-self: flex-start;
  padding: 0;
  border: none;
  background: none;
  font-size: 12px;
  color: var(--text-muted);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  cursor: pointer;
}

.session-count:hover,
.session-count:focus-visible {
  color: var(--text-primary);
}

.modal-card.modal-wide {
  max-width: 620px;
}

.session-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 50vh;
  overflow-y: auto;
}

.session-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-color);
}

.session-row:last-child {
  border-bottom: none;
}

.session-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.session-device {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.session-meta {
  font-size: 12px;
  color: var(--text-muted);
}

.current-tag {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
}

.end-all {
  margin-right: auto;
}

.confirm-row {
  align-items: center;
}

.confirm-row button {
  white-space: nowrap;
}

.confirm-text {
  margin-right: auto;
  font-size: 13px;
  color: var(--text-primary);
}


.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.form-row .form-group {
  margin-bottom: 8px;
}

.form-hint {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-muted);
}

.form-hint.warn {
  color: #f59e0b;
}

.generated-note {
  margin: 0 0 16px;
  font-size: 0.85rem;
  color: var(--text-secondary);
}

.settings-users-pane {
  display: flex;
  flex-direction: column;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 20px;
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

.role-badge {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(14, 165, 233, 0.15);
  color: #0ea5e9;
  border: 1px solid rgba(14, 165, 233, 0.25);
}

.role-badge.admin {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.25);
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

.text-up {
  color: #10b981;
}

.text-unstable {
  color: #f59e0b;
}

.table-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
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
}

.btn-action:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.text-down {
  color: #ef4444;
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
  max-width: 480px;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
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

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.form-input, .form-select {
  width: 100%;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface-selected);
  color: var(--text-primary);
  font-size: 13px;
}

.form-input:focus, .form-select:focus {
  border-color: #0ea5e9;
  outline: none;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
  border-top: 1px solid var(--border-color);
  padding-top: 16px;
}

.font-mono {
  font-family: var(--font-mono);
}

.font-bold {
  font-weight: 700;
}

.tnum {
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
}
</style>
