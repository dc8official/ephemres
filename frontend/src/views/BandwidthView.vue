<template>
  <div class="bandwidth-view">
    <!-- Header Toolbar -->
    <div class="bandwidth-toolbar">
      <div class="toolbar-left">
        <h1 class="page-title">Traffic Analytics</h1>
        <!-- The page updates itself once a minute (build plan 7.1); this line says how fresh it is
             and lets the user hold it still, e.g. while reading a table. -->
        <div class="toolbar-sub-row">
          <span class="page-sub update-status" aria-live="polite">
            <template v-if="paused">Updates paused</template>
            <template v-else-if="pageHidden">Updates resume when this tab is shown</template>
            <template v-else>{{ updatedLabel }}</template>
          </span>
          <button
            type="button"
            class="btn-text pause-toggle"
            :aria-pressed="paused"
            @click="togglePause"
          >{{ paused ? 'Resume' : 'Pause' }}</button>
        </div>
      </div>

      <div class="toolbar-right">
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

    <!-- Scope (build plan 7.5 step 2): the shared filter picks routers by site, location, tags,
         type or search, and the device picker narrows to one of them. Every figure below is
         what the routers in scope saw. Both live in the URL like the other pages' filters. -->
    <FilterBar :state="filterState" class="traffic-filter-bar">
      <label class="filter-field">
        <span>Device</span>
        <select :value="selectedDeviceId" class="form-select" @change="setDevice($event.target.value)">
          <option value="">{{ allDevicesLabel }}</option>
          <optgroup v-for="group in deviceGroups" :key="group.label" :label="group.label">
            <option v-for="d in group.devices" :key="d.id" :value="d.id">{{ deviceOptionLabel(d) }}</option>
          </optgroup>
        </select>
      </label>
    </FilterBar>

    <!-- Flow sources not registered yet: one line instead of entries in the picker (review P4).
         Their flows count under "All devices" only, as they have no site or tags yet. -->
    <!-- Failures of page-level actions show here instead of a blocking alert(). -->
    <div v-if="actionError" class="inline-alert" role="alert">{{ actionError }}</div>
    <div v-if="unmatchedExporters.length" class="unregistered-notice" role="status">
      <div class="unregistered-line">
        <span>{{ unregisteredLabel }}</span>
        <button
          type="button"
          class="btn-text"
          :aria-expanded="showUnregistered"
          aria-controls="unregistered-list"
          @click="showUnregistered = !showUnregistered"
        >{{ showUnregistered ? 'Hide' : 'Review' }}</button>
      </div>
      <ul v-if="showUnregistered" id="unregistered-list" class="unregistered-list">
        <li v-for="u in unmatchedExporters" :key="u.ip_address">
          <span class="font-mono tnum">{{ u.ip_address }}</span>
          <span class="text-muted text-xs tnum">{{ unregisteredDetail(u) }}</span>
          <span v-if="isAdmin" class="unregistered-actions">
            <button type="button" class="btn-secondary btn-small" @click="openEnrollModal(u.ip_address)">Enroll</button>
            <button type="button" class="btn-secondary btn-small" @click="openMappingModal(u.ip_address)">Link to an endpoint</button>
          </span>
        </li>
      </ul>
    </div>

    <!-- Now (build plan 7.5 step 4, review P1/O1/O4): the WAN links against capacity over the
         last five minutes, then one status line for the flow collection itself. Everything
         under the "Over the last" heading follows the time window. -->
    <WanLinksNow
      :data="wanLinks"
      :scope-label="scopeLabel"
      :is-admin="isAdmin"
      :loading="loading && !wanLinks"
      @set-speed="openSpeedEditor"
    >
      <p class="now-status-line text-xs" :class="{ 'is-warn': stoppedRouters.length }" role="status">
        <span v-for="(part, i) in statusParts" :key="i" :title="part.title || null">{{ part.text }}</span>
      </p>
    </WanLinksNow>

    <h2 class="window-heading">Over the last {{ windowWords }}</h2>

    <SegmentedControl
      class="view-switcher"
      :model-value="activeView"
      :options="viewOptions"
      aria-label="Traffic view"
      @update:model-value="setView"
    />

    <!-- ── Overview ── -->
    <template v-if="activeView === 'overview'">
    <!-- Main Section: one chart per WAN link (build plan 7.5 step 5), or every link on one chart -->
    <div class="chart-section table-card">
      <div class="section-header section-header-split">
        <div>
          <div class="section-title">
            {{ perLinkCharts ? 'WAN links' : chartTitle }}
          </div>
          <span class="section-hint">{{ perLinkCharts ? linkChartsHint : chartHint }}</span>
        </div>
        <div class="chart-header-actions">
          <SegmentedControl
            v-if="showLayoutToggle"
            :model-value="chartLayout"
            :options="layoutOptions"
            aria-label="Chart layout"
            size="sm"
            @update:model-value="setChartLayout"
          />
          <SegmentedControl
            v-if="showTotalsToggle && !perLinkCharts"
            :model-value="chartTotals"
            :options="totalsOptions"
            aria-label="Chart totals"
            size="sm"
            @update:model-value="setChartTotals"
          />
          <SegmentedControl v-if="!perLinkCharts" v-model="chartMetricMode" :options="metricOptions" aria-label="Chart metric" size="sm" />
        </div>
      </div>
      <template v-if="perLinkCharts">
        <WanLinkCharts v-if="linkSeriesReady" :links="wanLinks.links" :timezone="wanLinks.timezone" />
        <div v-else class="loading-spinner">Loading link charts...</div>
      </template>
      <div v-else class="chart-container">
        <p v-if="seriesPoints.length > 0" class="sr-only">{{ chartSummary }}</p>
        <LineChart
          v-if="seriesPoints.length > 0"
          :data="chartData"
          :options="chartOptions"
        />
        <div v-else-if="!loading" class="no-chart-data">
          <template v-if="['1h', '6h'].includes(selectedWindow)">No flow records in the last {{ selectedWindow }}.</template>
          <template v-else>No hourly totals for the last {{ selectedWindow }} yet. They are built from minute data periodically.</template>
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
                <th scope="col">Host</th>
                <th scope="col">Site</th>
                <th scope="col" class="text-right" title="What the host received">Received</th>
                <th scope="col" class="text-right" title="What the host sent">Sent</th>
                <th scope="col" class="text-right">Total Bytes</th>
                <th scope="col" class="text-right">Flows / Avg packet</th>
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
                <!-- An endpoint's site, else the sites of the routers that saw the address. -->
                <td class="text-muted" :title="(t.sites || []).join(', ')">{{ siteLabel(t.sites) }}</td>
                <td class="text-right tnum ingress-text">{{ formatBytes(t.ingress_bytes) }}</td>
                <td class="text-right tnum egress-text">{{ formatBytes(t.egress_bytes) }}</td>
                <td class="text-right tnum font-bold">{{ formatBytes(t.total_bytes) }}</td>
                <td class="text-right tnum text-muted">
                  <div>{{ formatNumber(t.flow_count) }}</div>
                  <span class="text-xs text-muted block font-mono">{{ t.avg_packet_size || 0 }} B/pkt</span>
                </td>
              </tr>
              <tr v-if="topEndpoints.length === 0">
                <td colspan="6" class="text-center text-muted">No talker statistics in this window.</td>
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
        <div v-if="visibleZones.length > 0" class="breakdown-list">
          <div
            v-for="zone in visibleZones"
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
      </div>
    </div>
    </template>

    <!-- ── Interfaces ── -->
    <template v-else-if="activeView === 'interfaces'">
      <template v-if="selectedDeviceId">
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
          <SegmentedControl
            v-if="showTotalsToggle"
            :model-value="chartTotals"
            :options="totalsOptions"
            aria-label="Chart totals"
            size="sm"
            @update:model-value="setChartTotals"
          />
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
          <template v-else>No hourly totals for the last {{ selectedWindow }} yet. They are built from minute data periodically.</template>
        </div>
        <div v-else class="loading-spinner">Loading telemetry series...</div>
      </div>
    </div>

    <!-- One device's interfaces (build plan 7.5 step 6: the same table and columns as across
         devices). Click a row to isolate its traffic in the chart above. -->
    <div v-if="selectedDeviceId && interfaceList.length > 0" class="table-card interfaces-panel">
      <div class="section-header section-header-split">
        <div>
          <div class="section-title">Interfaces</div>
          <span class="section-hint">{{ interfaceList.length }} interfaces · Now: last 5 complete minutes · Click a row to isolate its traffic</span>
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
      <InterfacesTable
        :rows="interfaceList"
        :is-admin="isAdmin"
        :selected-idx="selectedInterfaceIdx"
        :window-label="selectedWindow"
        :timezone="wanLinks?.timezone || ''"
        @select="(item) => toggleInterfaceIsolation(item.interface_idx)"
        @edit="(item) => openInterfaceModal(item.interface_idx)"
      />
    </div>

        <div v-if="interfaceList.length === 0 && !loading" class="table-card empty-view">
          No interface data for this device in the last {{ selectedWindow }}.
        </div>
      </template>
      <!-- Every router in scope (review I1): WAN links first, then the busiest interfaces.
           A row opens its device with that interface isolated. -->
      <div v-else class="table-card interfaces-panel">
        <div class="section-header">
          <div>
            <div class="section-title">Interfaces across devices</div>
            <span class="section-hint">{{ crossDeviceHint }}</span>
          </div>
        </div>
        <InterfacesTable
          :rows="interfaceList"
          multi-device
          :is-admin="isAdmin"
          :window-label="selectedWindow"
          :timezone="wanLinks?.timezone || ''"
          :empty-text="filterState.isActive.value ? 'No interfaces on the devices that match the filter.' : 'No interface data yet: routers send it with their flows or over SNMP.'"
          @select="openDeviceInterface"
          @edit="(item) => openSpeedEditor(item)"
        />
      </div>
    </template>

    <!-- ── Conversations ── -->
    <ConversationsPanel
      v-else-if="activeView === 'conversations'"
      :window="selectedWindow"
      :scope="scope"
      v-model:ip="selectedIpFilter"
      v-model:port="selectedPortFilter"
      v-model:dscp="selectedDscpFilter"
      v-model:dscp-label="selectedDscpLabel"
      :refresh-key="refreshKey"
    />

    <!-- ── QoS ── -->
    <template v-else-if="activeView === 'qos'">
      <!-- Nothing marked: one line instead of "Best Effort 100%" and an empty table (Q1). -->
      <div v-if="!markedQosClasses.length" class="table-card no-chart-data qos-unmarked">
        Nothing is DSCP-marked on these links in the last {{ selectedWindow }}: all traffic is best effort.
      </div>
      <template v-else>
        <div class="table-card breakdown-panel qos-panel">
          <div class="section-header">
            <div>
              <div class="section-title">DiffServ classes</div>
              <span class="section-hint">Share of traffic by DSCP marking · Click a class to list its conversations</span>
            </div>
          </div>
          <div class="breakdown-list">
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
        </div>
        <ConversationsPanel
          :window="selectedWindow"
          :scope="scope"
          initial-quick="qos"
          title="QoS-marked conversations"
          v-model:ip="selectedIpFilter"
          v-model:port="selectedPortFilter"
          v-model:dscp="selectedDscpFilter"
          v-model:dscp-label="selectedDscpLabel"
          :refresh-key="refreshKey"
        />
      </template>
    </template>

    <!-- ── Security ── -->
    <template v-else-if="activeView === 'security'">
      <!-- Same window and scope as the table under them (review P1): until v3.3.6 the tiles
           counted the last five minutes above a table covering the picked window. -->
      <p class="kpi-scope-note">Last {{ windowWords }} · {{ scopeLabel }}</p>
      <div class="kpi-grid security-kpis">
        <KpiTile
          label="Suspicious conversations"
          :value="anomalies ? formatNumber(anomalies.conversations) : null"
          :tone="anomalies?.conversations ? 'down' : 'neutral'"
          sub="SYN without ACK, or RST without FIN, over the conversation"
        />
        <KpiTile
          label="Flows in them"
          :value="anomalies ? formatNumber(anomalies.flows) : null"
          :tone="anomalies?.flows ? 'down' : 'neutral'"
          :sub="anomalies ? `of ${formatNumber(anomalies.all_flows)} flows` : ''"
        />
      </div>
      <ConversationsPanel
        :window="selectedWindow"
        :scope="scope"
        initial-quick="anomalies"
        initial-sort="flows"
        show-seen
        :timezone="wanLinks?.timezone || ''"
        title="Suspicious conversations"
        :hint="`Last ${selectedWindow} · SYN-only conversations suggest scans; RST-only ones refused connections. Sorted by flow count.`"
        v-model:ip="selectedIpFilter"
        v-model:port="selectedPortFilter"
        v-model:dscp="selectedDscpFilter"
        v-model:dscp-label="selectedDscpLabel"
        :refresh-key="refreshKey"
      />
    </template>

    <!-- Mapping Modal for Unmatched Exporter -->
    <div v-if="showMappingModal" class="modal-overlay" @click.self="showMappingModal = false">
      <div role="dialog" aria-modal="true" aria-labelledby="bw-modal-1-title" class="modal-card">
        <div class="modal-header">
          <h3 id="bw-modal-1-title">Link Exporter Alias</h3>
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
      <div role="dialog" aria-modal="true" aria-labelledby="bw-modal-2-title" class="modal-card">
        <div class="modal-header">
          <h3 id="bw-modal-2-title">Enroll Flow Exporter</h3>
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
      <div role="dialog" aria-modal="true" aria-labelledby="bw-modal-3-title" class="modal-card wide">
        <div class="modal-header">
          <h3 id="bw-modal-3-title">⚙️ Configure Router Interface Aliases & Speeds<span v-if="editingDeviceName" class="modal-device"> · {{ editingDeviceName }}</span></h3>
          <button class="btn-close" @click="showInterfaceModal = false">✕</button>
        </div>

        <form @submit.prevent="saveInterfaces" class="modal-form">
          <p class="modal-subtitle">
            Assign human-readable SNMP interface names and provisioned line speeds for accurate utilization calculation.
            Mark links to the internet or a provider as WAN: fleet totals then count traffic once, at the network edge.
          </p>

          <div class="interface-config-list">
            <!-- Each row, and under a WAN row its traffic alert rule (v3.3.6). -->
            <div v-for="(iface, i) in editingInterfaces" :key="i" class="iface-row-wrap">
            <div class="interface-config-row">
              <div class="form-group idx-col">
                <label :for="`iface-idx-${i}`" :class="{ 'row-label-repeat': i > 0 }">ifIndex *</label>
                <input
                  :id="`iface-idx-${i}`"
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
                <label :for="`iface-name-${i}`" :class="{ 'row-label-repeat': i > 0 }">Interface Name / Alias</label>
                <input
                  :id="`iface-name-${i}`"
                  type="text"
                  v-model="iface.name"
                  :placeholder="iface.snmpName || 'e.g. GigabitEthernet0/1 or WAN-Uplink'"
                  :title="iface.snmpName ? `Empty: the device's own name (${iface.snmpName}) is shown` : null"
                  class="form-input"
                />
              </div>

              <div class="form-group role-col">
                <label :for="`iface-role-${i}`" :class="{ 'row-label-repeat': i > 0 }">Role</label>
                <select :id="`iface-role-${i}`" v-model="iface.role" class="form-select">
                  <option value="lan">Internal</option>
                  <option value="wan">WAN (edge)</option>
                </select>
              </div>

              <div class="form-group preset-col">
                <label :for="`iface-preset-${i}`" :class="{ 'row-label-repeat': i > 0 }">Speed Preset</label>
                <select :id="`iface-preset-${i}`" v-model="iface.preset" @change="onPresetChange(iface)" class="form-select">
                  <option value="unset">{{ iface.snmpSpeed ? `From SNMP: ${formatMbps(iface.snmpSpeed)}` : 'Not set' }}</option>
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
                <label :for="`iface-mbps-${i}`" :class="{ 'row-label-repeat': i > 0 }">Mbps</label>
                <input
                  :id="`iface-mbps-${i}`"
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
            <InterfaceAlertControl
              v-if="iface.role === 'wan'"
              v-model="iface.alert"
              :has-speed="(iface.preset !== 'unset' && Number(iface.speed_mbps) > 0) || !!iface.snmpSpeed"
              class="iface-alert-line"
            />
            </div>
          </div>

          <div class="add-row-action">
            <button type="button" class="btn-secondary btn-small" @click="addInterfaceRow">
              + Add Interface
            </button>
          </div>

          <div v-if="interfaceError" class="inline-alert" role="alert">{{ interfaceError }}</div>
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
import { ref, shallowRef, onMounted, onUnmounted, computed, watch, nextTick } from 'vue'
import { useEscapeClose } from '../composables/useEscapeClose.js'
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
  getAnomalySummary,
  getBandwidthOverview,
  getWanLinks,
  getTrafficSeries,
  getTopTalkers,
  getApplicationDistribution,
  getFlowExporters,
  enrollFlowExporter,
  mapFlowExporter,
  getInterfaceTelemetry,
  getInterfaces,
  updateEndpointInterfaces,
  getEndpoints,
} from '../services/api.js'
import { isAdmin } from '../services/auth.js'
import { formatBps, formatBytes, formatNumber, formatPps } from '../utils/format.js'
import { useChartTheme } from '../composables/useChartTheme.js'
import { summarizeSeries } from '../utils/chartSummary.js'
import { aliasesPayload, applyPreset, editRow, rowFromTelemetry, formatMbps } from '../utils/interfaceConfig.js'
import InterfaceAlertControl from '../components/ui/InterfaceAlertControl.vue'
import KpiTile from '../components/ui/KpiTile.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'
import TimeRangePicker from '../components/ui/TimeRangePicker.vue'
import ConversationsPanel from '../components/traffic/ConversationsPanel.vue'
import WanLinksNow from '../components/traffic/WanLinksNow.vue'
import WanLinkCharts from '../components/traffic/WanLinkCharts.vue'
import InterfacesTable from '../components/traffic/InterfacesTable.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import { useEndpointFilter, filterParams } from '../composables/useEndpointFilter.js'
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
const route = useRoute()
const router = useRouter()
// The time window lives in the URL like the other filters, so it can be bookmarked and survives
// Back (build plan 7.1). The header's Traffic link carries no query, so the last window is also
// kept for this browser tab and put back in the URL on return. 1h, the default, stays out.
const WINDOW_STORAGE_KEY = 'trafficWindow'
function rememberedWindow() {
  try {
    const w = sessionStorage.getItem(WINDOW_STORAGE_KEY)
    return windowOptions.includes(w) ? w : null
  } catch {
    return null
  }
}
const selectedWindow = ref(
  windowOptions.includes(route.query.window) ? route.query.window : (rememberedWindow() || '1h')
)
const loading = ref(false)

