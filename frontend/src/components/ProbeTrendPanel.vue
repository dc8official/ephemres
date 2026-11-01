<template>
  <div class="probe-trend" :class="{ compact }">
    <div v-if="!hasData" class="empty-state">
      {{ emptyText }}
    </div>
    <div v-else class="chart-wrapper">
      <p class="sr-only">{{ summary }}</p>
      <LineChart :data="chartData" :options="chartOptions" />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Line as LineChart } from 'vue-chartjs'
import {
  Chart as ChartJS,
  LinearScale,
  PointElement,
  LineElement,
  BarController,
  BarElement,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js'
import { readToken, useChartTheme } from '../composables/useChartTheme.js'
import { summarizeSeries } from '../utils/chartSummary.js'

ChartJS.register(LinearScale, PointElement, LineElement, BarController, BarElement, Tooltip, Legend, Filler)

// Per-cycle ICMP history on a real time axis: average RTT with a min-max band and packet loss
// as bars on their own 0-100 % axis. Buckets with no samples leave a gap in the lines.
const props = defineProps({
  points: { type: Array, required: true },
  bucketSeconds: { type: Number, default: 60 },
  start: { type: [String, Number, Date], required: true },
  end: { type: [String, Number, Date], required: true },
  compact: { type: Boolean, default: false },
  emptyText: { type: String, default: 'No per-probe history for this period.' },
})

const { theme, withAlpha } = useChartTheme()

const startMs = computed(() => new Date(props.start).getTime())
const endMs = computed(() => new Date(props.end).getTime())
const hasData = computed(() => props.points.length > 0)

// {x, y} series with a null point wherever consecutive samples are more than 1.5 buckets apart.
function withGaps(key) {
  const out = []
  const gapMs = props.bucketSeconds * 1500
  let prev = null
  for (const p of props.points) {
    const t = Date.parse(p.timestamp)
    if (prev !== null && t - prev > gapMs) out.push({ x: prev + props.bucketSeconds * 1000, y: null })
    out.push({ x: t, y: p[key] })
    prev = t
  }
  return out
}

// Text alternative for the canvas chart.
const summary = computed(() => {
  const pts = props.points.map((p) => ({ t: Date.parse(p.timestamp), v: p.rtt_avg_ms }))
  const sent = props.points.reduce((a, p) => a + p.sent, 0)
  const received = props.points.reduce((a, p) => a + p.received, 0)
  const loss = sent ? `${((1 - received / sent) * 100).toFixed(1)}% of ${sent} probes lost` : 'no probes sent'
  return `${summarizeSeries('Average round-trip time', pts, (v) => `${v.toFixed(1)} ms`)} Packet loss: ${loss}.`
})

const chartData = computed(() => {
  const rtt = theme.value.series.rtt
  const lossColor = readToken('--status-text-down', '#b91c1c')
  return {
    datasets: [
      {
        label: 'Max RTT',
        data: withGaps('rtt_max_ms'),
        borderColor: withAlpha(rtt, 0.35),
        borderWidth: 1,
        pointRadius: 0,
        fill: false,
        spanGaps: false,
        yAxisID: 'y',
        order: 2,
      },
      {
        label: 'Min RTT',
        data: withGaps('rtt_min_ms'),
        borderColor: withAlpha(rtt, 0.35),
        backgroundColor: withAlpha(rtt, 0.15),
        borderWidth: 1,
        pointRadius: 0,
        fill: '-1',
        spanGaps: false,
        yAxisID: 'y',
        order: 2,
      },
      {
        label: 'Avg RTT',
        data: withGaps('rtt_avg_ms'),
        borderColor: rtt,
        borderWidth: 2,
        pointRadius: props.points.length > 120 ? 0 : 2,
        fill: false,
        spanGaps: false,
        tension: 0.2,
        yAxisID: 'y',
        order: 1,
      },
      {
        type: 'bar',
        label: 'Loss %',
        data: props.points.filter((p) => p.loss_pct > 0).map((p) => ({ x: Date.parse(p.timestamp), y: p.loss_pct })),
        backgroundColor: withAlpha(lossColor, 0.6),
        borderWidth: 0,
        barThickness: props.points.length > 200 ? 2 : 4,
        yAxisID: 'yLoss',
        order: 3,
      },
    ],
  }
})

function formatTime(ms) {
  const d = new Date(ms)
  const span = endMs.value - startMs.value
  if (span <= 36 * 3600 * 1000) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (span <= 8 * 86400 * 1000) return d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const chartOptions = computed(() => {
  const t = theme.value
  const byTime = new Map(props.points.map((p) => [Date.parse(p.timestamp), p]))
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: true,
    interaction: { mode: 'nearest', axis: 'x', intersect: false },
    scales: {
      x: {
        type: 'linear',
        min: startMs.value,
        max: endMs.value,
        grid: { display: false },
        ticks: { color: t.text, font: { size: 11 }, maxTicksLimit: props.compact ? 6 : 10, callback: (v) => formatTime(v) },
      },
      y: {
        beginAtZero: true,
        grid: { color: t.grid },
        title: { display: !props.compact, text: 'RTT (ms)', color: t.text, font: { family: t.fontMono, size: 11 } },
        ticks: { color: t.text, font: { family: t.fontMono, size: 11 }, callback: (v) => `${v} ms` },
      },
      yLoss: {
        position: 'right',
        min: 0,
        max: 100,
        grid: { drawOnChartArea: false },
        title: { display: !props.compact, text: 'Loss %', color: t.text, font: { family: t.fontMono, size: 11 } },
        ticks: { color: t.text, font: { family: t.fontMono, size: 11 }, callback: (v) => `${v}%` },
      },
    },
    plugins: {
      legend: {
        position: 'top',
        labels: { color: t.text, font: { size: 11 }, filter: (item) => item.text !== 'Min RTT' },
      },
      tooltip: {
        callbacks: {
          title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleString() : ''),
          label: (ctx) => {
            const p = byTime.get(ctx.parsed.x)
            if (!p || ctx.dataset.label !== 'Avg RTT') {
              return ctx.dataset.label === 'Loss %' ? `Loss: ${ctx.parsed.y}%` : null
            }
            const lines = [`Avg RTT: ${p.rtt_avg_ms ?? '—'} ms`]
            if (p.rtt_min_ms != null) lines.push(`Range: ${p.rtt_min_ms}–${p.rtt_max_ms} ms`)
            if (p.jitter_ms != null) lines.push(`Jitter: ${p.jitter_ms} ms`)
            lines.push(`Loss: ${p.loss_pct ?? 0}% (${p.received}/${p.sent} replies)`)
            return lines
          },
        },
      },
    },
  }
})
</script>

<style scoped>
.chart-wrapper {
  position: relative;
  height: 280px;
}

.compact .chart-wrapper {
  height: 180px;
}

.empty-state {
  padding: 2rem 1rem;
  text-align: center;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
</style>
