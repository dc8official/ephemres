<template>
  <div class="topology-container">
    <!-- Map Toolbar -->
    <div class="topology-toolbar">
      <div class="toolbar-left">
        <h2 class="topology-title">
          <span class="icon">🕸</span> Live Network Topology Map
        </h2>
        <span class="status-badge" :class="stabilized ? 'badge-stabilized' : 'badge-stabilizing'">
          {{ stabilized ? (layoutDirection === 'UD' ? '● Layout Fixed (Vertical)' : '● Layout Fixed (Horizontal)') : '◌ Stabilizing Hierarchical Layout...' }}
        </span>
        <span class="sse-indicator" :class="sseConnected ? 'sse-live' : 'sse-connecting'">
          <span class="pulse-dot"></span>
          {{ sseConnected ? 'Live SSE' : 'Reconnecting...' }}
        </span>
        <span v-if="isDiscoveringAll" class="discovery-pill pulse">
          ◌ Discovering fleet routes in background...
        </span>
      </div>
      <div class="toolbar-right">
        <button 
          v-if="isAdmin" 
          class="btn-primary" 
          :disabled="isDiscoveringAll" 
          @click="handleDiscoverAll"
          title="Run throttled background traceroute discovery across all endpoints"
        >
          {{ isDiscoveringAll ? '◌ Discovering Fleet...' : '⚡ Discover Fleet Routes' }}
        </button>
        <button 
          v-if="isAdmin" 
          class="btn-secondary" 
          :disabled="isRebuilding" 
          @click="handleRebuildHierarchy"
          title="Reconstruct in-memory graph from current database baseline routes"
        >
          {{ isRebuilding ? 'Rebuilding...' : '↻ Rebuild Hierarchy' }}
        </button>
        <button class="btn-secondary" @click="toggleLayoutDirection" :title="layoutDirection === 'UD' ? 'Switch to Horizontal (Left-to-Right) view' : 'Switch to Vertical (Top-to-Bottom) view'">
          {{ layoutDirection === 'UD' ? '↔ Horizontal View' : '↕ Vertical View' }}
        </button>
        <button class="btn-secondary" @click="resetView" title="Center and zoom map to fit all nodes">
          🔍 Reset View
        </button>
        <button class="btn-secondary" @click="fetchTopology" :disabled="loading">
          {{ loading ? 'Updating...' : '↻ Refresh Topology' }}
        </button>
      </div>
    </div>

    <!-- Canvas Container -->
    <div class="canvas-wrapper">
      <div ref="container" class="vis-network-canvas"></div>

      <!-- Loading / Error Overlays -->
      <div v-if="loading && !networkInitialized" class="loading-overlay">
        <div class="spinner"></div>
        <p>Loading multi-endpoint merged topology map...</p>
      </div>
      <div v-if="error" class="error-overlay">
        <p class="error-msg">⚠️ {{ error }}</p>
        <button class="btn-primary" @click="fetchTopology">Retry</button>
      </div>

      <!-- Dynamic Legend Overlay -->
      <div class="map-legend">
        <div class="legend-title">Topology Legend</div>
        <div class="legend-items">
          <div class="legend-item">
            <span class="node-icon square state-root"></span>
            <span>Ephemres Engine</span>
            <span class="count-badge glow-root">{{ legendCounts.root }}</span>
          </div>
          <div class="legend-item">
            <span class="node-icon circle state-up"></span>
            <span>Monitored UP</span>
            <span class="count-badge glow-up">{{ legendCounts.up }}</span>
          </div>
          <div class="legend-item">
            <span class="node-icon circle state-unstable"></span>
            <span>Monitored UNSTABLE</span>
            <span class="count-badge glow-unstable">{{ legendCounts.unstable }}</span>
          </div>
          <div class="legend-item">
            <span class="node-icon circle state-down"></span>
            <span>Monitored DOWN</span>
            <span class="count-badge glow-down">{{ legendCounts.down }}</span>
          </div>
          <div class="legend-item">
            <span class="node-icon hexagon state-transit"></span>
            <span>Transit Router</span>
            <span class="count-badge glow-transit">{{ legendCounts.transit }}</span>
          </div>
          <div class="legend-item" v-if="legendCounts.failurePoint > 0">
            <span class="node-icon hexagon state-failure-point"></span>
            <span>Failure Point (RCA)</span>
            <span class="count-badge glow-failure">{{ legendCounts.failurePoint }}</span>
          </div>
          <div class="legend-item" v-if="legendCounts.inferredDown > 0">
            <span class="node-icon hexagon state-inferred-down"></span>
            <span>Transit INFERRED DOWN</span>
            <span class="count-badge glow-inferred">{{ legendCounts.inferredDown }}</span>
          </div>
          <div class="legend-item" v-if="legendCounts.l2 > 0">
            <span class="l2-pill">L2</span>
            <span>Layer 2 Segment</span>
            <span class="count-badge">{{ legendCounts.l2 }}</span>
          </div>
          <div class="legend-item" v-if="legendCounts.subnet > 0">
            <span class="node-icon box state-subnet"></span>
            <span>Subnet Hub (L2)</span>
            <span class="count-badge glow-subnet">{{ legendCounts.subnet }}</span>
          </div>
        </div>
      </div>

      <!-- Node Inspector Side Drawer -->
      <div class="inspector-drawer" :class="{ open: selectedNode !== null }">
        <div class="drawer-header" v-if="selectedNode">
          <div>
            <h3 class="drawer-title">{{ selectedNode.label }}</h3>
            <p class="drawer-sub">{{ selectedNode.ip_address }}</p>
          </div>
          <button class="btn-close" @click="selectedNode = null">✕</button>
        </div>

        <div class="drawer-body" v-if="selectedNode">
          <!-- Node Meta Details Card -->
          <div class="meta-card">
            <div class="meta-row">
              <span class="meta-label">Node Category:</span>
              <span class="meta-value badge" :class="selectedNode.node_type || selectedNode.type">
                {{ formatNodeType(selectedNode.node_type || selectedNode.type) }}
              </span>
            </div>
            <div class="meta-row">
              <span class="meta-label">Operational Status:</span>
              <span class="status-pill" :class="getStatusClass(selectedNode.status || selectedNode.state)">
                {{ selectedNode.status || selectedNode.state }}
              </span>
            </div>
            <div class="meta-row" v-if="selectedNode.is_l2_segment || selectedNode.device_type === 'L2_SEGMENT'">
              <span class="meta-label">Segment Type:</span>
              <span class="meta-value text-blue">Layer 2 Broadcast Segment</span>
            </div>
            <div class="meta-row" v-if="selectedNode.subnet">
              <span class="meta-label">Subnet / VLAN:</span>
              <span class="meta-value text-blue">{{ selectedNode.subnet }}</span>
            </div>
            <div class="meta-row" v-if="selectedNode.device_type">
              <span class="meta-label">Device Type:</span>
              <span class="meta-value">{{ selectedNode.device_type }}</span>
            </div>
          </div>

          <!-- Single Endpoint Route Refresh Action -->
          <div v-if="selectedNode.endpoint_id && isAdmin" class="drawer-actions-card">
            <button 
              class="btn-secondary full-width" 
              :disabled="isRefreshingSingle" 
              @click="handleRefreshSingleRoute(selectedNode.endpoint_id)"
              title="Execute traceroute and re-index upstream path for this endpoint"
            >
              {{ isRefreshingSingle ? '◌ Tracing Endpoint Route...' : '↻ Refresh Endpoint Route' }}
            </button>
          </div>

          <!-- Embedded RCA Diagnostics Component for Monitored Nodes with Endpoint ID -->
          <div v-if="selectedNode.endpoint_id" class="drawer-rca-section">
            <EndpointRcaDetail :endpointId="selectedNode.endpoint_id" />
          </div>

          <!-- Diagnostic Traceroute for Transit or unlinked Nodes -->
          <div v-else class="traces-section">
            <h4 class="section-title">
              <span>🩺 Transit Node Diagnostics</span>
            </h4>
            <div class="empty-trace">
              <p>Transit router node automatically inferred from traceroute discovery paths.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Network } from 'vis-network'
