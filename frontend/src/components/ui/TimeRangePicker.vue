<template>
  <div class="time-range">
    <SegmentedControl
      :model-value="modelValue"
      :options="options"
      :aria-label="ariaLabel"
      :size="size"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <div v-if="allowCustom && modelValue === CUSTOM" class="custom-range">
      <label class="custom-field">
        <span>From</span>
        <input
          type="datetime-local"
          class="form-input"
          :value="start"
          @input="emit('update:start', $event.target.value)"
        />
      </label>
      <label class="custom-field">
        <span>To</span>
        <input
          type="datetime-local"
          class="form-input"
          :value="end"
          @input="emit('update:end', $event.target.value)"
        />
      </label>
      <button type="button" class="btn-secondary btn-small" :disabled="!start || !end" @click="emit('apply')">
        Apply
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import SegmentedControl from './SegmentedControl.vue'

// The one time-window selector for every page: preset windows plus an optional custom range.
// v-model holds the window key ('1h', '24h', ... or 'custom'); v-model:start / v-model:end hold
// datetime-local strings for the custom range, and `apply` fires when the user confirms it.
const CUSTOM = 'custom'

const props = defineProps({
  modelValue: { type: String, required: true },
  windows: { type: Array, default: () => ['1h', '6h', '24h', '7d', '30d'] },
  allowCustom: { type: Boolean, default: false },
  start: { type: String, default: '' },
  end: { type: String, default: '' },
  ariaLabel: { type: String, default: 'Time range' },
  size: { type: String, default: 'md' },
})
const emit = defineEmits(['update:modelValue', 'update:start', 'update:end', 'apply'])

const LABELS = { '1h': '1h', '6h': '6h', '24h': '24h', '7d': '7d', '30d': '30d', '1y': '1y' }

const options = computed(() => {
  const presets = props.windows.map((w) => ({ value: w, label: LABELS[w] || w, title: `Last ${w}` }))
  return props.allowCustom ? [...presets, { value: CUSTOM, label: 'Custom', title: 'Custom range' }] : presets
})
</script>

<style scoped>
.time-range {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.custom-range {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 0.5rem;
}

.custom-field {
  display: inline-flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.custom-field .form-input {
  min-height: var(--control-height);
}
</style>
