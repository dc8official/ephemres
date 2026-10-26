<template>
  <div class="bandwidth-view">
    <!-- Header Toolbar -->
    <div class="bandwidth-toolbar">
      <div class="toolbar-left">
        <h1 class="page-title">Traffic Analytics</h1>
        <div class="toolbar-sub-row">
          <span class="page-sub">
            v3.3.2.v Flow Engine
            <span class="separator">·</span>
            NetFlow v5, NetFlow v9 & IPFIX
          </span>
        </div>
      </div>

      <div class="toolbar-right">
        <!-- Scope Switcher (Network Aggregate vs Individual Exporter) -->
        <div class="scope-selector">
          <label class="scope-label" for="scope-select">Scope:</label>
          <div class="scope-input-wrapper">
            <input
              v-if="flowExporters.length > 5 || unmatchedExporters.length > 0"
              v-model="exporterSearchText"
              type="text"
              class="scope-search-input"
              placeholder="Filter devices..."
              aria-label="Filter flow exporters"
            />
            <select
              id="scope-select"
              v-model="selectedExporterId"
              @change="handleScopeChange"
              class="scope-select"
            >
              <option value="">🌐 All Network Traffic (Global Overview)</option>
              <optgroup
                v-if="filteredFlowExporters.length > 0"
                :label="`Configured Flow Exporters (${filteredFlowExporters.length})`"
              >
                <option v-for="exp in filteredFlowExporters" :key="exp.id" :value="exp.id">
                  🟢 {{ exp.hostname }} ({{ exp.primary_ip }})
                </option>
              </optgroup>
              <optgroup
                v-if="filteredUnmatchedExporters.length > 0"
                :label="`Discovered Sources (Unregistered - ${filteredUnmatchedExporters.length})`"
              >
                <option
                  v-for="u in filteredUnmatchedExporters"
                  :key="u.ip_address"
                  :value="`unmatched:${u.ip_address}`"
                >
                  ⚠ {{ u.ip_address }} (Unregistered - {{ formatNumber(u.flow_count || 0) }} flows)
                </option>
              </optgroup>
              <option
                v-if="filteredFlowExporters.length === 0 && filteredUnmatchedExporters.length === 0 && exporterSearchText"
                disabled
                value=""
              >
                No matching flow exporters found
              </option>
            </select>
          </div>
        </div>

        <!-- Auto-Refresh Interval Selector (UI-07) -->
        <div class="auto-refresh-selector">
          <label class="refresh-label">Refresh:</label>
          <select v-model="autoRefreshInterval" @change="handleAutoRefreshChange" class="refresh-select">
            <option value="0">Off</option>
            <option value="15">15s</option>
            <option value="30">30s</option>
            <option value="60">60s</option>
          </select>
        </div>

        <TimeRangePicker
          :model-value="selectedWindow"
          :windows="windowOptions"
          aria-label="Time window"
          @update:model-value="setWindow"
        />

        <button
          class="btn-secondary"
          @click="refreshAll"
          :disabled="loading"
          aria-label="Refresh telemetry data"
        >
          <span>{{ loading ? 'Syncing...' : '↻ Refresh' }}</span>
        </button>
      </div>
    </div>

    <!-- Unregistered Exporter Focus Callout -->
    <div
      v-if="isUnmatchedScope"
      class="unmatched-scope-card table-card"
      role="alert"
      aria-live="polite"
    >
      <div class="unmatched-scope-header">
        <span class="unmatched-scope-icon">⚠</span>
        <div class="unmatched-scope-info">
          <div class="unmatched-scope-badge">DISCOVERED FLOW SOURCE (UNREGISTERED)</div>
          <h2 class="unmatched-scope-title">
            Unregistered Device Streaming Flows: <span class="font-mono text-accent">{{ activeUnmatchedIp }}</span>
          </h2>
          <p class="unmatched-scope-desc">
            Ephemres is actively receiving NetFlow/IPFIX UDP packets from <strong>{{ activeUnmatchedIp }}</strong>, but this device is not yet registered in Ephemres' endpoint inventory. Flow packets are currently staged in discovery. To enable per-interface metrics, line-speed utilization, and historical conversation tracking, enroll this source as an endpoint or link it as an alias to an existing router.
          </p>
        </div>
      </div>
      <div class="unmatched-scope-actions">
        <button
          class="btn-primary"
          @click="openEnrollModal(activeUnmatchedIp)"
        >
          + Enroll {{ activeUnmatchedIp }} as Flow Exporter
        </button>
        <button
          class="btn-secondary"
          @click="openMappingModal(activeUnmatchedIp)"
        >
          Link to Existing Endpoint Alias →
        </button>
        <button
          class="btn-text"
          @click="selectedExporterId = ''; handleScopeChange()"
        >
          Return to All Traffic
        </button>
      </div>
    </div>

    <!-- Unmatched Exporter Discovery Banner (BUG-05 Multi-IP Grid) -->
    <div
      v-if="unmatchedExporters.length > 0 && !isUnmatchedScope"
      class="unmatched-banner"
      role="alert"
      aria-live="polite"
    >
      <div class="unmatched-header-row">
        <div class="unmatched-icon">⚠</div>
        <div class="unmatched-title">
          Unmatched Flow Exporters: {{ unmatchedExporters.length }} Unknown Router/Switch Stream{{ unmatchedExporters.length > 1 ? 's' : '' }} Detected
        </div>
      </div>
      <div class="unmatched-grid">
        <div 
          v-for="u in unmatchedExporters" 
          :key="u.ip_address"
          class="unmatched-card"
        >
          <div class="unmatched-card-info">
            <span class="unmatched-ip tnum font-mono">{{ u.ip_address }}</span>
            <span class="unmatched-sub text-muted" v-if="u.flow_count">
              {{ formatNumber(u.flow_count) }} flows
            </span>
          </div>
          <div class="unmatched-card-actions">
            <button
              class="btn-primary btn-small"
              @click="openEnrollModal(u.ip_address)"
              :title="`Enroll ${u.ip_address} as dedicated flow exporter`"
            >
              + Enroll Exporter
            </button>
            <button
              class="btn-secondary btn-small"
              @click="openMappingModal(u.ip_address)"
              :title="`Link ${u.ip_address} as endpoint alias`"
            >
              Link Alias →
            </button>
          </div>
        </div>
      </div>
    </div>

    <template v-if="!isUnmatchedScope">
    <SegmentedControl
      class="view-switcher"
      :model-value="activeView"
      :options="viewOptions"
      aria-label="Traffic view"
      @update:model-value="setView"
    />

    <!-- ── Overview ── -->
    <template v-if="activeView === 'overview'">
    <!-- KPI strip: last five complete minutes for the selected scope -->
    <p class="kpi-scope-note">Last 5 complete minutes · {{ scopeLabel }}</p>
    <div class="kpi-grid">
      <KpiTile
        label="Ingress"
        :value="formatBps(overview.total_ingress_bps)"
        :marker="theme.series.in"
        :sub="`${formatPps(overview.total_ingress_pps)} · to monitored hosts`"
      />
      <KpiTile
        label="Egress"
        :value="formatBps(overview.total_egress_bps)"
        :marker="theme.series.out"
        :sub="`${formatPps(overview.total_egress_pps)} · from monitored hosts`"
      />
      <KpiTile
        label="Transit"
        :value="formatBps(overview.total_transit_bps)"
        :marker="theme.series.transit"
        :sub="`${formatPps(overview.total_transit_pps)} · no monitored host on either side`"
      />
      <KpiTile label="Avg packet size" :value="overview.avg_packet_size || null" unit="B">
        <template #sub>
          <span
            v-if="overview.avg_packet_size"
            class="badge-packet-profile"
            :class="getPacketProfileClass(overview.avg_packet_size)"
          >
            {{ getPacketProfileLabel(overview.avg_packet_size) }}
          </span>
          <span v-else class="text-xs">No packets in this window</span>
        </template>
      </KpiTile>
      <KpiTile
        label="Flow exporters sending"
        :value="overview.active_exporters_count"
        :sub="exporterSummary"
      />
      <KpiTile label="Flows" :value="formatNumber(overview.total_flows_count)">
        <template #sub>
          <span
            v-if="overview.anomaly_flows_count"
            class="badge-anomaly-kpi"
            title="SYN-only or RST-without-FIN flows in the last 5 minutes"
          >
            ⚠ {{ formatNumber(overview.anomaly_flows_count) }} SYN/RST anomalies
          </span>
          <span v-else class="text-success text-xs">✓ No SYN/RST anomalies</span>
        </template>
      </KpiTile>
    </div>

    <!-- Main Section: Stacked Area Traffic Chart -->
    <div class="chart-section table-card">
      <div class="section-header section-header-split">
        <div>
          <div class="section-title">
            {{ chartTitle }}
          </div>
          <span class="section-hint">{{ chartHint }}</span>
        </div>
        <div class="chart-header-actions">
          <SegmentedControl v-model="chartMetricMode" :options="metricOptions" aria-label="Chart metric" size="sm" />
        </div>
      </div>
      <div class="chart-container">
        <p v-if="seriesPoints.length > 0" class="sr-only">{{ chartSummary }}</p>
        <LineChart
          v-if="seriesPoints.length > 0"
          :data="chartData"
          :options="chartOptions"
        />
        <div v-else-if="!loading" class="no-chart-data">
          <template v-if="['1h', '6h'].includes(selectedWindow)">No flow records in the last {{ selectedWindow }}.</template>
          <template v-else>No {{ selectedWindow === '30d' ? 'daily' : 'hourly' }} totals for the last {{ selectedWindow }} yet. They are built from minute data periodically.</template>
        </div>
        <div v-else class="loading-spinner">Loading telemetry series...</div>
      </div>
    </div>

    <div class="secondary-grid">
      <!-- Top Talkers Leaderboard with Click-to-Filter -->
      <div class="table-card talkers-panel">
        <div class="section-header section-header-split">
          <div>
            <div class="section-title">Top talkers</div>
            <span class="section-hint">Top 10 hosts and external addresses by volume · Click one to see its conversations</span>
          </div>
        </div>
        <div class="table-responsive">
          <table class="dense-table interactive-table" aria-label="Top Talkers Table">
            <thead>
              <tr>
                <th>Host</th>
                <th class="text-right">Ingress</th>
                <th class="text-right">Egress</th>
                <th class="text-right">Total Bytes</th>
                <th class="text-right">Flows / Avg packet</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="t in topEndpoints"
                :key="t.ip_address"
                class="cursor-pointer"
                tabindex="0"
                @click="openConversationsFor({ ip: t.ip_address })"
                @keydown.enter="openConversationsFor({ ip: t.ip_address })"
              >
                <td>
                  <div class="node-cell">
                    <span class="node-host font-bold">{{ t.hostname || t.ip_address }}</span>
                    <span class="node-ip tnum text-muted" v-if="t.hostname">{{ t.ip_address }}</span>
                  </div>
                </td>
                <td class="text-right tnum ingress-text">{{ formatBytes(t.ingress_bytes) }}</td>
                <td class="text-right tnum egress-text">{{ formatBytes(t.egress_bytes) }}</td>
                <td class="text-right tnum font-bold">{{ formatBytes(t.total_bytes) }}</td>
                <td class="text-right tnum text-muted">
                  <div>{{ formatNumber(t.flow_count) }}</div>
                  <span class="text-xs text-muted block font-mono">{{ t.avg_packet_size || 0 }} B/pkt</span>
                </td>
              </tr>
              <tr v-if="topEndpoints.length === 0">
                <td colspan="5" class="text-center text-muted">No talker statistics in this window.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="table-card breakdown-panel">
        <div class="section-header">
          <div>
            <div class="section-title">Applications</div>
            <span class="section-hint">Share of all traffic by service port · Click one to see its conversations</span>
          </div>
        </div>
        <div v-if="applicationStats.length" class="app-bars">
          <button
            v-for="app in applicationStats"
            :key="`${app.protocol_name}-${app.port}`"
            type="button"
            class="app-bar-row"
            :disabled="app.is_other"
            :title="app.is_other ? 'All other services combined' : `Show conversations on port ${app.port}`"
            @click="!app.is_other && openConversationsFor({ port: app.port })"
          >
            <span class="app-bar-label">{{ app.is_other ? 'Other' : app.service_label }}</span>
            <span class="app-bar-track"><span class="app-bar-fill" :class="{ other: app.is_other }" :style="{ width: `${Math.min(100, app.percentage)}%` }"></span></span>
            <span class="app-bar-value tnum">{{ app.percentage.toFixed(1) }}% · {{ formatBytes(app.total_bytes) }}</span>
          </button>
        </div>
        <div v-else class="no-chart-data">No application traffic in this window.</div>

        <div class="section-header zones-header">
          <div>
            <div class="section-title">Traffic zones</div>
            <span class="section-hint">{{ zonesHint }}</span>
          </div>
        </div>
        <template v-if="zonesResolution !== 'unavailable'">
          <div v-if="trafficZones.length > 0" class="breakdown-list">
            <div
              v-for="zone in trafficZones"
              :key="zone.zone"
              class="breakdown-item"
            >
              <div class="breakdown-row-top">
                <span class="zone-badge" :class="getZoneBadgeClass(zone.zone)">
                  <span class="zone-icon">{{ getZoneIcon(zone.zone) }}</span> {{ zone.zone }}
                </span>
                <span class="breakdown-pct tnum font-bold">{{ zone.percentage.toFixed(1) }}%</span>
              </div>
              <div class="progress-bar-bg">
                <div class="progress-bar-fill" :class="getZoneFillClass(zone.zone)" :style="{ width: `${Math.min(100, zone.percentage)}%` }"></div>
              </div>
              <div class="breakdown-meta tnum text-muted text-xs">
                <span>{{ formatBytes(zone.total_bytes) }}</span>
                <span>{{ formatNumber(zone.flow_count) }} flows</span>
              </div>
            </div>
          </div>
        </template>
        <div v-else class="no-chart-data">Zones need address or endpoint detail, which the daily tier does not keep. Pick a window of 7 days or less.</div>
      </div>
    </div>
    </template>

    <!-- ── Interfaces ── -->
    <template v-else-if="activeView === 'interfaces'">
      <template v-if="selectedExporterId">
    <!-- Main Section: Stacked Area Traffic Chart -->
    <div class="chart-section table-card">
      <div class="section-header section-header-split">
        <div>
          <div class="section-title">
            {{ chartTitle }}
          </div>
          <span class="section-hint">{{ chartHint }}</span>
        </div>
        <div class="chart-header-actions">
          <SegmentedControl v-model="chartMetricMode" :options="metricOptions" aria-label="Chart metric" size="sm" />
        </div>
      </div>
      <div class="chart-container">
        <p v-if="seriesPoints.length > 0" class="sr-only">{{ chartSummary }}</p>
        <LineChart
          v-if="seriesPoints.length > 0"
          :data="chartData"
          :options="chartOptions"
        />
        <div v-else-if="!loading" class="no-chart-data">
          <template v-if="['1h', '6h'].includes(selectedWindow)">No flow records in the last {{ selectedWindow }}.</template>
          <template v-else>No {{ selectedWindow === '30d' ? 'daily' : 'hourly' }} totals for the last {{ selectedWindow }} yet. They are built from minute data periodically.</template>
        </div>
        <div v-else class="loading-spinner">Loading telemetry series...</div>
      </div>
    </div>

    <!-- Interface Telemetry Grid (ManageEngine NetFlow Analyzer Snapshot Benchmark) -->
    <div v-if="selectedExporterId && interfaceList.length > 0" class="table-card interfaces-panel">
      <div class="section-header section-header-split">
        <div>
          <div class="section-title">Active Router Interfaces & Telemetry</div>
          <span class="section-hint">{{ interfaceList.length }} Interfaces Detected · Click row to isolate throughput</span>
        </div>
        <div class="header-actions-row">
          <button
            v-if="isAdmin"
            type="button"
            class="btn-secondary btn-small"
            @click="openInterfaceModal"
            title="Configure interface aliases and link speeds"
          >
            ⚙ Configure Interfaces
          </button>
          <div v-if="selectedInterfaceIdx !== null" class="isolated-badge-row">
            <span class="status-pill status-isolated">
              Isolating Interface #{{ selectedInterfaceIdx }}
            </span>
            <button class="btn-text btn-small" @click="clearInterfaceIsolation">Clear Isolation ✕</button>
          </div>
        </div>
      </div>
      <div class="table-responsive">
        <table class="dense-table interactive-table" aria-label="Interface Telemetry Table">
          <thead>
            <tr>
              <th style="width: 80px;">Index</th>
              <th>Interface Name / Alias</th>
              <th class="text-right">Link Speed</th>
              <th class="text-right">Ingress Rate</th>
              <th class="text-right">Egress Rate</th>
              <th style="width: 300px;" title="Bar: average. Ticks: 95th percentile (|) and busiest minute (▲).">Utilization · avg / p95 / peak</th>
              <th class="text-center" style="width: 90px;">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in interfaceList"
              :key="item.interface_idx"
              :class="{ 'row-selected': selectedInterfaceIdx === item.interface_idx }"
              @click="toggleInterfaceIsolation(item.interface_idx)"
              class="cursor-pointer"
            >
              <td class="font-mono tnum">#{{ item.interface_idx }}</td>
              <td class="name-cell">
                <div class="name-cell-inner">
                  <span class="font-bold">{{ item.name }}</span>
                  <button
                    v-if="isAdmin"
                    type="button"
                    class="btn-row-edit"
                    @click.stop="openInterfaceModal(item.interface_idx)"
                    title="Configure this interface"
                  >
                    ✎
                  </button>
                </div>
              </td>
              <td class="text-right tnum text-muted">
                <span v-if="item.speed_configured !== false">{{ formatSpeed(item.speed_mbps) }}</span>
                <span v-else class="speed-unset" title="Set the link speed to see utilization">Not set</span>
              </td>
              <td class="text-right tnum ingress-text">{{ formatBps(item.in_bps) }}</td>
              <td class="text-right tnum egress-text">{{ formatBps(item.out_bps) }}</td>
              <td>
                <span v-if="item.utilization_percentage == null" class="text-muted text-xs">— set link speed</span>
                <div v-else class="util-cell">
                  <div class="util-bar-bg">
                    <div
                      class="util-bar-fill"
                      :class="getUtilClass(item.utilization_percentage)"
                      :style="{ width: `${Math.min(100, item.utilization_percentage)}%` }"
                    ></div>
                    <span
                      v-if="item.p95_utilization_percentage != null"
                      class="util-marker util-marker-p95"
                      :style="{ left: `${Math.min(100, item.p95_utilization_percentage)}%` }"
                      :title="`95th percentile: ${item.p95_utilization_percentage.toFixed(1)}%`"
                    ></span>
                    <span
                      v-if="item.peak_utilization_percentage != null"
                      class="util-marker util-marker-peak"
                      :style="{ left: `${Math.min(100, item.peak_utilization_percentage)}%` }"
                      :title="`Busiest minute: ${item.peak_utilization_percentage.toFixed(1)}%`"
                    ></span>
                  </div>
                  <span class="util-text tnum" :class="getUtilClass(item.p95_utilization_percentage ?? item.utilization_percentage)">
                    {{ item.utilization_percentage.toFixed(1) }}<span class="util-sub">
                      / {{ (item.p95_utilization_percentage ?? 0).toFixed(1) }} / {{ (item.peak_utilization_percentage ?? 0).toFixed(1) }}%</span>
                  </span>
                </div>
              </td>
              <td class="text-center">
                <span :class="item.is_active ? 'badge-active' : 'badge-idle'">
                  {{ item.is_active ? 'Active' : 'Idle' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

        <div v-if="interfaceList.length === 0 && !loading" class="table-card empty-view">
          No interface data for this exporter in the last {{ selectedWindow }}.
        </div>
      </template>
      <div v-else class="table-card empty-view">
        <p class="empty-title">Interfaces belong to one exporter</p>
        <p class="text-muted">Pick an exporter to see per-interface rates, utilization and peaks.</p>
        <div class="exporter-picks">
          <button
            v-for="exp in flowExporters"
            :key="exp.id"
            type="button"
            class="btn-secondary btn-small"
            @click="selectedExporterId = exp.id; handleScopeChange()"
          >
            {{ exp.hostname }} <span class="text-muted font-mono">{{ exp.primary_ip }}</span>
          </button>
          <span v-if="flowExporters.length === 0" class="text-muted">No exporters are configured yet.</span>
        </div>
      </div>
    </template>

    <!-- ── Conversations ── -->
    <ConversationsPanel
      v-else-if="activeView === 'conversations'"
      :window="selectedWindow"
      :exporter-id="selectedExporterId || null"
      v-model:ip="selectedIpFilter"
      v-model:port="selectedPortFilter"
      v-model:dscp="selectedDscpFilter"
      v-model:dscp-label="selectedDscpLabel"
      :refresh-key="refreshKey"
    />

    <!-- ── QoS ── -->
    <template v-else-if="activeView === 'qos'">
      <div class="table-card breakdown-panel qos-panel">
        <div class="section-header">
          <div>
            <div class="section-title">DiffServ classes</div>
            <span class="section-hint">Share of traffic by DSCP marking · Click a class to list its conversations</span>
          </div>
        </div>
          <div v-if="qosClasses.length > 0" class="breakdown-list">
            <div
              v-for="qos in qosClasses"
              :key="qos.dscp"
              class="breakdown-item cursor-pointer"
              :class="{ 'item-selected': selectedDscpFilter === qos.dscp }"
              tabindex="0"
              @click="toggleDscpFilter(qos.dscp, qos.dscp_label)"
              @keydown.enter="toggleDscpFilter(qos.dscp, qos.dscp_label)"
              :title="`Filter conversations by ${qos.dscp_label} (DSCP ${qos.dscp})`"
            >
              <div class="breakdown-row-top">
                <span
                  class="badge-dscp"
                  :class="{ 'badge-dscp-ef': qos.dscp === 46, 'badge-dscp-af': qos.dscp_label && qos.dscp_label.includes('AF') }"
                >
                  {{ qos.dscp_label }}
                </span>
                <span class="breakdown-pct tnum font-bold">{{ qos.percentage.toFixed(1) }}%</span>
              </div>
              <div class="progress-bar-bg">
                <div class="progress-bar-fill fill-qos" :style="{ width: `${Math.min(100, qos.percentage)}%` }"></div>
              </div>
              <div class="breakdown-meta tnum text-muted text-xs">
                <span>{{ formatBytes(qos.total_bytes) }}</span>
                <span>{{ formatNumber(qos.total_packets) }} pkts · {{ formatNumber(qos.flow_count) }} flows</span>
              </div>
            </div>
          </div>
        <div v-if="!qosClasses.length" class="no-chart-data">No QoS markings recorded in this window.</div>
      </div>
      <ConversationsPanel
        :window="selectedWindow"
        :exporter-id="selectedExporterId || null"
        initial-quick="qos"
        title="QoS-marked conversations"
        v-model:ip="selectedIpFilter"
        v-model:port="selectedPortFilter"
        v-model:dscp="selectedDscpFilter"
        v-model:dscp-label="selectedDscpLabel"
        :refresh-key="refreshKey"
      />
    </template>

    <!-- ── Security ── -->
    <template v-else-if="activeView === 'security'">
      <p class="kpi-scope-note">Last 5 complete minutes · {{ scopeLabel }}</p>
      <div class="kpi-grid security-kpis">
        <KpiTile
          label="Suspicious conversations"
          :value="formatNumber(overview.anomaly_conversations_count)"
          :tone="overview.anomaly_conversations_count ? 'down' : 'neutral'"
          sub="SYN without ACK, or RST without FIN, over the conversation"
        />
        <KpiTile
          label="Flows in them"
          :value="formatNumber(overview.anomaly_flows_count)"
          :tone="overview.anomaly_flows_count ? 'down' : 'neutral'"
          :sub="`of ${formatNumber(overview.total_flows_count)} flows`"
        />
      </div>
      <ConversationsPanel
        :window="selectedWindow"
        :exporter-id="selectedExporterId || null"
        initial-quick="anomalies"
        initial-sort="flows"
        title="Suspicious conversations"
        :hint="`Last ${selectedWindow} · SYN-only conversations suggest scans; RST-only ones refused connections. Sorted by flow count.`"
        v-model:ip="selectedIpFilter"
        v-model:port="selectedPortFilter"
        v-model:dscp="selectedDscpFilter"
        v-model:dscp-label="selectedDscpLabel"
        :refresh-key="refreshKey"
      />
    </template>

    </template>

    <!-- Mapping Modal for Unmatched Exporter -->
    <div v-if="showMappingModal" class="modal-overlay" @click.self="showMappingModal = false">
      <div class="modal-card">
        <div class="modal-header">
          <h3>Link Exporter Alias</h3>
          <button class="btn-close" @click="showMappingModal = false">✕</button>
        </div>
        <div class="modal-form">
          <p class="text-sm text-secondary">
            Map unmatched IP <strong class="tnum">{{ selectedUnmatchedIp }}</strong> as an alias to an existing monitored endpoint router/switch:
          </p>

          <div class="form-group">
            <label for="endpoint-select">Monitored Target *</label>
            <select
              id="endpoint-select"
              v-model="targetEndpointId"
              class="form-select"
              required
            >
              <option :value="null" disabled>Select target node...</option>
              <option
                v-for="ep in availableEndpoints"
                :key="ep.id"
                :value="ep.id"
              >
                {{ ep.hostname }} ({{ ep.ip_address }})
              </option>
            </select>
          </div>

          <div v-if="mappingError" class="alert-error" role="alert">
            {{ mappingError }}
          </div>

          <div class="modal-actions">
            <button class="btn-secondary" @click="showMappingModal = false">Cancel</button>
            <button
              class="btn-primary"
              @click="submitExporterMapping"
              :disabled="!targetEndpointId || mappingLoading"
            >
              {{ mappingLoading ? 'Saving...' : 'Link Exporter IP' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Enrollment Modal for Unmatched Exporter -->
    <div v-if="showEnrollModal" class="modal-overlay" @click.self="showEnrollModal = false">
      <div class="modal-card">
        <div class="modal-header">
          <h3>Enroll Flow Exporter</h3>
          <button class="btn-close" @click="showEnrollModal = false">✕</button>
        </div>
        <div class="modal-form">
          <p class="text-sm text-secondary">
            Enroll streaming device <strong class="tnum">{{ enrollForm.ip_address }}</strong> as a dedicated Flow Exporter node.
          </p>

          <div class="form-group">
            <label for="enroll-hostname">Exporter Hostname *</label>
            <input
              id="enroll-hostname"
              v-model="enrollForm.hostname"
              class="form-input"
              placeholder="e.g. Cisco-Core-01"
              required
            />
          </div>

          <div class="form-group">
            <label for="enroll-desc">Description</label>
            <input
              id="enroll-desc"
              v-model="enrollForm.description"
              class="form-input"
              placeholder="e.g. Edge router sending NetFlow v9"
            />
          </div>

          <div v-if="enrollError" class="alert-error" role="alert">
            {{ enrollError }}
          </div>

          <div class="modal-actions">
            <button class="btn-secondary" @click="showEnrollModal = false">Cancel</button>
            <button
              class="btn-primary"
              @click="submitExporterEnrollment"
              :disabled="!enrollForm.hostname || enrollLoading"
            >
              {{ enrollLoading ? 'Enrolling...' : 'Enroll Exporter' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Router Interface Configuration Modal (Admin Only) -->
    <div v-if="showInterfaceModal" class="modal-overlay" @click.self="showInterfaceModal = false">
      <div class="modal-card wide">
        <div class="modal-header">
          <h3>⚙️ Configure Router Interface Aliases & Speeds</h3>
          <button class="btn-close" @click="showInterfaceModal = false">✕</button>
        </div>

        <form @submit.prevent="saveInterfaces" class="modal-form">
          <p class="modal-subtitle">
            Assign human-readable SNMP interface names and provisioned line speeds for accurate utilization calculation.
          </p>

          <div class="interface-config-list">
            <div
              v-for="(iface, i) in editingInterfaces"
              :key="i"
              class="interface-config-row"
            >
              <div class="form-group idx-col">
                <label v-if="i === 0">ifIndex *</label>
                <input
                  type="number"
                  v-model.number="iface.index"
                  min="1"
                  max="2147483647"
                  required
                  placeholder="e.g. 1"
                  class="form-input"
                />
              </div>

              <div class="form-group name-col">
                <label v-if="i === 0">Interface Name / Alias *</label>
                <input
                  type="text"
                  v-model="iface.name"
                  required
                  placeholder="e.g. GigabitEthernet0/1 or WAN-Uplink"
                  class="form-input"
                />
              </div>

              <div class="form-group preset-col">
                <label v-if="i === 0">Speed Preset</label>
                <select v-model="iface.preset" @change="onPresetChange(iface)" class="form-select">
                  <option :value="10">10 Mbps (Ethernet)</option>
                  <option :value="100">100 Mbps (FastEth)</option>
                  <option :value="1000">1 Gbps (GigEth)</option>
                  <option :value="10000">10 Gbps (10GE)</option>
                  <option :value="40000">40 Gbps (40GE)</option>
                  <option :value="100000">100 Gbps (100GE)</option>
                  <option value="custom">Custom</option>
                </select>
              </div>

              <div class="form-group speed-col" v-if="iface.preset === 'custom'">
                <label v-if="i === 0">Mbps</label>
                <input
                  type="number"
                  v-model.number="iface.speed_mbps"
                  min="1"
                  required
                  class="form-input"
                />
              </div>

              <div class="remove-col">
                <label v-if="i === 0">&nbsp;</label>
                <button
                  type="button"
                  class="btn-icon-danger"
                  @click="removeInterfaceRow(i)"
                  title="Remove interface"
                >
                  ✕
                </button>
              </div>
            </div>
          </div>

          <div class="add-row-action">
            <button type="button" class="btn-secondary btn-small" @click="addInterfaceRow">
              + Add Interface
            </button>
          </div>

          <div class="modal-actions" style="margin-top: 1rem;">
            <button type="button" class="btn-secondary" @click="showInterfaceModal = false">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="savingInterfaces">
              {{ savingInterfaces ? 'Saving...' : 'Save Interfaces' }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, shallowRef, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Line as LineChart } from 'vue-chartjs'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js'
import {
  getBandwidthOverview,
  getTrafficSeries,
  getTopTalkers,
  getApplicationDistribution,
  getFlowExporters,
  enrollFlowExporter,
  mapFlowExporter,
  getInterfaceTelemetry,
  updateEndpointInterfaces,
  getEndpoints,
} from '../services/api.js'
import { isAdmin } from '../services/auth.js'
import { formatBps, formatBytes, formatNumber, formatPortLabel, formatPps } from '../utils/format.js'
import { useChartTheme } from '../composables/useChartTheme.js'
import { summarizeSeries } from '../utils/chartSummary.js'
import KpiTile from '../components/ui/KpiTile.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'
import TimeRangePicker from '../components/ui/TimeRangePicker.vue'
import ConversationsPanel from '../components/traffic/ConversationsPanel.vue'

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler
)

const windowOptions = ['1h', '6h', '24h', '7d', '30d']
const selectedWindow = ref('1h')
const loading = ref(false)

// Auto-Refresh state (UI-07)
const autoRefreshInterval = ref('0')
let refreshTimer = null

function handleAutoRefreshChange() {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
  const secs = parseInt(autoRefreshInterval.value, 10)
  if (secs > 0) {
    refreshTimer = setInterval(() => {
      if (!loading.value) {
        refreshAll()
      }
    }, secs * 1000)
  }
}

const route = useRoute()
const router = useRouter()
const selectedExporterId = ref(route.query.exporter_id || '')
const flowExporters = shallowRef([])
const interfaceList = shallowRef([])
const selectedInterfaceIdx = ref(null)

// Exporter Scope Filtering & Unmatched Detection State
const exporterSearchText = ref('')

const isUnmatchedScope = computed(() => {
  return typeof selectedExporterId.value === 'string' && selectedExporterId.value.startsWith('unmatched:')
})

const activeUnmatchedIp = computed(() => {
  if (isUnmatchedScope.value) {
    return selectedExporterId.value.replace('unmatched:', '')
  }
  return ''
})

const filteredFlowExporters = computed(() => {
  const all = flowExporters.value
  if (!exporterSearchText.value.trim()) return all
  const q = exporterSearchText.value.toLowerCase().trim()
  return all.filter((exp) => {
    if (exp.id === selectedExporterId.value) return true
    return (
      (exp.hostname && exp.hostname.toLowerCase().includes(q)) ||
      (exp.primary_ip && exp.primary_ip.toLowerCase().includes(q))
    )
  })
})

const filteredUnmatchedExporters = computed(() => {
  const all = unmatchedExporters.value
  if (!exporterSearchText.value.trim()) return all
  const q = exporterSearchText.value.toLowerCase().trim()
  return all.filter((u) => {
    if (`unmatched:${u.ip_address}` === selectedExporterId.value) return true
    return u.ip_address && u.ip_address.toLowerCase().includes(q)
  })
})

// Enrollment modal state
const showEnrollModal = ref(false)
const enrollForm = ref({
  ip_address: '',
  hostname: '',
  description: '',
  device_role: 'FLOW_EXPORTER',
})
const enrollLoading = ref(false)
const enrollError = ref(null)

const overview = ref({
  total_ingress_bps: 0,
  total_egress_bps: 0,
  total_ingress_pps: 0,
  total_egress_pps: 0,
  total_transit_bps: 0,
  total_transit_pps: 0,
  avg_packet_size: 0,
  active_exporters_count: 0,
  unmatched_exporters_count: 0,
  total_flows_count: 0,
  anomaly_flows_count: 0,
})

// Throughput Metric Display Modes:
// - 'bps': Standard capacity planning throughput in bits per second (converted from bytes).
// - 'pps': Packet rate in packets per second; essential for detecting PPS exhaustion attacks and hardware NIC limits.
// - 'fps': Flow rate in active flows per second; reflects connection churn and state table stress.
const chartMetricMode = ref('bps') // 'bps', 'pps', 'fps'

const { theme, withAlpha } = useChartTheme()

const metricOptions = [
  { value: 'bps', label: 'Throughput', title: 'Bits per second' },
  { value: 'pps', label: 'Packets', title: 'Packets per second' },
  { value: 'fps', label: 'Flows', title: 'Flow records per second' },
]

const availableEndpoints = shallowRef([])
const unmatchedExporters = shallowRef([])

const topEndpoints = shallowRef([])
const applicationStats = shallowRef([])
const qosClasses = shallowRef([])
const trafficZones = shallowRef([])

// Cross-filters shared by the Conversations, QoS and Security views. A talker, application or
// QoS class sets one; ConversationsPanel shows and clears them. They live in the URL so a
// filtered view can be shared.
const selectedIpFilter = ref(route.query.ip || null)
const selectedPortFilter = ref(route.query.port ? Number(route.query.port) : null)
const selectedDscpFilter = ref(route.query.dscp ? Number(route.query.dscp) : null)
const selectedDscpLabel = ref('')

// Bumped after each refresh so the conversation panels reload with the page.
const refreshKey = ref(0)

const VIEWS = ['overview', 'interfaces', 'conversations', 'qos', 'security']
const activeView = computed(() => (VIEWS.includes(route.query.view) ? route.query.view : 'overview'))

const viewOptions = computed(() => [
  { value: 'overview', label: 'Overview' },
  { value: 'interfaces', label: 'Interfaces', title: selectedExporterId.value ? 'Per-interface rates' : 'Pick an exporter first' },
  { value: 'conversations', label: 'Conversations' },
  { value: 'qos', label: qosClasses.value.length ? `QoS (${qosClasses.value.length})` : 'QoS' },
  {
    value: 'security',
    label: overview.value.anomaly_conversations_count ? `Security (${overview.value.anomaly_conversations_count})` : 'Security',
  },
])

// route.query only updates once a navigation resolves, so back-to-back changes (view + filter)
// build on the query still in flight instead of overwriting each other.
let pendingQuery = null

function replaceQuery(changes) {
  const query = { ...(pendingQuery || route.query), ...changes }
  for (const key of Object.keys(query)) {
    if (query[key] === null || query[key] === undefined || query[key] === '') delete query[key]
  }
  pendingQuery = query
  router.replace({ query }).finally(() => {
    if (pendingQuery === query) pendingQuery = null
  })
}

function setView(view) {
  replaceQuery({ view: view === 'overview' ? undefined : view })
}

function openConversationsFor({ ip = null, port = null } = {}) {
  selectedIpFilter.value = ip
  selectedPortFilter.value = port
  selectedDscpFilter.value = null
  selectedDscpLabel.value = ''
  setView('conversations')
}

watch([selectedIpFilter, selectedPortFilter, selectedDscpFilter], ([ip, port, dscp]) => {
  replaceQuery({ ip, port, dscp })
})

const zonesResolution = ref('ip')
const zonesHint = computed(() => {
  if (zonesResolution.value === 'endpoint') {
    return 'Hourly data keeps no addresses: only monitored endpoints count as internal'
  }
  return 'Monitored endpoints and private (RFC 1918) addresses count as internal'
})

const scopeLabel = computed(() => {
  if (!selectedExporterId.value) return 'all exporters'
  const exp = flowExporters.value.find((e) => e.id === selectedExporterId.value)
  return exp ? exp.hostname : 'selected exporter'
})

const exporterSummary = computed(() => {
  const configured = flowExporters.value.length
  const unregistered = unmatchedExporters.value.length
  const parts = [`of ${configured} configured`]
  if (unregistered) parts.push(`${unregistered} unregistered`)
  return parts.join(' · ')
})

const anomalyConversationsCount = computed(() => {
  return topConversations.value.filter(
    (c) => c.anomaly_hint === 'SYN_ONLY' || c.anomaly_hint === 'TCP_RST'
  ).length
})

const qosConversationsCount = computed(() => {
  return topConversations.value.filter((c) => Number(c.dscp) > 0).length
})

const bulkConversationsCount = computed(() => {
  return topConversations.value.filter(
    (c) => c.traffic_profile === 'Bulk Transfer' || (c.avg_packet_size && c.avg_packet_size >= 1100)
  ).length
})

// Unified conversation filtration pipeline combining cross-filters, quick triage pills, and text search:
const filteredConversations = computed(() => {
  let list = topConversations.value

  // Cross-Filter by Endpoint Node IP
  if (selectedIpFilter.value) {
    const ip = selectedIpFilter.value
    list = list.filter((c) => c.src_ip === ip || c.dst_ip === ip)
  }

  // Cross-Filter by Destination Service Port
  if (selectedPortFilter.value !== null) {
    const port = Number(selectedPortFilter.value)
    list = list.filter((c) => Number(c.dst_port) === port)
  }

  // Cross-Filter by DSCP
  if (selectedDscpFilter.value !== null) {
    const dscp = Number(selectedDscpFilter.value)
    list = list.filter((c) => Number(c.dscp) === dscp)
  }

  // Quick Triage Filter
  if (conversationQuickFilter.value === 'anomalies') {
    list = list.filter((c) => c.anomaly_hint === 'SYN_ONLY' || c.anomaly_hint === 'TCP_RST')
  } else if (conversationQuickFilter.value === 'qos') {
    list = list.filter((c) => Number(c.dscp) > 0)
  } else if (conversationQuickFilter.value === 'bulk') {
    list = list.filter(
      (c) => c.traffic_profile === 'Bulk Transfer' || (c.avg_packet_size && c.avg_packet_size >= 1100)
    )
  }

  if (!conversationFilter.value.trim()) return list
  const q = conversationFilter.value.toLowerCase().trim()
  return list.filter((c) => {
    return (
      (c.src_ip && c.src_ip.toLowerCase().includes(q)) ||
      (c.dst_ip && c.dst_ip.toLowerCase().includes(q)) ||
      (c.src_hostname && c.src_hostname.toLowerCase().includes(q)) ||
      (c.dst_hostname && c.dst_hostname.toLowerCase().includes(q)) ||
      String(c.dst_port).includes(q) ||
      (c.protocol && c.protocol.toLowerCase().includes(q)) ||
      (c.dscp_label && c.dscp_label.toLowerCase().includes(q)) ||
      (c.anomaly_hint && c.anomaly_hint.toLowerCase().includes(q)) ||
      (c.traffic_profile && c.traffic_profile.toLowerCase().includes(q)) ||
      (Array.isArray(c.tcp_flags_labels) && c.tcp_flags_labels.some((f) => f.toLowerCase().includes(q)))
    )
  })
})

function formatSpeed(speedMbps) {
  if (!speedMbps) return 'Not set'
  if (speedMbps >= 1000000) return `${(speedMbps / 1000000).toFixed(0)} Tbps`
  if (speedMbps >= 1000) return `${(speedMbps / 1000).toFixed(0)} Gbps`
  return `${speedMbps} Mbps`
}

function getUtilClass(util) {
  if (util >= 80) return 'util-danger'
  if (util >= 60) return 'util-warning'
  return 'util-healthy'
}

function handleScopeChange() {
  selectedInterfaceIdx.value = null
  router.push({
    query: {
      ...route.query,
      exporter_id: selectedExporterId.value || undefined,
    },
  })
  refreshData()
}

async function fetchTrafficSeriesData() {
  try {
    const expId = (!isUnmatchedScope.value && selectedExporterId.value) ? selectedExporterId.value : null
    const seriesRes = await getTrafficSeries(selectedWindow.value, expId, null, selectedInterfaceIdx.value)
    applySeries(seriesRes.data?.data)
  } catch (err) {
    console.error('Failed to load traffic series for interface isolation:', err)
  }
}

async function toggleInterfaceIsolation(ifIdx) {
  if (selectedInterfaceIdx.value === ifIdx) {
    selectedInterfaceIdx.value = null
  } else {
    selectedInterfaceIdx.value = ifIdx
  }
  await fetchTrafficSeriesData()
}

async function clearInterfaceIsolation() {
  selectedInterfaceIdx.value = null
  await fetchTrafficSeriesData()
}

function openEnrollModal(ip) {
  enrollForm.value = {
    ip_address: ip,
    hostname: `router-${ip.replace(/\./g, '-')}`,
    description: `Flow Exporter streaming from ${ip}`,
    device_role: 'FLOW_EXPORTER',
  }
  enrollError.value = null
  showEnrollModal.value = true
}

async function submitExporterEnrollment() {
  if (!enrollForm.value.hostname || !enrollForm.value.ip_address) return
  enrollLoading.value = true
  enrollError.value = null
  try {
    const res = await enrollFlowExporter(enrollForm.value)
    showEnrollModal.value = false
    if (res.data?.data?.id) {
      selectedExporterId.value = res.data.data.id
      router.push({
        query: {
          ...route.query,
          exporter_id: res.data.data.id,
        },
      })
    }
    await refreshData()
  } catch (err) {
    enrollError.value = err.response?.data?.detail || 'Failed to enroll flow exporter.'
  } finally {
    enrollLoading.value = false
  }
}

// Mapping Modal
const showMappingModal = ref(false)
const selectedUnmatchedIp = ref('')
const targetEndpointId = ref(null)
const mappingLoading = ref(false)
const mappingError = ref(null)

// Interface Configuration Modal (Admin Only)
const showInterfaceModal = ref(false)
const editingInterfaces = ref([])
const savingInterfaces = ref(false)

function openInterfaceModal(focusIdx = null) {
  const current = interfaceList.value || []
  let rows = current.map((item) => {
    const sp = item.speed_mbps || 1000
    return {
      index: Number(item.interface_idx) || 1,
      name: item.name || `GigabitEthernet0/${item.interface_idx}`,
      speed_mbps: sp,
      preset: [10, 100, 1000, 10000, 40000, 100000].includes(sp) ? sp : 'custom',
    }
  })

  if (rows.length === 0) {
    rows = [{ index: 1, name: 'GigabitEthernet0/1', speed_mbps: 1000, preset: 1000 }]
  }

  editingInterfaces.value = rows
  showInterfaceModal.value = true
}

function addInterfaceRow() {
  const nextIdx = editingInterfaces.value.length + 1
  editingInterfaces.value.push({
    index: nextIdx,
    name: `GigabitEthernet0/${nextIdx}`,
    speed_mbps: 1000,
    preset: 1000,
  })
}

function removeInterfaceRow(index) {
  editingInterfaces.value.splice(index, 1)
}

function onPresetChange(row) {
  if (row.preset !== 'custom') {
    row.speed_mbps = Number(row.preset)
  }
}

async function saveInterfaces() {
  if (!selectedExporterId.value) return
  savingInterfaces.value = true
  try {
    const payload = {}
    for (const row of editingInterfaces.value) {
      const idxStr = String(row.index).trim()
      if (idxStr) {
        payload[idxStr] = {
          name: row.name.trim() || `if${idxStr}`,
          speed_mbps: Number(row.speed_mbps) || 1000,
        }
      }
    }
    await updateEndpointInterfaces(selectedExporterId.value, payload)
    showInterfaceModal.value = false
    await refreshData()
  } catch (err) {
    console.error('Failed to update interfaces:', err)
    alert(err.response?.data?.detail || 'Failed to update interfaces.')
  } finally {
    savingInterfaces.value = false
  }
}

// Chart Title & Multi-Mode Metric Computeds
const chartTitle = computed(() => {
  const matched = selectedInterfaceIdx.value !== null
    ? interfaceList.value.find(i => i.interface_idx === selectedInterfaceIdx.value)
    : null
  const ifName = matched
    ? (matched.alias?.name || matched.name || `Interface #${selectedInterfaceIdx.value}`)
    : (selectedInterfaceIdx.value !== null ? `Interface #${selectedInterfaceIdx.value}` : null)

  if (ifName) {
    if (chartMetricMode.value === 'pps') return `${ifName} Packet Rate Telemetry`
    if (chartMetricMode.value === 'fps') return `${ifName} Flow Rate Telemetry`
    return `${ifName} Interface Telemetry (Isolated)`
  }

  if (chartMetricMode.value === 'pps') return 'Packet Rate Telemetry (pps)'
  if (chartMetricMode.value === 'fps') return 'Flow Burst Rate Telemetry (fps)'
  return 'Aggregated Flow Throughput'
})

// Chart Data
const seriesPoints = shallowRef([])
const seriesMeta = ref({ bucket_seconds: 60, directional: true })

function applySeries(data) {
  if (!data?.points) return
  seriesPoints.value = data.points
  seriesMeta.value = {
    bucket_seconds: data.bucket_seconds || 60,
    directional: data.directional !== false,
  }
}

const WINDOW_SECONDS = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 }
const MAX_FILLED_BUCKETS = 3000

// Lay the points on a regular bucket grid covering the whole window, with null where no data
// arrived, so outages and aggregation lag show as gaps instead of a continuous line.
const filledPoints = computed(() => {
  const points = seriesPoints.value
  const step = (seriesMeta.value.bucket_seconds || 60) * 1000
  const span = (WINDOW_SECONDS[selectedWindow.value] || 3600) * 1000
  const now = Date.now()
  const lastBucket = Math.floor(now / step) * step - step
  const firstBucket = lastBucket - span + step
  const byTime = new Map(points.map((p) => [Date.parse(p.timestamp), p]))
  if ((lastBucket - firstBucket) / step + 1 > MAX_FILLED_BUCKETS) {
    return points.map((p) => ({ t: Date.parse(p.timestamp), p }))
  }
  const grid = []
  for (let t = firstBucket; t <= lastBucket; t += step) {
    grid.push({ t, p: byTime.get(t) || null })
  }
  // Keep any point that falls off the grid (e.g. a bucket still being filled).
  for (const [t, p] of byTime) {
    if (t > lastBucket) grid.push({ t, p })
  }
  return grid
})

const chartHint = computed(() => {
  const bucket = seriesMeta.value.bucket_seconds || 60
  const res = bucket >= 86400 ? '1-day' : bucket >= 3600 ? '1-hour' : '1-minute'
  const direction = seriesMeta.value.directional ? '' : ' · direction not kept at this resolution'
  return `Last ${selectedWindow.value} · ${res} averages · gaps mean no data${direction}`
})

function formatAxisTime(t) {
  const d = new Date(t)
  const span = WINDOW_SECONDS[selectedWindow.value] || 3600
  if (span <= 86400) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (span <= 604800) return d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function lineDataset(label, data, color, opts = {}) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: withAlpha(color, 0.2),
    borderWidth: 2,
    fill: opts.fill ?? true,
    tension: 0.3,
    spanGaps: false,
    pointRadius: data.length > 100 ? 0 : 2,
  }
}

const chartData = computed(() => {
  const grid = filledPoints.value
  const labels = grid.map((g) => formatAxisTime(g.t))
  const pick = (key) => grid.map((g) => (g.p ? g.p[key] || 0 : null))
  const series = theme.value.series

  const matched = selectedInterfaceIdx.value !== null
    ? interfaceList.value.find(i => i.interface_idx === selectedInterfaceIdx.value)
    : null
  const prefix = selectedInterfaceIdx.value !== null
    ? `${matched?.alias?.name || matched?.name || `Interface #${selectedInterfaceIdx.value}`} `
    : ''

  if (chartMetricMode.value === 'fps') {
    return { labels, datasets: [lineDataset(`${prefix}Flows (per second)`, pick('flows_per_sec'), series.flows)] }
  }

  const unit = chartMetricMode.value === 'pps' ? 'pps' : 'bps'
  const datasets = []
  if (seriesMeta.value.directional) {
    datasets.push(lineDataset(`${prefix}Ingress (${unit})`, pick(`ingress_${unit}`), series.in))
    datasets.push(lineDataset(`${prefix}Egress (${unit})`, pick(`egress_${unit}`), series.out))
    const transit = pick(`transit_${unit}`)
    if (transit.some((v) => v)) {
      datasets.push(lineDataset(`Transit (${unit})`, transit, series.transit, { fill: false }))
    }
  } else {
    datasets.push(lineDataset(`Total (${unit})`, pick(`transit_${unit}`), series.transit))
  }
  return { labels, datasets }
})

// Text alternative for the canvas chart.
const chartSummary = computed(() => {
  const unit = chartMetricMode.value
  const fmt = unit === 'pps' ? formatPps : unit === 'fps' ? (v) => `${v.toFixed(2)} flows/s` : formatBps
  const pts = seriesPoints.value.map((p) => ({ t: Date.parse(p.timestamp), p }))
  const keys = unit === 'fps'
    ? [['flows_per_sec', 'Flows']]
    : seriesMeta.value.directional
      ? [[`ingress_${unit}`, 'Ingress'], [`egress_${unit}`, 'Egress'], [`transit_${unit}`, 'Transit']]
      : [[`transit_${unit}`, 'Total']]
  return [`${chartTitle.value}, last ${selectedWindow.value}.`]
    .concat(keys.map(([key, label]) => summarizeSeries(label, pts.map(({ t, p }) => ({ t, v: p[key] ?? null })), fmt)))
    .join(' ')
})

const chartOptions = computed(() => {
  const t = theme.value
  const formatValue = (val) => {
    if (val === null || val === undefined) return 'no data'
    if (chartMetricMode.value === 'pps') return formatPps(val)
    if (chartMetricMode.value === 'fps') return `${val} fps`
    return formatBps(val)
  }

  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    normalized: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: {
        grid: { display: false },
        ticks: { color: t.text, font: { family: t.fontSans, size: 11 }, maxTicksLimit: 12, autoSkip: true },
      },
      y: {
        beginAtZero: true,
        ticks: {
          color: t.text,
          font: { family: t.fontMono, size: 11 },
          callback: (val) => formatValue(val),
        },
        grid: { color: t.grid },
      },
    },
    plugins: {
      legend: {
        position: 'top',
        labels: { color: t.text, font: { family: t.fontSans, size: 12 } },
      },
      tooltip: {
        callbacks: {
          label: (ctx) => `${ctx.dataset.label}: ${formatValue(ctx.raw)}`,
        },
      },
    },
  }
})

function setWindow(w) {
  selectedWindow.value = w
  refreshData()
}

function getPacketProfileLabel(avgBytes) {
  const b = Number(avgBytes) || 0
  if (b >= 1100) return 'Bulk / Jumbo MTU'
  if (b <= 150) return 'Control / Micro-Flows'
  return 'Mixed / Standard MTU'
}

function getPacketProfileClass(avgBytes) {
  const b = Number(avgBytes) || 0
  if (b >= 1100) return 'profile-bulk'
  if (b <= 150) return 'profile-micro'
  return 'profile-mixed'
}

function getZoneBadgeClass(zone) {
  if (!zone) return 'badge-zone-default'
  if (zone.includes('East-West')) return 'badge-zone-eastwest'
  if (zone.includes('Ingress')) return 'badge-zone-ingress'
  if (zone.includes('Egress')) return 'badge-zone-egress'
  return 'badge-zone-external'
}

function getZoneFillClass(zone) {
  if (!zone) return 'fill-default'
  if (zone.includes('East-West')) return 'fill-eastwest'
  if (zone.includes('Ingress')) return 'fill-ingress'
  if (zone.includes('Egress')) return 'fill-egress'
  return 'fill-external'
}

function getZoneIcon(zone) {
  if (!zone) return '•'
  if (zone.includes('East-West')) return '⇄'
  if (zone.includes('Ingress')) return '↓'
  if (zone.includes('Egress')) return '↑'
  return '🌐'
}

function toggleDscpFilter(dscp, label) {
  if (selectedDscpFilter.value === dscp) {
    selectedDscpFilter.value = null
    selectedDscpLabel.value = ''
  } else {
    selectedDscpFilter.value = dscp
    selectedDscpLabel.value = label || `DSCP ${dscp}`
  }
}

function openMappingModal(ip) {
  selectedUnmatchedIp.value = ip
  targetEndpointId.value = availableEndpoints.value.length > 0 ? availableEndpoints.value[0].id : null
  mappingError.value = null
  showMappingModal.value = true
}

async function submitExporterMapping() {
  if (!targetEndpointId.value || !selectedUnmatchedIp.value) return
  mappingLoading.value = true
  mappingError.value = null
  try {
    const targetId = targetEndpointId.value
    await mapFlowExporter(selectedUnmatchedIp.value, targetId)
    showMappingModal.value = false
    selectedExporterId.value = targetId
    router.push({
      query: {
        ...route.query,
        exporter_id: targetId,
      },
    })
    await refreshData()
  } catch (err) {
    mappingError.value = err.response?.data?.detail || 'Failed to map exporter IP.'
  } finally {
    mappingLoading.value = false
  }
}

// Refresh button and auto-refresh: reload the page data and the conversation panels.
async function refreshAll() {
  await refreshData()
  refreshKey.value++
}

async function refreshData() {
  loading.value = true
  try {
    // An unregistered source has no data of its own yet: load only the inventory for the
    // enrol / link actions instead of showing fleet-wide numbers under its name.
    if (isUnmatchedScope.value) {
      const [expRes, epListRes] = await Promise.all([getFlowExporters(), getEndpoints()])
      if (expRes.data?.data) {
        unmatchedExporters.value = expRes.data.data.unmatched || []
        flowExporters.value = expRes.data.data.exporters || []
      }
      if (epListRes.data?.data) {
        availableEndpoints.value = epListRes.data.data
      }
      return
    }

    const expId = selectedExporterId.value || null
    const [ovRes, seriesRes, talkersRes, appRes, expRes, epListRes, ifRes] = await Promise.all([
      getBandwidthOverview(expId),
      getTrafficSeries(selectedWindow.value, expId, null, selectedInterfaceIdx.value),
      getTopTalkers(selectedWindow.value, 10, null, expId),
      getApplicationDistribution(selectedWindow.value, expId),
      getFlowExporters(),
      getEndpoints(),
      expId ? getInterfaceTelemetry(expId, selectedWindow.value) : Promise.resolve({ data: { data: { interfaces: [] } } }),
    ])

    if (ovRes.data?.data) {
      overview.value = ovRes.data.data
    }
    applySeries(seriesRes.data?.data)
    if (talkersRes.data?.data) {
      topEndpoints.value = talkersRes.data.data.top_endpoints || []
    }
    if (appRes.data?.data) {
      applicationStats.value = appRes.data.data.applications || []
      qosClasses.value = appRes.data.data.qos_classes || []
      trafficZones.value = appRes.data.data.traffic_zones || []
      zonesResolution.value = appRes.data.data.zones_resolution || 'ip'
    }
    if (expRes.data?.data) {
      unmatchedExporters.value = expRes.data.data.unmatched || []
      flowExporters.value = expRes.data.data.exporters || []
    }
    if (epListRes.data?.data) {
      availableEndpoints.value = epListRes.data.data
    }
    if (ifRes?.data?.data?.interfaces) {
      interfaceList.value = ifRes.data.data.interfaces
    } else {
      interfaceList.value = []
    }
  } catch (err) {
    console.error('Failed to load bandwidth telemetry data:', err)
  } finally {
    loading.value = false
  }
}

watch(
  () => route.query.exporter_id,
  (newId) => {
    if (newId !== selectedExporterId.value) {
      selectedExporterId.value = newId || ''
      selectedInterfaceIdx.value = null
      refreshData()
    }
  }
)

onMounted(() => {
  refreshData()
})

onUnmounted(() => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
})
</script>

