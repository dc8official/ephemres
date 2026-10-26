# Ephemres API Reference — Version 3.3.2.v

Ephemres v3.3.2.v exposes a RESTful and Server-Sent Events (SSE) API built on the Native ASGI 3.0 Engine (`app.core.asgi`). The API is located under the `/api/v1` base path and requires JWT Bearer authentication or HttpOnly session cookies for protected endpoints.

## Base URL
`http(s)://<server-ip>:<port>/api/v1`

## Response Envelope
Most JSON endpoints wrap their payload in a common envelope (`app.schemas.APIResponse`):
```json
{ "status": "success", "data": { "...": "..." }, "meta": null, "error": null }
```
* `status` is `"success"` or `"error"`.
* `meta` carries pagination details (`total`, `page`, `page_size`, `total_pages`) on list endpoints, otherwise `null`.
* On failure, `error` is `{"code": "...", "message": "..."}`. HTTP errors raised by route validation and authentication (`401`, `403`, `404`, `422`, …) return `{"detail": "..."}` instead.

Exceptions: `GET /health/liveness`, `GET /health/readiness`, streaming responses (SSE, CSV, PDF), and the examples below that show a bare object.

---

## 1. System & Real-Time Events

### `GET /api/v1/version`
Returns the current platform version metadata (no authentication required).
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": { "version": "3.3.2.v", "platform": "Ephemres v3.3.2.v" },
    "meta": null,
    "error": null
  }
  ```

### `GET /api/v1/health`
Static process health check: returns `{"status": "success", "data": {"status": "ok", "version": "3.3.2.v"}, ...}` whenever the API process is serving requests. It does **not** check the database; use readiness for that.

### `GET /api/v1/health/liveness` (also `/health/liveness`)
Bare liveness probe: `{"status": "ok", "process": "healthy", "version": "3.3.2.v"}`.

### `GET /api/v1/health/readiness` (also `/health/readiness`)
Readiness probe that runs `SELECT 1` against PostgreSQL and pings FastStore when it is enabled. Returns `200` with `"status": "ready"` or `503` with `"status": "degraded"`, plus per-dependency `database` and `redis` (FastStore) objects with `status` and `error`.

### `GET /api/v1/events/stream`
Connects to the real-time Server-Sent Events (SSE) telemetry stream.
- **Headers:** `Accept: text/event-stream`
- **Response Headers:** `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`
- **Event Types Streamed:**
  - `CONNECTED`: Emitted immediately upon connection.
  - `STATE_TRANSITION`: Emitted on endpoint state changes (`UP`, `UP-UNSTABLE`, `DOWN-UNSTABLE`, `DOWN`).
  - `NODE_STATE_CHANGE`: Emitted for real-time topology canvas node recoloring.
  - `RCA_INCIDENT`: Emitted on root-cause analysis triggers and resolutions.
  - `: heartbeat\n\n`: Emitted every 15 seconds if idle to prevent proxy timeouts.

---

## 2. Authentication (`/auth`)

All routes (except `/login` and `/version`) require a valid session or JWT token:
`Authorization: Bearer <your_jwt_token>`

### `POST /auth/login`
Authenticates a user and returns an access token.
- **Request Body:** JSON containing `username` and `password`.
- **Response:** `200 OK` with JSON `{ "access_token": "...", "token_type": "bearer" }`

### `POST /auth/logout`
Terminates the active session and invalidates the session token in the active storage driver.

### `POST /auth/change-password`
Updates the authenticated user's password.

---

## 3. Endpoints Management (`/endpoints`)

### `GET /endpoints`
Lists monitored endpoints with their current state, 24-hour uptime, and latest RTT.
- **Query Params:**
  - `status` (string, optional, case-insensitive): `ACTIVE` / `DISABLED` filter on `endpoint_status`; `MONITORED` / `UNMONITORED` filter on `monitoring_enabled`. Any other value returns `400`.
  - `page` (int ≥ 1, optional) and `page_size` (int 1–500, optional): SQL-level pagination. Supplying either enables it (`page_size` defaults to 50, `page` to 1); omitting both returns every endpoint.
- **Response Envelope:**
  ```json
  {
    "status": "success",
    "data": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "hostname": "core-sw-01",
        "ip_address": "10.0.0.1",
        "device_type": "SWITCH",
        "endpoint_status": "ACTIVE",
        "current_operational_state": "UP",
        "current_detailed_state": "UP",
        "current_health_score": 100.0,
        "avg_rtt_ms": 1.42,
        "uptime_percentage_24h": 99.95,
        "last_seen": "2026-10-21T08:00:32Z"
      }
    ],
    "meta": { "total": 1, "page": 1, "page_size": 1, "total_pages": 1 }
  }
  ```
  When paginated, `meta.total` is the number of endpoints matching the filter across all pages and `total_pages = ceil(total / page_size)`. Without pagination, `meta` describes the full list (`page` = 1, `total_pages` = 1).

### `POST /endpoints`
Onboards a new endpoint for ICMP monitoring.
- **Request Body:**
  ```json
  {
    "hostname": "Web-App-Gateway",
    "ip_address": "192.168.10.1",
    "device_type": "ROUTER",
    "location": "Main Datacenter",
    "description": "Edge router for the web tier",
    "monitoring_enabled": true,
    "allow_incident_trace": true,
    "allow_topology_discovery": true,
    "enable_rca": true,
    "enable_scheduled_discovery": true,
    "is_l2_segment": false,
    "flow_exporter_ips": ["10.255.255.1"],
    "flow_interface_aliases": {"1": "WAN-Fiber"},
    "manual_parent_id": null
  }
  ```
- `ip_address`, `hostname`, and `device_type` are required; `ip_address` must be a valid IPv4/IPv6 address and is unique. `device_type` is free text (e.g. `ROUTER`, `SWITCH`, `SERVER`, `FIREWALL`).
- `device_role` is not set here: new endpoints are `ACTIVE_HOST`. Flow exporters are created or converted through `POST /bandwidth/exporters/enroll`.
- Synthetic TCP/HTTP/SSL probe settings are not part of this API (see the note in [Architecture §5](architecture.md)).

### `GET /endpoints/{id}`
Retrieves detailed information, status, baseline metrics, secondary exporter IP addresses (`flow_exporter_ips`), and interface aliases (`flow_interface_aliases`, returned as `{"<ifIndex>": {"name": "...", "speed_mbps": 1000, "role": "wan"}}`) for a specific endpoint. `speed_mbps` is `null` when no link speed was set (legacy name-only aliases included), and `role` is `"wan"` or `"lan"`.

### `GET /endpoints/probe-summary`
Per-endpoint ICMP statistics from the per-cycle probe history (v3.4) over the last `hours` (query param, default `24`, max `168`): `loss_pct`, reply-weighted `rtt_avg_ms`, `rtt_p95_ms` (95th percentile of per-cycle averages), `jitter_ms`, `sample_count`, `last_probe_at`, and `rtt_sparkline` (hourly mean RTT, oldest first, `null` for hours without replies). Endpoints without samples (passive, paused, not yet probed) are absent.

### `GET /endpoints/{id}/probe-series`
RTT min/avg/max, jitter and loss over time for one endpoint.
- **Query Params:** `start_date`, `end_date` (ISO 8601, optional; default the last 24 hours).
- Resolution follows the span: one point per monitoring cycle up to 2 days, 10-minute buckets up to 8 days, hourly beyond. The response carries `bucket_seconds` and `source` (`endpoint_probe_samples` or `endpoint_probe_hourly`). Raw samples are kept 30 days, hourly aggregates 365 days.
- Each point: `timestamp`, `sample_count`, `sent`, `received`, `loss_pct`, `rtt_min_ms`, `rtt_avg_ms`, `rtt_max_ms`, `jitter_ms` (`null` where no reply arrived).
- **Errors:** `400` when `start_date` is not before `end_date`.

### `GET /endpoints/{id}/traces`
Returns the 10 most recent diagnostic traceroutes for the endpoint.

### `GET /endpoints/{id}/rca`
Returns root-cause analysis incidents recorded for the endpoint. `baseline_snapshot` and `failure_trace_snapshot` are hop lists. `flow_evidence` is `null` unless an exporter saw the endpoint's traffic in the 24 h before the RCA; otherwise it holds what flows showed when the RCA ran (evidence only, the verdict comes from traceroute): `paths` (per exporter and direction: `out_if`/`out_if_name`, `next_hop`, `last_seen`, `after_failure`), `traffic_after_failure` (traffic still seen after probes started failing, i.e. the probe path or ICMP is the likely problem), `route_changes` seen from 15 minutes before the failure, and a one-line `summary`.

### `GET /endpoints/{id}/flow-paths`
Where exporting routers forwarded the endpoint's traffic, from flows (requires the next hop in the export template for `next_hop`; see the NetFlow guide §2.5).
- `paths`: up to 100 rows from the last 24 h, newest first: `exporter_hostname`, `direction` (`to`/`from` the endpoint), `in_if`/`out_if` with alias names, `next_hop`, `first_seen`, `last_seen`, `bytes`, `flow_count`, and `active` (traffic in the last `active_minutes`, 15).
- `route_changes`: up to 50 from the last 7 days, newest first: `detected_at`, `exporter_hostname`, `direction`, old and new out interface (with names) and next hop. A change is recorded when a different path carries most of the traffic for 3 minutes in a row; load balancing is not a change.
- `next_hop_reported`: `false` when none of the paths carries a next hop.

### `POST /endpoints/{id}/refresh-baseline`
Re-runs baseline route discovery for the endpoint (requires `Admin` role).

### `PATCH /endpoints/{id}`
Updates configuration flags or properties for an endpoint. Synchronizes changes directly with the in-memory `EndpointRegistry` and flow correlator routing table.
- **Request Body:** Accepts any subset of: `hostname`, `device_type`, `location`, `description`, `monitoring_enabled`, `allow_incident_trace`, `allow_topology_discovery`, `enable_rca`, `enable_scheduled_discovery`, `is_l2_segment`, `flow_exporter_ips` (array of IP strings), `flow_interface_aliases` (object mapping `ifIndex` to a label, e.g. `{"1": "WAN-Fiber"}`), `manual_parent_id` (UUID or `null`), and `endpoint_status` (`ACTIVE` or `DISABLED`).
- `ip_address` cannot be changed after creation; delete and re-create the endpoint instead.

### `DELETE /endpoints/{id}`
Removes an endpoint from monitoring and deregisters it from the polling engine.

---

## 4. Topology & RCA (`/topology`)

### `GET /topology`
Retrieves the parent-child topology DAG (nodes with state and hop level, plus edges). The crossing-free hierarchical layout is computed in the browser by vis-network; the API returns graph data only.
- **Response:** `200 OK` returning an object containing arrays of `nodes` and `edges`.
- Traceroute links are checked every 5 minutes against the next hops exporters reported in the last 15 minutes. An edge then carries `flow_status`: `confirmed` (traffic goes to the next traceroute hop), `differs` (traffic leaves for another next hop) or `inferred` (the next hop did not answer traceroute; flows name it), with a `flow_detail` sentence. A `* * *` node named by flows carries `flow_inferred_ip`. Edges without a matching exporter or recent flows have no `flow_status`.

### `POST /topology/rebuild`
Rebuilds the in-memory topology DAG from the database (requires `Admin` role).

### `POST /topology/discover-all`
Queues background traceroute discovery for all active endpoints that are currently `UP`, run sequentially with a 500 ms delay between targets (requires `Admin` role).

---

## 5. Reports & Telemetry (`/reports`)

### `GET /reports/fleet-summary`
Fleet-wide SLA and state summary for a time window.
- **Query Params:** `start_date`, `end_date` (required, ISO 8601).

### `GET /reports/uptime/{endpoint_id}`
Retrieves SLA uptime availability percentage and state distribution metrics.
- **Query Params:** `start_date`, `end_date` (required).

### `GET /reports/incidents/{endpoint_id}`
Retrieves paginated outage incidents (DOWN periods) for an endpoint.
- **Query Params:** `start_date`, `end_date` (required), `page` (default 1), `page_size` (default 50, max 200).

### `GET /reports/events/{endpoint_id}`
Retrieves paginated historical state transition events.
- **Query Params:** `start_date`, `end_date` (required), `page` (default 1), `page_size` (max 1500; `size` is accepted as an alias).

### `POST /api/v1/telemetry/export/batch`
Streams a sanitized CSV containing bulk telemetry data across multiple endpoints using deterministic keyset pagination. Note the path: this route is mounted at `/api/v1/telemetry`, not under `/reports`.
- **Access Control:** Any authenticated user (exports are read-only).
- **Request Body:**
  ```json
  {
    "endpoint_ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    "start_time": "2026-09-01T00:00:00Z",
    "end_time": "2026-09-08T00:00:00Z",
    "columns": [
      "Endpoint_ID",
      "Hostname",
      "IP_Address",
      "Device_Type",
      "Timestamp",
      "Operational_State",
      "Detailed_State",
      "Packet_Success_Rate",
      "Avg_RTT_ms"
    ]
  }
  ```
- **Column Customization Behavior:**
  - If `columns` is omitted or empty, all standard columns are included.
  - Non-negotiable identity columns (`Hostname` and `IP_Address`) are strictly enforced and automatically injected into every CSV stream.
  - CSV cells are sanitized against spreadsheet formula injection (`=`, `+`, `-`, `@`, `\t`, `\r`).

### `POST /reports/pdf`
Compiles and streams an executive-ready, immutable ISO A4 PDF report with air-gapped SSRF protection and cryptographic header verification.
- **Access Control:** Any authenticated user (exports are read-only).
- **Request Body:**
  ```json
  {
    "template_type": "a",
    "endpoint_ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    "start_date": "2026-09-01T00:00:00Z",
    "end_date": "2026-09-08T00:00:00Z",
    "include_rca": true
  }
  ```
- **Parameters:**
  - `template_type` (str, case-insensitive): `"a"` / `"availability"` (Availability & SLA Matrix), `"b"` / `"bandwidth"` (Bandwidth & Interface Capacity), or `"c"` / `"master"` (Combined Master Audit Dossier). Any other value returns `400`.
  - `endpoint_ids` (list[UUID]): List of target endpoint UUIDs to compile into the report.
  - `start_date` (datetime): UTC start boundary.
  - `end_date` (datetime): UTC end boundary.
  - `include_rca` (bool, default `true`): Include root-cause analysis diagnostic summaries in outage incident ledgers.
- **Response Headers:**
  - `Content-Type: application/pdf`
  - `Content-Disposition: attachment; filename="<generated-report-name>.pdf"`
  - `X-Report-SHA256: <64-character-sha256-hexdigest>`
- **Security & Immutability:**
  - Air-gapped rendering: refuses every network scheme (`http://`, `https://`, `ftp://`, …); local files are readable only under `backend/app/templates/reports/` and `frontend/src/assets/`; images may also come from inline `data:` URIs.
  - Native binary PDF 1.4 compilation: Generated directly by `app.core.pdf` with cryptographic checksum verification via the `X-Report-SHA256` response header.

