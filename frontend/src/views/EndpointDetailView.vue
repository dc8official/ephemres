<template>
  <div class="detail-wrapper">


    <!-- Main Container -->
    <div class="detail-container">
      
      <!-- Back & Action Header -->
      <div class="back-header">
        <Button 
          label="Back to Dashboard" 
          icon="pi pi-arrow-left" 
          severity="secondary" 
          outlined 
          class="back-btn" 
          @click="goToDashboard" 
        />
        
        <div v-if="endpoint" class="device-actions">
          <Button 
            v-if="isAdmin"
            icon="pi pi-cog" 
            label="Settings" 
            @click="openSettingsModal" 
            severity="secondary"
            size="small"
            class="settings-btn"
          />
          <Button 
            icon="pi pi-refresh" 
            label="Refresh" 
            @click="loadData" 
            :loading="loading"
            severity="secondary"
            size="small"
            class="refresh-btn"
          />
          <StatusBadge :status="endpoint.endpoint_status" />
          <span class="monitoring-badge" :class="endpoint.monitoring_enabled ? 'enabled' : 'disabled'">
            <i class="pi" :class="endpoint.monitoring_enabled ? 'pi-eye' : 'pi-eye-slash'"></i>
            {{ endpoint.monitoring_enabled ? 'Active' : 'Paused' }}
          </span>
        </div>
      </div>

      <!-- Loading State -->
      <div v-if="loading && !endpoint" class="loading-state">
        <i class="pi pi-spin pi-spinner spinner-icon"></i>
        <p>Analyzing telemetry and assembling uptime charts...</p>
      </div>

      <!-- Error State -->
      <div v-else-if="error" class="error-message">
        <i class="pi pi-exclamation-triangle"></i>
        <div>
          <h4>Telemetry Load Failure</h4>
          <p>{{ error }}</p>
          <Button label="Retry Query" icon="pi pi-refresh" severity="danger" class="mt-2" @click="loadData" />
        </div>
      </div>

      <!-- Main Dashboard Content -->
      <div v-else-if="endpoint" class="content-layout">
        
        <!-- Endpoint Overview Header Card -->
        <Card class="overview-card">
          <template #content>
            <div class="meta-grid">
              <div class="meta-item main-meta">
                <i class="pi" :class="getDeviceIcon(endpoint.device_type)"></i>
                <div>
                  <h2>{{ endpoint.hostname }}</h2>
                  <p class="ip-display">{{ endpoint.ip_address }}</p>
                </div>
              </div>
              <div class="meta-item">
                <span class="meta-label">Device Type</span>
                <span class="meta-value">{{ endpoint.device_type }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">Physical Location</span>
                <span class="meta-value">{{ endpoint.location || 'Not Specified' }}</span>
              </div>
              <div class="meta-item full-width">
                <span class="meta-label">Metadata Description</span>
                <span class="meta-value desc">{{ endpoint.description || 'No description provided.' }}</span>
              </div>
            </div>
          </template>
        </Card>

        <!-- Primary Perspective Tab Switcher -->
        <div class="perspective-tabs" role="tablist" aria-label="Target Observability Perspective">
          <button
            role="tab"
            class="perspective-tab"
            :class="{ active: activePerspective === 'icmp' }"
            :aria-selected="activePerspective === 'icmp'"
            id="tab-icmp"
            aria-controls="panel-icmp"
            @click="activePerspective = 'icmp'"
          >
            <i class="pi pi-chart-line"></i>
            <span>ICMP Health & Diagnostics</span>
          </button>
          <button
            role="tab"
            class="perspective-tab"
            :class="{ active: activePerspective === 'flow' }"
            :aria-selected="activePerspective === 'flow'"
            id="tab-flow"
            aria-controls="panel-flow"
            @click="switchPerspective('flow')"
          >
            <i class="pi pi-sliders-h"></i>
            <span>Bandwidth & Flow Telemetry</span>
          </button>
        </div>

        <!-- Tab 1: ICMP Health & Diagnostics Panel -->
        <div v-show="activePerspective === 'icmp'" id="panel-icmp" role="tabpanel" aria-labelledby="tab-icmp">
          <!-- Flow Exporter Passive Telemetry Banner -->
          <div v-if="endpoint.device_role === 'FLOW_EXPORTER'" class="passive-exporter-banner">
            <div class="banner-content">
              <i class="pi pi-info-circle banner-icon"></i>
              <div class="banner-body">
                <span class="banner-title">Dedicated Flow Exporter (Passive Mode)</span>
                <p>This router is enrolled as a dedicated NetFlow/IPFIX exporter. Active ICMP pinging is not monitored.</p>
              </div>
            </div>
            <button
              class="btn-primary btn-small"
              @click="switchPerspective('flow')"
            >
              Switch to Bandwidth / Flows →
            </button>
          </div>

        <!-- Date Query & Filter Toolbar -->
        <div class="toolbar-card">
          <div class="toolbar-left">
            <span class="toolbar-title">Analysis Period:</span>
            <div class="range-buttons">
              <Button 
                label="Last 24h" 
                :outlined="filterRange !== '24h'" 
                severity="success" 
                size="small"
                @click="setRange('24h')" 
              />
              <Button 
                label="Last 7 Days" 
                :outlined="filterRange !== '7d'" 
                severity="success" 
                size="small"
                @click="setRange('7d')" 
              />
              <Button 
                label="Last 30 Days" 
                :outlined="filterRange !== '30d'" 
                severity="success" 
                size="small"
                @click="setRange('30d')" 
              />
              <Button 
                label="Custom Range" 
                :outlined="filterRange !== 'custom'" 
                severity="success" 
                size="small"
                @click="setRange('custom')" 
              />
            </div>
          </div>

          <!-- Custom range calendar controls -->
          <div v-if="filterRange === 'custom'" class="toolbar-right">
            <div class="date-input-group">
              <label for="start_date">Start</label>
              <input type="datetime-local" id="start_date" v-model="customStartDate" class="date-picker-input" />
            </div>
            <div class="date-input-group">
              <label for="end_date">End</label>
              <input type="datetime-local" id="end_date" v-model="customEndDate" class="date-picker-input" />
            </div>
            <Button 
              label="Query" 
              icon="pi pi-search" 
              severity="success" 
              size="small"
              :loading="loading" 
              @click="loadData" 
            />
          </div>
        </div>

        <!-- Metrics Grid -->
        <div v-if="uptimeReport" class="metrics-grid">
          
          <div class="metric-card uptime">
            <div class="metric-header">
              <span>Availability Rate</span>
              <i class="pi pi-percentage"></i>
            </div>
            <div class="metric-body">
              <span class="value" :class="getUptimeClass(uptimeReport.uptime_percentage)">
                {{ uptimeReport.uptime_percentage }}%
              </span>
              <span class="subtext">SLA Target: 99.90%</span>
            </div>
          </div>

          <div class="metric-card incidents">
            <div class="metric-header">
              <span>Outages / Incidents</span>
              <i class="pi pi-exclamation-circle"></i>
            </div>
            <div class="metric-body">
              <span class="value" :class="uptimeReport.incident_count > 0 ? 'alert' : 'clean'">
                {{ uptimeReport.incident_count }}
              </span>
              <span class="subtext">Unstable transitions recorded</span>
            </div>
          </div>

          <div class="metric-card active-time">
            <div class="metric-header">
              <span>Operational Uptime</span>
              <i class="pi pi-clock"></i>
            </div>
            <div class="metric-body">
              <span class="value">{{ formatDuration(uptimeReport.uptime_seconds) }}</span>
              <span class="subtext">Aggregate UP duration</span>
            </div>
          </div>

          <div class="metric-card downtime">
            <div class="metric-header">
              <span>Down / Outage Duration</span>
              <i class="pi pi-ban"></i>
            </div>
            <div class="metric-body">
              <span class="value" :class="{ 'has-down': uptimeReport.downtime_seconds > 0 }">
                {{ formatDuration(uptimeReport.downtime_seconds) }}
              </span>
              <span class="subtext">Aggregate OFFLINE duration</span>
            </div>
          </div>

        </div>

        <!-- Comparative Root Cause Analysis (RCA) Component -->
        <div class="visualizer-container">
          <EndpointRcaDetail :endpointId="endpointId" :endpoint="endpoint" />
        </div>

        <!-- State Timeline Component -->
        <div class="visualizer-container">
          <div class="panel-header">
            <h3><i class="pi pi-align-left header-icon"></i>State Transition Timeline</h3>
            <span class="panel-desc">Visual timeline showing transitions between UP, UNSTABLE, and DOWN states</span>
          </div>
          <StateTimeline 
            :events="chartEvents" 
            :gaps="[]" 
            :periodStart="periodStartStr" 
            :periodEnd="periodEndStr" 
          />
        </div>

        <!-- RTT Latency Trend Component -->
        <div class="visualizer-container">
          <div class="panel-header">
            <h3><i class="pi pi-chart-line header-icon"></i>ICMP Latency & Jitter Trend (RTT)</h3>
            <span class="panel-desc">Line graph representing round-trip-time (ms) averages per cycle</span>
          </div>
          <RTTTrendPanel :events="chartEvents" />
        </div>

        <!-- Detailed Event Logs Table -->
        <div class="visualizer-container">
          <div class="panel-header table-header">
            <div>
              <h3><i class="pi pi-list header-icon"></i>Detailed Transition Audit Logs</h3>
              <span class="panel-desc">Historical record of all telemetry events in the queried period</span>
            </div>
            <span class="total-badge">{{ totalEvents }} cycles</span>
          </div>

          <div class="table-responsive">
            <table class="audit-table">
              <thead>
                <tr>
                  <th>Start Time (Local)</th>
                  <th>End Time (Local)</th>
                  <th>Duration</th>
                  <th>Operational State</th>
                  <th>Detailed State</th>
                  <th>Health Score</th>
                  <th>Avg RTT</th>
                  <th>Cycles</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="events.length === 0">
                  <td colspan="8" class="table-empty">
                    No telemetry records logged for this target during the selected period.
                  </td>
                </tr>
                <tr v-for="ev in sortedEvents" :key="ev.id">
                  <td class="tnum">{{ formatDate(ev.start_time) }}</td>
                  <td class="tnum">{{ ev.end_time ? formatDate(ev.end_time) : 'Ongoing (Active)' }}</td>
                  <td class="tnum font-mono">{{ ev.duration_seconds ? formatDuration(ev.duration_seconds) : 'N/A' }}</td>
                  <td>
                    <StatusBadge :status="ev.operational_state" />
                  </td>
                  <td>
                    <span class="table-detail-badge" :class="ev.detailed_state.toLowerCase()">
                      {{ ev.detailed_state }}
                    </span>
                  </td>
                  <td class="tnum font-mono font-bold" :class="getHealthClass(ev.health_score)">
                    {{ ev.health_score }}%
                  </td>
                  <td class="tnum font-mono">
                    {{ ev.avg_rtt_ms != null ? `${ev.avg_rtt_ms} ms` : '-' }}
                  </td>
                  <td class="tnum font-mono">{{ ev.monitoring_cycle_count }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Compact flat monochrome footer utility -->
          <div class="table-pagination-footer">
            <div class="pagination-info">
              Showing {{ pageStart }} - {{ pageEnd }} of {{ totalEvents }} logs
            </div>
            
            <div class="pagination-controls-wrapper">
              <!-- Sharp navigation arrows -->
              <div class="pagination-nav-arrows">
                <button 
                  class="nav-arrow-btn" 
                  :disabled="tablePage <= 1 || loadingTable" 
                  @click="goToPage(tablePage - 1)"
                >
                  &lt;
                </button>
                <span class="current-page-display">Page {{ tablePage }} of {{ totalPages }}</span>
                <button 
                  class="nav-arrow-btn" 
                  :disabled="tablePage >= totalPages || loadingTable" 
                  @click="goToPage(tablePage + 1)"
                >
                  &gt;
                </button>
              </div>

              <!-- Compact row-density selector drop-down -->
              <div class="density-selector-container">
                <select v-model="tableSize" @change="onDensityChange" class="density-dropdown" :disabled="loadingTable">
                  <option :value="50">Show 50</option>
                  <option :value="100">Show 100</option>
                  <option :value="250">Show 250</option>
                </select>
              </div>
            </div>
          </div>
        </div> <!-- End table card -->
        </div> <!-- End Tab 1: ICMP -->

        <!-- Tab 2: Bandwidth & Flow Telemetry Panel -->
        <div v-if="activePerspective === 'flow'" id="panel-flow" role="tabpanel" aria-labelledby="tab-flow" class="flow-panel-container">
          <!-- Flow Telemetry Header & Mode Switcher -->
          <div class="toolbar-card">
            <div class="toolbar-left">
              <span class="toolbar-title">Telemetry Perspective:</span>
              <div class="range-buttons">
                <Button
                  label="Device as Exporter"
                  :outlined="flowMode !== 'exporter'"
                  severity="success"
                  size="small"
                  @click="flowMode = 'exporter'; loadFlowTelemetry()"
                />
                <Button
                  label="Device as Participant"
                  :outlined="flowMode !== 'participant'"
                  severity="success"
                  size="small"
                  @click="flowMode = 'participant'; loadFlowTelemetry()"
                />
              </div>
            </div>

            <div class="toolbar-right">
              <div class="range-buttons">
                <Button
                  v-for="w in ['1h', '6h', '24h', '7d']"
                  :key="w"
                  :label="w"
                  :outlined="flowWindow !== w"
                  severity="secondary"
                  size="small"
                  @click="flowWindow = w; loadFlowTelemetry()"
                />
              </div>
            </div>
          </div>

          <!-- Flow Meta Grid -->
          <div class="flow-meta-card table-card p-4">
            <div class="meta-row">
              <div class="meta-col">
                <span class="meta-label">Configured Exporter IP Aliases</span>
                <div class="alias-chips">
                  <span
                    v-for="ip in (endpoint.flow_exporter_ips || [])"
                    :key="ip"
                    class="chip-ip tnum"
                  >
                    {{ ip }}
                  </span>
                  <span v-if="!endpoint.flow_exporter_ips || endpoint.flow_exporter_ips.length === 0" class="text-muted text-sm">
                    No secondary exporter IPs linked.
                  </span>
                </div>
              </div>
              <div class="meta-col full-width" v-if="flowMode === 'exporter'">
                <div class="iface-header-row">
                  <span class="meta-label">Router Interface Telemetry & Utilization (ifIndex)</span>
                  <button
                    v-if="isAdmin"
                    type="button"
                    class="btn-text btn-small iface-cfg-btn"
                    @click="openInterfaceModal"
                    title="Configure interface aliases and link speeds"
                  >
                    ⚙ Configure Interfaces
                  </button>
                </div>
                <div v-if="interfaceMeters.length > 0" class="interface-meter-grid">
                  <div v-for="iface in interfaceMeters" :key="iface.idx" class="iface-card">
                    <div class="iface-card-header">
                      <div class="iface-title">
                        <span class="iface-tag tnum">if{{ iface.idx }}</span>
                        <span class="iface-name" :title="iface.name">{{ iface.name }}</span>
                        <span v-if="iface.speed_mbps" class="iface-speed tnum text-muted text-xs">({{ formatSpeedLabel(iface.speed_mbps) }})</span>
                      </div>
                      <span class="iface-status-pill" :class="iface.active ? 'status-active' : 'status-idle'">
                        {{ iface.active ? 'ACTIVE' : 'IDLE' }}
                      </span>
                    </div>
                    <div class="iface-stats tnum">
                      <div class="stat-col">
                        <span class="stat-label">In:</span>
                        <span class="stat-val text-emerald">{{ formatBps(iface.ingressBps) }}</span>
                      </div>
                      <div class="stat-col">
                        <span class="stat-label">Out:</span>
                        <span class="stat-val text-cyan">{{ formatBps(iface.egressBps) }}</span>
                      </div>
                    </div>
                    <div class="meter-bar-track">
                      <div class="meter-bar-fill" :style="{ width: Math.min(100, iface.utilization) + '%' }" :class="getUtilColorClass(iface.utilization)"></div>
                    </div>
                    <div class="meter-meta tnum">
                      <span>Est. Utilization</span>
                      <span>{{ (iface.utilization || 0).toFixed(1) }}%</span>
                    </div>
                  </div>
                </div>
                <div v-else class="text-muted text-sm py-2">
                  No interface aliases configured.
                </div>
              </div>
            </div>
          </div>

          <!-- Flow Telemetry Sub-View Switcher -->
          <div class="flow-subview-switcher">
            <button
              class="subview-btn"
              :class="{ active: flowSubView === 'bandwidth' }"
              @click="flowSubView = 'bandwidth'"
            >
              <i class="pi pi-chart-line"></i>
              <span>Flow Bandwidth</span>
            </button>
            <button
              class="subview-btn"
              :class="{ active: flowSubView === 'correlated' }"
              @click="flowSubView = 'correlated'"
            >
              <i class="pi pi-sliders-h"></i>
              <span>Correlated Incident View (RTT vs Bandwidth)</span>
            </button>
          </div>

          <!-- Flow Bandwidth Chart -->
          <div v-if="flowSubView === 'bandwidth'" class="table-card p-4">
            <div class="section-header section-header-split">
              <div>
                <span class="font-bold text-base">Node Flow Bandwidth ({{ flowMode === 'exporter' ? 'Exported Transit' : 'Participant Traffic' }})</span>
                <span class="badge-window tnum ml-2">{{ flowWindow }} Window</span>
              </div>
              <div class="header-controls">
                <div class="metric-mode-pills" role="group" aria-label="Chart Metric Mode">
                  <button
                    class="btn-pill btn-pill-sm"
                    :class="{ active: nodeChartMetricMode === 'bps' }"
                    @click="nodeChartMetricMode = 'bps'"
                    title="Throughput in bits per second"
                  >
                    Throughput (bps)
                  </button>
                  <button
                    class="btn-pill btn-pill-sm"
                    :class="{ active: nodeChartMetricMode === 'pps' }"
                    @click="nodeChartMetricMode = 'pps'"
                    title="Packet rate in packets per second"
                  >
                    Packet Rate (pps)
                  </button>
                </div>
              </div>
            </div>
            <div class="flow-chart-box">
              <LineChart
                v-if="flowSeriesPoints.length > 0"
                :data="flowChartData"
                :options="flowChartOptions"
              />
              <div v-else-if="!loadingFlow" class="no-data-msg">
                No network flow telemetry recorded for this node in the selected {{ flowWindow }} window.
              </div>
              <div v-else class="no-data-msg">Loading flow telemetry...</div>
            </div>
          </div>

          <!-- Correlated Incident View (Dual-Axis Chart) -->
          <div v-else-if="flowSubView === 'correlated'" class="table-card p-4">
            <div class="section-header">
              <div>
                <span class="font-bold text-base">Correlated Incident Analysis (ICMP RTT Latency vs Flow Bandwidth)</span>
                <p class="text-xs text-muted mt-1">Cross-correlates ICMP ping spikes with network saturation to pinpoint whether latency spikes are induced by heavy link utilization.</p>
              </div>
              <div class="header-controls">
                <div v-if="interfaceTelemetry && interfaceTelemetry.length > 0" class="interface-filter-wrapper">
                  <label for="correlated-if-select" class="filter-lbl">Interface:</label>
                  <select 
                    id="correlated-if-select" 
                    v-model="selectedCorrelatedInterfaceIdx" 
                    class="filter-select"
                    @change="loadFlowTelemetry"
                  >
                    <option :value="null">All Interfaces (Aggregate)</option>
                    <option 
                      v-for="iface in interfaceTelemetry" 
                      :key="iface.interface_idx" 
                      :value="iface.interface_idx"
                    >
                      #{{ iface.interface_idx }} {{ iface.interface_name }}
                    </option>
                  </select>
                </div>
                <span class="badge-window tnum">{{ flowWindow }} Window</span>
              </div>
            </div>
            <div class="flow-chart-box">
              <LineChart
                v-if="flowSeriesPoints.length > 0"
                :data="correlatedChartData"
                :options="correlatedChartOptions"
              />
              <div v-else-if="!loadingFlow" class="no-data-msg">
                No telemetry recorded for cross-correlation in the selected {{ flowWindow }} window.
              </div>
              <div v-else class="no-data-msg">Correlating RTT and flow points...</div>
            </div>
          </div>

          <!-- Top Node Conversations / Flows Table -->
          <div class="table-card">
            <div class="section-header section-header-split p-4">
              <div>
                <span class="font-bold text-base">Forensic Node Conversations</span>
                <span class="text-muted text-xs block mt-1">Granular 1-minute rollup pairs with packet physics &amp; threat telemetry</span>
              </div>
              <div class="header-controls">
                <input
                  v-model="nodeConversationFilter"
                  class="filter-select"
                  style="min-width: 220px;"
                  placeholder="Filter IP, port, flag, profile..."
                  aria-label="Filter node conversations"
                />
              </div>
            </div>
            <div class="table-responsive">
              <table class="dense-table" aria-label="Forensic Node Conversations Table">
                <thead>
                  <tr>
                    <th>Source Host / IP</th>
                    <th>Destination Host / IP</th>
                    <th>Protocol</th>
                    <th>Service Port</th>
                    <th>Packet Physics &amp; Profile</th>
                    <th>Flags &amp; QoS</th>
                    <th class="text-right">Volume</th>
                    <th class="text-right">Burst Count</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(fc, idx) in filteredNodeConversations" :key="idx">
                    <td>
                      <span class="tnum font-mono">{{ fc.src_ip }}</span>
                      <span v-if="fc.src_hostname" class="text-muted text-xs block">({{ fc.src_hostname }})</span>
                    </td>
                    <td>
                      <span class="tnum font-mono">{{ fc.dst_ip }}</span>
                      <span v-if="fc.dst_hostname" class="text-muted text-xs block">({{ fc.dst_hostname }})</span>
                    </td>
                    <td><span class="status-pill status-unknown">{{ fc.protocol }}</span></td>
                    <td>
                      <span class="service-port-badge" :class="getPortBadgeClass(fc.dst_port)">
                        {{ formatPortLabel(fc.dst_port, fc.protocol) }}
                      </span>
                    </td>
                    <td>
                      <div class="physics-profile-cell">
                        <span class="badge-profile" :class="getTrafficProfileBadgeClass(fc.traffic_profile)">
                          {{ fc.traffic_profile || 'Interactive' }}
                        </span>
                        <div class="packet-physics-meta text-xs text-muted tnum font-mono">
                          <span>{{ fc.avg_packet_size || 0 }} B/pkt</span> · 
                          <span>{{ fc.packets_per_flow || 0 }} pkts/fl</span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div class="metadata-badge-group">
                        <span
                          v-if="fc.protocol === 'TCP' && fc.tcp_flags_labels && fc.tcp_flags_labels.length > 0"
                          class="badge-tcp-flags font-mono"
                          :title="`TCP Flags: ${fc.tcp_flags_labels.join(', ')} (0x${(fc.tcp_flags || 0).toString(16).padStart(2, '0')})`"
                        >
                          {{ fc.tcp_flags_labels.join(' ') }}
                        </span>
                        <span
                          v-if="fc.anomaly_hint === 'SYN_ONLY'"
                          class="badge-anomaly badge-syn-only"
                          title="Anomaly: SYN-only packets observed without ACK (potential port scan)"
                        >
                          SYN ONLY
                        </span>
                        <span
                          v-else-if="fc.anomaly_hint === 'TCP_RST'"
                          class="badge-anomaly badge-tcp-rst"
                          title="Anomaly: Connection reset (RST) observed"
                        >
                          RST
                        </span>
                        <span
                          v-if="fc.dscp > 0"
                          class="badge-dscp"
                          :class="{ 'badge-dscp-ef': fc.dscp === 46, 'badge-dscp-af': fc.dscp_label && fc.dscp_label.includes('AF') }"
                          :title="`ToS: 0x${(fc.tos || 0).toString(16).padStart(2, '0')} · DSCP: ${fc.dscp} (${fc.dscp_label})`"
                        >
                          {{ fc.dscp_label }}
                        </span>
                      </div>
                    </td>
                    <td class="text-right tnum font-bold">{{ formatBytes(fc.total_bytes) }}</td>
                    <td class="text-right tnum text-muted">{{ formatNumber(fc.flow_count) }}</td>
                  </tr>
                  <tr v-if="filteredNodeConversations.length === 0">
                    <td colspan="8" class="text-center text-muted p-4">
                      {{ nodeConversations.length === 0 ? `No active conversations found for this node in the selected ${flowWindow} window.` : 'No conversations matching current filter.' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div> <!-- End Tab 2: Flow -->

      </div>
    </div>

    <!-- Admin Endpoint Settings Modal -->
    <div class="modal-overlay" v-if="showSettingsModal" @click.self="showSettingsModal = false">
      <div class="modal-card wide">
        <div class="modal-header">
          <h3>⚙️ Endpoint Governance & Settings</h3>
          <button class="btn-close" @click="showSettingsModal = false">✕</button>
        </div>

        <form @submit.prevent="saveEndpointSettings" class="modal-form">
          <div class="form-row">
            <div class="form-group">
              <label>Hostname *</label>
              <input v-model="editForm.hostname" type="text" required />
            </div>
            <div class="form-group">
              <label>IP Address</label>
              <input v-model="editForm.ip_address" type="text" disabled />
            </div>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>Device Type</label>
              <select v-model="editForm.device_type" class="form-select">
                <option v-for="d in ['Server', 'Router', 'Switch', 'Access Point', 'Firewall', 'Printer', 'Other']" :key="d" :value="d">{{ d }}</option>
              </select>
            </div>
            <div class="form-group">
              <label>Physical Location</label>
              <input v-model="editForm.location" type="text" placeholder="Data Center 1 - Rack 4" />
            </div>
          </div>

          <div class="form-group">
            <label>Description</label>
            <textarea v-model="editForm.description" rows="2" placeholder="Primary gateway router" class="form-textarea"></textarea>
          </div>

          <!-- Governance Controls Section -->
          <div class="governance-box">
            <h4 class="box-title">Governance & Diagnostics Controls</h4>

            <div class="setting-switch-item">
              <div class="switch-info">
                <span class="switch-title">Enable Root Cause Analysis (RCA)</span>
                <span class="switch-desc">Triggers differential route traceroute snapshot comparison on failure</span>
              </div>
              <input type="checkbox" v-model="editForm.enable_rca" class="check-box" />
            </div>

            <div class="setting-switch-item">
              <div class="switch-info">
                <span class="switch-title">Midnight Scheduled Discovery</span>
                <span class="switch-desc">Includes endpoint in sequential 00:00 baseline update passes</span>
              </div>
              <input type="checkbox" v-model="editForm.enable_scheduled_discovery" class="check-box" />
            </div>

            <div class="setting-switch-item">
              <div class="switch-info">
                <span class="switch-title">Layer 2 Segment Target</span>
                <span class="switch-desc">Direct local VLAN broadcast segment without L3 router hops</span>
              </div>
              <input type="checkbox" v-model="editForm.is_l2_segment" class="check-box" />
            </div>

            <div class="setting-switch-item">
              <div class="switch-info">
                <span class="switch-title">Include in Topology Map</span>
                <span class="switch-desc">Display endpoint and its transit hops on the interactive network DAG graph</span>
              </div>
              <input type="checkbox" v-model="editForm.allow_topology_discovery" class="check-box" />
            </div>

            <div class="setting-switch-item">
              <div class="switch-info">
                <span class="switch-title">Active ICMP Monitoring</span>
                <span class="switch-desc">Enable background uptime & latency polling cycles</span>
              </div>
              <input type="checkbox" v-model="editForm.monitoring_enabled" class="check-box" />
            </div>
          </div>

          <div class="modal-footer-bar">
            <button type="button" class="btn-discovery" @click="triggerDiscovery" :disabled="discovering">
              <span v-if="discovering">Discovering...</span>
              <span v-else>📡 Run Route Discovery</span>
            </button>
            <div class="footer-buttons">
              <button type="button" class="btn-secondary" @click="showSettingsModal = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="savingSettings">
                {{ savingSettings ? 'Saving...' : 'Save Settings' }}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>

    <!-- Interface Configuration Modal -->
    <div class="modal-overlay" v-if="showInterfaceModal" @click.self="showInterfaceModal = false">
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

          <div class="modal-footer-bar">
            <div class="footer-buttons" style="margin-left: auto;">
              <button type="button" class="btn-secondary" @click="showInterfaceModal = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="savingInterfaces">
                {{ savingInterfaces ? 'Saving...' : 'Save Interfaces' }}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, shallowRef, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { getEndpoint, getUptimeReport, getEndpointEvents, updateEndpoint, refreshEndpointBaseline, logout, getTrafficSeries, getTopTalkers, getInterfaceTelemetry, updateEndpointInterfaces } from '../services/api.js'
import { user, isAdmin, loadUserFromStorage, clearUserState } from '../services/auth.js'
import StateTimeline from '../components/StateTimeline.vue'
import RTTTrendPanel from '../components/RTTTrendPanel.vue'
import EndpointRcaDetail from '../components/EndpointRcaDetail.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { Line as LineChart } from 'vue-chartjs'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js'

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler
)

import Card from 'primevue/card'
import Button from 'primevue/button'
import { useSSE } from '../composables/useSSE.js'

const route = useRoute()
const router = useRouter()
const endpointId = route.params.id

const { subscribe } = useSSE()
let unsubscribeSSE = null

const isDarkMode = ref(true)

const activePerspective = ref('icmp')
const flowMode = ref('exporter')
const flowWindow = ref('1h')
const loadingFlow = ref(false)
const flowSeriesPoints = shallowRef([])
const nodeConversations = shallowRef([])
const nodeConversationFilter = ref('')
const filteredNodeConversations = computed(() => {
  const q = nodeConversationFilter.value.trim().toLowerCase()
  if (!q) return nodeConversations.value
  return nodeConversations.value.filter((c) => {
    return (
      (c.src_ip && c.src_ip.toLowerCase().includes(q)) ||
      (c.dst_ip && c.dst_ip.toLowerCase().includes(q)) ||
      (c.src_hostname && c.src_hostname.toLowerCase().includes(q)) ||
      (c.dst_hostname && c.dst_hostname.toLowerCase().includes(q)) ||
      String(c.dst_port).includes(q) ||
      (c.protocol && c.protocol.toLowerCase().includes(q)) ||
      (c.traffic_profile && c.traffic_profile.toLowerCase().includes(q)) ||
      (c.dscp_label && c.dscp_label.toLowerCase().includes(q)) ||
      (c.anomaly_hint && c.anomaly_hint.toLowerCase().includes(q)) ||
      (Array.isArray(c.tcp_flags_labels) && c.tcp_flags_labels.some((f) => f.toLowerCase().includes(q)))
    )
  })
})
const nodeChartMetricMode = ref('bps')

const interfaceTelemetry = shallowRef([])
const selectedCorrelatedInterfaceIdx = ref(null)
const showInterfaceModal = ref(false)
const savingInterfaces = ref(false)
const editingInterfaces = ref([])

function switchPerspective(persp) {
  activePerspective.value = persp
  if (persp === 'flow') {
    loadFlowTelemetry()
  }
}

async function loadFlowTelemetry() {
  if (!endpoint.value) return
  loadingFlow.value = true
  try {
    const epId = endpoint.value.id
    const isExporter = flowMode.value === 'exporter' || endpoint.value.device_role === 'FLOW_EXPORTER'
    const res = await getTrafficSeries(
      flowWindow.value,
      isExporter ? epId : null,
      !isExporter ? epId : null,
      selectedCorrelatedInterfaceIdx.value
    )
    if (res.data?.data?.points) {
      flowSeriesPoints.value = res.data.data.points
    }
    const talkersRes = await getTopTalkers(
      flowWindow.value,
      50,
      !isExporter ? epId : null,
      isExporter ? epId : null
    )
    if (talkersRes.data?.data?.top_conversations) {
      const epIp = String(endpoint.value.ip_address).split('/')[0].trim()
      if (isExporter) {
        nodeConversations.value = talkersRes.data.data.top_conversations
      } else {
        nodeConversations.value = talkersRes.data.data.top_conversations.filter(
          (c) => c.src_ip === epIp || c.dst_ip === epIp || !c.src_ip
        )
      }
    }
    if (isExporter) {
      try {
        const ifRes = await getInterfaceTelemetry(epId, flowWindow.value)
        if (ifRes.data?.data?.interfaces) {
          interfaceTelemetry.value = ifRes.data.data.interfaces
        }
      } catch (ifErr) {
        console.warn('Failed to load interface telemetry:', ifErr)
      }
    }
  } catch (err) {
    console.error('Failed to load flow telemetry for node:', err)
  } finally {
    loadingFlow.value = false
  }
}

const flowChartData = computed(() => {
  const labels = flowSeriesPoints.value.map((p) => {
    const d = new Date(p.timestamp)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  })

  if (nodeChartMetricMode.value === 'pps') {
    return {
      labels,
      datasets: [
        {
          label: 'Ingress (pps)',
          data: flowSeriesPoints.value.map((p) => p.ingress_pps || 0),
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.25)',
          borderWidth: 2,
          fill: true,
          tension: 0.3,
        },
        {
          label: 'Egress (pps)',
          data: flowSeriesPoints.value.map((p) => p.egress_pps || 0),
          borderColor: '#0ea5e9',
          backgroundColor: 'rgba(14, 165, 233, 0.25)',
          borderWidth: 2,
          fill: true,
          tension: 0.3,
        },
      ],
    }
  }

  return {
    labels,
    datasets: [
      {
        label: 'Ingress (bps)',
        data: flowSeriesPoints.value.map((p) => p.ingress_bps),
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.25)',
        borderWidth: 2,
        fill: true,
        tension: 0.3,
      },
      {
        label: 'Egress (bps)',
        data: flowSeriesPoints.value.map((p) => p.egress_bps),
        borderColor: '#0ea5e9',
        backgroundColor: 'rgba(14, 165, 233, 0.25)',
        borderWidth: 2,
        fill: true,
        tension: 0.3,
      },
    ],
  }
})

const flowChartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  normalized: true,
  scales: {
    x: { grid: { display: false }, ticks: { color: '#888', font: { size: 11 } } },
    y: {
      grid: { color: 'rgba(128, 128, 128, 0.1)' },
      ticks: {
        color: '#888',
        font: { family: 'JetBrains Mono', size: 11 },
        callback: (val) => {
          if (!val || val <= 0) return nodeChartMetricMode.value === 'pps' ? '0 pps' : '0 bps'
          if (nodeChartMetricMode.value === 'pps') return formatPps(val)
          return formatBps(val)
        },
      },
    },
  },
  plugins: {
    legend: { position: 'top', labels: { color: '#888', font: { size: 11 } } },
    tooltip: {
      callbacks: {
        label: (ctx) => {
          if (nodeChartMetricMode.value === 'pps') {
            return `${ctx.dataset.label}: ${formatPps(ctx.raw)}`
          }
          return `${ctx.dataset.label}: ${formatBps(ctx.raw)}`
        },
      },
    },
  },
}))