// --- Automatic update (build plan 7.1) ------------------------------------------------------
// The figures are one-minute totals, so refreshing faster than once a minute only redraws the
// same numbers. ephemres-flowd writes its minute buckets every 10 s, so a minute is complete
// about 10 s after it ends: update at UPDATE_SECOND past each minute. Nothing runs while the
// tab is hidden; on return the page updates at once if the data is over a minute old.
const UPDATE_SECOND = 15
const paused = ref(false)
const pageHidden = ref(typeof document !== 'undefined' && document.visibilityState === 'hidden')
const lastUpdated = ref(null)
const nowTick = ref(Date.now())
let updateTimer = null
let clockTimer = null

const updatedLabel = computed(() => {
  if (!lastUpdated.value) return 'Updating…'
  const secs = Math.max(0, Math.round((nowTick.value - lastUpdated.value) / 1000))
  if (secs < 5) return 'Updated just now'
  if (secs < 60) return `Updated ${secs} s ago`
  const mins = Math.round(secs / 60)
  return `Updated ${mins} min ago`
})

function scheduleNextUpdate() {
  clearTimeout(updateTimer)
  const now = new Date()
  const next = new Date(now)
  next.setSeconds(UPDATE_SECOND, 0)
  if (next <= now) next.setMinutes(next.getMinutes() + 1)
  updateTimer = setTimeout(async () => {
    if (!paused.value && !pageHidden.value && !loading.value) {
      await refreshAll()
    }
    scheduleNextUpdate()
  }, next - now)
}