<style scoped>
.view-switcher {
  margin: 0.25rem 0 1rem;
}

.app-bars {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0 1.25rem 1rem;
}

.app-bar-row {
  display: grid;
  grid-template-columns: minmax(6rem, 9rem) 1fr auto;
  align-items: center;
  gap: 0.75rem;
  min-height: var(--target-min);
  padding: 0.25rem 0.375rem;
  border-radius: var(--radius-sm);
  text-align: left;
  font-size: var(--text-sm);
  color: var(--text-primary);
}

.app-bar-row:not(:disabled):hover {
  background: var(--bg-surface-hover);
}

.app-bar-row:disabled {
  cursor: default;
}

.app-bar-label {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-weight: 600;
}

.app-bar-track {
  height: 8px;
  border-radius: 4px;
  background: var(--bg-surface-selected);
  overflow: hidden;
}

.app-bar-fill {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--series-1);
}

.app-bar-fill.other {
  background: var(--series-8);
}

.app-bar-value {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  white-space: nowrap;
}

.zones-header {
  border-top: 1px solid var(--border-color);
}

.empty-view {
  padding: 2rem 1.5rem;
  text-align: center;
  color: var(--text-secondary);
}

.empty-view .empty-title {
  margin: 0 0 0.25rem;
  font-weight: 700;
  color: var(--text-primary);
}