import { DataSet } from 'vis-data'
import { getTopology, rebuildTopology, discoverAllTopologyRoutes, refreshEndpointBaseline } from '../services/api.js'
import { isAdmin } from '../services/auth.js'
import EndpointRcaDetail from './EndpointRcaDetail.vue'
import { useSSE } from '../composables/useSSE.js'

const container = ref(null)
const loading = ref(true)
const error = ref(null)
const networkInitialized = ref(false)
const stabilized = ref(false)
const layoutDirection = ref('UD')
const { sseConnected, subscribe, subscribeBatched } = useSSE()
let unsubscribeSSE = null

const selectedNode = ref(null)
const isDiscoveringAll = ref(false)
const isRebuilding = ref(false)
const isRefreshingSingle = ref(false)

async function handleDiscoverAll() {
  if (isDiscoveringAll.value) return
  isDiscoveringAll.value = true
  try {
    await discoverAllTopologyRoutes()
  } catch (err) {
    console.error('Failed to trigger fleet discovery:', err)
    isDiscoveringAll.value = false
  }
}

async function handleRebuildHierarchy() {
  if (isRebuilding.value) return
  isRebuilding.value = true
  try {
    await rebuildTopology()
    await fetchTopology()
  } catch (err) {
    console.error('Failed to rebuild topology hierarchy:', err)
  } finally {
    isRebuilding.value = false
  }
}

