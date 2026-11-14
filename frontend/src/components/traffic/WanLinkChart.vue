<template>
  <!-- One WAN link over the window (build plan 7.5 step 5, review O2). With a speed the y-axis is
       % of capacity with a 100% line, so links of different sizes read alike; without one it is
       bits/s and the hint says so. -->
  <div class="link-chart">
    <p class="sr-only">{{ summary }}</p>
    <div class="link-chart-canvas">
      <LineChart :data="chartData" :options="chartOptions" :plugins="[downSpansPlugin]" />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Line as LineChart } from 'vue-chartjs'
import { Chart } from 'chart.js'
import { formatBps } from '../../utils/format.js'
import { useChartTheme, withAlpha, readToken } from '../../composables/useChartTheme.js'
import { summarizeSeries } from '../../utils/chartSummary.js'

const props = defineProps({
  // A row of GET /bandwidth/wan-links?series=1 (with `series`).
  link: { type: Object, required: true },
  // Server timezone name, for the axis and tooltips.
  timezone: { type: String, default: '' },
})

const { theme } = useChartTheme()

const s = computed(() => props.link.series || { times: [], bucket_seconds: 3600 })
const speedBps = computed(() => (props.link.speed_mbps ? props.link.speed_mbps * 1e6 : null))
const hourly = computed(() => s.value.bucket_seconds >= 3600)

// % of the speed when there is one, else bits/s.
const toY = (v) => (v == null ? null : speedBps.value ? (v / speedBps.value) * 100 : v)

function fmtTime(iso) {
  const d = new Date(iso)
  const span = (s.value.times.length || 1) * s.value.bucket_seconds
  const opts = span <= 86400
    ? { hour: '2-digit', minute: '2-digit', hour12: false }
    : span <= 7 * 86400
      ? { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }
      : { month: 'short', day: 'numeric' }
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: props.timezone || undefined }).format(d)
  } catch {
    return new Intl.DateTimeFormat(undefined, opts).format(d)
  }
}

// The busier direction over the window: its usual range shows, the other's is one legend click away.
const busier = computed(() => {
  const max = (a) => Math.max(0, ...(a || []).filter((v) => v != null))
  return max(s.value.in_bps) >= max(s.value.out_bps) ? 'in' : 'out'
})

const chartData = computed(() => {
  const t = theme.value
  const series = s.value
  const labels = series.times.map(fmtTime)
  const col = (key) => (series[key] || []).map(toY)
  const datasets = []
  for (const dir of ['in', 'out']) {
    const color = dir === 'in' ? t.series.in : t.series.out
    const hidden = dir !== busier.value
    // The band: its top, and its bottom filled up to the top ('-1').
    datasets.push({
      label: `Usual ${dir}`, group: `usual-${dir}`, data: col(`usual_${dir}_high`), hidden,
      borderWidth: 0, pointRadius: 0, fill: false, backgroundColor: withAlpha(color, 0.14), spanGaps: false,
      // The legend swatch: the band's own 14% fill all but vanished on the dark theme.
      legendFill: withAlpha(color, 0.45), legendStroke: color,
    })
    datasets.push({
      label: `Usual ${dir} (low)`, group: `usual-${dir}`, data: col(`usual_${dir}_low`), hidden,
      borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: withAlpha(color, 0.14), spanGaps: false,
    })
  }
  for (const dir of ['in', 'out']) {
    const color = dir === 'in' ? t.series.in : t.series.out
    datasets.push({
      label: dir === 'in' ? 'In' : 'Out', group: dir, data: col(`${dir}_bps`), raw: series[`${dir}_bps`],
      borderColor: color, backgroundColor: color, borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.25, spanGaps: false,
    })
    // Each hour's busiest minute, faint: an hour's average hides a ten-minute burst (review O3).
    if (hourly.value) {
      datasets.push({
        label: `Busiest minute ${dir}`, group: `peak-${dir}`, data: col(`peak_${dir}_bps`), raw: series[`peak_${dir}_bps`],
        borderColor: withAlpha(color, 0.5), backgroundColor: withAlpha(color, 0.5), borderWidth: 1, borderDash: [3, 3],
        pointRadius: 0, fill: false, tension: 0.25, spanGaps: false,
      })
    }
  }
  if (speedBps.value) {
    datasets.push({
      label: 'Capacity', group: 'capacity', data: labels.map(() => 100),
      borderColor: readToken('--status-down-color', '#dc2626'), borderWidth: 1, borderDash: [6, 4],
      pointRadius: 0, fill: false,
    })
  }
  return { labels, datasets }
})