.exporter-picks {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem;
  margin-top: 1rem;
}

.qos-panel,
.security-kpis {
  margin-bottom: 1rem;
}

.bandwidth-view {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.bandwidth-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}

.toolbar-sub-row {
  margin-top: 4px;
}

.page-title {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
}

.page-sub {
  font-size: 0.8125rem;
  color: var(--text-muted);
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* Unmatched Discovery Alert */
.unmatched-banner {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-radius: var(--radius, 8px);
  background: rgba(245, 158, 11, 0.12);
  border: 1px solid rgba(245, 158, 11, 0.4);
  color: #b45309;
}

.unmatched-icon {
  font-size: 20px;
  flex-shrink: 0;
}

.unmatched-title {
  font-size: 0.875rem;
  font-weight: 700;
}

/* KPI Metric Strip */
.kpi-scope-note {
  margin: 0 0 0.5rem;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.speed-unset {
  color: var(--status-text-warn);
  font-weight: 600;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

.ingress-text {
  color: #10b981;
}
.egress-text {
  color: #0ea5e9;
}

/* Charts & Panels */
.chart-section {
  padding: 16px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding: 0 4px;
}

.section-title {
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--text-primary);
}

.section-hint {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.chart-container {
  height: 320px;
  position: relative;
}

.no-chart-data, .loading-spinner {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  font-size: 0.875rem;
}

/* Secondary Grid */
.secondary-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 16px;
}

@media (max-width: 1024px) {
  .secondary-grid {
    grid-template-columns: 1fr;
  }
}
.node-cell {
  display: flex;
  flex-direction: column;
}

.node-host {
  font-size: 0.8125rem;
}

.node-ip {
  font-size: 0.75rem;
}

.text-right {
  text-align: right;
}
.text-center {
  text-align: center;
}
.font-bold {
  font-weight: 700;
}
.font-mono {
  font-family: var(--font-mono);
}
.block {
  display: block;
}
.text-xs {
  font-size: 0.6875rem;
}

/* Auto-Refresh (UI-07) */
.auto-refresh-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}

.refresh-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
}