async function handleRefreshSingleRoute(endpointId) {
  if (!endpointId || isRefreshingSingle.value) return
  isRefreshingSingle.value = true
  try {
    await refreshEndpointBaseline(endpointId)
    await fetchTopology()
  } catch (err) {
    console.error('Failed to refresh route for endpoint:', err)
  } finally {
    isRefreshingSingle.value = false
  }
}

const legendCounts = reactive({
  root: 1,
  up: 0,
  unstable: 0,
  down: 0,
  transit: 0,
  failurePoint: 0,
  inferredDown: 0,
  l2: 0,
  subnet: 0,
})

let network = null
let nodesDataSet = null
let edgesDataSet = null

// Dark theme monitoring
const isDark = ref(true)
let themeObserver = null

function getCssVar(name, fallback = '') {
  if (typeof window === 'undefined') return fallback
  const val = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return val || fallback
}

watch(isDark, () => {
  if (nodesDataSet) {
    const updatedNodes = nodesDataSet.get().map(node => {
      const nodeType = node.rawNode?.node_type || node.rawNode?.type
      const nodeStatus = node.rawNode?.status || node.rawNode?.state
      return {
        ...node,
        color: getNodeColors(nodeStatus, nodeType),
        font: { ...node.font, color: getNodeFontColor(nodeStatus, nodeType) }
      }
    })
    nodesDataSet.update(updatedNodes)
  }
  if (edgesDataSet) {
    const edgeStroke = getCssVar('--topo-edge-stroke', isDark.value ? '#475569' : '#94A3B8')
    const edgeHighlight = getCssVar('--topo-edge-highlight', isDark.value ? '#60A5FA' : '#2563EB')
    const updatedEdges = edgesDataSet.get().map(edge => ({
      ...edge,
      color: { color: edgeStroke, highlight: edgeHighlight }
    }))
    edgesDataSet.update(updatedEdges)
  }
})

function formatNodeType(type) {
  if (type === 'root') return 'Ephemres Engine (Root)'
  if (type === 'monitored') return 'Monitored Target'
  if (type === 'subnet_hub') return 'Subnet Hub (L2)'
  return 'Transit Router'
}

function updateLegendCounts() {
  if (!nodesDataSet) return
  const allNodes = nodesDataSet.get()
  let root = 0, up = 0, unstable = 0, down = 0, transit = 0, failurePoint = 0, inferredDown = 0, l2 = 0, subnet = 0

  for (const n of allNodes) {
    const raw = n.rawNode || {}
    const type = raw.type || raw.node_type
    const status = raw.status || raw.state

    if (type === 'root' || n.shape === 'square') root++
    if (type === 'subnet_hub' || n.shape === 'box') subnet++
    if (raw.is_l2_segment) l2++

    if (status === 'UP-UNSTABLE' || status === 'DOWN-UNSTABLE') unstable++
    if (status === 'FAILURE_POINT') failurePoint++
    if (status === 'INFERRED_DOWN') inferredDown++

    if (type === 'monitored' || n.shape === 'dot') {
      if (status === 'UP') up++
      else if (status === 'DOWN') down++
    }
    if ((type === 'transit' || n.shape === 'hexagon') && status !== 'FAILURE_POINT' && status !== 'INFERRED_DOWN') {
      transit++
    }
  }

  legendCounts.root = root
  legendCounts.up = up
  legendCounts.unstable = unstable
  legendCounts.down = down
  legendCounts.transit = transit
  legendCounts.failurePoint = failurePoint
  legendCounts.inferredDown = inferredDown
  legendCounts.l2 = l2
  legendCounts.subnet = subnet
}

