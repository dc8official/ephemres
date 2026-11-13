// The shared endpoint filter in the browser (v3.3.6 build plan 4.3): site, location, tags,
// device type and search text, kept in the page URL (?site=Abuja&tag=mpls&tag=core&q=gw) so a
// filtered view can be bookmarked or shared, and carried between pages by the header links.
//
// The browser never decides which endpoints match: it asks GET /endpoints/match, the same
// server-side rules reports, exports and the map use (services/endpoint_filter.py). Pages keep
// their own full list for live updates and whole-fleet counts and use `matchIds` to narrow it.
import { computed, ref, watch, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { matchEndpoints } from '../services/api.js'

export const FILTER_KEYS = ['site', 'location', 'tag', 'type', 'q']

function asList(value) {
  if (value === undefined || value === null || value === '') return []
  return (Array.isArray(value) ? value : [value]).filter((v) => typeof v === 'string' && v.trim() !== '')
}

// The filter part of a route query, as plain values ready to put in another URL.
export function filterQueryFrom(query) {
  const out = {}
  for (const key of FILTER_KEYS) {
    const values = key === 'q' ? asList(query?.[key]).slice(0, 1) : asList(query?.[key])
    if (values.length) out[key] = key === 'q' ? values[0] : (values.length === 1 ? values[0] : values)
  }
  return out
}

// API parameters (GET query) or body (POST): lists for every field, q a string.
export function filterParams(filter) {
  return {
    site: filter.site,
    location: filter.location,
    tag: filter.tag,
    type: filter.type,
    q: filter.q || undefined,
  }
}

const MATCH_DEBOUNCE_MS = 250

export function useEndpointFilter() {
  const route = useRoute()
  const router = useRouter()

  // The filter as the URL has it; every change goes through the URL.
  const filter = computed(() => {
    const q = route.query
    return {
      site: asList(q.site),
      location: asList(q.location),
      tag: asList(q.tag),
      type: asList(q.type),
      q: asList(q.q)[0] || '',
    }
  })

  const isActive = computed(() => {
    const f = filter.value
    return !!(f.site.length || f.location.length || f.tag.length || f.type.length || f.q)
  })

  // Only the filter keys of the current URL, for links to other pages.
  const filterQuery = computed(() => filterQueryFrom(route.query))

  // Changes some filter fields, keeping the page's other query keys (view, sort, status...).
  function setFilter(partial) {
    const query = { ...route.query }
    for (const [key, value] of Object.entries(partial)) {
      if (!FILTER_KEYS.includes(key)) continue
      const values = key === 'q' ? (value ? [String(value)] : []) : asList(value)
      if (values.length === 0) delete query[key]
      else query[key] = key === 'q' ? values[0] : values
    }
    router.replace({ query })
  }

  function clearFilter() {
    setFilter({ site: [], location: [], tag: [], type: [], q: '' })
  }

  // Which endpoints match, from the server. null while there is no filter (everything matches).
  const matchIds = ref(null)
  const matchTotal = ref(null)
  const matchDescription = ref('')
  const matchLoading = ref(false)
  let timer = null
  let seq = 0

  async function refreshMatch() {
    if (!isActive.value) {
      matchIds.value = null
      matchTotal.value = null
      matchDescription.value = ''
      return
    }
    const mine = ++seq
    matchLoading.value = true
    try {
      const res = await matchEndpoints(filterParams(filter.value))
      if (mine !== seq) return // a newer filter is already on its way
      const data = res.data?.data || {}
      matchIds.value = new Set(data.ids || [])
      matchTotal.value = data.total ?? (data.ids || []).length
      matchDescription.value = data.description || ''
    } catch (err) {
      if (mine === seq) {
        // Show nothing rather than everything: an unfiltered view under a filter bar would
        // read as "these all match".
        matchIds.value = new Set()
        matchTotal.value = 0
      }
      console.error('Failed to apply endpoint filter:', err)
    } finally {
      if (mine === seq) matchLoading.value = false
    }
  }

  watch(
    () => JSON.stringify(filter.value),
    () => {
      clearTimeout(timer)
      timer = setTimeout(refreshMatch, MATCH_DEBOUNCE_MS)
    },
    { immediate: false }
  )
  refreshMatch()
  onUnmounted(() => clearTimeout(timer))

  // True when the endpoint (an object with `id`, or an id) is in the filter.
  function matches(epOrId) {
    if (matchIds.value === null) return true
    const id = typeof epOrId === 'object' && epOrId !== null ? epOrId.id : epOrId
    return matchIds.value.has(String(id))
  }

  return {
    filter,
    isActive,
    filterQuery,
    setFilter,
    clearFilter,
    matchIds,
    matchTotal,
    matchDescription,
    matchLoading,
    refreshMatch,
    matches,
  }
}