function formatPps(pps) {
  if (!pps || pps <= 0) return '0 pps'
  if (pps >= 1e6) return `${(pps / 1e6).toFixed(2)} Mpps`
  if (pps >= 1e3) return `${(pps / 1e3).toFixed(1)} kpps`
  return `${Math.round(pps)} pps`
}

function formatNumber(num) {
  if (!num) return '0'
  return new Intl.NumberFormat().format(num)
}

function formatPortLabel(port, proto) {
  const p = Number(port)
  switch (p) {
    case 443: return '443 / HTTPS'
    case 80: return '80 / HTTP'
    case 53: return '53 / DNS'
    case 22: return '22 / SSH'
    case 123: return '123 / NTP'
    case 161: return '161 / SNMP'
    case 2055: return '2055 / NetFlow'
    case 4739: return '4739 / IPFIX'
    default: return `${port}`
  }
}

function getPortBadgeClass(port) {
  const p = Number(port)
  if (p === 443 || p === 80) return 'port-web'
  if (p === 53) return 'port-dns'
  if (p === 22) return 'port-ssh'
  if (p === 2055 || p === 4739) return 'port-telemetry'
  return 'port-generic'
}

function getTrafficProfileBadgeClass(profile) {
  switch (profile) {
    case 'Bulk Transfer': return 'badge-profile-bulk'
    case 'SYN Scan / Probe': return 'badge-profile-syn'
    case 'Reset / Refused': return 'badge-profile-rst'
    case 'Micro-Flow / Control': return 'badge-profile-micro'
    case 'Real-Time / VoIP': return 'badge-profile-voip'
    default: return 'badge-profile-interactive'
  }
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B'
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(2)} MB`
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(1)} KB`
  return `${bytes} B`
}

