<template>
  <!-- Settings → Discovery & topology (v3.3.6 layout): how routes to endpoints are discovered.
       Parallel discovery joins this tab in phase 4. The lockout threshold that shared the old
       "Security & Discovery" tab moved to Users & access. -->
  <div class="settings-discovery-pane">
    <div class="settings-grid">
      <!-- Network Discovery & Diagnostics -->
      <div class="settings-card">
        <div class="card-header">
          <h2 class="card-title">🌐 Route discovery</h2>
        </div>
        <p class="card-desc">How routes to endpoints are traced: how many at once, how often, and when to skip it.</p>

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

        <!-- v3.3.6 phase 4: real controls in place of the old fixed "Max Concurrent Traces"
             text. Both wait for the page's shared Save bar. -->
        <div class="setting-row">
          <div>
            <label class="setting-label" for="discovery-parallel">Parallel route traces</label>
            <p class="setting-hint">
              Traceroutes run at once. Traces through the same router still take turns, because
              routers answer only about once a second and a crowd of traces comes back full of
              gaps. Incident and root-cause traces always get 2 more slots.
            </p>
          </div>
          <select
            id="discovery-parallel"
            v-model.number="settings.discoveryParallel"
            class="form-select font-mono tnum"
            @change="emitChange"
          >
            <option v-for="n in [1, 2, 3, 4, 6, 8]" :key="n" :value="n">
              {{ n }}{{ n === 2 ? ' (default)' : '' }}
            </option>
          </select>
        </div>

        <div class="setting-row">
          <div>
            <label class="setting-label" for="rediscovery-hours">Re-trace every</label>
            <p class="setting-hint">
              Each endpoint's route is traced again on this schedule, spread out over the period.
              Route changes noticed in between (a transit device down, replies arriving with a
              different hop count, flow data) are traced straight away.
            </p>
          </div>
          <select
            id="rediscovery-hours"
            v-model.number="settings.rediscoveryHours"
            class="form-select font-mono tnum"
            @change="emitChange"
          >
            <option :value="6">6 hours</option>
            <option :value="12">12 hours</option>
            <option :value="24">24 hours (default)</option>
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
.settings-discovery-pane {
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
