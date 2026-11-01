import { clearUserState } from './auth.js'

/**
 * Native Zero-Dependency Fetch Client (Vertical Integration Architecture).
 * Replaces axios with pure browser fetch, supporting credentials, JSON serialization,
 * query parameter formatting, Blob responses, and automatic 401 session eviction.
 */
class ApiClient {
  constructor(baseURL = '/api/v1') {
    this.baseURL = baseURL
  }

  async request(method, url, data = null, config = {}) {
    let fullUrl = url.startsWith('/') ? `${this.baseURL}${url}` : `${this.baseURL}/${url}`
    if (config.params) {
      const searchParams = new URLSearchParams()
      for (const [key, value] of Object.entries(config.params)) {
        if (value !== null && value !== undefined) {
          if (Array.isArray(value)) {
            for (const item of value) {
              if (item !== null && item !== undefined) {
                searchParams.append(key, item)
              }
            }
          } else {
            searchParams.append(key, value)
          }
        }
      }
      const qs = searchParams.toString()
      if (qs) {
        fullUrl += (fullUrl.includes('?') ? '&' : '?') + qs
      }
    }

    const headers = { ...(config.headers || {}) }
    let body = undefined

    if (data !== null && data !== undefined) {
      if (data instanceof FormData || data instanceof Blob || typeof data === 'string') {
        body = data
      } else {
        headers['Content-Type'] = headers['Content-Type'] || 'application/json'
        body = JSON.stringify(data)
      }
    }

    const signal = config.signal || (config.timeout ? AbortSignal.timeout(config.timeout) : undefined)

    const fetchOptions = {
      method: method.toUpperCase(),
      headers,
      body,
      credentials: 'include',
      ...(signal ? { signal } : {}),
    }

    try {
      const response = await fetch(fullUrl, fetchOptions)

      const headerObj = {}
      response.headers.forEach((v, k) => {
        headerObj[k.toLowerCase()] = v
      })

      const contentType = response.headers.get('content-type') || ''
      let resData
      if (config.responseType === 'blob' && (response.ok || !contentType.includes('application/json'))) {
        resData = await response.blob()
      } else {
        const text = await response.text()
        try {
          resData = text ? JSON.parse(text) : null
        } catch {
          resData = text
        }
      }

      if (!response.ok) {
        const error = new Error(`Request failed with status ${response.status}`)
        error.response = {
          status: response.status,
          statusText: response.statusText,
          data: resData,
          headers: headerObj,
        }

        if (response.status === 401) {
          clearUserState()
          if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
            window.location.href = '/login'
          }
        }

        throw error
      }

      return {
        data: resData,
        status: response.status,
        statusText: response.statusText,
        headers: headerObj,
      }
    } catch (err) {
      throw err
    }
  }

  get(url, config = {}) {
    return this.request('GET', url, null, config)
  }

  post(url, data = null, config = {}) {
    return this.request('POST', url, data, config)
  }

  put(url, data = null, config = {}) {
    return this.request('PUT', url, data, config)
  }

  patch(url, data = null, config = {}) {
    return this.request('PATCH', url, data, config)
  }

  delete(url, config = {}) {
    return this.request('DELETE', url, null, config)
  }
}

const api = new ApiClient('/api/v1')

export default api

export function getVersion() {
  return api.get('/version')
}

export function getEndpoints(status = null) {
  const params = {}
  if (status) params.status = status
  return api.get('/endpoints/', { params })
}

export function getEndpoint(id) {
  return api.get(`/endpoints/${id}`)
}

// Per-cycle probe history: RTT min/avg/max, jitter and loss over time (v3.4).
export function getProbeSeries(id, startDate = null, endDate = null) {
  const params = {}
  if (startDate) params.start_date = startDate
  if (endDate) params.end_date = endDate
  return api.get(`/endpoints/${id}/probe-series`, { params })
}

// Per-endpoint loss, mean/p95 RTT, last probe time and hourly RTT sparkline.
export function getProbeSummary(hours = 24) {
  return api.get('/endpoints/probe-summary', { params: { hours } })
}

export function getTopology() {
  return api.get('/topology')
}

export function rebuildTopology() {
  return api.post('/topology/rebuild')
}

export function discoverAllTopologyRoutes() {
  return api.post('/topology/discover-all')
}

export function getEndpointTraces(id) {
  return api.get(`/endpoints/${id}/traces`)
}

export function getEndpointRca(id) {
  return api.get(`/endpoints/${id}/rca`)
}

// Where exporters forwarded this endpoint's traffic (24 h) and route changes seen in flows (7 days).
export function getEndpointFlowPaths(id) {
  return api.get(`/endpoints/${id}/flow-paths`)
}

export function refreshEndpointBaseline(id) {
  return api.post(`/endpoints/${id}/refresh-baseline`)
}

export function getUptimeReport(id, startDate, endDate) {
  return api.get(`/reports/uptime/${id}`, {
    params: {
      start_date: startDate,
      end_date: endDate,
    },
  })
}

export function getEndpointEvents(id, startDate, endDate, page = 1, pageSize = 50) {
  return api.get(`/reports/events/${id}`, {
    params: {
      start_date: startDate,
      end_date: endDate,
      page,
      page_size: pageSize,
    },
  })
}

export function getAvailabilityReport(id, startDate, endDate) {
  return api.get(`/reports/availability/${id}`, {
    params: {
      start_date: startDate,
      end_date: endDate,
    },
  })
}

