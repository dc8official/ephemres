# Ephemres API Reference — Version 3.3.5

Ephemres v3.3.5 exposes a RESTful and Server-Sent Events (SSE) API built on the Native ASGI 3.0 Engine (`app.core.asgi`). The API is located under the `/api/v1` base path and requires JWT Bearer authentication or HttpOnly session cookies for protected endpoints.

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
    "data": { "version": "3.3.5", "platform": "Ephemres v3.3.5" },
    "meta": null,
    "error": null
  }
  ```

### `GET /api/v1/health`
Static process health check: returns `{"status": "success", "data": {"status": "ok", "version": "3.3.5"}, ...}` whenever the API process is serving requests. It does **not** check the database; use readiness for that.

### `GET /api/v1/health/liveness` (also `/health/liveness`)
Bare liveness probe: `{"status": "ok", "process": "healthy", "version": "3.3.5"}`.

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
  - `ROUTE_HINT`: Emitted when NetFlow route augmentation (`PathTracker` in `ephemres-flowd`) sees a sustained dominant path change for a monitored endpoint, or (v3.3.6, `reason: "ttl"`) when the engine sees an endpoint's reply TTL change and hold for 3 cycles; the leader API worker also uses it to queue a targeted traceroute.
  - `TOPOLOGY_UPDATED`: Emitted when the topology map should be reloaded, with a `reason`: `ROUTE_DISCOVERED` (an endpoint's path was redrawn, with its `endpoint_id`), `ENDPOINT_ROUTE_CHANGED`, `NORMAL_PATH_SET`, `NORMAL_PATH_PROMOTED`, `MANUAL_REBUILD`, `FLEET_DISCOVERY_STARTED` (with `enqueued_count`) or `FLEET_DISCOVERY_COMPLETED`. Since v3.3.6 route changes go through the event broker (on the `NODE_STATE_CHANGE` channel), so every API worker rebuilds its map and tells its own browsers; `ROUTE_DISCOVERED` and `MANUAL_REBUILD` still reach only the worker that did the work.
  - `: heartbeat\n\n`: Emitted every 15 seconds if idle to prevent proxy timeouts.

---

## 2. Authentication (`/auth`)

All routes except `POST /auth/login`, `POST /auth/logout`, `/version` and the health routes (`/api/v1/health` and the liveness and readiness probes) require a valid session token, sent either as the `ephemres_access_token` cookie (what the browser does) or as a header:
`Authorization: Bearer <your_jwt_token>`

### `POST /auth/login`
Authenticates a user and starts a session.
- **Request Body:** JSON containing `username` and `password`.
- **Response:** `200 OK` with `data` `{ "username": "...", "role": "ADMIN", "must_change_password": false, "message": "Login successful." }`. The token itself is not in the body: it is set as the `ephemres_access_token` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS or with HSTS enabled). The token and cookie last until the session's absolute end plus one hour: 13 hours for an admin, 7 days and 1 hour for a viewer. The session itself usually ends sooner, after the account's idle timeout (see "Session rules" below). A script can take the token from the `Set-Cookie` response header and send it back as that cookie or as a Bearer header.
- Past the account's session limit (`max_sessions`), the account's oldest session ends.
- When `must_change_password` is `true`, every route that needs a session answers `403` until the password is changed through `POST /auth/change-password` (signing out still works).

### `POST /auth/logout`
Ends the session (its row in PostgreSQL `user_sessions` is deleted, so the token stops working on every worker) and clears the cookie. It always answers `200`, even with an expired or invalid token, so a browser can always sign out.

### `POST /auth/change-password`
Updates the authenticated user's password. The account's other sessions end (reason `password_changed`); the one making the change stays signed in.

### `POST /auth/activity`
Records real user interaction on the calling session and restarts its idle timeout. The browser calls it at most once a minute while the user clicks, types or moves between pages. No other route restarts the idle timeout: polling and the live-event stream don't. Writes at most once per 30 seconds per session. Allowed while a password change is pending.
- **Response:** `200 OK` with `data` `{ "recorded": true }`.

### Session rules (v3.3.6)
- A session is accepted while its row exists in `user_sessions`, the account's idle timeout (`users.session_idle_minutes`) has not passed since its last activity, and, for an admin, it is less than 12 hours old. The timeout is read from the account on every request, so changing it applies to open sessions at once.
- A refused session answers `401` with the header `X-Session-End-Reason`, one of `idle_timeout`, `absolute_timeout`, `signed_in_elsewhere` (ended by the session limit), `signed_out`, `password_changed` or `ended_by_admin` (role change, deactivation). For `idle_timeout`, `X-Session-Idle-Minutes` gives the timeout that applied. With no record of why (an old or unknown token), the header is left out.

---

## 3. Endpoints Management (`/endpoints`)

### `GET /endpoints`
Lists monitored endpoints with their current state, 24-hour uptime, and latest RTT.
- **Query Params:**
  - `status` (string, optional, case-insensitive): `ACTIVE` / `DISABLED` filter on `endpoint_status`; `MONITORED` / `UNMONITORED` filter on `monitoring_enabled`. Any other value returns `400`.
  - `page` (int ≥ 1, optional) and `page_size` (int 1–500, optional): SQL-level pagination. Supplying either enables it (`page_size` defaults to 50, `page` to 1); omitting both returns every endpoint.
  - The shared endpoint filter (v3.3.6): `site`, `location`, `tag`, `type` (each repeatable: `?tag=mpls&tag=core`) and `q` (search). Any value within a field matches; all fields must match. Site, location and type match ignoring case; `q` searches hostname, IP, site, location, description and tags. A tag with a comma, quote, backslash or brace answers `400`.
- Each endpoint also carries `site` (string or null), `location` and `tags` (list of lower-case strings).
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
Per-endpoint ICMP statistics from the per-cycle probe history (v3.3.5) over the last `hours` (query param, default `24`, max `168`): `loss_pct`, reply-weighted `rtt_avg_ms`, `rtt_p95_ms` (95th percentile of per-cycle averages), `jitter_ms`, `sample_count`, `last_probe_at`, and `rtt_sparkline` (hourly mean RTT, oldest first, `null` for hours without replies). Endpoints without samples (passive, paused, not yet probed) are absent.

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
Traces the route to the endpoint now (requires `Admin` role). A path different from the current one counts at once (an admin asked), without the second confirming trace other triggers need.

### Routes: current, normal and the change log (v3.3.6)
Each endpoint keeps a **current path** (what root-cause analysis compares a failure with) and a **normal path**. Every trace updates only the current path; a different path counts once two traces at least 2 minutes apart agree. A current path held 24 hours becomes normal unless the normal path is pinned. Traces start when an endpoint is added, by hand, after a recovery, on the rolling schedule, from flow data and reply-TTL hints, and when a monitored device other endpoints route through goes down or comes back.

#### `GET /endpoints/{id}/route`
`data` is `null` before the first trace, else: `current_hops`, `current_since`, `last_traced_at`, `normal_hops`, `normal_since`, `normal_pinned`, `on_normal_path`, `first_diff_hop`, `pending_hops` / `pending_since` (a new path waiting for its confirming trace), `flapping_since`, and `changes` (newest first, up to 50): `changed_at`, `kind` (`path_changed`, `back_to_normal`, `became_normal`, `made_normal`, `pinned`, `unpinned`, `flapping`), `trigger` (`added`, `manual`, `scheduled`, `recovery`, `route_hint`, `transit`, `ttl`, `confirm`), `from_hops`, `to_hops`, `first_diff_hop`, `detail`.

#### `POST /endpoints/{id}/route/normal` (admin)
Body `{ "pinned": false }`: the current path becomes the normal one now; with `true` it also stays normal until unpinned. `404` before the first trace. Logged in the route-change log and audited as `ENDPOINT:SET_NORMAL_PATH`.

#### `DELETE /endpoints/{id}/route/pin` (admin)
Unpins the normal path; it follows the current one again after 24 hours. `404` when not pinned. Audited as `ENDPOINT:UNPIN_NORMAL_PATH`.

### `PATCH /endpoints/{id}`
Updates configuration flags or properties for an endpoint. Synchronizes changes directly with the in-memory `EndpointRegistry` and flow correlator routing table.
- **Request Body:** Accepts any subset of: `hostname`, `device_type`, `location`, `description`, `monitoring_enabled`, `allow_incident_trace`, `allow_topology_discovery`, `enable_rca`, `enable_scheduled_discovery`, `is_l2_segment`, `flow_exporter_ips` (array of IP strings), `flow_interface_aliases` (object mapping `ifIndex` to a label, e.g. `{"1": "WAN-Fiber"}`), `manual_parent_id` (UUID or `null`), and `endpoint_status` (`ACTIVE` or `DISABLED`). `snmp_credential_id` (v3.3.6): an SNMP credential set's id, or `null` to stop SNMP polling (`400` if the set does not exist); also accepted by `POST /endpoints` and returned by `GET /endpoints/{id}`.
- `ip_address` cannot be changed after creation; delete and re-create the endpoint instead.

### `DELETE /endpoints/{id}`
Removes an endpoint from monitoring and deregisters it from the polling engine.

---

### `GET /endpoints/match`
The shared filter's matching endpoints (same query params as `GET /endpoints`): `{ "ids": [...], "total": 2, "filter": { "site": [...], "location": [...], "tag": [...], "type": [...], "q": "" }, "description": "Site Abuja · Tags mpls, core" }`. The dashboard and the topology map use it to narrow their own full lists, so every page applies the same server-side rules.

### `GET /endpoints/suggestions`
Values in use, each with its endpoint count, for autocomplete and the filter bar: `sites`, `locations`, `tags`, `device_types`, each `[{ "value": "Abuja", "count": 3 }]`. One spelling per value (the most common). With `?site=Abuja`, `locations` lists only those within that site.

### `POST /endpoints/bulk-update` (admin)
Changes many endpoints at once (dashboard bulk assign). Body: which endpoints — `endpoint_ids` (list), `filter` (`{ "site": [...], "location": [...], "tag": [...], "type": [...], "q": "" }`), or both (the listed endpoints the filter matches); an empty filter alone answers `400`. What to change — `site`, `location` (a value, or `""`/`null` to clear; a value in another case takes the spelling in use), `add_tags`, `remove_tags`, `snmp_credential_id` (v3.3.6: a set's id, or `null` for SNMP off). Response: `{ "matched": 5, "changed": 3 }`. Audited as `ENDPOINT:BULK_UPDATE`.

### `POST /endpoints/rename` (admin)
Renames a value on every endpoint that has it, which also merges two spellings. Body: `field` (`site`, `location` or `tag`), `from_value` (matched ignoring case), `to_value` (empty or null removes it), `site` (location only: rename within that site). Renaming onto an existing value takes that value's spelling. Response: `{ "changed": 2, "to": "Server Room" }`. Audited as `ENDPOINT:RENAME_LABEL`.

### Site, location and tags on create and update
`POST /endpoints` and `PATCH /endpoints/{id}` accept `site`, `location` and `tags`. Values are trimmed and inner spaces collapsed; a site or location typed in another case is stored in the spelling already in use; tags are lower-cased and deduplicated (at most 20, no commas, quotes, backslashes or braces). On `PATCH`, `""` or `null` clears site or location; a field left out is unchanged.

## 4. Topology & RCA (`/topology`)

### `GET /topology`
Retrieves the parent-child topology DAG (nodes with state and hop level, plus edges). The crossing-free hierarchical layout is computed in the browser by vis-network; the API returns graph data only.
- **Response:** `200 OK` returning an object containing arrays of `nodes` and `edges`.
- Traceroute links are checked every 5 minutes against the next hops exporters reported in the last 15 minutes. An edge then carries `flow_status`: `confirmed` (traffic goes to the next traceroute hop), `differs` (traffic leaves for another next hop) or `inferred` (the next hop did not answer traceroute; flows name it), with a `flow_detail` sentence. A `* * *` node named by flows carries `flow_inferred_ip`. Edges without a matching exporter or recent flows have no `flow_status`.

### `POST /topology/rebuild`
Rebuilds the in-memory topology DAG from the database (requires `Admin` role).

### `POST /topology/discover-all`
Queues background traceroute discovery for all active endpoints that are currently `UP` (requires `Admin` role). They run up to the `discovery_parallel` setting at once, 500 ms apart, with traces through the same router taking turns.

---

## 5. Reports & Telemetry (`/reports`)

### `GET /reports/fleet-summary`
Fleet-wide SLA and state summary for a time window.
- **Query Params:** `start_date`, `end_date` (required, ISO 8601), and optionally the shared endpoint filter (`site`, `location`, `tag`, `type`, `q`, as on `GET /endpoints`), which limits the summary to the matching endpoints by their current assignment.
- Each endpoint carries `site`, `location` and `tags`; with a filter the response also has `filter` and `filter_description`.

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
- **Filter (v3.3.6):** `filter` (`{ "site": [...], "location": [...], "tag": [...], "type": [...], "q": "" }`) may replace `endpoint_ids` (all matching endpoints) or narrow it (the listed endpoints the filter matches).
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
  - `filter` (object, optional, v3.3.6): the shared endpoint filter (`site`, `location`, `tag`, `type`, `q`). Alone it selects every matching endpoint; with `endpoint_ids` it keeps the listed ones it matches. The PDF prints it under the scope title ("Filter: Site Abuja · Tag mpls. Sites, locations and tags as assigned on …"). No endpoints left answers `400`.
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
Retrieves the active system configuration from the database. The alerting and flow fields are also returned in camelCase; the other fields are snake_case only.
- **Response:**
  ```json
  {
    "performance_mode": true,
    "l2_auto_bypass": true,
    "session_timeout": 120,
    "lockout_threshold": 5,
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
  - `performance_mode` (bool, optional): Routes live events between processes through FastStore instead of PostgreSQL `LISTEN`/`NOTIFY`. Sessions and lockouts stay in PostgreSQL either way, and flow ingestion does not depend on it.
  - `l2_auto_bypass` (bool, optional): Controls Layer-2 direct ICMP diagnostic bypass.
  - `session_timeout` (int, 1–1440 min): No longer used since v3.3.6, where each account has its own idle timeout (`/users`). Still accepted and stored so older clients and restored backups don't fail; migration `0022` used it once to set existing viewer accounts' timeouts.
  - `lockout_threshold` (int, 1–100): Maximum failed attempts before IP lockout.
  - `discovery_parallel` (int, 1–8, default 2; v3.3.6): Route traceroutes run at once. Traces whose paths share a router take turns. Incident and root-cause traces get 2 slots on top.
  - `rediscovery_hours` (int, 1–168, default 24; v3.3.6): Each endpoint's route is re-traced on this period, spread out (the UI offers 6, 12 and 24). Replaces the run at midnight.
  - `traffic_alerts_enabled` (bool, default `false`; v3.3.6): Traffic alerts on WAN links with a speed set (see the user guide). Delivered only to channels whose `severity_filters` name `TRAFFIC_HIGH`, `TRAFFIC_NORMAL`, `TRAFFIC_SILENT` or `TRAFFIC_RESUMED`, as event type `TRAFFIC_ALERT`.
  - `traffic_alert_threshold_pct` (int, 50–99, default 80; v3.3.6): Default utilisation threshold, inbound or outbound (the UI offers 70–95).
  - `traffic_alert_minutes` (int, 5–60, default 15; v3.3.6): Minutes above the threshold, every one of them, before a link alerts.
  - `snmp_enabled` (bool, default `false`; v3.3.6): SNMP polling by ephemres-snmpd of endpoints that have a credential set.
  - `snmp_poll_seconds` (30, 60, 120 or 300, default 60; v3.3.6): How often each device's status is polled; other values return `422`.
  - `flow_ingestion_enabled` (bool, optional): Master toggle for passive NetFlow/IPFIX collection. Turning it on runs the flow preflight below first (with the ports in the same request, if given) and answers `400` if it fails.
  - `flow_netflow_port` (int, 1–65535, optional): NetFlow UDP listening port (default: 2055).
  - `flow_ipfix_port` (int, 1–65535, optional): IPFIX UDP listening port (default: 4739).
  - `flow_sampling_multiplier` (int, >= 1, optional): Hardware packet sampling multiplier (e.g. 1000 for 1-in-1000 sampling).

### `POST /settings/flow/preflight`
Checks what `ephemres-flowd` needs before flow ingestion is turned on: FastStore opens, and the UDP ports it will listen on (the ones saved in Settings) are free. The UDP check binds `flow.bind_host` and those ports exactly as the collector would; ports held by an active `ephemres-flowd` are not reported as conflicts. `rmem_max` below 2 MB is reported but does not make `ready` false. (The Redis version and stream-write fields were removed: flows no longer go through a FastStore stream.)
- **Response:** `200 OK`
  ```json
  {
    "status": "success",
    "data": {
      "faststore_connected": true,
      "rmem_max": 212992,
      "rmem_max_supported": false,
      "udp_ports_available": true,
      "port_conflicts": [],
      "ready": true,
      "message": "Kernel rmem_max is 212992 B, below the recommended 2 MB (2097152 B)."
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

### SNMP (`/snmp`, v3.3.6)
Credential sets for ephemres-snmpd and what it learned per endpoint. Secrets (`community`, `auth_password`, `priv_password`) are write-only: responses carry `has_community`, `has_auth_password`, `has_priv_password` instead. Changes are audited (`SNMP_CREDENTIALS:CREATE` / `UPDATE` / `DELETE`, `SNMP:TEST`) without secrets.
- `GET /snmp/credentials` (admin): every set with `level` (`v2c`, `noAuthNoPriv`, `authNoPriv`, `authPriv`) and `endpoints` (how many use it).
- `POST /snmp/credentials` (admin): `name` (unique, 1–64 characters), `version` (`2c` or `3`), `community` (v2c); `username`, `auth_protocol` (`md5`, `sha`, `sha224`, `sha256`, `sha384`, `sha512` or null), `auth_password` (8+), `priv_protocol` (`aes`, `aes128`, `aes192`, `aes256` (net-snmp-style keys), `aes192c`, `aes256c` (Cisco-style keys), `3des`, `des`, or null), `priv_password` (8+) or `priv_same: true` (one password for both), `context` (v3); `port` (161), `timeout_ms` (500–10000, 2000), `retries` (0–3, 1). `422` with a plain reason when a combination is incomplete; `409` on a duplicate name.
- `PATCH /snmp/credentials/{id}` (admin): any subset; a secret left out or empty keeps the stored one.
- `DELETE /snmp/credentials/{id}` (admin): `{ "endpoints_turned_off": n }`; those endpoints are no longer polled.
- `POST /snmp/test` (admin, at most 6 a minute per API worker): `{ "address": "10.20.0.1", "credential_id": "…" }` or `{ "address": "…", "credential": { …fields as in POST… } }` to test before saving. Runs the device survey (a few seconds) and answers `{ ok, error, text, findings, profile, interfaces, system }`.
- `GET /snmp/endpoints/{id}` (any signed-in user): `{ credential_id, credential_name, state: { status (pending, ok, partial, no_reply, auth_failed, error), last_error, last_poll_at, last_ok_at, last_probe_at, probe_requested_at, last_reboot_at, sys_uptime, inventory_at, profile }, interfaces: [{ if_index, if_name, if_descr, if_alias, if_type, speed_mbps, mac, admin_status, oper_status, last_change, hc_counters }], events: [{ at, kind (up, down, added, removed, renumbered, reboot), if_index, if_name, detail }] }` (the 50 latest events).
- `GET /snmp/endpoints/{id}` also returns `neighbours` (step 3): `[{ protocol (lldp, lldp-v2, cdp), local_if_index, local_port, remote_name, remote_chassis, remote_port, remote_port_desc, remote_platform, remote_addresses, matched_endpoint_id, matched_by (address, mac, name), matched_hostname, first_seen, last_seen }]`.
- `GET /topology` edges placed by LLDP/CDP carry `via` (`"LLDP"` or `"CDP"`).
- Alert channels accept the severities `LINK_DOWN` and `LINK_UP` (event type `LINK_ALERT`, WAN links only); like traffic severities, a channel gets them only when ticked.
- `POST /snmp/endpoints/{id}/probe` (admin): asks ephemres-snmpd to read the whole device again at its next turn; `400` if the endpoint has no set.

## 8. User Governance (`/users`)

Manage user accounts and role-based access control (requires `Admin` role).

An admin can't reset, deactivate or change the role of their own account through these routes (`400`); their own password changes through `POST /auth/change-password`. They can change their own `session_idle_minutes` and `max_sessions`.

### `GET /users/session-options`
The idle timeout choices and defaults per role and the session limit bounds, so a form offers exactly what the API accepts:
`{ "idle_minutes": { "ADMIN": [15, 30, 60], "VIEWER": [30, 60, 120, 240] }, "default_idle_minutes": { "ADMIN": 30, "VIEWER": 120 }, "admin_absolute_hours": 12, "max_sessions": { "min": 1, "max": 10, "default": 2 } }`. The session limit default is `[security] max_active_sessions_per_user`.

### `GET /users/`
Lists all user accounts: `id`, `username`, `role`, `is_active`, `must_change_password`, `last_login`, `created_at`, `session_idle_minutes`, `max_sessions`, `active_sessions` (live sessions now; `null` if the session store could not be read).

### `POST /users/`
Creates a new user account.
- **Request Body:** `username` (3 to 50 characters), `role` (`ADMIN` or `VIEWER`), `password` (optional), `session_idle_minutes` (optional, one of the role's choices; default: the role's default), `max_sessions` (optional, 1 to 10; default: `[security] max_active_sessions_per_user`). A timeout the role doesn't offer answers `400`.
- With no `password`, one is generated and returned once as `generated_password` in the response. It is not stored in readable form and can't be shown again. Every new account must change its password at first sign-in.

### `POST /users/{id}/reset-password`
Sets a new password and signs the user out everywhere.
- **Request Body:** `password` (optional). Without it, a password is generated and returned once as `generated_password`.
- The user must change it at the next sign-in.

### `PATCH /users/{id}`
Changes any of `role` (`ADMIN` / `VIEWER`), `is_active`, `session_idle_minutes` and `max_sessions`. A role change or deactivation signs the user out everywhere (reason `ended_by_admin`). `session_idle_minutes` must be one of the choices of the role the account will have after the request (`400` otherwise). A role change without a new timeout fits the old one to the new role: the largest choice not above it, or the smallest choice (a viewer's 240 becomes an admin's 60, an admin's 15 becomes a viewer's 30). A new timeout applies to open sessions on their next request; a new `max_sessions` from the next sign-in.

### `GET /users/{id}/sessions`
The account's live sessions, newest first: `id` (the session's row id), `created_at`, `last_activity_at`, `ends_at` (when it ends if nobody uses it: the earlier of the idle end and the absolute end), `client_ip`, `user_agent` (both recorded at sign-in; empty for sessions from before v3.3.6), and `is_current` (the caller's own session). The token id is never returned. Sessions that have timed out are ended first, so they are not listed.

### `DELETE /users/{id}/sessions/{session_id}`
Ends one session; that browser is told an administrator ended it. Audited as `USER:END_SESSION`. `404` if there is no such live session for the account; `400` for the caller's own current session (use `POST /auth/logout`).

### `DELETE /users/{id}/sessions`
Ends every session of the account, keeping the caller's current session when it is their own account. Audited as `USER:END_ALL_SESSIONS`.

### `DELETE /users/{id}`
Deactivates the account (`is_active = false`) and signs it out everywhere. The account and its audit history are kept; nothing is deleted.

---

## 9. Network Flow & Bandwidth Telemetry (`/bandwidth`)

High-precision passive telemetry endpoints for fleet-wide bandwidth analysis, top talkers, application protocol distribution, exporter health, and conversation drilling.

### Scope: the shared filter and one device (v3.3.6)
Every `GET /bandwidth/*` read below takes the shared filter (`site`, `location`, `tag`, `type`, `q`, the same query params as `GET /endpoints`) and an optional `device_id`. They pick **routers**: the endpoints the filter matches (the list `GET /endpoints/match` gives), of which only routers have flow or SNMP rows. The figures are what those routers saw.
- No filter and no device: every router, plus flows from unregistered sources. Any filter leaves unregistered sources out (they have no site or tags).
- `device_id`: one router. A device outside the filter returns empty results rather than ignoring the filter.
- `exporter_id` is accepted as a synonym for `device_id` on every read, so older links keep working.

### Counted once (v3.3.6)
Traffic between two sites crosses two routers and both export it. Conversations, top talkers, applications, QoS, traffic zones, the overview's directions and the "All flows" series count each conversation once per bucket, at the largest report among the routers in scope; the bandwidth PDF does the same. Up to 2 hours they read `flow_minute_rollups`; beyond, `flow_conversation_hourly_rollups` for whole hours and the minute table for the partial first hour, so their totals agree.

**Directions** follow the traffic zones rule: an address is *internal* when it is a monitored endpoint or private (RFC 1918, loopback, link-local, IPv6 ULA). Inbound = outside to inside, outbound = inside to outside, internal = inside to inside, transit = neither end inside.

### `GET /bandwidth/overview`
Rates over the last five complete minutes for the routers in scope. Since v3.3.6 `total_ingress_*`, `total_egress_*`, `total_internal_*` and `total_transit_*` are the four directions of the zones rule (above), each conversation counted once; until then ingress and egress meant "towards / from a monitored endpoint", so users' PCs counted as transit. Also returns packet rates, `avg_packet_size`, `anomaly_conversations_count` and `anomaly_flows_count` (conversations whose OR'd TCP flags are SYN without ACK or RST without FIN, and the flows in them), `window_seconds` and `exporter_id`.
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

### `GET /bandwidth/wan-links` (v3.3.6)
Each WAN link in scope now, against its capacity: the Traffic page's headline. One row per interface with the WAN role.
- **Query Params:** the scope (above); `window` (optional, `1h`–`30d`) and `series` (bool, default `false`) for the per-link charts.
- **Per link:** `exporter_id`, `exporter_hostname`, `site`, `interface_idx`, `name`;
  - `speed_mbps`, `speed_source` (`manual`, `snmp` or `default`) and `snmp_speed_mbps` (SNMP's own speed, so a typed 20 Mb/s on a 1 Gb/s port can say so);
  - `in_bps`, `out_bps` over the last five complete minutes, `utilization_percentage` (the busier direction; `null` without a speed), `traffic_source` (`snmp` or `flow`);
  - `usual_in`, `usual_out`: the usual range for this hour of the week (`low_bps`, `high_bps`, `samples`), from the last 28 days of hourly averages in the server's timezone; `usual_grouping` (`weekday_weekend` under 21 days of history, else `day_of_week`) and `usual_days`. `null` until the hour has 3 samples;
  - `above_usual_in_pct`, `above_usual_out_pct`: the percentage over the range's top, only when more than 10% over it;
  - `today_peak_in_bps` / `today_peak_out_bps` with the hour they were in (`today_peak_in_hour`, `today_peak_out_hour`): the busiest minute since the server's local midnight;
  - `link_state` from SNMP (`up`, `down`, `shut_down`, `not_answering`; `null` for a router that isn't polled), `down_since`, `snmp_last_ok_at`;
  - `alerts`: open traffic alerts (`kind` `high` or `silent`, `direction`, `since`, `peak_pct`).
- **Order:** sites by name (none last); within a site, utilisation descending, links without a speed after by traffic.
- **With `series=1`:** each link gets `series`, columns over the window (`window` defaults to `1h`): `bucket_seconds` (60 up to 6h, else 3600), `times`, `in_bps`, `out_bps`, `peak_in_bps` / `peak_out_bps` (the busiest minute of each hour), `usual_in_low` / `usual_in_high` / `usual_out_low` / `usual_out_high` per bucket, `down_spans` (`start`, `end`, `null` while still down) from SNMP link events, `window_peak_bps` and `window_peak_percentage`. The bucket still filling is left out; a bucket with no data is `null`.
- **Response:** `window_start`, `window_end`, `day_start` (local midnight), `timezone` (the server's zone name, e.g. `Africa/Lagos`; the page shows times in it), `links`, `window`.

### `GET /bandwidth/anomalies` (v3.3.6)
Suspicious conversations over the picked window, for the Security tiles (the overview's anomaly counts cover five minutes).
- **Query Params:** the scope (above), `window` (`1h`–`30d`; `1y` returns `400`).
- **Response:** `window`, `conversations`, `flows`, `all_flows` (every flow in the window), and per rule `syn_conversations` / `syn_flows`, `rst_conversations` / `rst_flows`. The rule is the conversations' `anomalies` filter.

### `GET /bandwidth/traffic-series`
Returns time-series data points formatted for Chart.js stacked area charts (Ingress vs. Egress).
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `exporter_id` (UUID, optional): Filter by exporter endpoint UUID.
  - `endpoint_id` (UUID, optional): Filter by source or destination endpoint UUID.
  - `interface_idx` (int, optional, with `exporter_id`): One interface only.
  - `wan_only` (bool, default `false`): Fleet or exporter scope only (ignored with `endpoint_id` or `interface_idx`). Returns in/out summed over WAN-role interfaces as `ingress_*`/`egress_*`, from the interface rollups. The response's `wan_only` says whether it applied: with no WAN interface marked it is `false` and the normal series is returned.
- Each point also has `internal_bps`/`internal_pps` and `transit_bps`/`transit_pps` (the zones rule; for the routers in scope each conversation counts once, v3.3.6), packet rates and `flows_per_sec`. The response carries `bucket_seconds` and `directional`; only the fleet-wide `1y` window (API only, daily tier) keeps no endpoint attribution, so there it is `false` and all traffic is reported as transit. Windows up to 30d keep endpoints (minute or hourly tier).
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
- **Query Parameters:** `window`, the scope (above), `endpoint_id` (either side), `ip` (either side), `port` (service port), `dscp` (0–63), `quick` (`all` | `anomalies` | `qos` | `bulk`), `sort` (`bytes` | `flows` | `packets`), `limit` (1–200, default 25), `offset`.
- `quick`: `anomalies` = the OR of the conversation's TCP flags over the window is SYN without ACK or RST without FIN (the same rule as the overview KPI and the PDF report); `qos` = DSCP > 0; `bulk` (v3.3.6) = average packet ≥ 1100 bytes, seen for at least 30 minutes, at 5 Mb/s or more over the time it was seen (`traffic_profile` "Bulk Transfer"; large packets without that are "Download").
- **Response:** `items` (the fields of `top_conversations` below, plus those listed next), `counts` (`all`, `anomalies`, `qos`, `bulk` over every conversation matching the other filters), `total` (count for the requested `quick`), `limit`, `offset`, `window`.
- **Item fields added in v3.3.6:**
  - `service_name`: the port's name from the one table the page and the PDF share ("HTTPS", "QUIC (HTTP/3)" for UDP 443); `null` for a port with no name. `packets_per_flow` is a whole number.
  - `routers`: the routers in scope that reported the conversation, each `exporter_id`, `hostname` (`null` for an exporter that is no endpoint), `site`, and `wan_link`: the router's WAN link name **only when it has exactly one**. Flows are stored per router, not per interface, so with two WAN links the one it arrived on is not known. Sorted by site (none last), then hostname. The bytes are counted once however many routers are listed. Flows from a source matched to no endpoint (stored under the all-zero exporter id by v3.3.0, so kept on upgraded servers for up to 90 days) name no router.
  - `first_seen`, `last_seen`: the start of the first bucket it was seen in and the end of the last (never later than now). To the minute for windows up to 2 hours, to the hour beyond.
- **Errors:** `400` for an unknown `quick` or `sort`, or an invalid `ip`.

### `GET /bandwidth/top-talkers`
Retrieves the highest-volume addresses and the largest conversations within the specified window. Each address's ingress (as destination) and egress (as source) come from one aggregation; `top_conversations` is the first page of `GET /bandwidth/conversations`.
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `limit` (int, default `25`, range `1`–`200`): Maximum records to return.
  - The scope (above); `endpoint_id` (UUID, optional): flows involving one endpoint.
- Each address has `sites` (v3.3.6): an endpoint's own site, else the sites of the routers that saw the address. `ingress_bytes` is what the address received, `egress_bytes` what it sent.
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
Returns the top 15 (protocol, service port) pairs by volume plus an `"Other"` row (`is_other: true`) for the rest; `percentage` is the share of all traffic in the window (`total_bytes`). Applications, QoS and zones count each conversation once (above) and come from one pass over the same rows as conversations. `service_label` comes from the shared port-name table. `traffic_zones` are always classified by address (`zones_resolution` is always `ip`): windows up to 2 hours read `flow_minute_rollups`, longer windows the hourly conversation tier `flow_conversation_hourly_rollups` (90 days). `zones_window_start` gives the start actually used, so a `1y` window reports zones for the last 90 days only.
`qos_source` and `qos_window_start` give the rollup table and start time the QoS (`qos_classes`) breakdown was read from. On TimescaleDB the hourly/daily aggregates have no ToS column, so for `24h`+ windows QoS comes from `flow_conversation_hourly_rollups`, which holds 90 days; a `1y` window then reports QoS for the last 90 days only.
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
      "qos_source": "flow_conversation_hourly_rollups",
      "qos_window_start": "2026-07-23T12:00:00Z",
      "zones_window_start": "2026-07-23T12:00:00Z",
      "zones_resolution": "ip",
      "traffic_zones": []
    },
    "meta": null,
    "error": null
  }
  ```

### `GET /bandwidth/exporters`
Lists all configured flow exporters with interface aliases, along with detected unmatched exporter candidates from the FastStore sliding discovery cache. Since v3.3.6 it takes the shared filter (not `device_id`), lists routers polled over SNMP that export no flows too, and each item has `site`, `snmp_polled` and `sends_flows`; the list is sorted by site (none last), then hostname. Unmatched sources are listed whatever the filter.
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

### Interface traffic sources (v3.3.6)
`GET /bandwidth/interfaces`, `GET /bandwidth/traffic-series` with `exporter_id` or `wan_only`, the WAN totals in `GET /bandwidth/overview`, traffic alerts and the bandwidth PDF read interface traffic per minute from `snmp_interface_minute_rollups` where an interface has a row for that minute, and from `flow_interface_minute_rollups` otherwise. Windows over 7 days do the same per hour with the two hourly aggregates; an hour with any SNMP minute is taken from SNMP. Flow counts always come from flows.

### `GET /bandwidth/interfaces`
Per-interface traffic for one router, or (v3.3.6) for every router in scope.
- **Query Params:** the scope (above), `window` (default `1h`; same values as the other bandwidth endpoints). Without a device, every router in scope; with no filter either, every live endpoint that reported interface traffic in the window, is polled over SNMP or has interfaces typed in. Until v3.3.6 `exporter_id` was required (`422` without it).
- **Response:** `exporter_id` (the device, `null` across devices), `devices` (how many routers), `window`, `covered_seconds`, `source`, `interfaces`.
- **Rows:** interfaces with traffic, typed in, or that SNMP sees up; plus ports an admin shut down and ports with an SNMP down event in the window. A port SNMP sees down with none of those is left out.
- **Order:** one device: by index, shut-down ports last. Across devices: WAN links first by average utilisation (no speed after), then the others by the busier direction's average, shut-down ports last.
- Rates average over `covered_seconds`: from the later of the window start and the router's first data in the window, to now. Up to 7 days the 1-minute rollup is used (`p95_resolution_seconds` 60); 30 days uses the hourly rollup (p95 over hourly averages, `p95_resolution_seconds` 3600). Idle minutes count as zero in the percentile.
- Traffic figures come from the device's SNMP counters for the minutes that have them and from flows otherwise (`services/interface_traffic.py`); `traffic_source` is `snmp` when any minute in the window came from counters, else `flow`. `flow_count` always counts flows. Names and speeds are the typed ones, else the device's from SNMP: each interface also carries `name_source` / `speed_source` (`manual`, `snmp` or `default`), `snmp_name`, `snmp_speed_mbps`, `oper_status` and `admin_status`.
- **Each interface:** `exporter_id`, `exporter_hostname`, `site`, `interface_idx`, `name`, `role` (`"wan"` or `"lan"`); average `in_bps`/`out_bps`, `in_p95_bps`/`out_p95_bps`, `in_peak_bps`/`out_peak_bps` (busiest minute), `utilization_percentage`, `p95_utilization_percentage`, `peak_utilization_percentage`; `now_in_bps` / `now_out_bps` over the last five complete minutes (the same as `GET /bandwidth/wan-links`). When no link speed is configured (`speed_configured: false`) the utilization fields are `null` and `speed_mbps` reports the 1 Gbps default.
- **Status (v3.3.6):** `status` is `up`, `down`, `shut_down` (admin down), `not_answering` (the router doesn't reply to SNMP) or `unknown` (another oper status) for a port SNMP knows; `traffic_seen` or `idle` otherwise. `down_spans` (`start`, `end`, `null` while still down) and `down_seconds` give the time it was down within the window.

### `PUT /bandwidth/endpoints/{id}/interfaces`
Replaces an exporter's interface names and link speeds (requires `Admin` role).
- **Request Body:** object keyed by SNMP `ifIndex`: `{"1": {"name": "WAN-Fiber", "speed_mbps": 1000, "role": "wan"}}`. `name` is 1–64 characters of letters, digits, spaces and `_-./:`; `speed_mbps` is 1–10,000,000, or omitted/`null` to leave the speed unset (utilization is then not shown); `role` is `"wan"` (a link to the internet or a provider, counted in the overview's WAN totals) or `"lan"` (default). `name` is optional since v3.3.6: left out, it is not stored and the interface shows the device's SNMP name (or "Interface N"). `alert` (optional, v3.3.6) is a WAN link's traffic alert rule: `{"mode": "default"}` (or omitted) follows the settings, `{"mode": "custom", "threshold_pct": 50–99, "minutes": 5–60}` sets its own, `{"mode": "off"}` opts it out; out-of-range values return `422`. `GET /bandwidth/interfaces` returns it per interface as `alert` (`null` when not set). The body replaces the whole map, so send every interface with its current values.
