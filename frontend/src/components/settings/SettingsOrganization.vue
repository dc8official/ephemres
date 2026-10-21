<template>
  <div class="settings-org-pane">
    <div class="settings-grid">
      <!-- Organization Profile & Report Branding Card -->
      <div class="settings-card full-width">
        <div class="card-header">
          <h2 class="card-title font-display">🏢 Organization Profile & Report Branding</h2>
          <button 
            type="button" 
            class="btn-primary" 
            @click="saveBranding" 
            :disabled="saving || !isDirty"
          >
            {{ saving ? 'Saving Branding...' : (isDirty ? '💾 Save Branding *' : '✓ Branding Saved') }}
          </button>
        </div>
        <p class="card-desc">
          Configure corporate identity, departments, confidentiality disclaimers, and company logo embedded across generated PDF audit reports.
        </p>

        <!-- Notification Toast -->
        <div v-if="toastMessage" :class="['alert-banner', toastType]" style="margin-bottom: 1rem;">
          <span>{{ toastMessage }}</span>
          <button class="btn-close" @click="toastMessage = null">✕</button>
        </div>

        <div class="form-layout">
          <!-- Left Column: Text Inputs -->
          <div class="form-fields">
            <div class="form-group">
              <label class="setting-label">Company / Organization Name</label>
              <p class="setting-hint">Appears in document headers and official audit title lines.</p>
              <input 
                type="text" 
                v-model="form.companyName" 
                placeholder="Company name here" 
                class="form-input"
                maxlength="150"
                @input="markDirty"
              />
            </div>

            <div class="form-group">
              <label class="setting-label">Department / Business Unit</label>
              <p class="setting-hint">Designates the operational entity responsible for the network scope.</p>
              <input 
                type="text" 
                v-model="form.department" 
                placeholder="Department here (e.g. NOC)" 
                class="form-input"
                maxlength="150"
                @input="markDirty"
              />
            </div>

            <div class="form-group">
              <label class="setting-label">Report Confidentiality Footer</label>
              <p class="setting-hint">Legal disclaimer printed across the bottom margin of every report page.</p>
              <input 
                type="text" 
                v-model="form.reportFooter" 
                placeholder="Report footer disclaimer here (e.g. Confidential — Internal Audit)" 
                class="form-input"
                maxlength="255"
                @input="markDirty"
              />
            </div>
          </div>

          <!-- Right Column: Logo Upload & Live Preview -->
          <div class="logo-upload-section">
            <label class="setting-label">Corporate Logo</label>
            <p class="setting-hint">Recommended format: Transparent PNG or high-DPI JPG (max 1 MB). PDF reports cannot embed SVG.</p>

            <div class="logo-preview-box">
              <div v-if="form.logoData" class="logo-active-view">
                <img :src="form.logoData" alt="Company Logo" class="preview-img" />
              </div>
              <div v-else class="logo-empty-view">
                <span class="empty-icon">🖼️</span>
                <span class="empty-text">No Logo Uploaded</span>
                <span class="empty-sub">Default [LOGO] badge will be used</span>
              </div>
            </div>

            <div class="logo-actions">
              <input 
                type="file" 
                ref="fileInputRef" 
                accept="image/png,image/jpeg" 
                style="display: none;" 
                @change="handleFileUpload" 
              />
              <button 
                type="button" 
                class="btn-secondary btn-small" 
                @click="triggerFileInput"
              >
                {{ form.logoData ? 'Change Logo' : 'Upload Logo' }}
              </button>
              <button 
                v-if="form.logoData" 
                type="button" 
                class="btn-text btn-small text-danger" 
                @click="removeLogo"
              >
                Remove Logo
              </button>
            </div>
          </div>
        </div>

      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getOrganizationSettings, updateOrganizationSettings } from '../../services/api.js'

const form = ref({
  companyName: '',
  department: '',
  logoData: '',
  reportFooter: '',
})

const fileInputRef = ref(null)
const isDirty = ref(false)
const saving = ref(false)
const toastMessage = ref(null)
const toastType = ref('success')

function markDirty() {
  isDirty.value = true
}