const flowSubView = ref('bandwidth')

function formatBps(bps) {
  if (!bps || bps <= 0) return '0 bps'
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gbps`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbps`
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(0)} kbps`
  return `${Math.round(bps)} bps`
}

function getUtilColorClass(util) {
  if (util >= 85) return 'util-danger'
  if (util >= 60) return 'util-warning'
  return 'util-healthy'
}

function formatSpeedLabel(mbps) {
  if (!mbps) return ''
  if (mbps >= 1000) return `${mbps / 1000} Gbps`
  return `${mbps} Mbps`
}

const interfaceMeters = computed(() => {
  if (interfaceTelemetry.value && interfaceTelemetry.value.length > 0) {
    return interfaceTelemetry.value.map((item) => ({
      idx: item.interface_idx,
      name: item.name,
      speed_mbps: item.speed_mbps,
      ingressBps: item.in_bps,
      egressBps: item.out_bps,
      utilization: item.utilization_percentage,
      active: item.is_active,
    }))
  }

  if (!endpoint.value?.flow_interface_aliases) return []
  const aliases = endpoint.value.flow_interface_aliases
  return Object.entries(aliases).map(([idx, val]) => {
    let name = `if${idx}`
    let speed = 1000
    if (typeof val === 'string') {
      name = val
    } else if (val && typeof val === 'object') {
      name = val.name || `if${idx}`
      speed = val.speed_mbps || 1000
    }
    return {
      idx,
      name,
      speed_mbps: speed,
      ingressBps: 0,
      egressBps: 0,
      utilization: 0,
      active: false,
    }
  })
})

function openInterfaceModal() {
  const current = endpoint.value?.flow_interface_aliases || {}
  const rows = Object.entries(current).map(([idx, val]) => {
    let name = `if${idx}`
    let speed = 1000
    if (typeof val === 'string') {
      name = val
    } else if (val && typeof val === 'object') {
      name = val.name || `if${idx}`
      speed = val.speed_mbps || 1000
    }
    return {
      index: Number(idx) || idx,
      name,
      speed_mbps: speed,
      preset: [10, 100, 1000, 10000, 40000, 100000].includes(speed) ? speed : 'custom'
    }
  })
  if (rows.length === 0) {
    rows.push({ index: 1, name: 'GigabitEthernet0/1', speed_mbps: 1000, preset: 1000 })
  }
  editingInterfaces.value = rows
  showInterfaceModal.value = true
}

function addInterfaceRow() {
  const nextIdx = (editingInterfaces.value.length + 1)
  editingInterfaces.value.push({ index: nextIdx, name: `GigabitEthernet0/${nextIdx}`, speed_mbps: 1000, preset: 1000 })
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
  if (!endpoint.value) return
  savingInterfaces.value = true
  try {
    const payload = {}
    for (const row of editingInterfaces.value) {
      const idxStr = String(row.index).trim()
      if (idxStr) {
        payload[idxStr] = {
          name: row.name.trim() || `if${idxStr}`,
          speed_mbps: Number(row.speed_mbps) || 1000
        }
      }
    }
    await updateEndpointInterfaces(endpoint.value.id, payload)
    endpoint.value.flow_interface_aliases = payload
    showInterfaceModal.value = false
    await loadFlowTelemetry()
  } catch (err) {
    console.error('Failed to update interfaces:', err)
    alert(err.response?.data?.detail || 'Failed to update interfaces.')
  } finally {
    savingInterfaces.value = false
  }
}

const events = shallowRef([])

const indexedEvents = computed(() => {
  const nowMs = Date.now()
  return events.value.map((ev) => ({
    raw: ev,
    startMs: Date.parse(ev.start_time) || 0,
    endMs: ev.end_time ? (Date.parse(ev.end_time) || nowMs) : nowMs,
  }))
})

const chartEvents = computed(() => {
  return [...indexedEvents.value]
    .sort((a, b) => a.startMs - b.startMs)
    .map((item) => item.raw)
})

const sortedEvents = computed(() => {
  return [...indexedEvents.value]
    .sort((a, b) => b.startMs - a.startMs)
    .map((item) => item.raw)
})

const correlatedChartData = computed(() => {
  const points = flowSeriesPoints.value
  const labels = new Array(points.length)
  const inData = new Array(points.length)
  const outData = new Array(points.length)
  const rttData = new Array(points.length)

  // Pre-sorted RTT intervals: O(M log M) once per computation
  const rttIntervals = indexedEvents.value
    .filter((item) => item.raw.avg_rtt_ms != null)
    .sort((a, b) => a.startMs - b.startMs)

  const fallbackRtt = endpoint.value?.avg_rtt_ms != null ? Number(endpoint.value.avg_rtt_ms) : 0
  const mLen = rttIntervals.length
  let cursor = 0

  for (let i = 0; i < points.length; i++) {
    const p = points[i]
    const pTime = Date.parse(p.timestamp) || 0
    const d = new Date(pTime)
    labels[i] = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    inData[i] = p.ingress_bps
    outData[i] = p.egress_bps

    let closestRtt = null
    if (mLen > 0) {
      while (cursor + 1 < mLen && rttIntervals[cursor].endMs < pTime) {
        cursor++
      }
      const cur = rttIntervals[cursor]
      if (pTime >= cur.startMs && pTime <= cur.endMs) {
        closestRtt = cur.raw.avg_rtt_ms
      } else {
        const distCur = pTime < cur.startMs ? cur.startMs - pTime : pTime - cur.endMs
        if (cursor > 0) {
          const prev = rttIntervals[cursor - 1]
          const distPrev = pTime < prev.startMs ? prev.startMs - pTime : pTime - prev.endMs
          closestRtt = distPrev <= distCur ? prev.raw.avg_rtt_ms : cur.raw.avg_rtt_ms
        } else {
          closestRtt = cur.raw.avg_rtt_ms
        }
      }
    }
    const resolved = closestRtt ?? fallbackRtt
    rttData[i] = Number(Number(resolved).toFixed(1))
  }

  const matched = selectedCorrelatedInterfaceIdx.value !== null
    ? interfaceTelemetry.value?.find(i => i.interface_idx === selectedCorrelatedInterfaceIdx.value)
    : null
  const ifName = matched?.interface_name || matched?.name || (selectedCorrelatedInterfaceIdx.value !== null ? `Interface #${selectedCorrelatedInterfaceIdx.value}` : '')

  const inLabel = ifName ? `${ifName} Ingress (bps)` : 'Ingress (bps)'
  const outLabel = ifName ? `${ifName} Egress (bps)` : 'Egress (bps)'

  return {
    labels,
    datasets: [
      {
        label: 'ICMP Latency (ms)',
        data: rttData,
        borderColor: '#f59e0b',
        backgroundColor: 'rgba(245, 158, 11, 0.15)',
        borderWidth: 2,
        yAxisID: 'y_rtt',
        tension: 0.2,
        pointRadius: points.length > 100 ? 0 : 2,
      },
      {
        label: inLabel,
        data: inData,
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.15)',
        borderWidth: 2,
        yAxisID: 'y_bandwidth',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      },
      {
        label: outLabel,
        data: outData,
        borderColor: '#0ea5e9',
        backgroundColor: 'rgba(14, 165, 233, 0.15)',
        borderWidth: 2,
        yAxisID: 'y_bandwidth',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      },
    ],
  }
})

const correlatedChartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  normalized: true,
  scales: {
    x: { grid: { display: false }, ticks: { color: '#888', font: { size: 11 } } },
    y_rtt: {
      type: 'linear',
      position: 'left',
      grid: { color: 'rgba(245, 158, 11, 0.15)' },
      title: {
        display: true,
        text: 'ICMP Latency (ms)',
        color: '#f59e0b',
        font: { family: 'JetBrains Mono', size: 11 },
      },
      ticks: {
        color: '#f59e0b',
        font: { family: 'JetBrains Mono', size: 11 },
        callback: (val) => `${val} ms`,
      },
    },
    y_bandwidth: {
      type: 'linear',
      position: 'right',
      grid: { drawOnChartArea: false },
      title: {
        display: true,
        text: 'Bandwidth (bps)',
        color: '#0ea5e9',
        font: { family: 'JetBrains Mono', size: 11 },
      },
      ticks: {
        color: '#0ea5e9',
        font: { family: 'JetBrains Mono', size: 11 },
        callback: (val) => formatBps(val),
      },
    },
  },
  plugins: {
    legend: { position: 'top', labels: { color: '#888', font: { size: 11 } } },
    tooltip: {
      callbacks: {
        label: (context) => {
          if (context.dataset.yAxisID === 'y_rtt') {
            return `ICMP Latency: ${context.parsed.y} ms`
          }
          return `${context.dataset.label}: ${formatBps(context.parsed.y)}`
        },
      },
    },
  },
}

