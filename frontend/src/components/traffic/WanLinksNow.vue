<template>
  <!-- The "Now" area (build plan 7.5 step 4, review P1 and O1): each WAN link against its
       capacity over the last five complete minutes, whatever window the page shows below. One
       row per link, grouped by site, busiest first (the API's order). -->
  <section class="table-card now-area" aria-labelledby="now-title">
    <div class="section-header">
      <div>
        <h2 id="now-title" class="section-title now-title">Now</h2>
        <span class="section-hint">{{ hint }}</span>
      </div>
    </div>

    <div v-if="!links.length" class="now-empty text-muted">
      <template v-if="loading">Loading WAN links…</template>
      <template v-else>
        No WAN links in scope. Mark a router's link to the internet or a provider as WAN in its
        interface settings to see it here.
      </template>
    </div>

    <div v-for="group in groups" :key="group.label" class="now-site">
      <div class="now-site-label">{{ group.label }}</div>
      <div
        v-for="link in group.links"
        :key="`${link.exporter_id}-${link.interface_idx}`"
        class="now-row"
      >
        <div class="now-name">
          <span class="font-bold">{{ link.name }}</span>
          <span class="text-muted text-xs now-sub">{{ link.exporter_hostname }} · {{ speedText(link) }}</span>
        </div>

        <!-- A link with a speed: a capacity bar per direction. Without one there is nothing to
             measure against, so figures only, and the way to set it. -->
        <div v-if="link.speed_mbps" class="now-bars">
          <div v-for="d in directions(link)" :key="d.key" class="cap-line">
            <span class="cap-dir text-xs text-muted">{{ d.label }}</span>
            <span class="cap-track" role="img" :aria-label="d.aria">
              <span
                v-if="d.band"
                class="cap-usual"
                :style="{ left: `${d.band.low}%`, width: `${d.band.high - d.band.low}%` }"
                :title="d.bandTitle"
              ></span>
              <span class="cap-now" :class="d.key" :style="{ width: `${d.nowPct}%` }"></span>
              <span
                v-if="d.peakPct != null"
                class="cap-peak"
                :style="{ left: `calc(${d.peakPct}% - 1px)` }"
                :title="d.peakTitle"
              ></span>
            </span>
            <span class="cap-value tnum text-xs">{{ formatBps(d.bps) }}</span>
          </div>
        </div>
        <div v-else class="now-bars now-figures">
          <span class="tnum text-xs"><span class="text-muted">In</span> {{ formatBps(link.in_bps) }}</span>
          <span class="tnum text-xs"><span class="text-muted">Out</span> {{ formatBps(link.out_bps) }}</span>
          <span v-if="usualText(link)" class="text-xs text-muted">{{ usualText(link) }}</span>
        </div>

        <div class="now-status">
          <span
            v-if="link.utilization_percentage != null"
            class="now-util tnum"
            :class="utilClass(link.utilization_percentage)"
            :title="'The busier direction against the link speed'"
          >{{ link.utilization_percentage.toFixed(0) }}%</span>
          <button
            v-else-if="isAdmin"
            type="button"
            class="btn-secondary btn-small"
            @click="$emit('set-speed', link)"
          >Set speed</button>
          <span v-else class="text-xs text-muted" title="An admin can set it in the router's interface settings">No speed set</span>
          <span
            v-for="(note, i) in notes(link)"
            :key="i"
            class="now-note text-xs"
            :class="note.tone"
            :title="note.title || null"
          >{{ note.text }}</span>
        </div>
      </div>
    </div>

    <slot />
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { formatBps } from '../../utils/format.js'

const props = defineProps({
  // GET /bandwidth/wan-links: { window_start, window_end, day_start, timezone, links }.
  data: { type: Object, default: null },
  scopeLabel: { type: String, default: '' },
  isAdmin: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
})
defineEmits(['set-speed'])

const links = computed(() => props.data?.links || [])

// Hours and "today" are the server's (the usual range is worked out in its timezone), so times
// are shown in that zone too, whatever the browser's.
const tz = computed(() => props.data?.timezone || undefined)

function fmt(iso, opts) {
  if (!iso) return ''
  const d = new Date(iso)
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz.value }).format(d)
  } catch {
    return new Intl.DateTimeFormat(undefined, opts).format(d)
  }
}
const clock = (iso) => fmt(iso, { hour: '2-digit', minute: '2-digit', hour12: false })
const dayClock = (iso) => fmt(iso, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
function sameDay(iso) {
  const day = (v) => fmt(v, { year: 'numeric', month: '2-digit', day: '2-digit' })
  return props.data?.window_end && day(iso) === day(props.data.window_end)
}
const when = (iso) => (sameDay(iso) ? clock(iso) : dayClock(iso))

const hint = computed(() => {
  const parts = ['Last 5 complete minutes']
  if (props.scopeLabel) parts.push(props.scopeLabel)
  if (props.data?.window_end) parts.push(`to ${clock(props.data.window_end)}`)
  return parts.join(' · ')
})

const groups = computed(() => {
  const out = []
  for (const link of links.value) {
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

function formatMbps(mbps) {
  if (mbps >= 1000 && mbps % 1000 === 0) return `${mbps / 1000} Gb/s`
  return `${mbps} Mb/s`
}

function speedText(link) {
  if (!link.speed_mbps) return 'speed not set'
  const speed = formatMbps(link.speed_mbps)
  if (link.speed_source === 'snmp') return `${speed} (from SNMP)`
  // A typed speed that differs from the port's own: the contract, not the port (8.3).
  if (link.snmp_speed_mbps && link.snmp_speed_mbps !== link.speed_mbps) {
    return `${speed}, set by hand (port ${formatMbps(link.snmp_speed_mbps)})`
  }
  return speed
}

const pct = (bps, link) => Math.min(100, Math.max(0, (bps / (link.speed_mbps * 1e6)) * 100))

// Where the usual range came from, in words: "weekdays at 17:00", "Fridays at 17:00".
function usualWhen(link) {
  const end = props.data?.window_end
  if (!end) return 'this hour'
  const hour = `${fmt(end, { hour: '2-digit', hour12: false })}:00`
  if (link.usual_grouping === 'day_of_week') return `${fmt(end, { weekday: 'long' })}s at ${hour}`
  // Saturday and Sunday in the server's zone, read in English whatever the display language.
  let weekday
  try {
    weekday = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: tz.value }).format(new Date(end))
  } catch {
    weekday = new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(new Date(end))
  }
  const weekend = weekday === 'Sat' || weekday === 'Sun'
  return `${weekend ? 'weekends' : 'weekdays'} at ${hour}`
}

function bandTitle(link, band) {
  return `Usual for ${usualWhen(link)}: ${formatBps(band.low_bps)} to ${formatBps(band.high_bps)} (hourly average, from ${band.samples} past hours)`
}

function directions(link) {
  return ['in', 'out'].map((key) => {
    const bps = link[`${key}_bps`]
    const band = link[`usual_${key}`]
    const peak = link[`today_peak_${key}_bps`]
    const peakHour = link[`today_peak_${key}_hour`]
    const label = key === 'in' ? 'In' : 'Out'
    const nowPct = pct(bps, link)
    const aria = [`${label} ${formatBps(bps)}, ${nowPct.toFixed(0)}% of ${formatMbps(link.speed_mbps)}`]
    if (band) aria.push(`usual ${formatBps(band.low_bps)} to ${formatBps(band.high_bps)}`)
    if (peak != null) aria.push(`today's peak ${formatBps(peak)}`)
    return {
      key,
      label,
      bps,
      nowPct,
      band: band ? { low: pct(band.low_bps, link), high: pct(band.high_bps, link) } : null,
      bandTitle: band ? bandTitle(link, band) : '',
      peakPct: peak != null ? pct(peak, link) : null,
      peakTitle: peak != null
        ? `Today's busiest minute: ${formatBps(peak)} (${pct(peak, link).toFixed(0)}%), in the ${clock(peakHour)} hour`
        : '',
      aria: aria.join(', '),
    }
  })
}

function usualText(link) {
  const b = link.usual_in
  if (!b) return ''
  const low = formatBps(b.low_bps)
  const high = formatBps(b.high_bps)
  return low === high ? `usual in ${low}` : `usual in ${low}–${high}`
}

function utilClass(u) {
  if (u >= 80) return 'tone-down'
  if (u >= 60) return 'tone-warn'
  return 'tone-up'
}

// What needs saying about a link, most serious first: the port's state, an open traffic alert,
// then traffic above its usual range for this hour.
function notes(link) {
  const out = []
  if (link.link_state === 'down') {
    out.push({ text: link.down_since ? `Down since ${when(link.down_since)}` : 'Down', tone: 'tone-down', title: 'SNMP reports the port down' })
  } else if (link.link_state === 'shut_down') {
    out.push({ text: 'Shut down', tone: 'text-muted', title: 'Disabled on the router (admin status down)' })
  } else if (link.link_state === 'not_answering') {
    out.push({
      text: 'SNMP not answering',
      tone: 'tone-warn',
      title: link.snmp_last_ok_at ? `Last answer ${dayClock(link.snmp_last_ok_at)}: the port's state is unknown` : "The port's state is unknown",
    })
  }
  for (const a of link.alerts || []) {
    if (a.kind === 'silent') {
      out.push({ text: `No traffic data since ${when(a.since)}`, tone: 'tone-warn', title: 'Traffic alert: link down, or the router stopped exporting' })
    } else {
      const dir = a.direction === 'out' ? 'out' : 'in'
      const peak = a.peak_pct != null ? `, peak ${a.peak_pct.toFixed(0)}%` : ''
      out.push({ text: `Busy since ${when(a.since)} (${dir}${peak})`, tone: 'tone-down', title: 'Open traffic alert' })
    }
  }
  const above = [['in', link.above_usual_in_pct], ['out', link.above_usual_out_pct]]
    .filter(([, v]) => v != null)
    .sort((a, b) => b[1] - a[1])
  if (above.length) {
    const [dir, value] = above[0]
    const band = link[`usual_${dir}`]
    out.push({
      text: `${dir === 'in' ? 'In' : 'Out'} ${value.toFixed(0)}% above usual`,
      tone: 'tone-warn',
      title: band ? bandTitle(link, band) : null,
    })
  } else if (!link.usual_in && !link.usual_out && link.link_state !== 'shut_down') {
    out.push({
      text: 'No usual range yet',
      tone: 'text-muted',
      title: 'Worked out from past weeks at this hour; it shows once there are 3 days with data for it',
    })
  }
  return out
}
</script>

<style scoped>
.now-area {
  margin-bottom: 1rem;
}
.now-title {
  margin: 0;
}
.now-empty {
  padding: 0.75rem 1rem;
  font-size: var(--text-sm);
}
.now-site + .now-site {
  border-top: 1px solid var(--border-color);
}
.now-site-label {
  padding: 0.5rem 1rem 0.25rem;
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.now-row {
  display: grid;
  grid-template-columns: minmax(10rem, 1.1fr) minmax(12rem, 2fr) minmax(9rem, 1fr);
  gap: 0.75rem;
  align-items: center;
  padding: 0.4rem 1rem;
}
.now-name {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.now-sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.now-bars {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}
.now-figures {
  flex-direction: row;
  flex-wrap: wrap;
  gap: 0.25rem 0.75rem;
}
.cap-line {
  display: grid;
  grid-template-columns: 2rem 1fr 5.5rem;
  gap: 0.5rem;
  align-items: center;
}
.cap-track {
  position: relative;
  height: 0.6rem;
  border-radius: var(--radius-sm);
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  overflow: hidden;
}
/* The usual range is drawn over "now" as a bracket, so it stays visible where the bar reaches
   into it: a bar ending inside the bracket reads as normal at a glance. A steady link's range can
   be a hair wide, hence the minimum width. */
.cap-usual {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 4px;
  z-index: 1;
  background: color-mix(in srgb, var(--text-primary) 20%, transparent);
  border-left: 1px solid var(--text-secondary);
  border-right: 1px solid var(--text-secondary);
}
.cap-now {
  position: absolute;
  top: 2px;
  bottom: 2px;
  left: 0;
  border-radius: 2px;
}
.cap-now.in {
  background: var(--series-in);
}
.cap-now.out {
  background: var(--series-out);
}
.cap-peak {
  position: absolute;
  z-index: 2;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--text-primary);
}
.cap-value {
  text-align: right;
}
.now-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem 0.6rem;
}
.now-util {
  font-weight: 700;
  font-size: var(--text-lg);
  min-width: 3rem;
}
.tone-up {
  color: var(--status-text-up);
}
.tone-warn {
  color: var(--status-text-warn);
}
.tone-down {
  color: var(--status-text-down);
}
@media (max-width: 760px) {
  .now-row {
    grid-template-columns: 1fr;
    gap: 0.35rem;
  }
}
</style>
