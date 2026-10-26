<template>
  <div class="panel-header">
    <div class="panel-heading">
      <component :is="as" class="panel-title">{{ title }}</component>
      <p v-if="hint || $slots.hint" class="panel-hint">
        <slot name="hint">{{ hint }}</slot>
      </p>
    </div>
    <div v-if="$slots.actions" class="panel-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup>
// Standard section heading for cards and panels: sentence-case title, optional one-line hint,
// and right-aligned actions that wrap below the title on narrow screens.
defineProps({
  title: { type: String, required: true },
  hint: { type: String, default: '' },
  as: { type: String, default: 'h3', validator: (v) => ['h2', 'h3', 'h4'].includes(v) },
})
</script>

<style scoped>
.panel-header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.panel-heading {
  min-width: 0;
}

.panel-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--text-lg);
  font-weight: 700;
  line-height: 1.3;
  color: var(--text-primary);
}

.panel-hint {
  margin: 0.25rem 0 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.panel-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
</style>