const toggleTheme = () => {
  isDarkMode.value = !isDarkMode.value
  if (isDarkMode.value) {
    localStorage.setItem('theme', 'dark')
    document.documentElement.classList.add('dark')
  } else {
    localStorage.setItem('theme', 'light')
    document.documentElement.classList.remove('dark')
  }
}

const showSettingsModal = ref(false)
const savingSettings = ref(false)
const discovering = ref(false)
const editForm = ref({
  hostname: '',
  ip_address: '',
  device_type: 'Router',
  location: '',
  description: '',
  monitoring_enabled: true,
  allow_incident_trace: true,
  allow_topology_discovery: true,
  enable_rca: true,
  enable_scheduled_discovery: true,
  is_l2_segment: false,
  manual_parent_id: null
})

function openSettingsModal() {
  if (!endpoint.value) return
  editForm.value = {
    hostname: endpoint.value.hostname || '',
    ip_address: endpoint.value.ip_address || '',
    device_type: endpoint.value.device_type || 'Router',
    location: endpoint.value.location || '',
    description: endpoint.value.description || '',
    monitoring_enabled: endpoint.value.monitoring_enabled ?? true,
    allow_incident_trace: endpoint.value.allow_incident_trace ?? true,
    allow_topology_discovery: endpoint.value.allow_topology_discovery ?? true,
    enable_rca: endpoint.value.enable_rca ?? true,
    enable_scheduled_discovery: endpoint.value.enable_scheduled_discovery ?? true,
    is_l2_segment: endpoint.value.is_l2_segment ?? false,
    manual_parent_id: endpoint.value.manual_parent_id || null
  }
  showSettingsModal.value = true
}