function togglePause() {
  paused.value = !paused.value
  // Resuming after a while: show current figures now rather than at the next tick.
  if (!paused.value && lastUpdated.value && Date.now() - lastUpdated.value > 60000 && !loading.value) {
    refreshAll()
  }
}

function onVisibilityChange() {
  pageHidden.value = document.visibilityState === 'hidden'
  if (!pageHidden.value && !paused.value && !loading.value && (!lastUpdated.value || Date.now() - lastUpdated.value > 60000)) {
    refreshAll()
  }
}

// Scope (build plan 7.5 step 2): the shared filter and one optional device, both in the URL.
// `exporter_id` was the device's key before v3.3.6; old links still open on that device.
const filterState = useEndpointFilter()
const selectedDeviceId = ref(route.query.device_id || route.query.exporter_id || '')
// The devices the picker offers: routers sending flows or polled over SNMP, in the filter.
const flowExporters = shallowRef([])
const scope = computed(() => ({
  ...filterParams(filterState.filter.value),
  device_id: selectedDeviceId.value || undefined,
}))
const interfaceList = shallowRef([])
const selectedInterfaceIdx = ref(null)

// Picker groups, one per site in the order the server sorts them (sites A-Z, no site last).
const deviceGroups = computed(() => {
  const groups = []
  for (const d of flowExporters.value) {
    const label = d.site || 'No site'
    let group = groups.find((g) => g.label === label)
    if (!group) groups.push((group = { label, devices: [] }))
    group.devices.push(d)
  }
  return groups
})