// Visual Node Styling & Categorization
function getNodeColors(status, nodeType) {
  const isDarkMode = isDark.value
  if (nodeType === 'root') {
    const bg = getCssVar('--topo-node-root-bg', isDarkMode ? '#1D4ED8' : '#2563EB')
    const border = getCssVar('--topo-node-root-border', isDarkMode ? '#3B82F6' : '#1D4ED8')
    const highlightBorder = getCssVar('--topo-edge-highlight', isDarkMode ? '#60A5FA' : '#3B82F6')
    return {
      background: bg,
      border: border,
      highlight: { background: bg, border: highlightBorder }
    }
  }
  if (nodeType === 'subnet_hub') {
    const bg = getCssVar('--topo-node-subnet-bg', isDarkMode ? '#1E293B' : '#EEF2FF')
    const border = getCssVar('--topo-node-subnet-border', '#6366F1')
    return {
      background: bg,
      border: border,
      highlight: { background: bg, border: border }
    }
  }

  if (nodeType === 'transit') {
    if (status === 'FAILURE_POINT') {
      const bg = getCssVar('--topo-node-failure-bg', isDarkMode ? '#991B1B' : '#FEE2E2')
      const border = getCssVar('--topo-node-failure-border', isDarkMode ? '#F97316' : '#DC2626')
      return {
        background: bg,
        border: border,
        highlight: { background: bg, border: border }
      }
    }
    if (status === 'INFERRED_DOWN') {
      const bg = getCssVar('--topo-node-inferred-bg', isDarkMode ? '#7F1D1D' : '#FEF2F2')
      const border = getCssVar('--topo-node-inferred-border', isDarkMode ? '#DC2626' : '#EF4444')
      return {
        background: bg,
        border: border,
        highlight: { background: bg, border: border }
      }
    }
    const bg = getCssVar('--topo-node-transit-bg', isDarkMode ? '#374151' : '#F1F5F9')
    const border = getCssVar('--topo-node-transit-border', isDarkMode ? '#6B7280' : '#64748B')
    return {
      background: bg,
      border: border,
      highlight: { background: bg, border: getCssVar('--topo-edge-highlight', '#3B82F6') }
    }
  }

  // Monitored nodes
  switch (status) {
    case 'UP':
      return {
        background: getCssVar('--topo-node-up-bg', isDarkMode ? '#064E3B' : '#ECFDF5'),
        border: getCssVar('--topo-node-up-border', isDarkMode ? '#10B981' : '#16A34A'),
        highlight: { 
          background: getCssVar('--topo-node-up-bg', isDarkMode ? '#064E3B' : '#ECFDF5'), 
          border: getCssVar('--topo-edge-highlight', isDarkMode ? '#60A5FA' : '#2563EB') 
        }
      }
    case 'UP-UNSTABLE':
    case 'DOWN-UNSTABLE':
      return {
        background: getCssVar('--topo-node-unstable-bg', isDarkMode ? '#78350F' : '#FFFBEB'),
        border: getCssVar('--topo-node-unstable-border', isDarkMode ? '#F59E0B' : '#D97706'),
        highlight: { 
          background: getCssVar('--topo-node-unstable-bg', isDarkMode ? '#78350F' : '#FFFBEB'), 
          border: getCssVar('--topo-edge-highlight', isDarkMode ? '#60A5FA' : '#2563EB') 
        }
      }
    case 'DOWN':
      return {
        background: getCssVar('--topo-node-down-bg', isDarkMode ? '#7F1D1D' : '#FEF2F2'),
        border: getCssVar('--topo-node-down-border', isDarkMode ? '#EF4444' : '#DC2626'),
        highlight: { 
          background: getCssVar('--topo-node-down-bg', isDarkMode ? '#7F1D1D' : '#FEF2F2'), 
          border: getCssVar('--topo-edge-highlight', isDarkMode ? '#60A5FA' : '#2563EB') 
        }
      }
    default:
      return {
        background: isDarkMode ? '#1F2937' : '#F3F4F6',
        border: isDarkMode ? '#6B7280' : '#9CA3AF',
        highlight: { 
          background: isDarkMode ? '#374151' : '#E5E7EB', 
          border: getCssVar('--topo-edge-highlight', '#3B82F6') 
        }
      }
  }
}

