<template>
  <!-- One chart per WAN link (build plan 7.5 step 5, review O2), grouped by site. Opened first:
       links with an open alert or down time in the window, then the busiest; at most 6, so a
       large fleet doesn't open forty charts. The rest are one line each with a sparkline and
       open on click; "Show all" opens every chart. -->
  <div class="link-charts">
    <div v-for="group in groups" :key="group.label" class="link-site">
      <div class="link-site-label">{{ group.label }}</div>
      <div class="link-site-grid">
        <template v-for="link in group.links" :key="keyOf(link)">
          <div v-if="isOpen(link)" class="link-card">
            <div class="link-card-head">
              <div class="link-card-name">
                <span class="font-bold">{{ link.name }}</span>
                <span class="text-muted text-xs">{{ link.exporter_hostname }} · {{ axisNote(link) }}</span>
              </div>
              <button
                type="button"
                class="btn-text text-xs"
                :aria-label="`Close the chart of ${link.name}`"
                @click="toggle(link)"
              >Close</button>
            </div>
            <WanLinkChart :link="link" :timezone="timezone" />
          </div>
          <button
            v-else
            type="button"
            class="link-row"
            :aria-label="`Open the chart of ${link.name} on ${link.exporter_hostname}`"
            @click="toggle(link)"
          >
            <span class="link-row-name">
              <span class="font-bold">{{ link.name }}</span>
              <span class="text-muted text-xs">{{ link.exporter_hostname }}</span>
            </span>
            <svg class="sparkline" viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden="true">
              <polyline :points="sparkline(link)" />
            </svg>
            <span class="text-xs tnum text-muted link-row-peak">{{ peakText(link) }}</span>
          </button>
        </template>
      </div>
    </div>
    <div v-if="links.length > defaultOpen.size" class="link-charts-more">
      <button type="button" class="btn-secondary btn-small" @click="showAll = !showAll">
        {{ showAll ? 'Show fewer' : `Show all ${links.length} charts` }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { formatBps } from '../../utils/format.js'
import WanLinkChart from './WanLinkChart.vue'

const MAX_OPEN = 6

const props = defineProps({
  // GET /bandwidth/wan-links?window=…&series=1 rows, in the API's order (by site, busiest first).
  links: { type: Array, default: () => [] },
  timezone: { type: String, default: '' },
})

const keyOf = (link) => `${link.exporter_id}-${link.interface_idx}`

// The user's own opens and closes, kept across the once-a-minute updates.
const toggled = ref(new Map())
const showAll = ref(false)

function hadTrouble(link) {
  return (link.alerts || []).length > 0 || link.link_state === 'down' || (link.series?.down_spans || []).length > 0
}

// Links opened without a click: trouble first, then the busiest in the window (% of speed, or
// bits/s for links without one, after those with a speed).
const defaultOpen = computed(() => {
  const ranked = [...props.links].sort((a, b) => {
    const ta = hadTrouble(a) ? 1 : 0
    const tb = hadTrouble(b) ? 1 : 0
    if (ta !== tb) return tb - ta
    const pa = a.series?.window_peak_percentage
    const pb = b.series?.window_peak_percentage
    if ((pa == null) !== (pb == null)) return pa == null ? 1 : -1
    if (pa != null && pb != null && pa !== pb) return pb - pa
    return (b.series?.window_peak_bps || 0) - (a.series?.window_peak_bps || 0)
  })
  return new Set(ranked.slice(0, MAX_OPEN).map(keyOf))
})

function isOpen(link) {
  if (showAll.value) return true
  const k = keyOf(link)
  return toggled.value.has(k) ? toggled.value.get(k) : defaultOpen.value.has(k)
}

function toggle(link) {
  const next = new Map(toggled.value)
  next.set(keyOf(link), !isOpen(link))
  toggled.value = next
  if (showAll.value && !next.get(keyOf(link))) showAll.value = false
}

const groups = computed(() => {
  const out = []
  for (const link of props.links) {
    const label = link.site || 'No site'
    let g = out.find((x) => x.label === label)
    if (!g) {
      g = { label, links: [] }
      out.push(g)
    }
    g.links.push(link)
  }
  return out
})

function axisNote(link) {
  if (!link.speed_mbps) return 'no speed set: in bits/s'
  return `% of ${link.speed_mbps >= 1000 && link.speed_mbps % 1000 === 0 ? `${link.speed_mbps / 1000} Gb/s` : `${link.speed_mbps} Mb/s`}`
}

// The busier direction per bucket, scaled to the speed (or to the link's own busiest bucket).
function sparkline(link) {
  const s = link.series
  if (!s?.times?.length) return ''
  const values = s.times.map((_, i) => Math.max(s.in_bps[i] || 0, s.out_bps[i] || 0))
  const top = link.speed_mbps ? link.speed_mbps * 1e6 : Math.max(1, ...values)
  const n = Math.max(1, values.length - 1)
  return values.map((v, i) => `${((i / n) * 100).toFixed(1)},${(20 - Math.min(1, v / top) * 19).toFixed(1)}`).join(' ')
}

function peakText(link) {
  const s = link.series
  if (!s) return ''
  if (s.window_peak_percentage != null) return `peak ${s.window_peak_percentage.toFixed(0)}%`
  return `peak ${formatBps(s.window_peak_bps)}`
}
</script>

<style scoped>
.link-site + .link-site {
  margin-top: 0.75rem;
}
.link-site-label {
  margin-bottom: 0.35rem;
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.link-site-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(26rem, 1fr));
  gap: 0.75rem;
}
.link-card {
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 0.5rem 0.75rem;
  background: var(--bg-surface);
}
.link-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.5rem;
  margin-bottom: 0.25rem;
}
.link-card-name {
  display: flex;
  flex-direction: column;
}
.link-row {
  display: grid;
  grid-template-columns: minmax(8rem, 1fr) minmax(6rem, 2fr) auto;
  gap: 0.75rem;
  align-items: center;
  width: 100%;
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  background: var(--bg-surface);
  color: inherit;
  text-align: left;
  cursor: pointer;
  align-self: start;
}
.link-row:hover {
  background: var(--bg-surface-hover);
}
.link-row-name {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.sparkline {
  width: 100%;
  height: 20px;
}
.sparkline polyline {
  fill: none;
  stroke: var(--series-in);
  stroke-width: 1;
  vector-effect: non-scaling-stroke;
}
.link-charts-more {
  margin-top: 0.75rem;
}
</style>