export function exportBatchTelemetry(endpointIds, startTime, endTime, columns = null) {
  const payload = {
    endpoint_ids: endpointIds,
    start_time: startTime,
    end_time: endTime,
  }
  if (columns && columns.length > 0) {
    payload.columns = columns
  }
  return api.post(
    '/telemetry/export/batch',
    payload,
    {
      responseType: 'blob',
    }
  )
}

export function exportTelemetryCsv(endpointId, startDate, endDate) {
  return api.get(`/reports/telemetry/export/${endpointId}`, {
    params: {
      start_date: startDate,
      end_date: endDate,
    },
    responseType: 'blob',
  })
}

export function login(username, password) {
  return api.post('/auth/login', { username, password })
}

export function logout() {
  return api.post('/auth/logout')
}

export function createEndpoint(data) {
  return api.post('/endpoints/', data)
}

export function updateEndpoint(id, data) {
  return api.patch(`/endpoints/${id}`, data)
}

export function deleteEndpoint(id) {
  return api.delete(`/endpoints/${id}`)
}

export function changePassword(data) {
  return api.post('/auth/change-password', data)
}

export function getUsers() {
  return api.get('/users/')
}

export function createUser(data) {
  return api.post('/users/', data)
}

export function resetUserPassword(userId, data) {
  return api.post(`/users/${userId}/reset-password`, data)
}

export function updateUser(userId, data) {
  return api.patch(`/users/${userId}`, data)
}

export function deleteUser(userId) {
  return api.delete(`/users/${userId}`)
}

export function getFleetSummary(startDate, endDate) {
  return api.get('/reports/fleet-summary', {
    params: {
      start_date: startDate,
      end_date: endDate,
    },
  })
}

export function getSettings() {
  return api.get('/settings')
}

export function updateSettings(data) {
  return api.patch('/settings', data)
}

export function getAlertChannels() {
  return api.get('/alerts/channels')
}

export function createAlertChannel(data) {
  return api.post('/alerts/channels', data)
}

export function getAlertChannel(id) {
  return api.get(`/alerts/channels/${id}`)
}

export function updateAlertChannel(id, data) {
  return api.put(`/alerts/channels/${id}`, data)
}

export function deleteAlertChannel(id) {
  return api.delete(`/alerts/channels/${id}`)
}

export function testAlertChannel(data) {
  return api.post('/alerts/channels/test', data)
}

export function getAlertHistory(page = 1, pageSize = 50) {
  return api.get('/alerts/history', {
    params: {
      page,
      page_size: pageSize,
    },
  })
}

export function getBandwidthOverview(exporterId = null) {
  const params = {}
  if (exporterId) params.exporter_id = exporterId
  return api.get('/bandwidth/overview', { params })
}

export function getTrafficSeries(window = '1h', exporterId = null, endpointId = null, interfaceIdx = null, wanOnly = false) {
  const params = { window }
  if (exporterId) params.exporter_id = exporterId
  if (endpointId) params.endpoint_id = endpointId
  if (interfaceIdx !== null && interfaceIdx !== undefined) params.interface_idx = interfaceIdx
  // WAN-interface totals; the server falls back to all flows when no interface is marked WAN.
  if (wanOnly) params.wan_only = true
  return api.get('/bandwidth/traffic-series', { params })
}

export function getTopTalkers(window = '1h', limit = 25, endpointId = null, exporterId = null) {
  const params = { window, limit }
  if (endpointId) params.endpoint_id = endpointId
  if (exporterId) params.exporter_id = exporterId
  return api.get('/bandwidth/top-talkers', { params })
}

// Server-filtered, paged conversations. `filters`: exporterId, endpointId, ip, port, dscp,
// quick ('all' | 'anomalies' | 'qos' | 'bulk'), sort ('bytes' | 'flows' | 'packets'), limit, offset.
export function getConversations(window = '1h', filters = {}) {
  const params = { window }
  const map = { exporterId: 'exporter_id', endpointId: 'endpoint_id', ip: 'ip', port: 'port', dscp: 'dscp', quick: 'quick', sort: 'sort', limit: 'limit', offset: 'offset' }
  for (const [key, name] of Object.entries(map)) {
    const value = filters[key]
    if (value !== null && value !== undefined && value !== '') params[name] = value
  }
  return api.get('/bandwidth/conversations', { params })
}

export function getApplicationDistribution(window = '1h', exporterId = null, endpointId = null) {
  const params = { window }
  if (exporterId) params.exporter_id = exporterId
  if (endpointId) params.endpoint_id = endpointId
  return api.get('/bandwidth/applications', { params })
}

export function getFlowExporters() {
  return api.get('/bandwidth/exporters')
}

export function enrollFlowExporter(data) {
  return api.post('/bandwidth/exporters/enroll', data)
}

export function mapFlowExporter(ip, endpointId) {
  return api.post(`/bandwidth/exporters/${encodeURIComponent(ip)}/map`, { endpoint_id: endpointId })
}

export function getInterfaceTelemetry(exporterId, window = '1h') {
  return api.get('/bandwidth/interfaces', { params: { exporter_id: exporterId, window } })
}

export function updateEndpointInterfaces(endpointId, data) {
  return api.put(`/bandwidth/endpoints/${endpointId}/interfaces`, data)
}

export function testFlowPreflight() {
  return api.post('/settings/flow/preflight')
}

export function generatePdfReport(data) {
  return api.post('/reports/pdf', data, { responseType: 'blob' })
}

export function getOrganizationSettings() {
  return api.get('/settings/organization')
}

export function updateOrganizationSettings(data) {
  return api.patch('/settings/organization', data)
}