function getNodeFontColor(status, nodeType) {
  const isDarkMode = isDark.value
  if (nodeType === 'root') {
    return getCssVar('--topo-node-root-text', '#FFFFFF')
  }
  if (nodeType === 'subnet_hub') {
    return getCssVar('--topo-node-subnet-text', isDarkMode ? '#E0E7FF' : '#1E1B4B')
  }
  if (nodeType === 'transit') {
    if (status === 'FAILURE_POINT') return getCssVar('--topo-node-failure-text', isDarkMode ? '#FEE2E2' : '#7F1D1D')
    if (status === 'INFERRED_DOWN') return getCssVar('--topo-node-inferred-text', isDarkMode ? '#FCA5A5' : '#991B1B')
    return getCssVar('--topo-node-transit-text', isDarkMode ? '#F3F4F6' : '#0F172A')
  }
  switch (status) {
    case 'UP': return getCssVar('--topo-node-up-text', isDarkMode ? '#ECFDF5' : '#064E3B')
    case 'UP-UNSTABLE':
    case 'DOWN-UNSTABLE': return getCssVar('--topo-node-unstable-text', isDarkMode ? '#FEF3C7' : '#78350F')
    case 'DOWN': return getCssVar('--topo-node-down-text', isDarkMode ? '#FEE2E2' : '#7F1D1D')
    default: return getCssVar('--text-primary', isDarkMode ? '#F3F4F6' : '#111111')
  }
}

function getStatusClass(status) {
  switch (status) {
    case 'UP': return 'status-up'
    case 'UP-UNSTABLE':
    case 'DOWN-UNSTABLE': return 'status-unstable'
    case 'DOWN': return 'status-down'
    case 'FAILURE_POINT': return 'status-failure-point'
    case 'INFERRED_DOWN': return 'status-inferred-down'
    default: return ''
  }
}

function computeDagLevels(nodesData, edgesData) {
  const nodeIds = new Set(nodesData.map(n => n.id))
  const adj = {}
  const inDegree = {}

  nodeIds.forEach(id => {
    adj[id] = []
    inDegree[id] = 0
  })

  edgesData.forEach(edge => {
    if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
      adj[edge.source].push(edge.target)
      inDegree[edge.target] = (inDegree[edge.target] || 0) + 1
    }
  })

  const levels = {}
  const queue = []

  nodeIds.forEach(id => {
    if (inDegree[id] === 0 || id === 'root') {
      levels[id] = 0
      queue.push(id)
    }
  })

  while (queue.length > 0) {
    const u = queue.shift()
    const uLevel = levels[u] || 0
    const children = adj[u] || []
    for (const v of children) {
      const candLevel = uLevel + 1
      if (levels[v] === undefined || candLevel > levels[v]) {
        levels[v] = candLevel
      }
      inDegree[v] -= 1
      if (inDegree[v] <= 0) {
        queue.push(v)
      }
    }
  }

  nodeIds.forEach(id => {
    if (levels[id] === undefined) {
      levels[id] = id === 'root' ? 0 : 1
    }
  })

  return levels
}

function formatVisData(nodesData, edgesData) {
  const dagLevels = computeDagLevels(nodesData, edgesData)

  const visNodes = nodesData.map(node => {
    const nodeType = node.node_type || node.type
    const nodeStatus = node.status || node.state
    const colors = getNodeColors(nodeStatus, nodeType)
    const isL2 = node.is_l2_segment || node.device_type === 'L2_SEGMENT'
    const computedLevel = node.level !== undefined ? node.level : (dagLevels[node.id] ?? 0)

    let shape = 'dot'
    let size = 24

    if (nodeType === 'root') {
      shape = 'square'
      size = 28
    } else if (nodeType === 'subnet_hub') {
      shape = 'box'
      size = 20
    } else if (nodeType === 'transit') {
      shape = 'hexagon'
      size = 18
    }

    let labelText = `${node.label}\n(${node.ip_address || ''})`
    if (nodeType === 'subnet_hub') {
      labelText = `🌐 ${node.label}`
    } else {
      if (isL2) {
        labelText += '\n[L2 Segment]'
      }
      if (nodeStatus === 'FAILURE_POINT') {
        labelText += '\n⚠️ FAILURE POINT'
      }
    }

    return {
      id: node.id,
      label: labelText.trim(),
      shape: shape,
      size: size,
      level: computedLevel,
      color: colors,
      group: node.subnet || 'default',
      font: { color: getNodeFontColor(nodeStatus, nodeType), size: 12, face: 'Inter, sans-serif' },
      rawNode: { ...node, status: nodeStatus, state: nodeStatus }
    }
  })

  const edgeStroke = getCssVar('--topo-edge-stroke', isDark.value ? '#475569' : '#94A3B8')
  const edgeHighlight = getCssVar('--topo-edge-highlight', isDark.value ? '#60A5FA' : '#2563EB')
  const visEdges = edgesData.map(edge => ({
    id: `${edge.source}->${edge.target}`,
    from: edge.source,
    to: edge.target,
    color: { color: edgeStroke, highlight: edgeHighlight },
    arrows: { to: { enabled: true, scaleFactor: 0.7 } },
    smooth: {
      type: 'cubicBezier',
      forceDirection: layoutDirection.value === 'UD' ? 'vertical' : 'horizontal',
      roundness: 0.4
    }
  }))

  return { visNodes, visEdges }
}