---

## 6. Enterprise Alerting & Notifications (`/alerts`)

Manage multi-channel alert delivery endpoints, live diagnostic test probes, and delivery history logs (requires `Admin` role).

### `GET /alerts/channels`
Lists all configured notification channels. Sensitive fields (webhook URLs, SMTP passwords, authorization tokens) are masked at the API boundary with `••••••••`.
- **Response:** `200 OK` returning an array of alert channel objects.

### `POST /alerts/channels`
Creates a new notification channel. Credentials and webhook URLs are encrypted at rest using AES-256-GCM.
- **Request Body:**
  ```json
  {
    "name": "NOC MS Teams Incidents",
    "channel_type": "TEAMS",
    "is_enabled": true,
    "endpoint_ids": [],
    "subnet_filters": ["10.0.0.0/16", "192.168.1.0/24"],
    "severity_filters": ["DOWN", "RECOVERED"],
    "config": {
      "webhook_url": "https://company.webhook.office.com/webhookb2/..."
    }
  }
  ```
- **Channel Types:** `TEAMS`, `DISCORD`, `SLACK`, `EMAIL_SMTP`, `GENERIC_WEBHOOK`.
- **Filtering Fields:**
  - `endpoint_ids` (array of UUID strings, default `[]`): Restricts delivery to specific endpoints. If empty, all endpoints are included.
  - `subnet_filters` (array of CIDR strings, default `[]`): Restricts delivery to endpoints whose IP addresses match the specified CIDR subnets (e.g. `["10.0.0.0/16"]`).
  - `severity_filters` (array of strings, default `["DOWN", "RECOVERED"]`): Matched against the event's operational state. Effective values are `"DOWN"` and `"RECOVERED"` (or its synonym `"UP"`); detailed states such as `UP-UNSTABLE` are not matched separately.