// A router polled over SNMP that sends no flows still has WAN traffic, from its counters.
function deviceOptionLabel(d) {
  return d.snmp_polled && !d.sends_flows ? `${d.hostname} (SNMP)` : d.hostname
}

const allDevicesLabel = computed(() => {
  if (!filterState.isActive.value) return 'All devices'
  const n = flowExporters.value.length
  return n === 1 ? 'The 1 device in the filter' : `All ${n} devices in the filter`
})

const showUnregistered = ref(false)
const unregisteredLabel = computed(() => {
  const n = unmatchedExporters.value.length
  return `${n} unregistered source${n === 1 ? '' : 's'} sending flows`
})

function unregisteredDetail(u) {
  const parts = []
  if (u.flows_per_sec !== null && u.flows_per_sec !== undefined) parts.push(`${formatNumber(u.flows_per_sec)} flows/s`)
  if (u.last_seen) parts.push(`last seen ${new Date(u.last_seen).toLocaleString()}`)
  return parts.join(' · ')
}

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
  wan_interfaces_count: 0,
  wan_in_bps: 0,
  wan_out_bps: 0,
  wan_in_pps: 0,
  wan_out_pps: 0,
  wan_links: [],
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
// Zones with no traffic are hidden: "External / Transit" read 0% on every branch router (O6).
const visibleZones = computed(() => trafficZones.value.filter((z) => z.total_bytes > 0))
// DSCP 0 is unmarked (best effort). The tab counts marked classes only, so a network that marks
// nothing no longer shows "QoS (1)" with nothing to look at (Q1).
const markedQosClasses = computed(() => qosClasses.value.filter((q) => q.dscp > 0))

