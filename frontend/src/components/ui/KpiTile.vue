<template>
  <component
    :is="clickable ? 'button' : 'div'"
    :type="clickable ? 'button' : undefined"
    class="kpi-tile"
    :class="{ clickable, active }"
    :aria-pressed="clickable ? active : undefined"
    @click="clickable && emit('select')"
  >
    <span class="kpi-tile-label">
      <span v-if="marker" class="kpi-tile-marker" :style="{ background: marker }" aria-hidden="true"></span>
      {{ label }}
    </span>
    <span class="kpi-tile-value tnum" :class="`tone-${tone}`">
      <template v-if="value === null || value === undefined || value === ''">—</template>
      <template v-else>{{ value }}<span v-if="unit" class="kpi-tile-unit">{{ unit }}</span></template>
    </span>
    <span v-if="sub || $slots.sub" class="kpi-tile-sub">
      <slot name="sub">{{ sub }}</slot>
    </span>
  </component>
</template>

<script setup>
// Headline number tile. A missing value renders as "—" rather than a made-up 0 or 100 %.
// With `clickable` it becomes a toggle button (e.g. a status filter) and emits `select`.
// `marker` (a CSS color) adds a swatch before the label to tie the tile to a chart series.
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: null },
  unit: { type: String, default: '' },
  sub: { type: String, default: '' },
  tone: {
    type: String,
    default: 'neutral',
    validator: (v) => ['neutral', 'up', 'warn', 'down', 'info', 'unknown'].includes(v),
  },
  marker: { type: String, default: '' },
  clickable: { type: Boolean, default: false },
  active: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])
</script>

<style scoped>
.kpi-tile {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
  padding: 0.875rem 1rem;
  text-align: left;
  font-family: var(--font-sans);
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  color: var(--text-primary);
}

.kpi-tile.clickable {
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.kpi-tile.clickable:hover {
  background: var(--bg-surface-hover);
  border-color: var(--border-color-strong);
}

.kpi-tile.active {
  border-color: var(--text-primary);
}

.kpi-tile-label {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-secondary);
}

.kpi-tile-marker {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex-shrink: 0;
}

.kpi-tile-value {
  font-family: var(--font-display);
  font-size: var(--text-2xl);
  font-weight: 700;
  line-height: 1.2;
}

.kpi-tile-unit {
  margin-left: 0.25rem;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-muted);
}

.kpi-tile-sub {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.tone-neutral { color: var(--text-primary); }
.tone-up { color: var(--status-text-up); }
.tone-warn { color: var(--status-text-warn); }
.tone-down { color: var(--status-text-down); }
.tone-info { color: var(--status-text-info); }
.tone-unknown { color: var(--status-text-unknown); }
</style>
