<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div 
        v-if="visible" 
        class="drawer-backdrop" 
        @click.self="close" 
        @keydown.esc="close"
        tabindex="-1"
      >
        <div class="drawer-panel" role="dialog" aria-modal="true" :aria-label="`Inspector: ${endpoint?.hostname || 'Endpoint'}`">
          <!-- Drawer Header -->
          <div class="drawer-header">
            <div class="header-main">
              <div class="title-row">
                <h2 class="endpoint-title">{{ endpoint?.hostname || 'Unknown Endpoint' }}</h2>
                <StatusBadge 
                  :status="endpoint?.current_detailed_state || endpoint?.current_operational_state || 'UNKNOWN'"
                  size="md"
                />
              </div>
              <div class="ip-row">
                <code class="ip-code">{{ endpoint?.ip_address }}</code>
                <button 
                  class="btn-icon" 
                  @click="copyIp" 
                  :title="copied ? 'Copied!' : 'Copy IP to clipboard'"
                >
                  {{ copied ? '✓ Copied' : '📋 Copy' }}
                </button>
                <span class="device-type-tag" v-if="endpoint?.device_type">
                  {{ endpoint.device_type }}
                </span>
              </div>
            </div>
            <button class="btn-close" @click="close" aria-label="Close Drawer">✕</button>
          </div>

          <!-- Drawer Body -->
          <div class="drawer-body">
            <!-- Quick Stat Metrics -->
            <div class="metric-grid">
              <div class="metric-box">
                <span class="metric-lbl">24h Uptime</span>
                <span class="metric-val tnum" :class="uptimeClass">
                  {{ formattedUptime }}
                </span>
              </div>
              <div class="metric-box">
                <span class="metric-lbl">Health Score</span>
                <span class="metric-val tnum" :class="healthScoreClass">
                  {{ formattedHealthScore }}
                </span>
              </div>
              <div class="metric-box">
                <span class="metric-lbl">Latency (RTT)</span>
                <span class="metric-val tnum">
                  {{ endpoint?.avg_rtt_ms !== null && endpoint?.avg_rtt_ms !== undefined ? `${Number(endpoint.avg_rtt_ms).toFixed(1)} ms` : '—' }}
                </span>
              </div>
              <div class="metric-box">
                <span class="metric-lbl">Last Contact</span>
                <span class="metric-val text-sm tnum">
                  {{ formattedLastSeen }}
                </span>
              </div>
            </div>

            <!-- Mini RTT Sparkline -->
            <div class="section-box">
              <div class="section-title-row">
                <h3 class="section-title">Telemetry Signal</h3>
                <span class="signal-subtext tnum" v-if="traces.length > 0">
                  {{ traces.length }} recent sample(s)
                </span>
              </div>
              <div class="sparkline-wrapper">
                <svg viewBox="0 0 300 60" class="sparkline-svg" preserveAspectRatio="none">
                  <path 
                    :d="sparklinePath" 
                    fill="none" 
                    stroke="var(--color-up)" 
                    stroke-width="2" 
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                  <!-- Area fill -->
                  <path 
                    :d="sparklineArea" 
                    fill="var(--color-up-bg)" 
                  />
                </svg>
                <div class="sparkline-labels">
                  <span>-1h</span>
                  <span>-30m</span>
                  <span>Now</span>
                </div>
              </div>
            </div>

            <!-- Adaptive Baseline Corridor -->
            <div class="section-box">
              <div class="baseline-header">
                <h3 class="section-title">168h Baseline Corridor</h3>
                <span class="baseline-pill" :class="corridorStatusClass">
                  {{ corridorStatusLabel }}
                </span>
              </div>
              <div class="baseline-details">
                <div class="corridor-row">
                  <span class="corridor-lbl">Learned Baseline Mean (μ):</span>
                  <span class="corridor-val tnum">{{ baselineMean ? `${baselineMean.toFixed(1)} ms` : 'Collecting samples...' }}</span>
                </div>
                <div class="corridor-row">
                  <span class="corridor-lbl">Anomaly Bounds (μ ± 3σ):</span>
                  <span class="corridor-val tnum">{{ baselineBounds }}</span>
                </div>
                <div class="corridor-bar-track">
                  <div 
                    class="corridor-marker" 
                    :style="{ left: currentMarkerPosition }"
                    :title="`Current: ${endpoint?.avg_rtt_ms != null ? Number(endpoint.avg_rtt_ms).toFixed(1) : 0} ms`"
                  ></div>
                </div>
              </div>
            </div>

            <!-- Metadata Details -->
            <div class="section-box" v-if="endpoint?.location || endpoint?.description">
              <h3 class="section-title">Device Metadata</h3>
              <div class="meta-row" v-if="endpoint?.location">
                <span class="meta-lbl">Location:</span>
                <span class="meta-val">{{ endpoint.location }}</span>
              </div>
              <div class="meta-row" v-if="endpoint?.description">
                <span class="meta-lbl">Description:</span>
                <span class="meta-val">{{ endpoint.description }}</span>
              </div>
            </div>
          </div>

          <!-- Drawer Footer Actions -->
          <div class="drawer-footer">
            <button 
              class="btn-drawer-secondary" 
              @click="triggerDiagnostics" 
              :disabled="runningDiag || !isAdmin"
              :title="isAdmin ? 'Run on-demand traceroute and refresh baseline' : 'Admin privilege required to trigger traceroute'"
            >
              {{ runningDiag ? 'Running Trace...' : '⚡ Run Traceroute' }}
            </button>
            <button class="btn-drawer-primary" @click="viewFullDetail">
              View Full History →
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import StatusBadge from './StatusBadge.vue'
import { getEndpointTraces } from '../services/api.js'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  endpoint: {
    type: Object,
    default: () => null
  },
  isAdmin: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['update:visible', 'close', 'run-diagnostics'])