.refresh-select {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  outline: none;
  cursor: pointer;
}

/* Unmatched Exporter Grid (BUG-05) */
.unmatched-header-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.unmatched-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 8px;
  width: 100%;
}

.unmatched-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-surface);
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
}

.unmatched-card-info {
  display: flex;
  flex-direction: column;
}

.unmatched-ip {
  font-weight: 700;
  font-size: 0.85rem;
  color: var(--text-primary);
}

.unmatched-sub {
  font-size: 0.7rem;
}

/* Top Conversations Header & Search (UI-09) */
.section-header-split {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.badge-dscp {
  display: inline-flex;
  align-items: center;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 600;
  background: rgba(139, 92, 246, 0.12);
  color: #8b5cf6;
  border: 1px solid rgba(139, 92, 246, 0.3);
}

.badge-dscp-ef {
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
  border-color: rgba(16, 185, 129, 0.4);
  font-weight: 700;
}

.badge-dscp-af {
  background: rgba(59, 130, 246, 0.15);
  color: #3b82f6;
  border-color: rgba(59, 130, 246, 0.4);
}

/* Scope Selector */
.scope-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}

.scope-input-wrapper {
  display: flex;
  align-items: center;
  gap: 6px;
}

.scope-search-input {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  width: 110px;
  outline: none;
  transition: border-color 0.15s ease, width 0.15s ease;
}