### `GET /alerts/channels/{id}`
Retrieves channel details by ID with masked secrets.

### `PUT /alerts/channels/{id}`
Updates an existing notification channel configuration (`name`, `channel_type`, `is_enabled`, `endpoint_ids`, `subnet_filters`, `severity_filters`, `config`). If secrets or custom header values contain masked bullets `••••••••`, existing decrypted values are preserved.

### `DELETE /alerts/channels/{id}`
Permanently deletes an alert channel and its associated delivery history logs.

### `POST /alerts/channels/test`
Executes an immediate live diagnostic test alert probe to verify destination reachability, SSRF policy compliance, and template formatting.
- **Request Body:** Accepts either `{"channel_id": "uuid"}` to test an existing channel, or `{"channel_type": "TEAMS", "config": {...}}` for pre-save validation.
- **Response:**
  ```json
  {
    "status": "success",
    "data": {
      "success": true,
      "status_code": 200,
      "message": "Test alert delivered successfully."
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /alerts/history` (Alias: `GET /alerts/logs`)
Retrieves paginated delivery audit logs across all channels.
- **Query Params:** `page` (default: 1), `page_size` (default: 50, max: 200).
- **Log Fields (`AlertDeliveryLog`):** `id`, `channel_id`, `channel_name`, `endpoint_id`, `endpoint_name`, `event_type`, `status` (`DELIVERED`, `FAILED`, `THROTTLED`), `status_code`, `retry_count`, `response_message`, `delivered_at`.