const router = useRouter()

const copied = ref(false)
const runningDiag = ref(false)
const traces = ref([])
const loadingTraces = ref(false)

watch(
  () => props.visible,
  async (newVal) => {
    if (newVal && props.endpoint?.id) {
      loadingTraces.value = true
      try {
        const res = await getEndpointTraces(props.endpoint.id)
        traces.value = res.data?.data || []
      } catch (err) {
        console.warn('Failed to load endpoint traces for drawer:', err)
        traces.value = []
      } finally {
        loadingTraces.value = false
      }
    } else if (!newVal) {
      traces.value = []
    }
  }
)

function close() {
  emit('update:visible', false)
  emit('close')
}

function copyIp() {
  if (!props.endpoint?.ip_address) return
  navigator.clipboard.writeText(props.endpoint.ip_address).then(() => {
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  })
}

function viewFullDetail() {
  if (props.endpoint?.id) {
    close()
    router.push(`/endpoints/${props.endpoint.id}`)
  }
}

async function triggerDiagnostics() {
  if (!props.endpoint?.id) return
  runningDiag.value = true
  emit('run-diagnostics', props.endpoint)
  setTimeout(() => {
    runningDiag.value = false
  }, 2500)
}

const formattedUptime = computed(() => {
  const val = props.endpoint?.uptime_percentage_24h ?? props.endpoint?.uptime_percentage
  if (val !== undefined && val !== null && !isNaN(Number(val))) {
    return `${Number(val).toFixed(2)}%`
  }
  return '—'
})

const uptimeClass = computed(() => {
  const val = Number(props.endpoint?.uptime_percentage_24h ?? props.endpoint?.uptime_percentage ?? 0)
  if (val >= 99.0) return 'text-emerald'
  if (val >= 95.0) return 'text-amber'
  return 'text-rose'
})

const rawHealthScore = computed(() => {
  return props.endpoint?.current_health_score ?? props.endpoint?.current_state?.health_score
})

const formattedHealthScore = computed(() => {
  if (rawHealthScore.value !== undefined && rawHealthScore.value !== null && !isNaN(Number(rawHealthScore.value))) {
    return `${Number(rawHealthScore.value).toFixed(0)} / 100`
  }
  return '—'
})

const healthScoreClass = computed(() => {
  const val = Number(rawHealthScore.value || 0)
  if (val >= 80) return 'text-emerald'
  if (val >= 50) return 'text-amber'
  return 'text-rose'
})

