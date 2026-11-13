// Sites, locations, tags and device types in use (GET /endpoints/suggestions), for autocomplete
// in the endpoint form and the options of the filter bar. One copy for the whole app: call
// refreshSuggestions() after anything that changes them (saving an endpoint, bulk assign,
// rename) and every page sees the new values.
import { ref } from 'vue'
import { getEndpointSuggestions } from '../services/api.js'

const sites = ref([])
const allLocations = ref([])
const tags = ref([])
const deviceTypes = ref([])
// Locations per site, fetched when a site is picked: { 'abuja': [{ value, count }] }.
const locationsBySite = ref({})
let loaded = null

async function load() {
  try {
    const res = await getEndpointSuggestions()
    const d = res.data?.data || {}
    sites.value = d.sites || []
    allLocations.value = d.locations || []
    tags.value = d.tags || []
    deviceTypes.value = d.device_types || []
    locationsBySite.value = {}
  } catch (err) {
    console.error('Failed to load site, location and tag suggestions:', err)
  }
}

export function refreshSuggestions() {
  loaded = load()
  return loaded
}

// Locations used within `site` (case-insensitive), or every location without one.
export async function loadLocationsFor(site) {
  if (!site) return allLocations.value
  const key = site.toLowerCase()
  if (!locationsBySite.value[key]) {
    try {
      const res = await getEndpointSuggestions(site)
      locationsBySite.value = { ...locationsBySite.value, [key]: res.data?.data?.locations || [] }
    } catch (err) {
      console.error('Failed to load locations for site:', err)
      return []
    }
  }
  return locationsBySite.value[key]
}

export function useLabelSuggestions() {
  if (!loaded) refreshSuggestions()
  return { sites, allLocations, tags, deviceTypes, locationsBySite, refreshSuggestions, loadLocationsFor }
}