---

## 7. System Administration & Settings (`/settings`)

Manage global runtime parameters and daemon operation modes.

### `GET /settings`
Retrieves the active system configuration from the database.
- **Response:**
  ```json
  {
    "performance_mode": true,
    "performanceMode": true,
    "l2_auto_bypass": true,
    "l2AutoBypass": true,
    "session_timeout": 120,
    "sessionTimeout": 120,
    "lockout_threshold": 5,
    "lockoutThreshold": 5,
    "alerting_enabled": true,
    "alertingEnabled": true,
    "flow_ingestion_enabled": true,
    "flowIngestionEnabled": true,
    "flow_netflow_port": 2055,
    "flowNetflowPort": 2055,
    "flow_ipfix_port": 4739,
    "flowIpfixPort": 4739,
    "flow_sampling_multiplier": 1,
    "flowSamplingMultiplier": 1
  }
  ```

### `PATCH /settings`
Updates runtime parameters (requires `Admin` role).
- **Request Body:** Accepts snake_case or camelCase properties:
  - `alerting_enabled` (bool, optional): Global master toggle to enable or suspend all outbound alert notifications.
  - `performance_mode` (bool, optional): Switches between In-RAM FastStore (`/dev/shm`) and PostgreSQL-native storage drivers.
  - `l2_auto_bypass` (bool, optional): Controls Layer-2 direct ICMP diagnostic bypass.
  - `session_timeout` (int, 1–1440 min): Idle session expiration duration.
  - `lockout_threshold` (int, 1–100): Maximum failed attempts before IP lockout.
  - `flow_ingestion_enabled` (bool, optional): Master toggle for passive NetFlow/IPFIX collection.
  - `flow_netflow_port` (int, 1–65535, optional): NetFlow UDP listening port (default: 2055).
  - `flow_ipfix_port` (int, 1–65535, optional): IPFIX UDP listening port (default: 4739).
  - `flow_sampling_multiplier` (int, >= 1, optional): Hardware packet sampling multiplier (e.g. 1000 for 1-in-1000 sampling).

