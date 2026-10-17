<template>
  <div
    class="endpoint-card"
    :class="{ 'card-selected': selected }"
    @click="handleClick"
  >
    <div class="card-header">
      <div class="card-title-row">
        <div class="card-checkbox" @click.stop>
          <input 
            type="checkbox" 
            :checked="selected" 
            @change="handleCheckboxChange" 
          />
        </div>
        <h3 class="card-hostname" :title="endpoint.hostname">{{ endpoint.hostname }}</h3>
      </div>
      
      <div class="card-actions" @click.stop>
        <button class="btn-icon inspect" @click="handleInspect" title="Quick Diagnostics">🔍</button>
        <button v-if="isAdmin" class="btn-icon" @click="handleEdit" title="Edit Endpoint">✎</button>
        <button v-if="isAdmin" class="btn-icon delete" @click="handleDelete" title="Delete Endpoint">🗑</button>
      </div>
    </div>

    <div class="card-body">
      <div class="status-row">
        <StatusBadge 
          :status="endpoint.current_detailed_state || endpoint.current_operational_state || 'UNKNOWN'"
          size="sm"
        />
        <span class="card-latency font-mono tnum" v-if="endpoint.avg_rtt_ms != null">
          {{ endpoint.avg_rtt_ms.toFixed(1) }} ms
        </span>
      </div>

      <div class="card-meta">
        <span class="card-ip">{{ endpoint.ip_address }}</span>
        <span class="card-separator">·</span>
        <span class="card-device-type">{{ endpoint.device_type }}</span>
      </div>

      <div class="card-footer">
        <div class="uptime-row">
          <span class="uptime-label">Uptime</span>
          <span class="uptime-value" :class="uptimeClass">
            {{ formattedUptime }}
          </span>
        </div>
        <span class="last-seen">Seen {{ timeAgo }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'

const props = defineProps({
  endpoint: { type: Object, required: true },
  isAdmin: { type: Boolean, default: false },
  selected: { type: Boolean, default: false }
})

const emit = defineEmits(['select', 'edit', 'delete', 'toggle-select', 'inspect'])

const handleClick = () => {
  emit('select', props.endpoint.id)
}

const handleCheckboxChange = () => {
  emit('toggle-select', props.endpoint.id)
}

const handleInspect = () => {
  emit('inspect', props.endpoint)
}

const handleEdit = () => {
  emit('edit', props.endpoint)
}

const handleDelete = () => {
  emit('delete', props.endpoint.id)
}

const statusClass = computed(() => {
  const s = props.endpoint.current_detailed_state || props.endpoint.current_operational_state
  if (s === 'UP') return 'status-up'
  if (s === 'UP-UNSTABLE' || s === 'DOWN-UNSTABLE') return 'status-unstable'
  if (s === 'DOWN') return 'status-down'
  return 'status-unknown'
})

const formattedUptime = computed(() => {
  const v = props.endpoint.uptime_percentage_24h
  if (v == null) return '—'
  return `${parseFloat(v).toFixed(2)}%`
})

const uptimeClass = computed(() => {
  const v = parseFloat(props.endpoint.uptime_percentage_24h)
  if (isNaN(v)) return ''
  if (v >= 99) return 'uptime-good'
  if (v >= 95) return 'uptime-warn'
  return 'uptime-bad'
})

const timeAgo = computed(() => {
  if (!props.endpoint.last_seen) return 'never'
  const now = Date.now()
  const past = new Date(props.endpoint.last_seen).getTime()
  const diff = Math.floor((now - past) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) {
    const m = Math.floor(diff / 60)
    return `${m}m ago`
  }
  if (diff < 86400) {
    const h = Math.floor(diff / 3600)
    return `${h}h ago`
  }
  const d = Math.floor(diff / 86400)
  return `${d}d ago`
})
</script>

<style scoped>
.endpoint-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 16px;
  cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s, background 0.15s;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
  content-visibility: auto;
  contain-intrinsic-size: 0 148px;
}

.endpoint-card:hover {
  box-shadow: var(--shadow-hover);
  border-color: var(--border-color-strong);
}

.card-selected {
  border-color: var(--accent);
  background: var(--bg-surface-selected);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.card-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.card-checkbox input {
  width: 14px;
  height: 14px;
  cursor: pointer;
  flex-shrink: 0;
  accent-color: var(--accent);
}

.card-hostname {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-actions,
.admin-actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.15s ease;
}

.endpoint-card:hover .card-actions,
.endpoint-card:hover .admin-actions {
  opacity: 1;
}

.btn-icon {
  width: 26px;
  height: 26px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
  font-size: 13px;
  transition: background 0.15s, color 0.15s;
}

.btn-icon:hover {
  background: var(--bg-surface-selected);
  color: var(--text-primary);
}

.btn-icon.delete:hover {
  background: var(--color-down-bg);
  color: var(--color-down);
}

.card-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-latency {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--bg-surface-hover);
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--border-color);
}

.card-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.card-ip {
  color: var(--text-secondary);
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
}

.card-separator {
  color: var(--text-muted);
}

.card-device-type {
  color: var(--text-muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid var(--border-color);
}

.uptime-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.uptime-label {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.uptime-value {
  font-size: 13px;
  font-weight: 700;
}

.uptime-good { color: var(--color-up); }
.uptime-warn { color: var(--color-up-unstable); }
.uptime-bad { color: var(--color-down); }

.last-seen {
  font-size: 12px;
  color: var(--text-muted);
}
</style>
