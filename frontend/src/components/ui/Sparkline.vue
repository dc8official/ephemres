<template>
  <svg
    v-if="hasData"
    class="sparkline"
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    role="img"
    :aria-label="fullLabel"
  >
    <path :d="path" fill="none" :stroke="color" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
    <circle v-if="lastPoint" :cx="lastPoint.x" :cy="lastPoint.y" r="1.75" :fill="color" />
  </svg>
  <span v-else class="sparkline-empty" :style="{ width: `${width}px` }">—</span>
</template>

<script setup>
import { computed } from 'vue'

// Tiny inline trend line. `values` may contain null for slots with no data; the line breaks there.
// The y axis starts at 0 so equal heights mean equal values across rows.
const props = defineProps({
  values: { type: Array, required: true },
  width: { type: Number, default: 96 },
  height: { type: Number, default: 22 },
  color: { type: String, default: 'var(--series-rtt)' },
  ariaLabel: { type: String, default: 'Trend' },
  unit: { type: String, default: '' },
})

const hasData = computed(() => props.values.some((v) => v !== null && v !== undefined))

const fullLabel = computed(() => {
  const vals = props.values.filter((v) => v !== null && v !== undefined)
  if (!vals.length) return `${props.ariaLabel}: no data`
  return `${props.ariaLabel}: latest ${vals[vals.length - 1]}${props.unit}, highest ${Math.max(...vals)}${props.unit}`
})

const coords = computed(() => {
  const vals = props.values
  const max = Math.max(...vals.filter((v) => v !== null && v !== undefined), 0) || 1
  const stepX = vals.length > 1 ? (props.width - 4) / (vals.length - 1) : 0
  return vals.map((v, i) =>
    v === null || v === undefined
      ? null
      : { x: 2 + i * stepX, y: props.height - 2 - (v / max) * (props.height - 4) }
  )
})

const path = computed(() => {
  let d = ''
  let pen = false
  for (const c of coords.value) {
    if (!c) {
      pen = false
      continue
    }
    d += `${pen ? 'L' : 'M'}${c.x.toFixed(1)},${c.y.toFixed(1)} `
    pen = true
  }
  return d.trim()
})

const lastPoint = computed(() => [...coords.value].reverse().find((c) => c) || null)
</script>

<style scoped>
.sparkline {
  display: block;
}

.sparkline-empty {
  display: inline-block;
  color: var(--text-muted);
  text-align: center;
}
</style>
