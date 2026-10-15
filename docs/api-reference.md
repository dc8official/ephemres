# Ephemres API Reference — Version 3.3.1n

Ephemres v3.3.1n exposes a RESTful and Server-Sent Events (SSE) API built on FastAPI. The API is located under the `/api/v1` base path and requires JWT Bearer authentication or HttpOnly session cookies for protected endpoints.

## Base URL
`http(s)://<server-ip>:<port>/api/v1`

---

## 1. System & Real-Time Events

### `GET /api/v1/version`
Returns the current platform version metadata.
- **Response:** `200 OK`
  ```json
  {
    "status": "ok",
    "version": "3.3.1n"
  }
  ```

### `GET /api/v1/health`
Performs system health check (database connection, monitoring engine status). Returns `{"status": "ok", "version": "3.3.1n"}`.

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
Lists all monitored endpoints with SQL-level pagination and filtering.
- **Query Params:**
  - `page` (int, default: 1): Page number.
  - `page_size` (int, default: 50): Number of items per page.
  - `status` (string, optional): Filter by state (`UP`, `UNSTABLE`, `DOWN`, `UNKNOWN`).
  - `site` (string, optional): Filter by location.
- **Response Envelope:**
  ```json
  {
    "items": [...],
    "total_count": 120,
    "page": 1,
    "page_size": 50,
    "total_pages": 3
  }
  ```

### `POST /endpoints`
Onboards a new endpoint for monitoring with optional synthetic probe configuration.
- **Request Body:**
  ```json
  {
    "hostname": "Web-App-Gateway",
    "ip_address": "192.168.10.1",
    "device_type": "ROUTER",
    "location": "Main Datacenter",
    "is_l2_segment": false,
    "allow_topology_discovery": true,
    "probe_type": "HTTP_STATUS",
    "probe_port": 443,
    "probe_url": "https://service.internal/health",
    "probe_expected_status": 200
  }
  ```

### `GET /endpoints/{id}`
Retrieves detailed information, status, baseline metrics, secondary exporter IP addresses (`flow_exporter_ips`), and interface aliases (`flow_interface_aliases`) for a specific endpoint.

### `PATCH /endpoints/{id}`
Updates configuration flags or properties for an endpoint. Synchronizes changes directly with the in-memory `EndpointRegistry` and flow correlator routing table.
- **Request Body:** Accepts optional fields:
  - `hostname` (string)
  - `ip_address` (string)
  - `device_type` (string: `ENDPOINT`, `TRANSIT_ROUTER`, `L2_SEGMENT`, `FIREWALL`, `SWITCH`)
  - `location` (string)
  - `flow_exporter_ips` (array of strings, e.g. `["192.168.100.1", "10.0.0.254"]`): Secondary exporter IP addresses aliased to this endpoint.
  - `flow_interface_aliases` (object, e.g. `{"1": "WAN-Fiber", "2": "LAN-Trunk"}`): Mapping SNMP `ifIndex` to human-readable labels.

### `DELETE /endpoints/{id}`
Removes an endpoint from monitoring and deregisters it from the polling engine.

---

## 4. Topology & RCA (`/topology`)

### `GET /topology`
Retrieves the complete L2/L3 parent-child adjacency DAG map computed with Sugiyama barycenter crossing reduction.
- **Response:** `200 OK` returning an object containing arrays of `nodes` and `edges`.

---

## 5. Reports & Telemetry (`/reports`)

### `GET /reports/uptime/{endpoint_id}`
Retrieves SLA uptime availability percentage and state distribution metrics.
- **Query Params:** `start_date`, `end_date`.

### `GET /reports/events/{endpoint_id}`
Retrieves paginated historical state transition events.
- **Query Params:** `start_date`, `end_date`, `page`, `page_size`.

### `GET /reports/rtt-trend/{endpoint_id}`
Retrieves time-series latency curves against historical continuous aggregate baselines.

### `GET /reports/timeline/{endpoint_id}`
Retrieves state transition timeline entries.

### `GET /reports/audit-logs`
Retrieves paginated administrative audit logs (requires `Admin` role).

### `POST /reports/telemetry/export-batch` (Alias: `POST /reports/telemetry/export/batch`)
Streams a sanitised CSV containing bulk telemetry data across multiple endpoints using deterministic keyset pagination.
- **Access Control:** Requires `Operator` or `Admin` role (`403 Forbidden` for `Viewer`).
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
- **Access Control:** Requires `Operator` or `Admin` role (`403 Forbidden` for `Viewer`).
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
  - `template_type` (str): `"a"` (Availability & SLA Matrix), `"b"` (Bandwidth & Interface Capacity), or `"master"` (Template C: Combined Master Audit Dossier).
  - `endpoint_ids` (list[UUID]): List of target endpoint UUIDs to compile into the report.
  - `start_date` (datetime): UTC start boundary.
  - `end_date` (datetime): UTC end boundary.
  - `include_rca` (bool, default `true`): Include root-cause analysis diagnostic summaries in outage incident ledgers.
- **Response Headers:**
  - `Content-Type: application/pdf`
  - `Content-Disposition: attachment; filename="<generated-report-name>.pdf"`
  - `X-Report-SHA256: <64-character-sha256-hexdigest>`