### `POST /settings/flow/preflight`
Performs live pre-flight verification of FastStore (`/dev/shm`) shared-memory stream write capabilities and UDP socket binding prior to enabling passive flow ingestion. The UDP check binds the configured `flow.bind_host` and ports exactly as the collector would; ports held by an active `ephemres-flowd` are not reported as conflicts. Returns backward-compatible status metrics:
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "redis_connected": true,
      "redis_version": "7.0.15",
      "redis_version_supported": true,
      "stream_write_success": true,
      "rmem_max": 212992,
      "rmem_max_supported": false,
      "udp_ports_available": true,
      "port_conflicts": [],
      "ready": true,
      "message": "Flow preflight passed successfully."
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /settings/organization`
Retrieves corporate organization profile and report branding settings. Available to all authenticated users for client-side rendering.
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "company_name": "Acme Corporation",
      "department": "Network Operations Center (NOC)",
      "logo_data": "data:image/png;base64,...",
      "report_footer": "Confidential — Internal Network Infrastructure Audit"
    }
  }
  ```

### `PATCH /settings/organization`
Updates corporate organization branding and PDF report headers/footers (requires `Admin` role).
- **Request Body:**
  ```json
  {
    "company_name": "Acme Corporation",
    "department": "NOC & Core Engineering",
    "logo_data": "data:image/png;base64,...",
    "report_footer": "Proprietary & Confidential — Tier-1 Operational Telemetry"
  }
  ```