function toggleLayoutDirection() {
  layoutDirection.value = layoutDirection.value === 'UD' ? 'LR' : 'UD'
  if (network) {
    stabilized.value = false
    const opts = {
      layout: {
        hierarchical: {
          enabled: true,
          direction: layoutDirection.value,
          sortMethod: 'directed',
          edgeMinimization: true,
          blockShifting: true,
          parentCentralization: true,
          nodeSpacing: 220,
          levelSeparation: 180
        }
      },
      edges: {
        smooth: {
          type: 'cubicBezier',
          forceDirection: layoutDirection.value === 'UD' ? 'vertical' : 'horizontal',
          roundness: 0.4
        }
      },
      physics: {
        enabled: (nodesDataSet ? nodesDataSet.length : 0) <= 250,
        hierarchicalRepulsion: {
          centralGravity: 0.0,
          springLength: 140,
          springConstant: 0.01,
          nodeDistance: 180,
          damping: 0.09
        },
        stabilization: {
          enabled: (nodesDataSet ? nodesDataSet.length : 0) <= 250,
          iterations: (nodesDataSet ? nodesDataSet.length : 0) <= 250 ? 200 : 0
        }
      }
    }
    network.setOptions(opts)
    if ((nodesDataSet ? nodesDataSet.length : 0) <= 250) {
      network.stabilize(200)
    }
  }
}

async function fetchTopology() {
  loading.value = true
  error.value = null
  try {
    const res = await getTopology()
    const rawData = res.data?.data || res.data || {}
    const rawNodes = rawData.nodes || []
    const rawEdges = rawData.edges || []

    const { visNodes, visEdges } = formatVisData(rawNodes, rawEdges)

    if (!networkInitialized.value) {
      nodesDataSet = new DataSet(visNodes)
      edgesDataSet = new DataSet(visEdges)

      await nextTick()

      const isLargeGraph = visNodes.length > 250
      const options = {
        nodes: { borderWidth: 2 },
        edges: { width: 2 },
        layout: {
          hierarchical: {
            enabled: true,
            direction: layoutDirection.value,
            sortMethod: 'directed',
            edgeMinimization: !isLargeGraph,
            blockShifting: true,
            parentCentralization: true,
            nodeSpacing: isLargeGraph ? 180 : 220,
            levelSeparation: isLargeGraph ? 150 : 180
          }
        },
        physics: {
          enabled: !isLargeGraph,
          hierarchicalRepulsion: {
            centralGravity: 0.0,
            springLength: 140,
            springConstant: 0.01,
            nodeDistance: 180,
            damping: 0.09
          },
          stabilization: {
            enabled: !isLargeGraph,
            iterations: isLargeGraph ? 0 : 200
          }
        },
        interaction: {
          hover: true,
          zoomView: true,
          dragView: true
        }
      }

      network = new Network(container.value, { nodes: nodesDataSet, edges: edgesDataSet }, options)

      if (isLargeGraph) {
        stabilized.value = true
      } else {
        network.on('stabilizationIterationsDone', () => {
          network.setOptions({ physics: { enabled: false } })
          stabilized.value = true
        })
      }

      network.on('click', (params) => {
        if (params.nodes && params.nodes.length > 0) {
          const nodeId = params.nodes[0]
          const clickedNodeData = nodesDataSet.get(nodeId)
          if (clickedNodeData && clickedNodeData.rawNode) {
            selectedNode.value = clickedNodeData.rawNode
          }
        }
      })

      networkInitialized.value = true
    } else {
      const currentNodes = new Set(visNodes.map(n => n.id))
      const toRemoveNodes = nodesDataSet.getIds().filter(id => !currentNodes.has(id))
      if (toRemoveNodes.length > 0) nodesDataSet.remove(toRemoveNodes)
      nodesDataSet.update(visNodes)

      const currentEdges = new Set(visEdges.map(e => e.id))
      const toRemoveEdges = edgesDataSet.getIds().filter(id => !currentEdges.has(id))
      if (toRemoveEdges.length > 0) edgesDataSet.remove(toRemoveEdges)
      edgesDataSet.update(visEdges)
    }

    updateLegendCounts()
  } catch (err) {
    console.error('Failed to fetch topology:', err)
    error.value = 'Failed to load live network topology.'
  } finally {
    loading.value = false
  }
}

