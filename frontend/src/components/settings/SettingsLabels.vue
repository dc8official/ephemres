<template>
  <!-- Rename or merge sites, locations and tags across every endpoint (v3.3.6 build plan 4.4).
       Values are free text, so misspellings happen; renaming one onto another merges them. -->
  <div class="settings-card">
    <div class="card-header">
      <h2 class="card-title font-display">🏷️ Sites, locations and tags</h2>
    </div>
    <p class="card-desc">
      Rename a value on every endpoint that has it. To merge two spellings, rename the wrong one to
      the right one. Leave the new name empty to remove the value.
    </p>

    <div class="label-columns">
      <section v-for="col in columns" :key="col.field" class="label-column" :aria-labelledby="`labels-${col.field}`">
        <h3 :id="`labels-${col.field}`" class="column-title">{{ col.title }}</h3>
        <!-- Locations belong to a site: rename one within its site, or everywhere. -->
        <label v-if="col.field === 'location'" class="column-scope">
          <span>Within site</span>
          <select v-model="locationSite" class="form-select">
            <option value="">All sites</option>
            <option v-for="s in sites" :key="s.value" :value="s.value">{{ s.value }}</option>
          </select>
        </label>
        <p v-if="!col.items.value.length" class="empty-note">None yet.</p>
        <ul v-else class="label-list">
          <li v-for="item in col.items.value" :key="item.value" class="label-row">
            <template v-if="editing && editing.field === col.field && editing.from === item.value">
              <form class="rename-form" @submit.prevent="saveRename">
                <input
                  ref="renameInput"
                  v-model="editing.to"
                  class="form-input"
                  :list="`rename-${col.field}`"
                  :aria-label="`New name for ${item.value}`"
                  autocomplete="off"
                />
                <datalist :id="`rename-${col.field}`">
                  <option v-for="o in col.items.value" :key="o.value" :value="o.value" />
                </datalist>
                <button type="submit" class="btn-primary btn-small" :disabled="saving">
                  {{ saving ? 'Saving...' : (editing.to.trim() ? 'Rename' : 'Remove') }}
                </button>
                <button type="button" class="btn-secondary btn-small" @click="editing = null">Cancel</button>
              </form>
            </template>
            <template v-else>
              <span class="label-value">{{ item.value }}</span>
              <span class="label-count tnum">{{ item.count }}</span>
              <button
                type="button"
                class="btn-action"
                :aria-label="`Rename ${col.noun} ${item.value}`"
                @click="startRename(col.field, item.value)"
              >Rename</button>
            </template>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import { renameEndpointLabel } from '../../services/api.js'
import { useLabelSuggestions, refreshSuggestions } from '../../composables/useLabelSuggestions.js'

const toast = useToast()
const { sites, allLocations, tags, loadLocationsFor, locationsBySite } = useLabelSuggestions()

const locationSite = ref('')
const siteLocations = ref([])
watch(
  [locationSite, allLocations],
  async ([site]) => {
    siteLocations.value = await loadLocationsFor(site)
  },
  { immediate: true }
)

const columns = [
  { field: 'site', title: 'Sites', noun: 'site', items: sites },
  { field: 'location', title: 'Locations', noun: 'location', items: siteLocations },
  { field: 'tag', title: 'Tags', noun: 'tag', items: tags },
]

const editing = ref(null)
const saving = ref(false)
const renameInput = ref(null)

function startRename(field, value) {
  editing.value = { field, from: value, to: value }
  nextTick(() => {
    const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
    el?.focus()
    el?.select()
  })
}

async function saveRename() {
  const { field, from, to } = editing.value
  if (to.trim() === from) {
    editing.value = null
    return
  }
  saving.value = true
  try {
    const res = await renameEndpointLabel({
      field,
      from_value: from,
      to_value: to.trim() || null,
      site: field === 'location' ? locationSite.value || null : null,
    })
    const changed = res.data?.data?.changed ?? 0
    const target = res.data?.data?.to
    toast.add({
      severity: 'success',
      summary: target ? 'Renamed' : 'Removed',
      detail: target
        ? `'${from}' is now '${target}' on ${changed} endpoint${changed === 1 ? '' : 's'}.`
        : `'${from}' removed from ${changed} endpoint${changed === 1 ? '' : 's'}.`,
      life: 4000,
    })
    editing.value = null
    locationsBySite.value = {}
    await refreshSuggestions()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Rename failed',
      detail: err.response?.data?.detail || 'Failed to rename.',
      life: 5000,
    })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.settings-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg, 12px);
  padding: 1.5rem;
  margin-top: 1.5rem;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.card-title {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.card-desc {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin: 0 0 1.25rem;
  line-height: 1.5;
}

.label-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1.5rem;
}

.column-title {
  font-size: 0.875rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 0.5rem;
}

.column-scope {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 0.5rem;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.empty-note {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin: 0;
}

.label-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}

.label-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-color);
  font-size: 0.85rem;
}

.label-value {
  flex: 1;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.label-count {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.btn-action {
  background: var(--bg-surface-selected);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 4px;
  cursor: pointer;
}

.btn-action:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.rename-form {
  display: flex;
  flex: 1;
  gap: 6px;
  align-items: center;
}

.rename-form .form-input {
  flex: 1;
  min-width: 0;
  padding: 5px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface-selected);
  color: var(--text-primary);
  font-size: 0.85rem;
}

.form-select {
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface-selected);
  color: var(--text-primary);
  font-size: 0.8rem;
}
</style>