- **Validation Constraints:**
  - `logo_data`: A base64 `data:image/png` or `data:image/jpeg` URI whose bytes match the declared type (SVG is rejected because PDF reports cannot embed it); max length 2,000,000 characters (~1.5 MB decoded). An empty string clears the logo.
  - `company_name`: String, max 150 characters.
  - `department`: String, max 150 characters.
  - `report_footer`: String, max 255 characters.

---

## 8. User Governance (`/users`)

Manage user accounts and role-based access control (requires `Admin` role).

### `GET /users/`
Lists all user accounts.

### `POST /users/`
Creates a new user account (assignable as `ADMIN` or `VIEWER`).

### `POST /users/{id}/reset-password`
Forces a password reset for a specific user.

### `PATCH /users/{id}`
Updates a user account role (`ADMIN` / `VIEWER`) or active status.

### `DELETE /users/{id}`
Deactivates or deletes a user account.

---

## 9. Network Flow & Bandwidth Telemetry (`/bandwidth`)

High-precision passive telemetry endpoints for fleet-wide bandwidth analysis, top talkers, application protocol distribution, exporter health, and conversation drilling.

### `GET /bandwidth/overview`
Rates over the last five complete minutes, fleet-wide or for one exporter (`exporter_id` query param). Ingress counts flows towards a monitored endpoint and egress flows from one (a flow between two monitored endpoints counts in both). Traffic with no monitored endpoint on either side is reported as `total_transit_bps` / `total_transit_pps` rather than split into ingress and egress. Also returns packet rates, `avg_packet_size`, `anomaly_conversations_count` and `anomaly_flows_count` (conversations whose OR'd TCP flags are SYN without ACK or RST without FIN, and the flows in them), `window_seconds` and `exporter_id`.
- **Network edge (WAN):** the totals above add up what every exporter saw, so traffic that crosses two monitored routers counts twice. `wan_in_bps`/`wan_out_bps` and `wan_in_pps`/`wan_out_pps` sum only interfaces an admin marked `role: "wan"` (see `PUT /bandwidth/endpoints/{id}/interfaces`), which counts each packet once as it enters or leaves the network; "in" is traffic arriving from outside. `wan_links` has one row per WAN interface, busiest first: `exporter_id`, `exporter_hostname`, `interface_idx`, `name`, in/out rates, `speed_mbps` and `utilization_percentage` (`null` without a configured speed) and `share_percentage` of all WAN bytes. `wan_interfaces_count` is 0, and the WAN fields are zero, until an interface is marked WAN.
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "total_ingress_bps": 45210340.50,
      "total_egress_bps": 41732622.00,
      "active_exporters_count": 4,
      "unmatched_exporters_count": 1,
      "total_flows_count": 128450,
      "total_transit_bps": 3120400.00,
      "window_seconds": 300,
      "exporter_id": null,
      "wan_interfaces_count": 1,
      "wan_in_bps": 31240000.00,
      "wan_out_bps": 9870000.00,
      "wan_links": [
        {"exporter_hostname": "core-gw-01", "interface_idx": 1, "name": "WAN-Fiber", "in_bps": 31240000.00, "out_bps": 9870000.00, "speed_mbps": 1000, "utilization_percentage": 3.12, "share_percentage": 100.0}
      ]
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /bandwidth/traffic-series`
Returns time-series data points formatted for Chart.js stacked area charts (Ingress vs. Egress).
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `exporter_id` (UUID, optional): Filter by exporter endpoint UUID.
  - `endpoint_id` (UUID, optional): Filter by source or destination endpoint UUID.
  - `interface_idx` (int, optional, with `exporter_id`): One interface only.
  - `wan_only` (bool, default `false`): Fleet or exporter scope only (ignored with `endpoint_id` or `interface_idx`). Returns in/out summed over WAN-role interfaces as `ingress_*`/`egress_*`, from the interface rollups. The response's `wan_only` says whether it applied: with no WAN interface marked it is `false` and the normal series is returned.
- Each point also has `transit_bps`/`transit_pps` (flows with no monitored endpoint on either side), packet rates and `flows_per_sec`. The response carries `bucket_seconds` and `directional`; the fleet-wide 30d tier keeps no endpoint attribution, so it is `false` and all traffic is reported as transit.
- With `exporter_id`, windows up to 6h read 1-minute interface rollups and longer windows the hourly interface rollup. The 24h/7d/30d flow aggregates use real-time aggregation, so the newest minutes are included.
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "window": "1h",
      "points": [
        {
          "timestamp": "2026-09-15T20:00:00Z",
          "ingress_bps": 12450800.00,
          "egress_bps": 11493046.15
        }
      ]
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /bandwidth/conversations`
Conversations (source, destination, protocol, service port) over the window, filtered, counted and paged on the server.
- **Query Parameters:** `window`, `exporter_id`, `endpoint_id` (either side), `ip` (either side), `port` (service port), `dscp` (0–63), `quick` (`all` | `anomalies` | `qos` | `bulk`), `sort` (`bytes` | `flows` | `packets`), `limit` (1–200, default 25), `offset`.
- `quick`: `anomalies` = the OR of the conversation's TCP flags over the window is SYN without ACK or RST without FIN (the same rule as the overview KPI and the PDF report); `qos` = DSCP > 0; `bulk` = average packet ≥ 1100 bytes.
- **Response:** `items` (same fields as `top_conversations` below), `counts` (`all`, `anomalies`, `qos`, `bulk` over every conversation matching the other filters), `total` (count for the requested `quick`), `limit`, `offset`, `window`.
- **Errors:** `400` for an unknown `quick` or `sort`, or an invalid `ip`.

### `GET /bandwidth/top-talkers`
Retrieves the highest-volume addresses and the largest conversations within the specified window. Each address's ingress (as destination) and egress (as source) come from one aggregation; `top_conversations` is the first page of `GET /bandwidth/conversations`.
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `limit` (int, default `25`, range `1`–`200`): Maximum records to return.
  - `exporter_id`, `endpoint_id` (UUID, optional): Scope to one exporter or to flows involving one endpoint.
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "top_endpoints": [
        {
          "endpoint_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          "ip_address": "192.168.1.10",
          "hostname": "db-prod-01",
          "total_bytes": 1048576000,
          "ingress_bytes": 503316480,
          "egress_bytes": 545259520,
          "flow_count": 4520
        }
      ],
      "top_conversations": [
        {
          "src_ip": "192.168.1.10",
          "dst_ip": "192.168.1.20",
          "protocol": "TCP",
          "dst_port": 5432,
          "total_bytes": 845200000,
          "flow_count": 1240
        }
      ]
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /bandwidth/applications`
Returns the top 15 (protocol, service port) pairs by volume plus an `"Other"` row (`is_other: true`) for the rest; `percentage` is the share of all traffic in the window (`total_bytes`). `zones_resolution` says how `traffic_zones` were classified: `ip` (addresses and monitored endpoints, windows up to 6h), `endpoint` (monitored endpoints only, 24h/7d), or `unavailable` (30d+, zones are zero).
`qos_source` and `qos_window_start` give the rollup table and start time the QoS (`qos_classes`) breakdown was read from. On TimescaleDB the hourly/daily aggregates have no ToS column, so for `24h`+ windows QoS comes from `flow_minute_rollups`, which holds 90 days; a `1y` window then reports QoS for the last 90 days only.
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "applications": [
        {
          "protocol_name": "TCP",
          "port": 443,
          "service_label": "HTTPS",
          "total_bytes": 524288000,
          "percentage": 50.0
        },
        {
          "protocol_name": "TCP",
          "port": 5432,
          "service_label": "PostgreSQL",
          "total_bytes": 314572800,
          "percentage": 30.0
        }
      ],
      "qos_classes": [],
      "qos_source": "flow_minute_rollups",
      "qos_window_start": "2026-07-23T12:00:00Z",
      "traffic_zones": []
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /bandwidth/exporters`
Lists all configured flow exporters with interface aliases, along with detected unmatched exporter candidates from the FastStore sliding discovery cache.
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "exporters": [
        {
          "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          "hostname": "core-gw-01",
          "primary_ip": "10.0.0.1",
          "flow_exporter_ips": ["192.168.100.1"],
          "interface_aliases": {
            "1": { "name": "WAN-Fiber", "speed_mbps": 1000, "role": "wan" },
            "2": { "name": "LAN-Trunk", "speed_mbps": null, "role": "lan" }
          },
          "last_flow_time": "2026-09-15T20:59:00Z",
          "flows_per_sec": 41.27
        }
      ],
      "unmatched": [
        {
          "ip_address": "172.16.50.1",
          "last_seen": "2026-09-15T21:00:00Z",
          "flows_per_sec": 12.5
        }
      ]
    },
    "meta": null,
    "error": null
  }
  ```

### `POST /bandwidth/exporters/{ip}/map`
One-click binds an unmatched exporter IP to an existing endpoint's `flow_exporter_ips` array (requires `Admin` role). Immediately removes the IP from the FastStore unmatched set and broadcasts a `channel:registry_sync` event to refresh the in-memory correlator table with zero downtime.
- **Path Parameter:** `ip` (string): The exporter IP to map.
- **Request Body:**
  ```json
  {
    "endpoint_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
  }
  ```
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "message": "Successfully mapped exporter IP 172.16.50.1 to core-gw-01."
    },
    "meta": null,
    "error": null
  }
  ```

