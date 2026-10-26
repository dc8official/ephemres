<template>
  <div
    class="segmented"
    :class="`segmented-${size}`"
    role="radiogroup"
    :aria-label="ariaLabel"
    @keydown="onKeydown"
  >
    <button
      v-for="(opt, i) in normalizedOptions"
      :key="opt.value"
      ref="buttons"
      type="button"
      role="radio"
      class="segment"
      :class="{ active: opt.value === modelValue }"
      :aria-checked="opt.value === modelValue"
      :tabindex="i === focusIndex ? 0 : -1"
      :title="opt.title"
      :disabled="opt.disabled"
      @click="select(opt)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

// Single-choice toggle group (time windows, chart metric, view mode).
// Keyboard: arrow keys move and select, Home/End jump to the ends (WAI-ARIA radio group).
const props = defineProps({
  modelValue: { type: [String, Number, Boolean], default: null },
  // Strings, numbers, or { value, label, title?, disabled? }.
  options: { type: Array, required: true },
  ariaLabel: { type: String, required: true },
  size: { type: String, default: 'md', validator: (v) => ['sm', 'md'].includes(v) },
})
const emit = defineEmits(['update:modelValue'])

const buttons = ref([])

const normalizedOptions = computed(() =>
  props.options.map((o) => (typeof o === 'object' && o !== null ? { label: String(o.value), ...o } : { value: o, label: String(o) }))
)

const focusIndex = computed(() => {
  const idx = normalizedOptions.value.findIndex((o) => o.value === props.modelValue)
  return idx >= 0 ? idx : 0
})

function select(opt) {
  if (opt.disabled || opt.value === props.modelValue) return
  emit('update:modelValue', opt.value)
}

function onKeydown(event) {
  const opts = normalizedOptions.value
  const enabled = opts.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0)
  if (enabled.length === 0) return
  const pos = Math.max(0, enabled.indexOf(focusIndex.value))
  let next = null
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = enabled[(pos + 1) % enabled.length]
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = enabled[(pos - 1 + enabled.length) % enabled.length]
  else if (event.key === 'Home') next = enabled[0]
  else if (event.key === 'End') next = enabled[enabled.length - 1]
  if (next === null) return
  event.preventDefault()
  select(opts[next])
  buttons.value[next]?.focus()
}
</script>

<style scoped>
.segmented {
  display: inline-flex;
  align-items: stretch;
  gap: 2px;
  padding: 2px;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
}

.segment {
  min-height: var(--control-height);
  min-width: var(--target-min);
  padding: 0 0.75rem;
  border-radius: calc(var(--radius) - 2px);
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  font-weight: 500;
  white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.segmented-sm .segment {
  min-height: var(--control-height-sm);
  padding: 0 0.5rem;
  font-size: var(--text-xs);
}

.segment:hover:not(:disabled):not(.active) {
  color: var(--text-primary);
  background: var(--bg-surface-hover);
}

.segment.active {
  background: var(--bg-surface);
  color: var(--text-primary);
  font-weight: 600;
  box-shadow: var(--shadow);
}

.segment:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