function triggerFileInput() {
  if (fileInputRef.value) {
    fileInputRef.value.click()
  }
}

function removeLogo() {
  form.value.logoData = ''
  markDirty()
}

function handleFileUpload(event) {
  const file = event.target.files?.[0]
  if (!file) return

  if (!file.type.match(/^image\/(png|jpeg)$/)) {
    toastMessage.value = 'Invalid file format. Please upload a PNG or JPG image.'
    toastType.value = 'error'
    return
  }

  if (file.size > 1024 * 1024) {
    toastMessage.value = 'Logo image exceeds the 1 MB size limit.'
    toastType.value = 'error'
    return
  }

  const reader = new FileReader()
  reader.onload = (e) => {
    form.value.logoData = e.target.result
    markDirty()
    toastMessage.value = 'Logo selected. Click "Save Branding" to persist changes.'
    toastType.value = 'success'
  }
  reader.readAsDataURL(file)
}

async function loadBranding() {
  try {
    const res = await getOrganizationSettings()
    if (res.data?.data) {
      const d = res.data.data
      form.value = {
        companyName: d.companyName || d.company_name || '',
        department: d.department || '',
        logoData: d.logoData || d.logo_data || '',
        reportFooter: d.reportFooter || d.report_footer || '',
      }
      isDirty.value = false
    }
  } catch (err) {
    console.error('Failed to load organization settings:', err)
  }
}

async function saveBranding() {
  saving.value = true
  toastMessage.value = null
  try {
    const payload = {
      companyName: form.value.companyName.trim(),
      department: form.value.department.trim(),
      logoData: form.value.logoData,
      reportFooter: form.value.reportFooter.trim(),
    }
    await updateOrganizationSettings(payload)
    isDirty.value = false
    toastMessage.value = 'Organization branding updated successfully.'
    toastType.value = 'success'
  } catch (err) {
    console.error('Failed to save organization branding:', err)
    toastMessage.value = err.response?.data?.detail || 'Failed to save organization branding.'
    toastType.value = 'error'
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadBranding()
})
</script>

<style scoped>
.settings-org-pane {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.settings-grid {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg, 12px);
  padding: 1.5rem;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.card-title {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.card-desc {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin: 0 0 1.5rem;
  line-height: 1.5;
}

.form-layout {
  display: grid;
  grid-template-columns: 1fr 300px;
  gap: 2rem;
}

@media (max-width: 900px) {
  .form-layout {
    grid-template-columns: 1fr;
  }
}

.form-fields {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.form-group {
  display: flex;
  flex-direction: column;
}

.setting-label {
  font-weight: 600;
  font-size: 0.875rem;
  color: var(--text-primary);
  margin-bottom: 0.25rem;
}

.setting-hint {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin: 0 0 0.5rem;
}

.form-input {
  background: var(--bg-app);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 0.625rem 0.875rem;
  border-radius: var(--radius-sm, 6px);
  font-size: 0.875rem;
  transition: all 0.15s ease;
}

.form-input:focus {
  outline: none;
  border-color: var(--accent-primary, #2563eb);
  background: var(--bg-surface);
}

.logo-upload-section {
  display: flex;
  flex-direction: column;
}

.logo-preview-box {
  height: 150px;
  border: 2px dashed var(--border-color);
  border-radius: var(--radius, 8px);
  background: var(--bg-app);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  margin: 0.75rem 0;
  padding: 10px;
}

.preview-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.logo-empty-view {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 4px;
}

.empty-icon {
  font-size: 1.75rem;
}

.empty-text {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--text-secondary);
}

.empty-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.logo-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.alert-banner {
  padding: 10px 14px;
  border-radius: var(--radius-sm, 6px);
  font-size: 0.85rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.alert-banner.success {
  background: rgba(16, 185, 129, 0.12);
  border: 1px solid rgba(16, 185, 129, 0.35);
  color: #059669;
}

.alert-banner.error {
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.35);
  color: #dc2626;
}

.btn-close {
  background: transparent;
  border: none;
  cursor: pointer;
  color: inherit;
  font-size: 12px;
}

.text-danger {
  color: #dc2626;
}

.text-danger:hover {
  text-decoration: underline;
}
</style>
