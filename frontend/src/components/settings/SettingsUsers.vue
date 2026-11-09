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
              <th>Last Active Sign-in</th>
              <th class="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="usersLoading">
              <td colspan="6" class="text-center py-6 text-muted">Loading user accounts...</td>
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
              <td class="font-mono tnum">
                {{ u.last_login ? new Date(u.last_login).toLocaleString() : 'Never' }}
              </td>
              <td class="text-right">
                <div class="table-actions">
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
            <select v-model="userForm.role" class="form-select">
              <option value="VIEWER">VIEWER (Read-Only Access)</option>
              <option value="ADMIN">ADMIN (Full Administrative Control)</option>
            </select>
          </div>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeAddModal">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="userSaving">
              {{ userSaving ? 'Creating...' : 'Register User' }}
            </button>
          </div>
        </form>
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
import { ref, reactive, onMounted, nextTick } from 'vue'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import {
  getUsers,
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

const userForm = reactive({
  username: '',
  password: '',
  role: 'VIEWER',
})

function openAddUserModal() {
  addOpenerElement = document.activeElement
  userForm.username = ''
  userForm.password = ''
  userForm.role = 'VIEWER'
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
  fetchUsersList()
})
</script>

<style scoped>
.generated-password {
  font-family: var(--font-mono, ui-monospace, monospace);
  letter-spacing: 0.02em;
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
