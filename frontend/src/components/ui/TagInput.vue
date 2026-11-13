<template>
  <!-- Tags as removable chips plus a text box with autocomplete. Enter, comma or Tab adds what
       was typed; Backspace in an empty box removes the last chip. -->
  <div class="tag-input" :class="{ compact, disabled }" @click="focusInput">
    <span v-for="tag in modelValue" :key="tag" class="tag-chip">
      {{ tag }}
      <button
        v-if="!disabled"
        type="button"
        class="tag-remove"
        :aria-label="`Remove tag ${tag}`"
        @click.stop="remove(tag)"
      >×</button>
    </span>
    <input
      ref="inputEl"
      v-model="draft"
      type="text"
      class="tag-draft"
      :list="listId"
      :placeholder="modelValue.length ? '' : placeholder"
      :aria-label="ariaLabel"
      :disabled="disabled"
      autocomplete="off"
      @keydown="onKeydown"
      @change="onPicked"
      @blur="commit"
    />
    <datalist :id="listId">
      <option v-for="s in remainingSuggestions" :key="s" :value="s" />
    </datalist>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  // Values offered as you type (tags already in use).
  suggestions: { type: Array, default: () => [] },
  placeholder: { type: String, default: 'Add a tag' },
  ariaLabel: { type: String, default: 'Tags' },
  // Smaller height, for filter bars.
  compact: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

// Each input needs its own datalist id (several tag inputs can be on one page).
const listId = `tag-suggestions-${Math.random().toString(36).slice(2, 10)}`
const inputEl = ref(null)
const draft = ref('')

// Same rules as the server (services/endpoint_filter.py): lower case, single spaces, and no
// commas, quotes, backslashes or braces (a comma is how you separate tags here anyway).
function clean(raw) {
  return String(raw || '').replace(/[{}"\\]/g, '').replace(/\s+/g, ' ').trim().toLowerCase()
}

const remainingSuggestions = computed(() =>
  props.suggestions.filter((s) => !props.modelValue.includes(String(s).toLowerCase()))
)

function add(raw) {
  const pieces = String(raw || '').split(',').map(clean).filter(Boolean)
  if (!pieces.length) return
  const next = [...props.modelValue]
  for (const p of pieces) if (!next.includes(p)) next.push(p)
  emit('update:modelValue', next)
}

function remove(tag) {
  emit('update:modelValue', props.modelValue.filter((t) => t !== tag))
}

function commit() {
  if (draft.value.trim()) {
    add(draft.value)
    draft.value = ''
  }
}

// Picking a suggestion from the list fires `change` with the full value.
function onPicked() {
  if (props.suggestions.some((s) => String(s).toLowerCase() === clean(draft.value))) commit()
}

function onKeydown(e) {
  if (e.key === 'Enter' || e.key === ',' || (e.key === 'Tab' && draft.value.trim())) {
    e.preventDefault()
    commit()
  } else if (e.key === 'Backspace' && !draft.value && props.modelValue.length) {
    remove(props.modelValue[props.modelValue.length - 1])
  }
}

function focusInput() {
  inputEl.value?.focus()
}
</script>

<style scoped>
.tag-input {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  min-height: var(--control-height, 34px);
  padding: 3px 6px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--bg-surface-selected);
  cursor: text;
}

.tag-input:focus-within {
  border-color: #0ea5e9;
}

.tag-input.compact {
  min-width: 12rem;
  max-width: 22rem;
}

.tag-input.disabled {
  opacity: 0.6;
  cursor: default;
}

.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 1px 4px 1px 8px;
  border-radius: 999px;
  background: rgba(14, 165, 233, 0.15);
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.6;
  white-space: nowrap;
}

.tag-remove {
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  padding: 0 3px;
  cursor: pointer;
}

.tag-remove:hover,
.tag-remove:focus-visible {
  color: var(--text-primary);
}

.tag-draft {
  flex: 1 1 5rem;
  min-width: 5rem;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  padding: 3px 2px;
}
</style>
