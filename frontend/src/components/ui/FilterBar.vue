<template>
  <!-- The shared endpoint filter bar (v3.3.6 build plan 4.3, option B): one per page, its state
       in the URL, the matching done on the server. Pages pass the useEndpointFilter() instance
       they also read `matches` from, and add their own controls (sort, counts) in the slot. -->
  <div class="filter-bar" role="search">
    <input
      v-model="searchDraft"
      type="search"
      class="form-input filter-search"
      placeholder="Search name, address, site, location or tag"
      aria-label="Search endpoints"
    />
    <label class="filter-field">
      <span>Site</span>
      <select :value="site" class="form-select" @change="onSite($event.target.value)">
        <option value="">All sites</option>
        <option v-for="s in siteOptions" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>
    </label>
    <label class="filter-field">
      <span>Location</span>
      <select
        :value="location"
        class="form-select"
        :disabled="!locationOptions.length && !location"
        @change="state.setFilter({ location: $event.target.value || [] })"
      >
        <option value="">{{ site ? 'All in this site' : 'All locations' }}</option>
        <option v-for="l in locationOptions" :key="l.value" :value="l.value">{{ l.label }}</option>
      </select>
    </label>
    <div class="filter-field">
      <span id="filter-tags-label">Tags</span>
      <TagInput
        :model-value="state.filter.value.tag"
        :suggestions="tags.map((t) => t.value)"
        placeholder="Any tag"
        aria-label="Filter by tags (any of them)"
        compact
        @update:model-value="(v) => state.setFilter({ tag: v })"
      />
    </div>
    <label v-if="showType" class="filter-field">
      <span>Type</span>
      <select :value="type" class="form-select" @change="state.setFilter({ type: $event.target.value || [] })">
        <option value="">All types</option>
        <option v-for="t in typeOptions" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>
    </label>
    <slot />
    <button v-if="state.isActive.value" type="button" class="btn-text clear-filters" @click="state.clearFilter()">
      Clear filters
    </button>
  </div>
</template>

<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import TagInput from './TagInput.vue'
import { useLabelSuggestions } from '../../composables/useLabelSuggestions.js'

const props = defineProps({
  // The page's useEndpointFilter() instance.
  state: { type: Object, required: true },
  showType: { type: Boolean, default: true },
})

const { sites, tags, deviceTypes, allLocations, loadLocationsFor } = useLabelSuggestions()

// The bar picks one site, one location and one type at a time; a URL may hold more (any of
// them match), in which case the first is shown.
const site = computed(() => props.state.filter.value.site[0] || '')
const location = computed(() => props.state.filter.value.location[0] || '')
const type = computed(() => props.state.filter.value.type[0] || '')

// Options with counts; a value from the URL that no endpoint has any more is still listed.
function withCurrent(list, current) {
  const options = list.map((x) => ({ value: x.value, label: `${x.value} (${x.count})` }))
  if (current && !list.some((x) => x.value.toLowerCase() === current.toLowerCase())) {
    options.unshift({ value: current, label: `${current} (0)` })
  }
  return options
}

const siteOptions = computed(() => withCurrent(sites.value, site.value))
const typeOptions = computed(() => withCurrent(deviceTypes.value, type.value))

const siteLocations = ref([])
watch(
  [site, allLocations],
  async ([s]) => {
    siteLocations.value = await loadLocationsFor(s)
  },
  { immediate: true }
)
const locationOptions = computed(() => withCurrent(siteLocations.value, location.value))

// A location belongs to its site, so picking another site starts the location over.
function onSite(value) {
  props.state.setFilter({ site: value || [], location: [] })
}

// Search goes to the URL (and the server) once typing pauses.
const searchDraft = ref(props.state.filter.value.q)
let searchTimer = null
watch(searchDraft, (v) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => props.state.setFilter({ q: v.trim() }), 300)
})
// Cleared from elsewhere (Clear filters, a link): follow the URL.
watch(
  () => props.state.filter.value.q,
  (q) => {
    if (q !== searchDraft.value.trim()) searchDraft.value = q
  }
)
onUnmounted(() => clearTimeout(searchTimer))
</script>

<style scoped>
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 0.75rem;
  margin: 1rem 0;
}

.filter-search {
  flex: 1 1 14rem;
  min-height: var(--control-height);
  max-width: 22rem;
}

.filter-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.filter-field .form-select {
  min-height: var(--control-height);
  max-width: 14rem;
}

.clear-filters {
  align-self: center;
  font-size: var(--text-sm);
  color: var(--text-secondary);
  text-decoration: underline;
}
</style>
