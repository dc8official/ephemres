<template>
  <!-- A WAN link's traffic alert rule in the interface editors (v3.3.6 build plan 7.4):
       Default (Settings → Alerts), Custom threshold and minutes, or Off. A link without a
       speed can't be watched, which the control says instead of offering choices. -->
  <div class="iface-alert">
    <span class="iface-alert-label">Traffic alert</span>
    <template v-if="hasSpeed">
      <select
        :value="modelValue.mode"
        class="form-select iface-alert-select"
        aria-label="Traffic alert rule"
        @change="update({ mode: $event.target.value })"
      >
        <option value="default">Default (Settings → Alerts)</option>
        <option value="custom">Custom</option>
        <option value="off">Off</option>
      </select>
      <template v-if="modelValue.mode === 'custom'">
        <span>above</span>
        <select
          :value="modelValue.threshold_pct"
          class="form-select iface-alert-num"
          aria-label="Threshold"
          @change="update({ threshold_pct: Number($event.target.value) })"
        >
          <option v-for="n in [70, 75, 80, 85, 90, 95]" :key="n" :value="n">{{ n }}%</option>
        </select>
        <span>for</span>
        <select
          :value="modelValue.minutes"
          class="form-select iface-alert-num"
          aria-label="Minutes"
          @change="update({ minutes: Number($event.target.value) })"
        >
          <option v-for="n in [5, 10, 15, 30, 60]" :key="n" :value="n">{{ n }} min</option>
        </select>
      </template>
    </template>
    <span v-else class="iface-alert-hint">Set a speed to watch this link</span>
  </div>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Object, required: true },
  hasSpeed: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

function update(change) {
  emit('update:modelValue', { ...props.modelValue, ...change })
}
</script>

<style scoped>
.iface-alert {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.iface-alert-label {
  font-weight: 600;
  color: var(--text-muted);
}

.iface-alert-select {
  width: auto;
  min-width: 13rem;
  padding: 4px 8px;
  font-size: 12px;
}

.iface-alert-num {
  width: auto;
  padding: 4px 8px;
  font-size: 12px;
}

.iface-alert-hint {
  color: var(--text-muted);
  font-style: italic;
}
</style>
