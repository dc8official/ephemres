# LNMP API Reference — Version 3.1.35s

The LNMP (Network Monitoring Platform) v3.1.35s exposes a RESTful and Server-Sent Events (SSE) API built on FastAPI. The API is located under the `/api/v1` base path and requires JWT Bearer authentication or HttpOnly session cookies for protected endpoints.

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
    "version": "3.1.35s"
  }
  ```

### `GET /api/v1/health`
Performs system health check (database connection, monitoring engine status). Returns `{"status": "ok", "version": "3.1.35s"}`.

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
Retrieves detailed information, status, and baseline metrics for a specific endpoint.

### `PATCH /endpoints/{id}`
Updates configuration flags or properties for an endpoint. Synchronizes changes directly with the in-memory `EndpointRegistry`.

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
    "alertingEnabled": true
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