// Shaded spans while SNMP saw the port down (Thursday's 40-minute outage on an hourly chart is
// less than a bucket: drawn at least 3 px wide so it can't vanish).
const downSpansPlugin = {
  id: 'downSpans',
  beforeDatasetsDraw(chart) {
    const spans = s.value.down_spans || []
    const times = s.value.times.map((x) => Date.parse(x))
    if (!spans.length || !times.length) return
    const step = s.value.bucket_seconds * 1000
    const { ctx, chartArea, scales } = chart
    const xAt = (i) => scales.x.getPixelForValue(i)
    const half = times.length > 1 ? (xAt(1) - xAt(0)) / 2 : 0
    ctx.save()
    ctx.fillStyle = withAlpha(readToken('--status-down-color', '#dc2626'), 0.18)
    for (const span of spans) {
      const a = Date.parse(span.start)
      const b = span.end ? Date.parse(span.end) : times[times.length - 1] + step
      const i0 = times.findIndex((t) => t + step > a)
      let i1 = -1
      for (let i = times.length - 1; i >= 0; i--) {
        if (times[i] < b) { i1 = i; break }
      }
      if (i0 < 0 || i1 < 0 || i1 < i0) continue
      const x0 = Math.max(chartArea.left, xAt(i0) - half)
      const x1 = Math.min(chartArea.right, Math.max(xAt(i1) + half, x0 + 3))
      ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top)
    }
    ctx.restore()
  },
}

function spanText(span) {
  const end = span.end ? fmtTime(span.end) : 'now'
  return `down ${fmtTime(span.start)} to ${end}`
}

const chartOptions = computed(() => {
  const t = theme.value
  const pct = !!speedBps.value
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    normalized: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: {
        grid: { display: false },
        ticks: { color: t.text, font: { family: t.fontSans, size: 10 }, maxTicksLimit: 6, autoSkip: true, maxRotation: 0 },
      },
      y: {
        beginAtZero: true,
        suggestedMax: pct ? 100 : undefined,
        ticks: {
          color: t.text,
          font: { family: t.fontMono, size: 10 },
          maxTicksLimit: 5,
          callback: (v) => (pct ? `${v}%` : formatBps(v)),
        },
        grid: { color: t.grid },
      },
    },
    plugins: {
      legend: {
        position: 'bottom',
        labels: {
          color: t.text,
          boxWidth: 10,
          font: { family: t.fontSans, size: 10 },
          // One legend entry per group: the band's bottom goes with its top.
          filter: (item, data) => !String(data.datasets[item.datasetIndex].label).endsWith('(low)'),
          // A band's swatch is drawn stronger than the band (legendFill / legendStroke), so it can
          // be found in the legend; everything else keeps Chart.js's own swatch.
          generateLabels: (chart) => Chart.defaults.plugins.legend.labels.generateLabels(chart).map((item) => {
            const ds = chart.data.datasets[item.datasetIndex]
            return ds.legendFill ? { ...item, fillStyle: ds.legendFill, strokeStyle: ds.legendStroke, lineWidth: 1 } : item
          }),
        },
        // Clicking an entry shows or hides its whole group (a band is two datasets).
        onClick: (_e, item, legend) => {
          const chart = legend.chart
          const group = chart.data.datasets[item.datasetIndex].group
          const show = !chart.isDatasetVisible(item.datasetIndex)
          chart.data.datasets.forEach((d, i) => {
            if (d.group === group) chart.setDatasetVisibility(i, show)
          })
          chart.update()
        },
      },
      tooltip: {
        filter: (item) => !String(item.dataset.label).startsWith('Usual') && item.dataset.group !== 'capacity',
        callbacks: {
          label: (ctx) => {
            const raw = ctx.dataset.raw?.[ctx.dataIndex]
            if (raw == null) return `${ctx.dataset.label}: no data`
            return pct ? `${ctx.dataset.label}: ${formatBps(raw)} (${ctx.raw.toFixed(0)}%)` : `${ctx.dataset.label}: ${formatBps(raw)}`
          },
          afterBody: (items) => {
            if (!items.length) return ''
            const i = items[0].dataIndex
            const lines = []
            for (const dir of ['in', 'out']) {
              const lo = s.value[`usual_${dir}_low`]?.[i]
              const hi = s.value[`usual_${dir}_high`]?.[i]
              if (lo != null && hi != null) lines.push(`Usual ${dir}: ${formatBps(lo)} to ${formatBps(hi)}`)
            }
            const t0 = Date.parse(s.value.times[i])
            const t1 = t0 + s.value.bucket_seconds * 1000
            for (const span of s.value.down_spans || []) {
              const a = Date.parse(span.start)
              const b = span.end ? Date.parse(span.end) : Infinity
              if (a < t1 && b > t0) lines.push(`Link ${spanText(span)}`)
            }
            return lines
          },
        },
      },
    },
  }
})

// Text alternative for the canvas.
const summary = computed(() => {
  const pts = (key) => s.value.times.map((t, i) => ({ t: Date.parse(t), v: s.value[key]?.[i] ?? null }))
  const parts = [`${props.link.name} on ${props.link.exporter_hostname}.`]
  parts.push(summarizeSeries('In', pts('in_bps'), formatBps), summarizeSeries('Out', pts('out_bps'), formatBps))
  for (const span of s.value.down_spans || []) parts.push(`Link ${spanText(span)}.`)
  return parts.join(' ')
})
</script>

<style scoped>
.link-chart-canvas {
  position: relative;
  height: 190px;
}
</style>
