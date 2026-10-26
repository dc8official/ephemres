import { ref, computed, onBeforeUnmount } from 'vue'

// Chart.js draws on canvas, so CSS variables do not reach it. This composable reads the chart
// tokens from App.vue and recomputes them whenever the `.dark` class on <html> changes.

const isDark = ref(typeof document !== 'undefined' && document.documentElement.classList.contains('dark'))
let observer = null
let subscribers = 0

function startObserver() {
  if (observer || typeof MutationObserver === 'undefined') return
  observer = new MutationObserver(() => {
    isDark.value = document.documentElement.classList.contains('dark')
  })
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
}

function stopObserver() {
  if (observer) {
    observer.disconnect()
    observer = null
  }
}

export function readToken(name, fallback = '') {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

// '#10b981' + 0.25 -> 'rgba(16, 185, 129, 0.25)'. Non-hex colors are returned unchanged.
export function withAlpha(color, alpha) {
  const hex = String(color).trim().replace('#', '')
  if (!/^[0-9a-f]{3}([0-9a-f]{3})?$/i.test(hex)) return color
  const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex
  const n = parseInt(full, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

export function useChartTheme() {
  isDark.value = document.documentElement.classList.contains('dark')
  subscribers += 1
  startObserver()
  onBeforeUnmount(() => {
    subscribers -= 1
    if (subscribers === 0) stopObserver()
  })

  const theme = computed(() => {
    // Depend on the theme flag so every token is re-read after a toggle.
    const dark = isDark.value
    return {
      isDark: dark,
      text: readToken('--chart-text', dark ? '#9ca3af' : '#4b5563'),
      grid: readToken('--chart-grid', dark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)'),
      fontSans: 'Inter',
      fontMono: 'JetBrains Mono',
      series: {
        in: readToken('--series-in', '#10b981'),
        out: readToken('--series-out', '#0ea5e9'),
        flows: readToken('--series-flows', '#8b5cf6'),
        rtt: readToken('--series-rtt', '#f59e0b'),
        transit: readToken('--series-transit', '#64748b'),
      },
      palette: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => readToken(`--series-${i}`)),
    }
  })

  return { isDark, theme, withAlpha }
}