async function saveEndpointSettings() {
  savingSettings.value = true
  try {
    await updateEndpoint(endpointId, editForm.value)
    showSettingsModal.value = false
    await loadData()
    alert('Endpoint settings updated successfully.')
  } catch (err) {
    console.error('Failed to update endpoint settings:', err)
    alert(err.response?.data?.detail || 'Failed to update settings.')
  } finally {
    savingSettings.value = false
  }
}

async function triggerDiscovery() {
  discovering.value = true
  try {
    await refreshEndpointBaseline(endpointId)
    alert('Route discovery executed successfully. Baseline updated.')
    await loadData()
  } catch (err) {
    console.error('Route discovery failed:', err)
    alert(err.response?.data?.detail || 'Route discovery failed.')
  } finally {
    discovering.value = false
  }
}
const endpoint = ref(null)
const uptimeReport = ref(null)
const loading = ref(true)
const error = ref(null)

const tablePage = ref(1)
const tableSize = ref(100)
const totalEvents = ref(0)
const totalPages = ref(1)
const loadingTable = ref(false)

const pageStart = computed(() => {
  if (totalEvents.value === 0) return 0
  return (tablePage.value - 1) * tableSize.value + 1
})

const pageEnd = computed(() => {
  return Math.min(tablePage.value * tableSize.value, totalEvents.value)
})

let pollInterval = null

function getLocalISOString(d) {
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hours = String(d.getHours()).padStart(2, '0')
  const minutes = String(d.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hours}:${minutes}`
}

// Date Range filters
const filterRange = ref('24h')
const customStartDate = ref(getLocalISOString(new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)))
const customEndDate = ref(getLocalISOString(new Date()))

// Detailed date strings for timeline component
const periodStartStr = ref('')
const periodEndStr = ref('')

const setRange = (range) => {
  filterRange.value = range
  if (range !== 'custom') {
    loadData()
  }
}

function getQueryRange() {
  const now = new Date()
  let start = ''
  let end = now.toISOString()
  
  if (filterRange.value === '24h') {
    const past = new Date(now.getTime() - 24 * 60 * 60 * 1000)
    start = past.toISOString()
  } else if (filterRange.value === '7d') {
    const past = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
    start = past.toISOString()
  } else if (filterRange.value === '30d') {
    const past = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000)
    start = past.toISOString()
  } else {
    const s = customStartDate.value ? new Date(customStartDate.value) : new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
    const e = customEndDate.value ? new Date(customEndDate.value) : now
    start = isNaN(s.getTime()) ? new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000).toISOString() : s.toISOString()
    end = isNaN(e.getTime()) ? now.toISOString() : e.toISOString()
  }
  return { start, end }
}

const loadTableEvents = async () => {
  loadingTable.value = true
  const { start, end } = getQueryRange()
  
  try {
    const res = await getEndpointEvents(endpointId, start, end, tablePage.value, tableSize.value)
    events.value = res.data.data
    totalEvents.value = res.data.meta?.total || 0
    totalPages.value = res.data.meta?.total_pages || 1
  } catch (err) {
    console.error('Failed to reload table events:', err)
  } finally {
    loadingTable.value = false
  }
}

const goToPage = (page) => {
  if (page >= 1 && page <= totalPages.value) {
    tablePage.value = page
    loadTableEvents()
  }
}

const onDensityChange = () => {
  tablePage.value = 1
  loadTableEvents()
}

const loadData = async () => {
  loading.value = true
  error.value = null
  tablePage.value = 1
  
  const { start, end } = getQueryRange()
  periodStartStr.value = start
  periodEndStr.value = end
  
  try {
    const [endpointRes, uptimeRes, tableEventsRes] = await Promise.all([
      getEndpoint(endpointId),
      getUptimeReport(endpointId, start, end),
      getEndpointEvents(endpointId, start, end, 1, tableSize.value) // Fetch paginated events starting from page 1
    ])
    
    endpoint.value = endpointRes.data.data
    uptimeReport.value = uptimeRes.data.data
    events.value = tableEventsRes.data.data
    
    totalEvents.value = tableEventsRes.data.meta?.total || 0
    totalPages.value = tableEventsRes.data.meta?.total_pages || 1

    if (endpoint.value.device_role === 'FLOW_EXPORTER') {
      activePerspective.value = 'flow'
    }
    if (activePerspective.value === 'flow') {
      await loadFlowTelemetry()
    }
  } catch (err) {
    console.error('Failed to query endpoint telemetry:', err)
    error.value = err.response?.data?.detail || 'Failed to assemble endpoint timeline. Verify server backend is responsive.'
  } finally {
    loading.value = false
  }
}

const getDeviceIcon = (type) => {
  switch (type) {
    case 'Server': return 'pi-server main-icon-styled server'
    case 'Router': return 'pi-directions main-icon-styled router'
    case 'Switch': return 'pi-sitemap main-icon-styled switch'
    case 'Access Point': return 'pi-wifi main-icon-styled wifi'
    case 'Firewall': return 'pi-lock main-icon-styled firewall'
    default: return 'pi-desktop main-icon-styled'
  }
}

const formatDuration = (seconds) => {
  if (seconds == null || seconds < 0) return '0s'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h ${minutes % 60}m`
}

const formatDate = (isoString) => {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString()
}

const getUptimeClass = (pct) => {
  if (pct >= 99.9) return 'perfect'
  if (pct >= 99.0) return 'good'
  if (pct >= 95.0) return 'warning'
  return 'critical'
}

const getHealthClass = (score) => {
  if (score >= 95) return 'text-success'
  if (score >= 70) return 'text-warning'
  return 'text-danger'
}

const goToDashboard = () => {
  router.push('/')
}

const handleLogout = async () => {
  try {
    await logout()
  } catch (err) {
    console.error('Logout error on backend', err)
  } finally {
    clearUserState()
    router.push('/login')
  }
}

onMounted(() => {
  const currentTheme = localStorage.getItem('theme') || 'dark'
  isDarkMode.value = currentTheme === 'dark'
  if (isDarkMode.value) {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }

  loadUserFromStorage()
  loadData()

  // Establish a 60-second polling interval to fetch fresh telemetry data
  pollInterval = setInterval(() => {
    if (!loading.value) {
      loadData()
      if (activePerspective.value === 'flow') {
        loadFlowTelemetry()
      }
    }
  }, 60000)

  unsubscribeSSE = subscribe((rawEvent) => {
    try {
      const event = typeof rawEvent?.data === 'string' ? JSON.parse(rawEvent.data) : rawEvent
      const targetId = event?.endpoint_id || event?.node_id || event?.data?.endpoint_id
      if ((event?.type === 'NODE_STATE_CHANGE' || event?.type === 'STATE_TRANSITION') && targetId === endpointId) {
        const rtt = event?.avg_rtt_ms ?? event?.data?.avg_rtt_ms
        if (endpoint.value && rtt != null) {
          endpoint.value.avg_rtt_ms = rtt
        }
        const newState = event?.new_state || event?.detailed_state || event?.operational_state || event?.data?.new_state
        if (endpoint.value && newState) {
          endpoint.value.current_operational_state = newState
          endpoint.value.current_detailed_state = newState
        }
        if (activePerspective.value === 'flow') {
          loadFlowTelemetry()
        }
      }
    } catch (e) {
      // Ignore malformed SSE heartbeats
    }
  })
})

