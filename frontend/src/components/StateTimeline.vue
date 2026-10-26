<template>
  <div class="state-timeline">
    <p class="sr-only">State over the period: {{ summary }}</p>
    <Bar v-if="chartData" :key="isDark" :data="chartData" :options="chartOptions" />
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { Bar } from 'vue-chartjs'
import { summarizeDurations } from '../utils/chartSummary.js'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend
} from 'chart.js'

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend
)

const props = defineProps({
  events: {
    type: Array,
    required: true
  },
  gaps: {
    type: Array,
    required: true
  },
  periodStart: {
    type: String,
    required: true
  },
  periodEnd: {
    type: String,
    required: true
  }
})

const isDark = ref(true)
let observer = null

onMounted(() => {
  isDark.value = document.documentElement.classList.contains('dark')
  observer = new MutationObserver(() => {
    isDark.value = document.documentElement.classList.contains('dark')
  })
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class']
  })
})

onBeforeUnmount(() => {
  if (observer) {
    observer.disconnect()
  }
})

const STATE_COLORS = computed(() => ({
  'UP': isDark.value ? '#4ade80' : '#16a34a',
  'UP-UNSTABLE': isDark.value ? '#f59e0b' : '#d97706',
  'DOWN-UNSTABLE': isDark.value ? '#f59e0b' : '#d97706',
  'DOWN': isDark.value ? '#f87171' : '#dc2626',
  'UNKNOWN': isDark.value ? '#4a4a4a' : '#64748b'
}))

// Text alternative: share of the period in each state (gaps count as UNKNOWN).
const summary = computed(() => {
  const pStart = new Date(props.periodStart).getTime()
  const pEnd = new Date(props.periodEnd).getTime()
  const segments = []
  for (const ev of props.events) {
    const startMs = Math.max(new Date(ev.start_time).getTime(), pStart)
    const endMs = Math.min(ev.end_time ? new Date(ev.end_time).getTime() : pEnd, pEnd)
    if (endMs > startMs) segments.push({ state: ev.detailed_state, startMs, endMs })
  }
  return summarizeDurations(segments)
})

const formatDuration = (ms) => {
  if (ms < 0) return '0m'
  const totalMins = Math.floor(ms / 60000)
  const hours = Math.floor(totalMins / 60)
  const mins = totalMins % 60
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

const chartData = computed(() => {
  const pStart = new Date(props.periodStart).getTime()
  const pEnd = new Date(props.periodEnd).getTime()
  const totalDuration = pEnd - pStart

  if (totalDuration <= 0) return null

  const allSegments = []

  props.events.forEach(ev => {
    const evStart = new Date(ev.start_time).getTime()
    let evEnd = ev.end_time ? new Date(ev.end_time).getTime() : pEnd
    if (evEnd === evStart) {
      evEnd = evStart + 60000 // Each high-density event represents a 60-second cycle
    }
    const start = Math.max(evStart, pStart)
    const end = Math.min(evEnd, pEnd)
    if (end > start) {
      allSegments.push({
        state: ev.detailed_state,
        startTime: ev.start_time,
        endTime: ev.end_time,
        startMs: start,
        endMs: end,
        isSplit: ev.is_split_event || false
      })
    }
  })

  props.gaps.forEach(gap => {
    const start = Math.max(new Date(gap.start_time).getTime(), pStart)
    const end = gap.end_time ? Math.min(new Date(gap.end_time).getTime(), pEnd) : pEnd
    if (end > start) {
      allSegments.push({
        state: 'UNKNOWN',
        startTime: gap.start_time,
        endTime: gap.end_time,
        startMs: start,
        endMs: end,
        isSplit: false
      })
    }
  })

  allSegments.sort((a, b) => a.startMs - b.startMs)

  const mergedSegments = []
  allSegments.forEach(seg => {
    if (mergedSegments.length === 0) {
      mergedSegments.push({ ...seg })
      return
    }
    const last = mergedSegments[mergedSegments.length - 1]
    // Group consecutive segments with same state within 65s of each other (per-minute resolution)
    if (last.state === seg.state && seg.startMs <= last.endMs + 65000) {
      last.endMs = Math.max(last.endMs, seg.endMs)
      last.endTime = seg.endTime
    } else {
      mergedSegments.push({ ...seg })
    }
  })

  const datasetsMap = {
    'UP': [],
    'UP-UNSTABLE': [],
    'DOWN-UNSTABLE': [],
    'DOWN': [],
    'UNKNOWN': []
  }

  mergedSegments.forEach(seg => {
    const startPx = (seg.startMs - pStart) / totalDuration
    const endPx = (seg.endMs - pStart) / totalDuration
    
    if (datasetsMap[seg.state]) {
      datasetsMap[seg.state].push({
        x: [startPx, endPx],
        y: 'Timeline',
        segmentData: seg
      })
    }
  })

  const datasets = Object.keys(datasetsMap).map(key => ({
    label: key,
    data: datasetsMap[key],
    backgroundColor: STATE_COLORS.value[key],
    grouped: false,
    borderWidth: key === 'UNKNOWN' ? 1 : 0,
    borderColor: key === 'UNKNOWN' ? (isDark.value ? '#4a4a4a' : '#475569') : 'transparent',
    borderSkipped: false
  }))

  return {
    labels: ['Timeline'],
    datasets: datasets.filter(ds => ds.data.length > 0)
  }
})

// The x axis runs 0..1 across the period; label ticks with the clock time they stand for.
function formatTick(fraction) {
  const pStart = new Date(props.periodStart).getTime()
  const pEnd = new Date(props.periodEnd).getTime()
  const span = pEnd - pStart
  const d = new Date(pStart + fraction * span)
  if (span <= 36 * 3600 * 1000) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (span <= 8 * 86400 * 1000) return d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const chartOptions = computed(() => {
  const gridColor = isDark.value ? '#3a3a3a' : '#e2e8f0'
  const textColor = isDark.value ? '#D0D0D0' : '#0f172a'

  return {
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'bottom',
        labels: {
          color: textColor,
          font: {
            family: 'monospace',
            size: 12
          }
        }
      },
      tooltip: {
        callbacks: {
          title: () => '',
          label: (context) => {
            const seg = context.raw.segmentData
            const startStr = new Date(seg.startTime).toLocaleString()
            const endStr = seg.endTime ? new Date(seg.endTime).toLocaleString() : 'Ongoing'
            const duration = formatDuration(seg.endMs - seg.startMs)
            
            let lines = [
              `State: ${seg.state}`,
              `Start: ${startStr}`,
              `End: ${endStr}`,
              `Duration: ${duration}`
            ]
            
            if (seg.isSplit) {
              lines.push('(RTT inherited from previous day)')
            }
            
            return lines
          }
        }
      }
    },
    scales: {
      x: {
        type: 'linear',
        min: 0,
        max: 1,
        grid: {
          color: gridColor,
          drawBorder: false
        },
        ticks: {
          color: textColor,
          font: {
            family: 'monospace',
            size: 12
          },
          maxTicksLimit: 8,
          callback: (value) => formatTick(value)
        },
        title: {
          display: false
        }
      },
      y: {
        display: false
      }
    }
  }
})
</script>

<style scoped>
.state-timeline {
  height: 200px;
  width: 100%;
  padding: 1rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  box-shadow: none;
  margin-bottom: 1.5rem;
  transition: background-color 0.2s, border-color 0.2s;
}
</style>

