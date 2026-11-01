# UI primitives

Shared controls from the v3.3.5 dashboard rework (`docs/release-notes/v3.3.5.md`). They use
only the CSS variables defined in `App.vue`, so they follow light and dark mode without extra
code. New or reworked screens use these instead of page-specific toggle, heading and KPI styles.

| Component | Use it for |
|---|---|
| `SegmentedControl` | Any single-choice toggle: chart metric, view mode, perspective. Keyboard: arrows, Home/End. |
| `TimeRangePicker` | Every time-window selector. Presets plus an optional custom range. |
| `PanelHeader` | Card and section titles: sentence-case title, one-line hint, right-aligned actions. |
| `KpiTile` | Headline numbers. Missing values show "—"; `clickable` turns it into a filter toggle. |
| `Sparkline` | Inline trend line for table rows; gaps for missing slots; labelled for screen readers. |

```vue
<SegmentedControl v-model="metric" aria-label="Chart metric"
  :options="[{ value: 'bps', label: 'Throughput' }, { value: 'pps', label: 'Packets' }]" />

<TimeRangePicker v-model="window" :windows="['1h', '6h', '24h', '7d']" />

<TimeRangePicker v-model="range" allow-custom v-model:start="from" v-model:end="to" @apply="load" />

<PanelHeader title="Top conversations" hint="Largest flows in the selected window">
  <template #actions><button class="btn-secondary btn-small">Export</button></template>
</PanelHeader>

<KpiTile label="Down" :value="3" tone="down" clickable :active="filter === 'DOWN'" @select="filter = 'DOWN'" />
```

Related helpers:

- `composables/useChartTheme.js`: chart text, grid and series colors from the `--chart-*` and
  `--series-*` tokens, recomputed when the theme changes.
- `utils/format.js`: bps, pps, bytes, number and port-label formatting.
- `utils/statusTone.js`: one threshold set for uptime, utilization and health; returns a tone
  that maps to the `--status-text-*` tokens.
- `utils/chartSummary.js`: text summaries (average, peak, latest; share of time per state) for
  the visually hidden element next to every canvas chart.
- `components/traffic/ConversationsPanel.vue`: the server-backed conversation table shared by
  Traffic Analytics and Endpoint Detail.
- `utils/interfaceConfig.js`: rows and payload for the interface editor in Traffic Analytics and
  Endpoint Detail (name, link speed or "Not set", WAN/LAN role).