.scope-search-input:focus {
  border-color: var(--accent, #3b82f6);
  width: 150px;
}

.scope-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
}

.scope-select {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  outline: none;
  cursor: pointer;
  max-width: 280px;
}

/* Unmatched Scope Focused Callout */
.unmatched-scope-card {
  padding: 18px 22px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: var(--radius, 8px);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.unmatched-scope-header {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}

.unmatched-scope-icon {
  font-size: 2rem;
  line-height: 1;
  color: #f59e0b;
}

.unmatched-scope-info {
  flex: 1;
}

.unmatched-scope-badge {
  display: inline-block;
  font-size: 0.6875rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.2);
  color: #d97706;
  margin-bottom: 6px;
}

.unmatched-scope-title {
  margin: 0 0 6px 0;
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--text-primary);
}

.unmatched-scope-desc {
  margin: 0;
  font-size: 0.875rem;
  line-height: 1.5;
  color: var(--text-secondary);
}

.unmatched-scope-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-left: 48px;
  flex-wrap: wrap;
}

.unmatched-card-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}

/* Interface Telemetry Panel */
.interfaces-panel {
  margin-top: 1.5rem;
}

.interactive-table tr.cursor-pointer {
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.interactive-table tr.row-selected {
  background: rgba(99, 102, 241, 0.15) !important;
  border-left: 3px solid #6366f1;
}

.isolated-badge-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-isolated {
  background: rgba(99, 102, 241, 0.15);
  color: #818cf8;
  border: 1px solid rgba(99, 102, 241, 0.3);
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: 12px;
  font-weight: 600;
}

.util-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.util-bar-bg {
  position: relative;
  flex: 1;
  height: 6px;
  background: var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 3px;
}

.util-marker {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 12px;
  transform: translateX(-1px);
  background: var(--text-secondary);
}

.util-marker-peak {
  top: -6px;
  width: 0;
  height: 0;
  background: none;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid var(--status-text-down);
  transform: translateX(-4px);
}

.util-sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 500;
}

