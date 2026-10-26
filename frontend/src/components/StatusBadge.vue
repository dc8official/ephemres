<template>
  <span 
    class="status-badge" 
    :class="[badgeClass, `size-${size}`]" 
    role="status" 
    :aria-label="ariaLabel"
  >
    <!-- Geometric Shape Indicator (WCAG 2.2 AA - Color Independence) -->
    <span class="shape-indicator" aria-hidden="true">
      <!-- UP: Solid Circle -->
      <svg v-if="normalizedStatus === 'UP'" viewBox="0 0 16 16" class="shape-icon">
        <circle cx="8" cy="8" r="6" fill="currentColor" />
      </svg>

      <!-- UNSTABLE: Equilateral Triangle -->
      <svg v-else-if="normalizedStatus === 'UNSTABLE'" viewBox="0 0 16 16" class="shape-icon">
        <path d="M8 2 L14 13 L2 13 Z" fill="currentColor" />
      </svg>

      <!-- DOWN: Solid Square -->
      <svg v-else-if="normalizedStatus === 'DOWN'" viewBox="0 0 16 16" class="shape-icon">
        <rect x="3" y="3" width="10" height="10" rx="1" fill="currentColor" />
      </svg>

      <!-- PASSIVE: Concentric Double Ring / Target -->
      <svg v-else-if="normalizedStatus === 'PASSIVE'" viewBox="0 0 16 16" class="shape-icon">
        <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2" />
        <circle cx="8" cy="8" r="2.5" fill="currentColor" />
      </svg>

      <!-- MAINTENANCE / PAUSED / OTHER: Diamond -->
      <svg v-else viewBox="0 0 16 16" class="shape-icon">
        <polygon points="8,2 14,8 8,14 2,8" fill="currentColor" />
      </svg>
    </span>

    <span class="status-text">{{ label }}</span>

    <!-- Pending Confirmation Counter (e.g., 2/3 cycles) -->
    <span 
      v-if="pendingCycles && pendingCycles > 0" 
      class="pending-pill"
      :title="`Pending transition to ${pendingState || 'new state'}: Cycle ${pendingCycles}/${pendingThreshold || 3}`"
    >
      {{ pendingCycles }}/{{ pendingThreshold || 3 }}
    </span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: {
    type: String,
    default: 'UNKNOWN'
  },
  pendingCycles: {
    type: Number,
    default: 0
  },
  pendingThreshold: {
    type: Number,
    default: 3
  },
  pendingState: {
    type: String,
    default: ''
  },
  size: {
    type: String,
    default: 'md' // 'sm', 'md', 'lg'
  }
})

const normalizedStatus = computed(() => {
  const s = (props.status || '').toUpperCase()
  if (s === 'UP') return 'UP'
  if (s.includes('UNSTABLE')) return 'UNSTABLE'
  if (s === 'DOWN') return 'DOWN'
  if (s === 'PASSIVE') return 'PASSIVE'
  if (s === 'PAUSED' || s === 'MAINTENANCE') return 'PAUSED'
  return 'UNKNOWN'
})

const label = computed(() => {
  return props.status || 'UNKNOWN'
})

const badgeClass = computed(() => {
  switch (normalizedStatus.value) {
    case 'UP':
      return 'badge-up'
    case 'UNSTABLE':
      return 'badge-unstable'
    case 'DOWN':
      return 'badge-down'
    case 'PASSIVE':
      return 'badge-passive'
    case 'PAUSED':
      return 'badge-paused'
    default:
      return 'badge-unknown'
  }
})

const ariaLabel = computed(() => {
  let str = `Status: ${label.value}`
  if (props.pendingCycles && props.pendingCycles > 0) {
    str += `, pending confirmation ${props.pendingCycles} of ${props.pendingThreshold || 3}`
  }
  return str
})
</script>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  font-family: inherit;
  font-weight: 600;
  border-radius: 9999px;
  line-height: 1;
  white-space: nowrap;
  border: 1px solid transparent;
  transition: all 0.15s ease-in-out;
}

/* Sizes */
.size-sm {
  padding: 0.15rem 0.45rem;
  font-size: 0.75rem;
}
.size-sm .shape-icon {
  width: 0.65rem;
  height: 0.65rem;
}

.size-md {
  padding: 0.25rem 0.625rem;
  font-size: 0.8125rem;
}
.size-md .shape-icon {
  width: 0.8rem;
  height: 0.8rem;
}

.size-lg {
  padding: 0.35rem 0.8rem;
  font-size: 0.9rem;
}
.size-lg .shape-icon {
  width: 0.95rem;
  height: 0.95rem;
}

.shape-indicator {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.status-text {
  font-feature-settings: 'tnum';
  letter-spacing: 0.025em;
}

/* Status Color Tokens */
.badge-up {
  background-color: var(--color-up-bg);
  color: var(--status-text-up);
  border-color: rgba(22, 163, 74, 0.35);
}

.badge-unstable {
  background-color: var(--color-up-unstable-bg);
  color: var(--status-text-warn);
  border-color: rgba(217, 119, 6, 0.35);
}

.badge-down {
  background-color: var(--color-down-bg);
  color: var(--status-text-down);
  border-color: rgba(220, 38, 38, 0.35);
}

.badge-passive {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-color: rgba(99, 102, 241, 0.35);
}

html.dark .badge-passive {
  color: #a5b4fc;
}

.badge-paused {
  background-color: var(--color-unknown-bg);
  color: var(--status-text-unknown);
  border-color: var(--border-color);
}

.badge-unknown {
  background-color: var(--color-unknown-bg);
  color: var(--status-text-unknown);
  border-color: var(--border-color);
}

/* Pending Cycle Counter Pill */
.pending-pill {
  font-size: 0.7em;
  padding: 0.05rem 0.3rem;
  background-color: rgba(0, 0, 0, 0.25);
  border-radius: 9999px;
  font-weight: 700;
  font-feature-settings: 'tnum';
  opacity: 0.9;
}
</style>
