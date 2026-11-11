<template>
  <div class="settings-security-pane">
    <div class="settings-grid">
      <!-- Network Discovery & Diagnostics -->
      <div class="settings-card">
        <div class="card-header">
          <h2 class="card-title">🌐 Network Discovery & Diagnostics</h2>
        </div>
        <p class="card-desc">Control automated traceroute behavior and subnet traversal optimization.</p>

        <div class="setting-row">
          <div>
            <label class="setting-label">Layer-2 Subnet Auto-Bypass</label>
            <p class="setting-hint">Automatically bypass ICMP/UDP traceroute subprocesses for hosts on the local /24 broadcast segment.</p>
          </div>
          <label class="switch">
            <input type="checkbox" v-model="settings.l2AutoBypass" @change="emitChange" />
            <span class="slider round"></span>
          </label>
        </div>

        <!-- "Max Concurrent Traces" was a fixed line of text here, not a control (it mirrored the
             hard-coded Semaphore(3) in services/diagnostics.py). It returns as a real setting
             with parallel discovery (v3.3.6 build plan, phase 4). -->
      </div>

      <!-- Security & Access Policies -->
      <div class="settings-card">
        <div class="card-header">
          <h2 class="card-title">🔒 Security & Access Policies</h2>
        </div>
        <!-- Session timeouts and limits are per account since v3.3.6, in the User Governance tab. -->
        <p class="card-desc">
          Brute-force throttling for sign-in. Session timeouts and session limits are set per
          account in User Governance.
        </p>

        <div class="setting-row">
          <div>
            <label class="setting-label">Brute-Force Lockout Threshold</label>
            <p class="setting-hint">Consecutive failed login attempts before IP and account cooldown is applied.</p>
          </div>
          <select v-model="settings.lockoutThreshold" class="form-select font-mono tnum" @change="emitChange">
            <option value="3">3 Failed Attempts</option>
            <option value="5">5 Failed Attempts (Default)</option>
            <option value="10">10 Failed Attempts</option>
          </select>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
  settings: {
    type: Object,
    required: true,
  },
})

const emit = defineEmits(['change'])

function emitChange() {
  emit('change')
}
</script>

<style scoped>
.settings-security-pane {
  display: flex;
  flex-direction: column;
}

.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 20px;
}

.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
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
  margin: 0 0 16px 0;
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

.form-select {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 13px;
}

.form-select:focus {
  border-color: #0ea5e9;
  outline: none;
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