.util-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.util-bar-fill.util-healthy {
  background: #10b981;
}

.util-bar-fill.util-warning {
  background: #f59e0b;
}

.util-bar-fill.util-danger {
  background: #ef4444;
}

.util-text {
  font-size: 0.75rem;
  font-weight: 700;
  min-width: 45px;
  text-align: right;
  white-space: nowrap;
}

.util-text.util-healthy {
  color: #10b981;
}

.util-text.util-warning {
  color: #f59e0b;
}

.util-text.util-danger {
  color: #ef4444;
}

.badge-active {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 600;
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.badge-idle {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 600;
  background: rgba(156, 163, 175, 0.15);
  color: #9ca3af;
  border: 1px solid rgba(156, 163, 175, 0.3);
}

.header-actions-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.name-cell-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.btn-row-edit {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 2px 6px;
  border-radius: 4px;
  transition: all 0.15s ease;
}

.btn-row-edit:hover {
  background: var(--bg-surface-hover);
  color: var(--accent-primary, #2563eb);
}

.interface-config-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 380px;
  overflow-y: auto;
  margin: 1rem 0;
  padding-right: 4px;
}

.interface-config-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  background: var(--bg-surface-hover);
  padding: 8px 12px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-color);
}

.interface-config-row .idx-col { width: 80px; }
.interface-config-row .name-col { flex: 1; }
.interface-config-row .preset-col { width: 170px; }
.interface-config-row .speed-col { width: 100px; }
.interface-config-row .remove-col { width: 36px; display: flex; align-items: flex-end; }