function siteLabel(sites) {
  if (!sites || !sites.length) return '—'
  return sites.length <= 2 ? sites.join(', ') : `${sites[0]} +${sites.length - 1}`
}

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
  { value: 'interfaces', label: 'Interfaces' },
  { value: 'conversations', label: 'Conversations' },
  { value: 'qos', label: markedQosClasses.value.length ? `QoS (${markedQosClasses.value.length})` : 'QoS' },
  {
    value: 'security',
    label: anomalies.value?.conversations ? `Security (${anomalies.value.conversations})` : 'Security',
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

const zonesHint = 'Monitored endpoints and private (RFC 1918) addresses count as internal'

const scopeLabel = computed(() => {
  if (selectedDeviceId.value) {
    const exp = flowExporters.value.find((e) => e.id === selectedDeviceId.value)
    return exp ? exp.hostname : 'selected device'
  }
  if (!filterState.isActive.value) return 'all devices'
  const n = flowExporters.value.length
  return `${n} device${n === 1 ? '' : 's'} in the filter`
})

// WAN edge: interfaces marked as WAN (by an admin or SNMP), which count each packet once.
// The ingress/egress/transit tiles were removed (review P3): "transit" there meant "no monitored
// host", so users' PCs made it the biggest number on the page. The four directions of the
// Traffic zones rule are in the chart's "All flows" mode.
const hasWan = computed(() => (overview.value.wan_interfaces_count || 0) > 0)

// The "Now" area's data (GET /bandwidth/wan-links) and the Security tiles' (the picked window).
const wanLinks = ref(null)
const anomalies = ref(null)

const WINDOW_WORDS = { '1h': 'hour', '6h': '6 hours', '24h': '24 hours', '7d': '7 days', '30d': '30 days' }
const windowWords = computed(() => WINDOW_WORDS[selectedWindow.value] || selectedWindow.value)

// Routers that export flows but sent none in the last five minutes (review O4: the status line
// turns amber). Picker devices that only answer SNMP are not expected to send any.
// The routers the status line speaks for: the picked device, else every one in the filter. The
// flow count beside it is for the same scope ("3 of 3 routers" next to one router's flows read
// as if the device picker did nothing).
const statusRouters = computed(() =>
  flowExporters.value.filter((d) => d.sends_flows && (!selectedDeviceId.value || d.id === selectedDeviceId.value)))
const stoppedRouters = computed(() => statusRouters.value.filter((d) => !(d.flows_per_sec > 0)))

function shortTime(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : ''
}

// One quiet line under the links (review O4) for what used to be three tiles: packet size,
// routers sending, flows. Same five minutes as the links.
const statusParts = computed(() => {
  const o = overview.value
  const parts = []
  const exporting = statusRouters.value.length
  const sending = Math.max(0, exporting - stoppedRouters.value.length)
  const routers = { text: `${sending} of ${exporting} router${exporting === 1 ? '' : 's'} sending flows` }
  if (stoppedRouters.value.length) {
    routers.text += ` · stopped: ${stoppedRouters.value.map((d) => d.hostname).join(', ')}`
    routers.title = stoppedRouters.value
      .map((d) => (d.last_flow_time ? `${d.hostname}: last flows ${shortTime(d.last_flow_time)}` : `${d.hostname}: none in 24 hours`))
      .join('\n')
  }
  parts.push(routers)
  parts.push({ text: `${formatNumber(o.total_flows_count || 0)} flows` })
  if (o.avg_packet_size) {
    parts.push({ text: `avg packet ${o.avg_packet_size} B (${getPacketProfileLabel(o.avg_packet_size).toLowerCase()})` })
  }
  const unregistered = unmatchedExporters.value.length
  if (unregistered && !filterState.isActive.value && !selectedDeviceId.value) parts.push({ text: `${unregistered} unregistered` })
  return parts
})

// Chart totals: WAN edge by default once WAN interfaces exist; "all" is every conversation the
// routers saw, counted once, split by the Traffic zones rule.
const chartTotals = ref(route.query.totals === 'all' ? 'all' : 'wan')
const totalsOptions = [
  { value: 'wan', label: 'WAN edge', title: 'Traffic on WAN-role interfaces, counted once' },
  { value: 'all', label: 'All flows', title: 'Every conversation the routers saw, counted once: inbound, outbound, internal and transit' },
]
const wantWanSeries = computed(() => chartTotals.value === 'wan' && selectedInterfaceIdx.value === null)
const showTotalsToggle = computed(() => selectedInterfaceIdx.value === null && (hasWan.value || seriesMeta.value.wanOnly))

// Chart layout (step 5, review O2): one small chart per WAN link by default; "All links
// together" is the single combined chart with its WAN edge / All flows switch. Kept in the URL.
const chartLayout = ref(route.query.chart === 'together' ? 'together' : 'links')
// No titles: SegmentedControl gives a title as the button's name, and the visible label is clearer.
const layoutOptions = [
  { value: 'links', label: 'Per link' },
  { value: 'together', label: 'All links together' },
]
const hasLinkRows = computed(() => (wanLinks.value?.links || []).length > 0)
const showLayoutToggle = computed(() => selectedInterfaceIdx.value === null && hasLinkRows.value)
const perLinkCharts = computed(() => showLayoutToggle.value && chartLayout.value === 'links')
// The Now request carries the charts only while they are on screen.
const wantLinkSeries = computed(() => activeView.value === 'overview' && chartLayout.value === 'links')
const linkSeriesReady = computed(() => wanLinks.value?.window === selectedWindow.value)

const linkChartsHint = computed(() => {
  const res = ['1h', '6h'].includes(selectedWindow.value) ? '1-minute' : '1-hour'
  const peak = res === '1-hour' ? ' · dashed: each hour\'s busiest minute' : ''
  return `Last ${selectedWindow.value} · ${res} averages${peak} · shaded: usual for that hour · red: link down (SNMP)`
})

async function fetchWanLinks() {
  try {
    const res = await getWanLinks(scope.value, wantLinkSeries.value ? selectedWindow.value : null)
    wanLinks.value = res.data?.data || null
  } catch (err) {
    console.error('Failed to load WAN links:', err)
  }
}

function setChartLayout(value) {
  chartLayout.value = value === 'together' ? 'together' : 'links'
  replaceQuery({ chart: chartLayout.value === 'together' ? 'together' : undefined })
  if (wantLinkSeries.value && !linkSeriesReady.value) fetchWanLinks()
}

// Back on the Overview from another tab: the charts' series weren't fetched there.
watch(activeView, () => {
  if (wantLinkSeries.value && !linkSeriesReady.value) fetchWanLinks()
})

function setChartTotals(value) {
  chartTotals.value = value === 'all' ? 'all' : 'wan'
  replaceQuery({ totals: chartTotals.value === 'all' ? 'all' : undefined })
  fetchTrafficSeriesData()
}

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

// Picks one device ('' for all in scope). The URL change reloads the page data (watch below).
function setDevice(id) {
  replaceQuery({ device_id: id || undefined, exporter_id: undefined })
}

async function fetchTrafficSeriesData() {
  try {
    const seriesRes = await getTrafficSeries(selectedWindow.value, scope.value, null, selectedInterfaceIdx.value, wantWanSeries.value)
    applySeries(seriesRes.data?.data)
  } catch (err) {
    console.error('Failed to load traffic series for interface isolation:', err)
  }
}

const crossDeviceHint = computed(() => {
  const devices = new Set(interfaceList.value.map((i) => i.exporter_id)).size
  return `${interfaceList.value.length} interfaces on ${devices} device${devices === 1 ? '' : 's'} · Now: last 5 complete minutes · Status from SNMP where a router is polled · Click a row to open its device`
})

// A row across devices: open its device on the Interfaces tab with that interface isolated in the
// chart. The device change reloads the page (watch below), which clears any isolation, so the
// interface is isolated once that reload is done.
let pendingIsolation = null
function openDeviceInterface(item) {
  pendingIsolation = item.interface_idx
  setDevice(String(item.exporter_id))
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
    // The new device has no site or tags yet: show it on its own.
    if (res.data?.data?.id) {
      filterState.clearFilter()
      setDevice(res.data.data.id)
    } else {
      await refreshData()
    }
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
// Shown inside the interface modal, next to the Save button that caused it.
const interfaceError = ref(null)
// Shown at the top of the page for actions that have no modal open.
const actionError = ref(null)

// The router being edited: the picked device from the Interfaces tab, or any router in scope
// from a "Set speed" in the Now area, without changing the page's scope.
const editingDeviceId = ref(null)
const editingDeviceName = ref('')

function openInterfaceModal(focusIdx = null, interfaces = null, deviceId = null, deviceName = '') {
  const current = interfaces || interfaceList.value || []
  let rows = current.map((item) => rowFromTelemetry(item))

  if (rows.length === 0) {
    rows = [editRow(1, 'GigabitEthernet0/1', null, 'lan')]
  }

  editingDeviceId.value = deviceId || selectedDeviceId.value || null
  editingDeviceName.value = deviceName
  editingInterfaces.value = rows
  showInterfaceModal.value = true
  // Put the cursor on the link's speed (a "Set speed" or a row's edit button).
  if (focusIdx !== null && typeof focusIdx === 'number') {
    const at = rows.findIndex((r) => Number(r.index) === focusIdx)
    if (at >= 0) nextTick(() => document.getElementById(`iface-preset-${at}`)?.focus())
  }
}

// "Set speed" on a link with none (review O1): the existing editor, for that link's router.
async function openSpeedEditor(link) {
  actionError.value = null
  try {
    const res = link.exporter_id === selectedDeviceId.value
      ? { data: { data: { interfaces: interfaceList.value } } }
      : await getInterfaceTelemetry(link.exporter_id, selectedWindow.value)
    openInterfaceModal(link.interface_idx, res.data?.data?.interfaces || [], link.exporter_id, link.exporter_hostname)
  } catch (err) {
    console.error('Failed to load interfaces for the speed editor:', err)
    actionError.value = err.response?.data?.detail || "Could not load the router's interfaces."
  }
}

function addInterfaceRow() {
  const nextIdx = editingInterfaces.value.length + 1
  editingInterfaces.value.push(editRow(nextIdx, `GigabitEthernet0/${nextIdx}`, null, 'lan'))
}

function removeInterfaceRow(index) {
  editingInterfaces.value.splice(index, 1)
}

function onPresetChange(row) {
  applyPreset(row)
}

async function saveInterfaces() {
  const deviceId = editingDeviceId.value || selectedDeviceId.value
  if (!deviceId) return
  savingInterfaces.value = true
  interfaceError.value = null
  try {
    const payload = aliasesPayload(editingInterfaces.value)
    await updateEndpointInterfaces(deviceId, payload)
    showInterfaceModal.value = false
    await refreshData()
  } catch (err) {
    console.error('Failed to update interfaces:', err)
    interfaceError.value = err.response?.data?.detail || 'Failed to update interfaces.'
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

  if (seriesMeta.value.wanOnly) {
    if (chartMetricMode.value === 'pps') return 'WAN edge packet rate'
    if (chartMetricMode.value === 'fps') return 'WAN edge flow rate'
    return 'WAN edge throughput'
  }
  if (chartMetricMode.value === 'pps') return 'Packet Rate Telemetry (pps)'
  if (chartMetricMode.value === 'fps') return 'Flow Burst Rate Telemetry (fps)'
  return 'Aggregated Flow Throughput'
})

// Chart Data
const seriesPoints = shallowRef([])
const seriesMeta = ref({ bucket_seconds: 60, directional: true, wanOnly: false })

function applySeries(data) {
  if (!data?.points) return
  seriesPoints.value = data.points
  seriesMeta.value = {
    bucket_seconds: data.bucket_seconds || 60,
    directional: data.directional !== false,
    wanOnly: data.wan_only === true,
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
  const scope = seriesMeta.value.wanOnly ? ' · WAN-role interfaces only' : ''
  return `Last ${selectedWindow.value} · ${res} averages · gaps mean no data${direction}${scope}`
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
  if (seriesMeta.value.wanOnly) {
    datasets.push(lineDataset(`WAN in (${unit})`, pick(`ingress_${unit}`), series.in))
    datasets.push(lineDataset(`WAN out (${unit})`, pick(`egress_${unit}`), series.out))
  } else if (seriesMeta.value.directional) {
    // The Traffic zones rule: internal = a monitored host or a private address.
    datasets.push(lineDataset(`${prefix}Inbound (${unit})`, pick(`ingress_${unit}`), series.in))
    datasets.push(lineDataset(`${prefix}Outbound (${unit})`, pick(`egress_${unit}`), series.out))
    for (const [key, label, color] of [['internal', 'Internal', series.internal], ['transit', 'Transit', series.transit]]) {
      const values = pick(`${key}_${unit}`)
      if (values.some((v) => v)) datasets.push(lineDataset(`${label} (${unit})`, values, color, { fill: false }))
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
    : seriesMeta.value.wanOnly
      ? [[`ingress_${unit}`, 'WAN in'], [`egress_${unit}`, 'WAN out']]
      : seriesMeta.value.directional
      ? [[`ingress_${unit}`, 'Inbound'], [`egress_${unit}`, 'Outbound'], [`internal_${unit}`, 'Internal'], [`transit_${unit}`, 'Transit']]
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
  replaceQuery({ window: w === '1h' ? undefined : w })
  try {
    sessionStorage.setItem(WINDOW_STORAGE_KEY, w)
  } catch {
    // storage unavailable: the URL still holds it
  }
  refreshData()
}

function getPacketProfileLabel(avgBytes) {
  const b = Number(avgBytes) || 0
  if (b >= 1100) return 'Bulk / Jumbo MTU'
  if (b <= 150) return 'Control / Micro-Flows'
  return 'Mixed / Standard MTU'
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
    filterState.clearFilter()
    setDevice(targetId)
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
    // The Security tiles' summary is fetched beside the rest; a window the conversation tables
    // don't cover (1y) gives an error there, shown as empty tiles rather than failing the page.
    const anomaliesReq = getAnomalySummary(selectedWindow.value, scope.value).catch(() => null)
    const [ovRes, wanRes, anomaliesRes, seriesRes, talkersRes, appRes, expRes, epListRes, ifRes] = await Promise.all([
      getBandwidthOverview(scope.value),
      getWanLinks(scope.value, wantLinkSeries.value ? selectedWindow.value : null),
      anomaliesReq,
      getTrafficSeries(selectedWindow.value, scope.value, null, selectedInterfaceIdx.value, wantWanSeries.value),
      getTopTalkers(selectedWindow.value, 10, null, scope.value),
      getApplicationDistribution(selectedWindow.value, scope.value),
      getFlowExporters(filterParams(filterState.filter.value)),
      getEndpoints(),
      // One device's interfaces, or every router's in scope (step 6).
      getInterfaces(scope.value, selectedWindow.value),
    ])

    if (ovRes.data?.data) {
      overview.value = ovRes.data.data
    }
    wanLinks.value = wanRes.data?.data || null
    anomalies.value = anomaliesRes?.data?.data || null
    applySeries(seriesRes.data?.data)
    if (talkersRes.data?.data) {
      topEndpoints.value = talkersRes.data.data.top_endpoints || []
    }
    if (appRes.data?.data) {
      applicationStats.value = appRes.data.data.applications || []
      qosClasses.value = appRes.data.data.qos_classes || []
      trafficZones.value = appRes.data.data.traffic_zones || []
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
    lastUpdated.value = Date.now()
  } catch (err) {
    console.error('Failed to load bandwidth telemetry data:', err)
  } finally {
    loading.value = false
  }
}

watch(
  () => route.query.device_id || route.query.exporter_id || '',
  (newId) => {
    if (newId !== selectedDeviceId.value) {
      selectedDeviceId.value = newId
      selectedInterfaceIdx.value = null
      const isolate = pendingIsolation
      pendingIsolation = null
      refreshAll().then(() => {
        if (isolate !== null && newId) toggleInterfaceIsolation(isolate)
      })
    }
  }
)

// A new filter: reload everything for the routers it picks. A device the filter no longer
// covers is let go, so the page doesn't sit empty under a picker that can't show the device.
watch(
  () => JSON.stringify(filterState.filter.value),
  async () => {
    await refreshAll()
    if (selectedDeviceId.value && !flowExporters.value.some((d) => d.id === selectedDeviceId.value)) {
      setDevice('')
    }
  }
)

// Back and forward between windows, or a pasted link, change the URL under the page.
watch(
  () => route.query.window,
  (w) => {
    const next = windowOptions.includes(w) ? w : '1h'
    if (next !== selectedWindow.value) {
      selectedWindow.value = next
      refreshData()
    }
  }
)

// Escape closes the modal; reopening it starts without the last attempt's message.
useEscapeClose(showMappingModal, () => { showMappingModal.value = false })
useEscapeClose(showEnrollModal, () => { showEnrollModal.value = false })
useEscapeClose(showInterfaceModal, () => { showInterfaceModal.value = false })
watch(showInterfaceModal, () => { interfaceError.value = null })

onMounted(() => {
  // Came back through a link without the window: show the remembered one in the URL too.
  if (!route.query.window && selectedWindow.value !== '1h') {
    replaceQuery({ window: selectedWindow.value })
  }
  // An old link's exporter_id becomes device_id.
  if (route.query.exporter_id && !route.query.device_id) {
    replaceQuery({ device_id: route.query.exporter_id, exporter_id: undefined })
  }
  refreshData()
  scheduleNextUpdate()
  clockTimer = setInterval(() => { nowTick.value = Date.now() }, 5000)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  clearTimeout(updateTimer)
  clearInterval(clockTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
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

/* KPI Metric Strip */
/* Now area's status line (review O4): quiet, amber when a router stopped sending flows. */
.now-status-line {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 1rem;
  margin: 0;
  padding: 0.5rem 1rem 0.6rem;
  border-top: 1px solid var(--border-color);
  color: var(--text-muted);
}
.now-status-line.is-warn {
  color: var(--status-text-warn);
}
/* Separates the five-minute "Now" from everything that follows the time window (review P1). */
.window-heading {
  margin: 0.5rem 0 0.5rem;
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text-primary);
}
.modal-device {
  font-weight: 400;
  color: var(--text-muted);
}

.kpi-scope-note {
  margin: 0 0 0.5rem;
  font-size: var(--text-xs);
  color: var(--text-muted);
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
  font-size: 0.75rem;
}

/* Update status (replaces the old refresh-interval menu) */
.update-status {
  font-variant-numeric: tabular-nums;
}

.pause-toggle {
  margin-left: 8px;
  font-size: var(--text-sm, 13px);
  color: var(--text-secondary);
  text-decoration: underline;
}

/* Unmatched Exporter Grid (BUG-05) */

/* Top Conversations Header & Search */
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
  font-size: 0.75rem;
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

/* Unmatched Scope Focused Callout */

/* Interface Telemetry Panel */
/* Scope: the shared filter bar with the device picker, and unregistered flow sources. */
.traffic-filter-bar {
  margin-bottom: 0.75rem;
}

.unregistered-notice {
  margin-bottom: 1rem;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--status-warn-color);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
}

.unregistered-line {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.unregistered-list {
  list-style: none;
  margin: 0.5rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.unregistered-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
}

.unregistered-actions {
  display: flex;
  gap: 0.375rem;
}

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

.header-actions-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* Index, name, role, speed and remove fit side by side; the global card is 520px. */
.modal-card.wide {
  max-width: 760px;
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

.iface-row-wrap {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.iface-alert-line {
  padding: 0 12px 4px;
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
.interface-config-row .name-col { flex: 1; min-width: 0; }
.interface-config-row .preset-col { width: 170px; }
.interface-config-row .role-col { width: 130px; }
.interface-config-row .speed-col { width: 100px; }
.interface-config-row .remove-col { width: 36px; display: flex; align-items: flex-end; }

.text-accent {
  color: var(--accent-primary, #3b82f6);
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

/* Interface editor rows (v3.3.6). Every row has its own labels, so each field is named for
   screen readers; past the first row they are hidden while the columns line up under the first
   row's. A label never wraps: in a narrow window "Interface Name / Alias" used to wrap into the
   Role label. Below 820 px the name takes a line of its own and every row shows its labels. */
.interface-config-row label {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.interface-config-row .name-col {
  min-width: 8rem;
}
.row-label-repeat {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
@media (max-width: 820px) {
  .interface-config-row {
    flex-wrap: wrap;
  }
  .interface-config-row .name-col {
    flex: 1 1 100%;
    order: -1;
  }
  .row-label-repeat {
    position: static;
    width: auto;
    height: auto;
    clip: auto;
  }
}
</style>