function resetView() {
  if (network) {
    network.fit({ animation: true })
  }
}

function initSSE() {
  unsubscribeSSE = subscribeBatched((batch) => {
    let needsFetch = false
    const updates = []
    let updatedAny = false

    for (const payload of batch) {
      if (payload?.type === 'TOPOLOGY_UPDATED') {
        needsFetch = true
        continue
      }
      const isNodeStateChange = payload?.type === 'NODE_STATE_CHANGE'
      const nodeId = payload?.endpoint_id || payload?.node_id || payload?.data?.endpoint_id
      if ((isNodeStateChange || isStateTransition) && nodeId && nodesDataSet) {
        const newState = isNodeStateChange
          ? (payload.new_state || payload.operational_state || payload.current_state)
          : (payload.detailed_state || payload.operational_state || payload.current_state || payload.new_state)
        const existing = nodesDataSet.get(nodeId)
        if (existing && newState) {
          const nodeType = existing.rawNode?.node_type || existing.rawNode?.type || 'monitored'
          const colors = getNodeColors(newState, nodeType)
          updates.push({
            id: nodeId,
            color: colors,
            rawNode: {
              ...existing.rawNode,
              status: newState,
              state: newState
            }
          })
          // Update currently inspected node if open
          if (selectedNode.value && selectedNode.value.id === nodeId) {
            selectedNode.value.status = newState
            selectedNode.value.state = newState
          }
          updatedAny = true
        }
      }
    }

    if (needsFetch) {
      isDiscoveringAll.value = false
      fetchTopology()
      return
    }

    if (updates.length > 0 && nodesDataSet) {
      nodesDataSet.update(updates)
    }
    if (updatedAny) {
      updateLegendCounts()
    }
  })
}

onMounted(() => {
  isDark.value = document.documentElement.classList.contains('dark')
  themeObserver = new MutationObserver(() => {
    isDark.value = document.documentElement.classList.contains('dark')
  })
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class']
  })

  fetchTopology()
  initSSE()
})

onUnmounted(() => {
  if (unsubscribeSSE) unsubscribeSSE()
  if (network) network.destroy()
  if (themeObserver) themeObserver.disconnect()
})
</script>

<style scoped>
.topology-container {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 120px);
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  overflow: hidden;
  position: relative;
}

.topology-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  background: var(--bg-surface-selected);
  border-bottom: 1px solid var(--border-color);
  z-index: 10;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.topology-title {
  font-size: 1.2rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-badge {
  font-size: 0.8rem;
  padding: 4px 10px;
  border-radius: 9999px;
}

.badge-stabilized {
  background: var(--color-up-bg);
  color: var(--color-up);
  border: 1px solid rgba(22, 163, 74, 0.35);
}

.badge-stabilizing {
  background: var(--color-up-unstable-bg);
  color: var(--color-up-unstable);
  border: 1px solid rgba(217, 119, 6, 0.35);
}

.sse-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 9999px;
  font-family: var(--font-mono);
}

.sse-live {
  background: var(--color-up-bg);
  color: var(--color-up);
  border: 1px solid rgba(22, 163, 74, 0.35);
}

.sse-connecting {
  background: var(--color-up-unstable-bg);
  color: var(--color-up-unstable);
  border: 1px solid rgba(217, 119, 6, 0.35);
}

.discovery-pill {
  font-size: 0.8rem;
  padding: 4px 10px;
  border-radius: 9999px;
  background: rgba(37, 99, 235, 0.12);
  color: var(--topo-node-root-bg);
  border: 1px solid rgba(37, 99, 235, 0.3);
  font-family: var(--font-mono);
}

.pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: pulse 2s infinite ease-in-out;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.85); }
}

.toolbar-right {
  display: flex;
  gap: 10px;
}

.canvas-wrapper {
  flex: 1;
  position: relative;
  width: 100%;
  height: 100%;
}

.vis-network-canvas {
  width: 100%;
  height: 100%;
}

.loading-overlay, .error-overlay {
  position: absolute;
  inset: 0;
  background: var(--bg-surface);
  opacity: 0.95;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  z-index: 20;
  color: var(--text-primary);
}

.map-legend {
  position: absolute;
  bottom: 20px;
  left: 20px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  padding: 14px 18px;
  border-radius: 8px;
  z-index: 10;
  font-size: 0.85rem;
  color: var(--text-secondary);
  box-shadow: var(--shadow-hover);
}