### `POST /bandwidth/exporters/enroll`
Creates a new passive endpoint for an unmatched exporter IP (requires `Admin` role). To attach the IP to an endpoint that already exists, use `POST /bandwidth/exporters/{ip}/map` instead.
- **Request Body:** `{"ip_address": "10.255.255.1", "hostname": "edge-fw-01", "description": "optional", "device_role": "FLOW_EXPORTER"}`. A blank `hostname` becomes `flow-exporter-<ip>`. `device_role` is `FLOW_EXPORTER` (default) or `HYBRID_GATEWAY`; any other value returns `422`.
- The new endpoint has `device_type = "Router"`, `monitoring_enabled = false` (no ICMP polling), and `flow_exporter_ips = [ip_address]`.
- **Errors:** `400` for an invalid IP; `409` if an endpoint with the same IP or hostname already exists.

### `GET /bandwidth/interfaces`
Per-interface ingress/egress throughput and utilization for one exporter.
- **Query Params:** `exporter_id` (UUID, required), `window` (default `1h`; same values as the other bandwidth endpoints).
- Rates average over `covered_seconds`: from the later of the window start and the exporter's first data in the window, to now. Up to 7 days the 1-minute rollup is used (`p95_resolution_seconds` 60); 30 days uses the hourly rollup (p95 over hourly averages, `p95_resolution_seconds` 3600). Idle minutes count as zero in the percentile.
- Each interface: average `in_bps`/`out_bps`, `in_p95_bps`/`out_p95_bps`, `in_peak_bps`/`out_peak_bps` (busiest minute), and `utilization_percentage`, `p95_utilization_percentage`, `peak_utilization_percentage`. When no link speed is configured (`speed_configured: false`) the utilization fields are `null` and `speed_mbps` reports the 1 Gbps default. `role` is `"wan"` or `"lan"`.

### `PUT /bandwidth/endpoints/{id}/interfaces`
Replaces an exporter's interface names and link speeds (requires `Admin` role).
- **Request Body:** object keyed by SNMP `ifIndex`: `{"1": {"name": "WAN-Fiber", "speed_mbps": 1000, "role": "wan"}}`. `name` is 1–64 characters of letters, digits, spaces and `_-./:`; `speed_mbps` is 1–10,000,000, or omitted/`null` to leave the speed unset (utilization is then not shown); `role` is `"wan"` (a link to the internet or a provider, counted in the overview's WAN totals) or `"lan"` (default). The body replaces the whole map, so send every interface with its current values.