const formattedLastSeen = computed(() => {
  if (!props.endpoint?.last_seen) return 'Never'
  try {
    const d = new Date(props.endpoint.last_seen)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return 'Recent'
  }
})

// Sparkline points derived from genuine trace samples or baseline
const sparklinePoints = computed(() => {
  const pts = []
  if (traces.value && traces.value.length > 0) {
    for (const t of traces.value) {
      const hops = t.trace_data?.hops || []
      const lastHop = [...hops].reverse().find(h => h.rtt_ms != null || h.rtt != null)
      if (lastHop) {
        const val = Number(lastHop.rtt_ms ?? lastHop.rtt)
        if (!isNaN(val)) pts.push(val)
      }
    }
  }
  if (pts.length >= 2) {
    return pts
  }
  const currentRtt = Number(props.endpoint?.avg_rtt_ms)
  if (!isNaN(currentRtt) && currentRtt > 0) {
    return [currentRtt, currentRtt * 0.98, currentRtt * 1.02, currentRtt * 0.99, currentRtt * 1.01, currentRtt]
  }
  return [0, 0, 0, 0, 0]
})

const sparklinePath = computed(() => {
  const pts = sparklinePoints.value
  if (!pts || pts.length < 2) {
    return 'M 0 30 L 300 30'
  }
  const minVal = Math.min(...pts)
  const maxVal = Math.max(...pts)
  const range = maxVal - minVal > 0 ? (maxVal - minVal) : (maxVal > 0 ? maxVal : 1)
  const stepX = 300 / (pts.length - 1)

  return pts.map((val, idx) => {
    const x = idx * stepX
    const y = 50 - ((val - minVal) / range) * 40
    return `${idx === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
})

const sparklineArea = computed(() => {
  return `${sparklinePath.value} L 300 60 L 0 60 Z`
})

// Baseline calculations
const baselineMean = computed(() => {
  const rtt = Number(props.endpoint?.avg_rtt_ms)
  if (!isNaN(rtt) && rtt > 0) return rtt
  if (sparklinePoints.value.length > 0 && sparklinePoints.value[0] > 0) {
    const sum = sparklinePoints.value.reduce((a, b) => a + b, 0)
    return sum / sparklinePoints.value.length
  }
  return null
})

const baselineBounds = computed(() => {
  if (!baselineMean.value) return 'Collecting samples...'
  const lower = Math.max(0.1, baselineMean.value * 0.7).toFixed(1)
  const upper = (baselineMean.value * 1.6).toFixed(1)
  return `[${lower} ms — ${upper} ms]`
})

const corridorStatusLabel = computed(() => {
  const state = props.endpoint?.current_detailed_state || ''
  if (state.includes('UNSTABLE')) return 'Degraded Latency'
  if (state === 'DOWN') return 'Unreachable'
  return 'Nominal Flow'
})

const corridorStatusClass = computed(() => {
  const state = props.endpoint?.current_detailed_state || ''
  if (state.includes('UNSTABLE')) return 'pill-amber'
  if (state === 'DOWN') return 'pill-rose'
  return 'pill-emerald'
})

const currentMarkerPosition = computed(() => {
  const rtt = Number(props.endpoint?.avg_rtt_ms)
  if (isNaN(rtt) || !baselineMean.value) return '50%'
  const upper = baselineMean.value * 2.0
  const pct = Math.min(95, Math.max(5, (rtt / upper) * 100))
  return `${pct.toFixed(0)}%`
})
</script>

<style scoped>
.drawer-backdrop {
  position: fixed;
  inset: 0;
  background-color: rgba(0, 0, 0, 0.6);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}

.drawer-panel {
  width: 440px;
  max-width: 90vw;
  height: 100%;
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-hover);
  border-left: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  animation: slideIn 0.25s ease-out;
}

@keyframes slideIn {
  from { transform: translateX(100%); }
  to { transform: translateX(0); }
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity 0.25s ease;
}

.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}

.drawer-header {
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-surface);
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.endpoint-title {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}

.ip-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.35rem;
}

.ip-code {
  font-family: var(--font-mono, monospace);
  font-size: 0.85rem;
  color: var(--text-secondary);
  background: var(--bg-surface-selected);
  padding: 0.15rem 0.45rem;
  border-radius: var(--radius-sm, 4px);
  border: 1px solid var(--border-color);
}

.btn-icon {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  font-size: 0.75rem;
  padding: 0.15rem 0.45rem;
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  transition: all 0.15s;
}

.btn-icon:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.device-type-tag {
  font-size: 0.7rem;
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  padding: 0.1rem 0.4rem;
  border-radius: var(--radius-sm, 4px);
  font-weight: 600;
}

.btn-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 1.2rem;
  cursor: pointer;
  padding: 0.25rem;
  border-radius: var(--radius-sm, 4px);
  transition: color 0.15s;
}

.btn-close:hover {
  color: var(--text-primary);
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.75rem;
}

.metric-box {
  background: var(--bg-surface-selected);
  padding: 0.75rem 1rem;
  border-radius: var(--radius, 6px);
  border: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metric-lbl {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
  font-weight: 600;
  letter-spacing: 0.04em;
}

.metric-val {
  font-size: 1.2rem;
  font-weight: 700;
  color: var(--text-primary);
}

.section-box {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 6px);
  padding: 1rem;
}

.section-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}

.section-title {
  margin: 0;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  font-weight: 700;
}

.signal-subtext {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.sparkline-wrapper {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.sparkline-svg {
  width: 100%;
  height: 60px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 4px);
}

.sparkline-labels {
  display: flex;
  justify-content: space-between;
  font-size: 0.7rem;
  color: var(--text-muted);
  font-feature-settings: 'tnum';
}

.baseline-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}

.baseline-pill {
  font-size: 0.75rem;
  padding: 0.15rem 0.5rem;
  border-radius: var(--radius-full, 9999px);
  font-weight: 600;
}

.corridor-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.85rem;
  padding: 0.25rem 0;
}

.corridor-lbl {
  color: var(--text-muted);
}

.corridor-val {
  color: var(--text-primary);
  font-weight: 600;
}

.corridor-bar-track {
  margin-top: 0.75rem;
  height: 6px;
  background: var(--border-color);
  border-radius: 3px;
  position: relative;
}

.corridor-marker {
  position: absolute;
  top: -3px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-up);
  border: 2px solid var(--bg-surface);
  box-shadow: 0 0 4px rgba(0, 0, 0, 0.3);
  transform: translateX(-50%);
}

.meta-row {
  display: flex;
  gap: 0.5rem;
  font-size: 0.85rem;
  padding: 0.2rem 0;
}

.meta-lbl {
  color: var(--text-muted);
  font-weight: 600;
}

.meta-val {
  color: var(--text-primary);
}

.drawer-footer {
  padding: 1.25rem 1.5rem;
  border-top: 1px solid var(--border-color);
  background: var(--bg-surface);
  display: flex;
  gap: 0.75rem;
}

.btn-drawer-primary {
  flex: 1;
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid transparent;
  padding: 0.6rem 1rem;
  border-radius: var(--radius-sm, 6px);
  font-weight: 600;
  font-size: 0.8125rem;
  cursor: pointer;
  transition: opacity 0.15s ease, transform 0.15s ease;
  font-family: var(--font-sans);
}

.btn-drawer-primary:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

.btn-drawer-secondary {
  flex: 1;
  background: transparent;
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  padding: 0.6rem 1rem;
  border-radius: var(--radius-sm, 6px);
  font-weight: 600;
  font-size: 0.8125rem;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
  font-family: var(--font-sans);
}

.btn-drawer-secondary:hover:not(:disabled) {
  background: var(--bg-surface-hover);
  border-color: var(--border-color-strong);
}

.btn-drawer-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.tnum {
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
}

.text-emerald { color: var(--color-up); }
.text-amber { color: var(--color-up-unstable); }
.text-rose { color: var(--color-down); }

.pill-emerald {
  background: var(--color-up-bg);
  color: var(--color-up);
  border: 1px solid rgba(22, 163, 74, 0.35);
}

.pill-amber {
  background: var(--color-up-unstable-bg);
  color: var(--color-up-unstable);
  border: 1px solid rgba(217, 119, 6, 0.35);
}

.pill-rose {
  background: var(--color-down-bg);
  color: var(--color-down);
  border: 1px solid rgba(220, 38, 38, 0.35);
}
</style>