.legend-title {
  font-weight: 600;
  margin-bottom: 10px;
  color: var(--text-primary);
}

.legend-items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-sans);
}

.node-icon {
  width: 12px;
  height: 12px;
  display: inline-block;
  flex-shrink: 0;
}

.node-icon.square { border-radius: 2px; }
.node-icon.box { border-radius: 2px; }
.node-icon.circle { border-radius: 50%; }
.node-icon.hexagon { clip-path: polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%); }

.state-root { background: var(--topo-node-root-bg); border: 1px solid var(--topo-node-root-border); }
.state-up { background: var(--topo-node-up-bg); border: 1px solid var(--topo-node-up-border); }
.state-unstable { background: var(--topo-node-unstable-bg); border: 1px solid var(--topo-node-unstable-border); }
.state-down { background: var(--topo-node-down-bg); border: 1px solid var(--topo-node-down-border); }
.state-transit { background: var(--topo-node-transit-bg); border: 1px solid var(--topo-node-transit-border); }
.state-failure-point { background: var(--topo-node-failure-bg); border: 1px solid var(--topo-node-failure-border); }
.state-inferred-down { background: var(--topo-node-inferred-bg); border: 1px solid var(--topo-node-inferred-border); }
.state-subnet { background: var(--topo-node-subnet-bg); border: 1px solid var(--topo-node-subnet-border); }

.count-badge {
  margin-left: auto;
  font-size: 0.75rem;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 9999px;
  font-family: var(--font-mono);
  font-feature-settings: "tnum";
}

.glow-root { background: rgba(37, 99, 235, 0.15); color: var(--topo-node-root-bg); }
.glow-up { background: var(--color-up-bg); color: var(--color-up); }
.glow-unstable { background: var(--color-up-unstable-bg); color: var(--color-up-unstable); }
.glow-down { background: var(--color-down-bg); color: var(--color-down); }
.glow-transit { background: var(--color-unknown-bg); color: var(--text-secondary); }
.glow-failure { background: var(--color-down-bg); color: var(--color-down); }
.glow-inferred { background: var(--color-down-bg); color: var(--color-down); }
.glow-subnet { background: rgba(99, 102, 241, 0.15); color: var(--topo-node-subnet-border); }

.l2-pill {
  background: rgba(37, 99, 235, 0.15);
  color: var(--topo-node-root-bg);
  font-size: var(--text-xs);
  font-weight: 700;
  padding: 1px 5px;
  border-radius: var(--radius-sm);
}

/* Inspector Side Drawer */
.inspector-drawer {
  position: absolute;
  top: 0;
  right: 0;
  width: 520px;
  max-width: 90vw;
  height: 100%;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-color);
  box-shadow: -6px 0 24px rgba(0, 0, 0, 0.15);
  z-index: 30;
  transform: translateX(100%);
  transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex;
  flex-direction: column;
}

.inspector-drawer.open {
  transform: translateX(0);
}

.drawer-header {
  padding: 20px;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  background: var(--bg-surface-selected);
}

.drawer-title {
  margin: 0;
  font-size: 1.1rem;
  color: var(--text-primary);
}

.drawer-sub {
  margin: 4px 0 0 0;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.drawer-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}

.meta-card {
  background: var(--bg-surface-selected);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.9rem;
}

.meta-label { color: var(--text-muted); }
.meta-value { color: var(--text-primary); font-weight: 500; }
.meta-value.text-blue { color: var(--topo-node-root-bg); }

.status-pill {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.8rem;
  font-weight: 600;
}

.status-up { background: var(--color-up-bg); color: var(--color-up); border: 1px solid rgba(22, 163, 74, 0.35); }
.status-unstable { background: var(--color-up-unstable-bg); color: var(--color-up-unstable); border: 1px solid rgba(217, 119, 6, 0.35); }
.status-down { background: var(--color-down-bg); color: var(--color-down); border: 1px solid rgba(220, 38, 38, 0.35); }
.status-failure-point { background: var(--topo-node-failure-bg); color: var(--topo-node-failure-text); border: 1px solid var(--topo-node-failure-border); }
.status-inferred-down { background: var(--topo-node-inferred-bg); color: var(--topo-node-inferred-text); border: 1px solid var(--topo-node-inferred-border); }

.drawer-actions-card {
  margin-bottom: 20px;
}

.full-width {
  width: 100%;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--border-color);
  border-top-color: #3B82F6;
  border-radius: 50%;
  animation: spin 1s infinite linear;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