onBeforeUnmount(() => {
  if (pollInterval) {
    clearInterval(pollInterval)
  }
  if (unsubscribeSSE) {
    unsubscribeSSE()
  }
})
</script>

<style scoped>
.detail-wrapper {
  min-height: 100vh;
  background-color: var(--canvas-bg);
}



.detail-container {
  padding: 2rem;
  max-width: 1400px;
  margin: 0 auto;
}

.back-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1.5rem;
}

.device-actions {
  display: flex;
  gap: 0.75rem;
}

.status-badge {
  font-size: 0.8rem;
  font-weight: 700;
  padding: 0.35rem 0.75rem;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  background-color: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}

.status-badge.active {
  color: var(--text-primary);
  border-color: var(--border-color-strong);
}

.status-badge.disabled {
  color: var(--text-muted);
  border-color: var(--border-color);
}

.monitoring-badge {
  font-size: 0.8rem;
  font-weight: 700;
  padding: 0.35rem 0.75rem;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  background-color: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}

.monitoring-badge.enabled {
  color: var(--text-primary);
  border-color: var(--border-color-strong);
}

.monitoring-badge.disabled {
  border: 1px solid var(--status-down-color);
  color: var(--status-down-color);
}

.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 8rem 0;
  gap: 1rem;
  font-family: monospace;
  color: var(--text-muted);
}

.spinner-icon {
  font-size: 2.5rem;
  color: var(--text-primary);
}

.error-message {
  display: flex;
  align-items: flex-start;
  gap: 1rem;
  background-color: rgba(220, 38, 38, 0.15);
  color: var(--status-down-color);
  padding: 1.5rem;
  border-radius: 4px;
  border: 1px solid var(--status-down-color);
  margin-bottom: 2rem;
}