.text-accent {
  color: var(--accent-primary, #3b82f6);
}

.badge-packet-profile {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.profile-bulk {
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.35);
}

.profile-micro {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.35);
}

.profile-mixed {
  background: rgba(59, 130, 246, 0.15);
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.35);
}

.badge-anomaly-kpi {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 700;
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.35);
}

.chart-header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* Multi-Tab Breakdown Panel */
.breakdown-panel {
  padding: 16px;
}

.breakdown-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 280px;
  overflow-y: auto;
  padding-right: 4px;
}

.breakdown-item {
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 6px);
  padding: 8px 12px;
  transition: all 0.15s ease;
}

.breakdown-item.cursor-pointer:hover {
  border-color: var(--accent-primary, #3b82f6);
}

.breakdown-item.item-selected {
  background: rgba(99, 102, 241, 0.12);
  border-color: #6366f1;
}

.breakdown-row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.breakdown-pct {
  font-size: 0.8125rem;
  color: var(--text-primary);
}

.progress-bar-bg {
  width: 100%;
  height: 6px;
  background: var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 6px;
}

.progress-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.fill-qos {
  background: linear-gradient(90deg, #8b5cf6, #ec4899);
}

.fill-eastwest {
  background: #3b82f6;
}

.fill-ingress {
  background: #10b981;
}

.fill-egress {
  background: #f59e0b;
}

.fill-external {
  background: #6b7280;
}

.breakdown-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* Zone Badges */
.zone-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 4px;
}

.badge-zone-eastwest {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.badge-zone-ingress {
  background: rgba(16, 185, 129, 0.12);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.badge-zone-egress {
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.badge-zone-external {
  background: rgba(107, 114, 128, 0.12);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}
</style>