- **Security & Immutability:**
  - Air-gapped rendering: Disallows all external network schemas (`http://`, `https://`, `ftp://`) and whitelists local filesystem access strictly to authorized template/asset paths.
  - Standard permission encryption (`pypdf` with `UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION`) enforces read-only immutability.

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
  - `severity_filters` (array of strings, default `["DOWN", "RECOVERED"]`): Triggering states (`"DOWN"`, `"RECOVERED"` / `"UP"`, `"UNSTABLE"`).

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
    "success": true,
    "data": {
      "success": true,
      "status_code": 200,
      "message": "Test alert delivered successfully."
    },
    "message": "Request processed successfully."
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
  - `performance_mode` (bool, optional): Switches between Redis-accelerated and PostgreSQL-native storage drivers.
  - `l2_auto_bypass` (bool, optional): Controls Layer-2 direct ICMP diagnostic bypass.
  - `session_timeout` (int, 1–1440 min): Idle session expiration duration.
  - `lockout_threshold` (int, 1–100): Maximum failed attempts before IP lockout.
  - `flow_ingestion_enabled` (bool, optional): Master toggle for passive NetFlow/IPFIX collection.
  - `flow_netflow_port` (int, 1–65535, optional): NetFlow UDP listening port (default: 2055).
  - `flow_ipfix_port` (int, 1–65535, optional): IPFIX UDP listening port (default: 4739).
  - `flow_sampling_multiplier` (int, >= 1, optional): Hardware packet sampling multiplier (e.g. 1000 for 1-in-1000 sampling).

### `GET /settings/flow/preflight`
Performs live pre-flight verification of Redis connectivity and Stream capabilities prior to enabling passive flow ingestion.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
    "data": {
      "redis_connected": true,
      "redis_version": "7.0.15",
      "redis_version_supported": true,
      "stream_write_success": true,
      "ready": true,
      "message": "Redis v7.0.15 ready with Stream support."
    },
    "message": "Request processed successfully."
  }
  ```

### `GET /settings/organization`
Retrieves corporate organization profile and report branding settings. Available to all authenticated users for client-side rendering.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
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
  - `logo_data`: Must be a valid Base64 data URI (`data:image/...`), max length 2,000,000 characters (~1 MB image file).
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
Retrieves fleet-wide real-time Ingress/Egress bandwidth in bits per second (bps), active exporter count, total flow count, and detected unmatched candidates over the last 5 minutes.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
    "data": {
      "total_ingress_bps": 45210340.50,
      "total_egress_bps": 41732622.00,
      "active_exporters_count": 4,
      "unmatched_exporters_count": 1,
      "total_flows_count": 128450
    },
    "message": "Request processed successfully."
  }
  ```

### `GET /bandwidth/traffic-series`
Returns time-series data points formatted for Chart.js stacked area charts (Ingress vs. Egress).
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `exporter_id` (UUID, optional): Filter by exporter endpoint UUID.
  - `endpoint_id` (UUID, optional): Filter by source or destination endpoint UUID.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
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
    "message": "Request processed successfully."
  }
  ```

### `GET /bandwidth/top-talkers`
Retrieves the highest-volume communicating endpoints and discrete IP conversations within the specified window.
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
  - `limit` (int, default `10`, range `1`–`50`): Maximum records to return.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
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
    "message": "Request processed successfully."
  }
  ```

### `GET /bandwidth/applications`
Returns protocol and service port volume distribution for donut/pie charts.
- **Query Parameters:**
  - `window` (string, default `"1h"`): Time window (`"1h"`, `"6h"`, `"24h"`, `"7d"`, `"30d"`).
- **Response:** `200 OK`
  ```json
  {
    "success": true,
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
      ]
    },
    "message": "Request processed successfully."
  }
  ```

### `GET /bandwidth/exporters`
Lists all configured flow exporters with interface aliases, along with detected unmatched exporter candidates from the Redis sliding discovery set.
- **Response:** `200 OK`
  ```json
  {
    "success": true,
    "data": {
      "exporters": [
        {
          "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          "hostname": "core-gw-01",
          "primary_ip": "10.0.0.1",
          "flow_exporter_ips": ["192.168.100.1"],
          "interface_aliases": { "1": "WAN-Fiber", "2": "LAN-Trunk" },
          "last_flow_time": null
        }
      ],
      "unmatched": [
        {
          "ip_address": "172.16.50.1",
          "last_seen": "2026-09-15T21:00:00Z"
        }
      ]
    },
    "message": "Request processed successfully."
  }
  ```

### `POST /bandwidth/exporters/{ip}/map`
One-click binds an unmatched exporter IP to an existing endpoint's `flow_exporter_ips` array (requires `Admin` role). Immediately removes the IP from the Redis unmatched set and broadcasts a `channel:registry_sync` event to refresh the in-memory correlator table with zero downtime.
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
    "success": true,
    "data": {
      "message": "Successfully mapped exporter IP 172.16.50.1 to core-gw-01."
    },
    "message": "Request processed successfully."
  }
  ```