.error-message h4 {
  font-weight: 700;
  margin-bottom: 0.25rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.content-layout {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.overview-card {
  background-color: var(--card-bg) !important;
  border: 1px solid var(--card-border) !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(1, 1fr);
  gap: 1.5rem;
}

@media (min-width: 768px) {
  .meta-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

.main-meta {
  display: flex;
  align-items: center;
  gap: 1rem;
}

@media (min-width: 768px) {
  .main-meta {
    grid-column: span 1;
  }
}

.main-icon-styled {
  font-size: 1.8rem;
  padding: 0.75rem;
  border-radius: 4px;
  background-color: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
}

.ip-display {
  font-family: monospace;
  color: var(--text-muted);
  font-size: 1rem;
  font-weight: 700;
  margin-top: 0.15rem;
}

h2 {
  color: var(--text-primary);
  font-weight: 700;
  font-size: 1.35rem;
  letter-spacing: -0.01em;
}

.meta-item {
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.meta-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  color: var(--text-muted);
  font-weight: 700;
  letter-spacing: 0.05em;
  margin-bottom: 0.25rem;
}

.meta-value {
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--text-primary);
}

.meta-value.desc {
  font-weight: 500;
  color: var(--text-secondary);
  line-height: 1.5;
}

.full-width {
  grid-column: span 1;
}

@media (min-width: 768px) {
  .full-width {
    grid-column: span 3;
    border-top: 1px solid var(--border-color);
    padding-top: 1rem;
  }
}

/* Date Filters Toolbar */
.toolbar-card {
  background: var(--card-bg);
  border-radius: 4px;
  padding: 1rem 1.5rem;
  border: 1px solid var(--card-border);
  display: flex;
  flex-direction: column;
  gap: 1rem;
  justify-content: space-between;
  align-items: flex-start;
  box-shadow: none;
}

@media (min-width: 1024px) {
  .toolbar-card {
    flex-direction: row;
    align-items: center;
  }
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}

.toolbar-title {
  font-weight: 700;
  color: var(--text-primary);
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.range-buttons {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  width: 100%;
}

@media (min-width: 1024px) {
  .toolbar-right {
    width: auto;
    justify-content: flex-end;
  }
}

.date-input-group {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.date-input-group label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.date-picker-input {
  background-color: var(--card-bg);
  border: 1px solid var(--card-border);
  border-radius: 4px;
  padding: 0.35rem 0.5rem;
  font-size: 0.85rem;
  outline: none;
  font-family: monospace;
  color: var(--text-primary);
}

.date-picker-input:focus {
  border-color: #049f6c;
}

/* Metrics Cards Grid */
.metrics-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(1, 1fr);
}

@media (min-width: 576px) {
  .metrics-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (min-width: 1024px) {
  .metrics-grid {
    grid-template-columns: repeat(4, 1fr);
  }
}

.metric-card {
  background: var(--card-bg);
  border-radius: 4px;
  border: 1px solid var(--card-border);
  padding: 1.25rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  box-shadow: none;
}

.metric-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.metric-header i {
  font-size: 1rem;
  color: var(--text-muted);
}

.metric-body {
  display: flex;
  flex-direction: column;
}

.metric-body .value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  font-family: monospace;
}

.metric-body .subtext {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
  font-family: monospace;
}

.value.perfect { color: var(--text-primary); }
.value.good { color: var(--text-primary); }
.value.warning { color: var(--status-warn-color); }
.value.critical { color: var(--status-down-color); }

.value.alert { color: var(--status-down-color); }
.value.clean { color: var(--text-primary); }
.value.has-down { color: var(--status-down-color); }

/* Visualizer Container panels */
.visualizer-container {
  background: var(--card-bg);
  border-radius: 4px;
  border: 1px solid var(--card-border);
  padding: 1.5rem;
  box-shadow: none;
}

.panel-header {
  margin-bottom: 1.5rem;
  border-bottom: 1px solid var(--card-border);
  padding-bottom: 1rem;
}

.panel-header h3 {
  font-size: 1rem;
  font-weight: 700;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 0.5rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.header-icon {
  color: var(--text-primary);
}

.panel-desc {
  font-size: 0.8rem;
  color: var(--text-secondary);
  display: block;
  margin-top: 0.25rem;
}

/* Detailed Audit Table */
.table-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.total-badge {
  background-color: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  font-size: 0.75rem;
  font-weight: 700;
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  font-family: monospace;
}

.table-responsive {
  width: 100%;
  overflow-x: auto;
}

.audit-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 0.85rem;
}

.audit-table th {
  background-color: var(--bg-app);
  color: var(--text-secondary);
  font-weight: 700;
  padding: 0.75rem 1rem;
  border-bottom: 2px solid var(--card-border);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.audit-table td {
  padding: 0.85rem 1rem;
  border-bottom: 1px solid var(--card-border);
  color: var(--text-primary);
}

.audit-table tr:hover {
  background-color: var(--bg-surface-hover);
}

.table-empty {
  text-align: center;
  padding: 3rem 0 !important;
  color: var(--text-muted);
  font-size: 0.85rem;
  font-family: monospace;
}

.font-mono {
  font-family: monospace;
}

.font-bold {
  font-weight: 700;
}

.text-success { color: var(--status-up-color); }
.text-warning { color: var(--status-warn-color); }
.text-danger { color: var(--status-down-color); }

.table-badge {
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.table-badge.up {
  color: var(--status-up-color);
}

.table-badge.down {
  color: var(--status-down-color);
}

.table-detail-badge {
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.table-detail-badge.up {
  color: var(--status-up-color);
}

.table-detail-badge.up-unstable,
.table-detail-badge.down-unstable {
  color: var(--status-warn-color);
}

.table-detail-badge.down {
  color: var(--status-down-color);
}

/* PrimeVue Button custom scoping overrides */
:deep(.p-button) {
  background-color: var(--bg-surface) !important;
  border: 1px solid var(--border-color) !important;
  color: var(--text-secondary) !important;
  font-family: monospace !important;
  font-size: 0.85rem !important;
  font-weight: 700 !important;
  border-radius: 4px !important;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  transition: all 0.15s ease;
}
:deep(.p-button:hover) {
  background-color: var(--bg-surface-hover) !important;
  border-color: var(--border-color-strong) !important;
  color: var(--text-primary) !important;
}
/* When button is active (i.e. not outlined) */
:deep(.p-button:not(.p-button-outlined)) {
  background-color: var(--text-primary) !important;
  border-color: var(--text-primary) !important;
  color: var(--text-inverse) !important;
}
:deep(.p-button:not(.p-button-outlined):hover) {
  background-color: var(--text-secondary) !important;
  border-color: var(--text-secondary) !important;
  color: var(--text-inverse) !important;
}
/* Back button specific */
.back-btn {
  background-color: var(--bg-surface) !important;
  border: 1px solid var(--border-color) !important;
  color: var(--text-primary) !important;
}
/* Danger button (logout, retry) */
:deep(.p-button-danger) {
  background-color: var(--bg-surface) !important;
  border: 1px solid var(--status-down-color) !important;
  color: var(--status-down-color) !important;
}
:deep(.p-button-danger:hover) {
  background-color: var(--bg-surface-hover) !important;
  border-color: var(--status-down-color) !important;
  color: var(--status-down-color) !important;
}
/* Text button (header menu navigation) */
:deep(.p-button-text) {
  background: transparent !important;
  border-color: transparent !important;
  color: var(--text-secondary) !important;
}
:deep(.p-button-text:hover) {
  background-color: var(--bg-surface-selected) !important;
  color: var(--text-primary) !important;
}
:deep(.p-button-text.p-button-danger:hover) {
  background-color: var(--bg-surface-selected) !important;
  color: var(--status-down-color) !important;
}

/* ==========================================================================
   Light Mode Theme Scoping Overrides (Remaining wrapper variables)
   ========================================================================== */


.version-tag {
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--text-muted, #737373);
  margin-left: 0.25rem;
  font-weight: 500;
  text-transform: none;
}


/* Compact Table Pagination Footer Styles */
.table-pagination-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid var(--card-border);
  font-family: monospace;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.pagination-controls-wrapper {
  display: flex;
  align-items: center;
  gap: 1.5rem;
}

.pagination-nav-arrows {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.nav-arrow-btn {
  background-color: transparent;
  border: 1px solid var(--card-border);
  color: var(--text-secondary);
  cursor: pointer;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 3px;
  font-size: 0.8rem;
  font-weight: bold;
  transition: all 0.15s ease;
}

.nav-arrow-btn:hover:not(:disabled) {
  border-color: var(--text-primary);
  color: var(--text-primary);
  background-color: rgba(255, 255, 255, 0.05);
}

.nav-arrow-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.current-page-display {
  font-weight: bold;
}

.density-dropdown {
  background-color: var(--card-bg);
  border: 1px solid var(--card-border);
  color: var(--text-secondary);
  font-family: monospace;
  font-size: 0.8rem;
  padding: 0.2rem 0.5rem;
  border-radius: 3px;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.density-dropdown:focus, .density-dropdown:hover {
  border-color: var(--text-secondary);
}



.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.form-textarea {
  resize: vertical;
}

.governance-box {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.box-title {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
  color: #3B82F6;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.setting-switch-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.switch-info {
  display: flex;
  flex-direction: column;
}

.switch-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
}

.switch-desc {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.check-box {
  width: 18px;
  height: 18px;
  cursor: pointer;
}

.modal-footer-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 10px;
  gap: 12px;
}

.footer-buttons {
  display: flex;
  gap: 10px;
}

.btn-discovery {
  background: rgba(59, 130, 246, 0.15);
  color: #60A5FA;
  border: 1px solid rgba(59, 130, 246, 0.4);
  padding: 8px 14px;
  border-radius: var(--radius);
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
}

.btn-discovery:hover {
  background: rgba(59, 130, 246, 0.25);
}

/* Perspective Tabs */
.perspective-tabs {
  display: flex;
  gap: 8px;
  border-bottom: 2px solid var(--border-color);
  margin: 16px 0 12px 0;
}

.perspective-tab {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  color: var(--text-secondary);
  font-weight: 600;
  font-size: 0.875rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.perspective-tab:hover {
  color: var(--text-primary);
}

.perspective-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.flow-panel-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.flow-chart-box {
  height: 280px;
  position: relative;
  margin-top: 12px;
}

.alias-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.chip-ip, .chip-if {
  display: inline-block;
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-family: var(--font-mono);
}

.p-4 {
  padding: 16px;
}

.font-bold {
  font-weight: 700;
}
.font-mono {
  font-family: var(--font-mono);
}
.text-base {
  font-size: 0.9375rem;
}
.text-sm {
  font-size: 0.8125rem;
}
.text-xs {
  font-size: 0.75rem;
}
.tnum {
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
}

.flow-subview-switcher {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1rem;
}

.subview-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  font-size: 0.85rem;
  padding: 0.4rem 0.85rem;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.subview-btn:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
  border-color: var(--border-color-strong);
}

.subview-btn.active {
  background: rgba(14, 165, 233, 0.15);
  border-color: #0ea5e9;
  color: #0ea5e9;
  font-weight: 600;
}

.interface-meter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0.85rem;
  margin-top: 0.5rem;
}

.iface-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.iface-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.iface-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}

.iface-tag {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid var(--border-color);
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.75rem;
  font-family: var(--font-mono);
  color: var(--text-muted);
}

.iface-name {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.iface-status-pill {
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
}

.status-active {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
}

.status-idle {
  background: rgba(100, 116, 139, 0.2);
  color: #94a3b8;
}

.iface-stats {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  font-family: var(--font-mono);
}

.meter-bar-track {
  height: 6px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 3px;
  overflow: hidden;
}

.meter-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.util-healthy {
  background: #10b981;
}

.util-warning {
  background: #f59e0b;
}

.util-danger {
  background: #ef4444;
}

.meter-meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.7rem;
  color: var(--text-muted);
}

.text-emerald {
  color: #10b981;
}

.text-cyan {
  color: #0ea5e9;
}

/* Flow Exporter Passive Telemetry Banner */
.passive-exporter-banner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(99, 102, 241, 0.1);
  border: 1px solid rgba(99, 102, 241, 0.3);
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 1.5rem;
  gap: 16px;
}

.banner-content {
  display: flex;
  align-items: center;
  gap: 12px;
}

.banner-icon {
  font-size: 1.5rem;
  color: #818cf8;
}

.banner-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.banner-title {
  font-weight: 700;
  color: #818cf8;
  font-size: 0.95rem;
}

.banner-body p {
  margin: 0;
  font-size: 0.825rem;
  color: var(--text-secondary);
}

.iface-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.iface-cfg-btn {
  background: none;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.75rem;
  font-weight: 600;
  transition: all 0.2s ease;
}

.iface-cfg-btn:hover {
  border-color: #6366f1;
  color: #818cf8;
}

.iface-speed {
  font-weight: 600;
  color: var(--text-muted);
}

/* Interface Configuration Modal */
.modal-subtitle {
  font-size: 0.85rem;
  color: var(--text-secondary);
  margin-bottom: 1rem;
}

.interface-config-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 380px;
  overflow-y: auto;
  margin-bottom: 12px;
  padding-right: 4px;
}

.interface-config-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.idx-col {
  width: 90px;
}

.name-col {
  flex: 1;
}

.preset-col {
  width: 170px;
}

.speed-col {
  width: 100px;
}

.remove-col {
  display: flex;
  align-items: flex-end;
  padding-bottom: 2px;
}

.btn-icon-danger {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #ef4444;
  border-radius: 4px;
  width: 34px;
  height: 34px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
}

.btn-icon-danger:hover {
  background: rgba(239, 68, 68, 0.25);
}

.add-row-action {
  margin-bottom: 1.5rem;
}

.header-controls {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.interface-filter-wrapper {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.filter-lbl {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.filter-select {
  background: var(--bg-surface-selected);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm, 4px);
  padding: 0.3rem 0.6rem;
  font-size: 0.8125rem;
  font-family: var(--font-sans);
  cursor: pointer;
  outline: none;
  transition: border-color 0.15s;
}

.filter-select:hover,
.filter-select:focus {
  border-color: var(--border-color-strong);
}

.badge-window {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.25rem 0.5rem;
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}

/* Section header split */
.section-header-split {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

/* Metric Mode Switcher Pills */
.metric-mode-pills {
  display: flex;
  background: var(--bg-surface-selected);
  border-radius: var(--radius-sm, 6px);
  padding: 2px;
  gap: 2px;
}

.btn-pill {
  padding: 4px 10px;
  font-size: 0.75rem;
  font-weight: 600;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.btn-pill:hover {
  color: var(--text-primary);
}

.btn-pill.active {
  background: var(--bg-surface, #ffffff);
  color: var(--text-primary);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}

html.dark .btn-pill.active {
  background: rgba(255, 255, 255, 0.12);
  color: #ffffff;
}

.btn-pill-sm {
  padding: 2px 8px;
  font-size: 0.7rem;
}

/* Service Port Badges */
.service-port-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-family: var(--font-mono, monospace);
  font-weight: 600;
}

.port-web {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.port-dns {
  background: rgba(16, 185, 129, 0.12);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.port-ssh {
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.port-telemetry {
  background: rgba(139, 92, 246, 0.12);
  color: #8b5cf6;
  border: 1px solid rgba(139, 92, 246, 0.3);
}

.port-generic {
  background: rgba(107, 114, 128, 0.12);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}

/* Physics & Profiles */
.physics-profile-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.badge-profile {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  width: fit-content;
}

.badge-profile-bulk {
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.35);
}

.badge-profile-syn {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.35);
}

.badge-profile-rst {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.35);
}

.badge-profile-micro {
  background: rgba(139, 92, 246, 0.15);
  color: #8b5cf6;
  border: 1px solid rgba(139, 92, 246, 0.35);
}

.badge-profile-voip {
  background: rgba(6, 182, 212, 0.15);
  color: #06b6d4;
  border: 1px solid rgba(6, 182, 212, 0.35);
}

.badge-profile-interactive {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.packet-physics-meta {
  font-size: 0.6875rem;
  white-space: nowrap;
}

/* Forensic L3/L4 Telemetry & Anomaly Badges */
.metadata-badge-group {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.badge-tcp-flags {
  display: inline-flex;
  align-items: center;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  background: rgba(100, 116, 139, 0.12);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
}

.badge-anomaly {
  display: inline-flex;
  align-items: center;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.6875rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.badge-syn-only {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.35);
}

.badge-tcp-rst {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.35);
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
</style>
