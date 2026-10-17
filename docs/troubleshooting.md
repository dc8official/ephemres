# Ephemres Troubleshooting & Disaster Recovery Guide — Version 3.3.2.v

This guide provides systematic diagnostic steps, platform logging architecture specifications, log triage procedures, and solutions for operational issues encountered when running Ephemres in production.

---

## 1. Platform Logging Architecture & Log Triage Guide

Ephemres incorporates a dual-logging architecture: all standard console output is captured by `systemd-journald`, while automated auto-rotating file handlers mirror structured logs directly to `/var/log/ephemres/` (or `./logs/` in non-root test environments) with strict storage quotas.

### A. The 6 Core Log Streams

| Log Stream | Destination / Location | Description & Contents | Default Retention / Rotation |
| :--- | :--- | :--- | :--- |
| **API Server Daemon** | `journalctl -u ephemres-api`<br>`/var/log/ephemres/ephemres-api.log` | Native ASGI REST request logs, authentication events, session validation, route handling, settings updates, and background workers. | 10 MB per file, 5 rotated backups (max 60 MB total). |
| **Monitoring Engine** | `journalctl -u ephemres-engine`<br>`/var/log/ephemres/ephemres-engine.log` | 32-second ICMP sweep cycles, synthetic HTTP/TCP/SSL probes, route traceroutes, RCA analysis, gap detection, and state transitions. | 10 MB per file, 5 rotated backups (max 60 MB total). |
| **Flow Collector Daemon** *(v3.2.0+)* | `journalctl -u ephemres-flowd`<br>`/var/log/ephemres/ephemres-flowd.log` | Passive NetFlow v5/v9 and IPFIX UDP packet decoding, template caching, and FastStore shared-memory stream buffering (`/dev/shm`). | 10 MB per file, 5 rotated backups (max 60 MB total). |
| **Platform Error Aggregator** | `/var/log/ephemres/error.log` | Unified platform-wide sink capturing all `ERROR` and `CRITICAL` log records across all Ephemres Python components. | 10 MB per file, 3 rotated backups (max 30 MB total). |
| **Reverse Proxy (Nginx)** | `/var/log/nginx/access.log`<br>`/var/log/nginx/error.log` | Inbound client HTTP requests, TLS termination, SSE long-polling connection lifecycle, and upstream proxy dispatch. | Managed by system `logrotate` (daily / weekly compression). |
| **Database & Cache Services** | `journalctl -u postgresql`<br>`/dev/shm/ephemres_faststore.db` | PostgreSQL query execution, TimescaleDB hypertable compression jobs, connection pool metrics, and FastStore In-RAM IPC WAL engine state. | Managed by PostgreSQL engine and FastStore RAM lifecycle. |

> [!NOTE]
> **Total Log Footprint Ceiling**: Application file logging in `/var/log/ephemres/` is bounded to a maximum ceiling of **$\sim$150 MB** across all services combined, preventing disk exhaustion on lean edge or VPS instances.

---

### B. Fast Diagnostic Command Playbook

#### 1. Live Multi-Service Streaming
Stream multiple platform daemons concurrently in a single terminal window:
```bash
# Concurrently follow API and Engine daemons in real-time
sudo journalctl -u ephemres-api -u ephemres-engine -f

# Include Flow Collector daemon (v3.2.0+)
sudo journalctl -u ephemres-api -u ephemres-engine -u ephemres-flowd -f
```

#### 2. Filtering by Severity Level
Filter out routine informational sweeps and isolate warnings, errors, and fatal exceptions:
```bash
# Display only ERROR, CRITICAL, and ALERT records for ephemres-api
sudo journalctl -u ephemres-api -p err..emerg --no-pager

# Display only ERROR and CRITICAL records for ephemres-engine
sudo journalctl -u ephemres-engine -p err..emerg --no-pager

# Inspect the unified platform error file in real-time
tail -n 100 -f /var/log/ephemres/error.log
```

#### 3. Time-Bounded Forensic Windows
Isolate log records during a specific outage or incident window:
```bash
# Inspect logs from the last 2 hours
sudo journalctl -u ephemres-engine --since "2 hours ago"

# Inspect logs during an exact incident window
sudo journalctl -u ephemres-api --since "2026-09-17 14:00:00" --until "2026-09-17 14:30:00"
```

#### 4. Reverse Proxy & Upstream Tracing
```bash
# Check if Nginx is returning 502 Bad Gateway or 504 Gateway Timeout
sudo tail -n 50 -f /var/log/nginx/error.log

# Track HTTP status codes returned to client browsers
sudo tail -n 100 /var/log/nginx/access.log | awk '{print $7, $9}'
```

---

### C. Symptom-to-Log Triage Decision Matrix

| Observable Symptom | Primary Log Target | High-Signal Search Term / Pattern | Immediate Triage Action |
| :--- | :--- | :--- | :--- |
| **Daemon in crash loop on boot** (`status=1/FAILURE`) | `journalctl -u ephemres-engine -n 100` | `asyncpg.exceptions`, `InternalServerError`, `max_tuples_decompressed` | Check TimescaleDB chunk pruning (Section 2.H). |
| **User repeatedly kicked to login page** | `/var/log/ephemres/ephemres-api.log`<br>`journalctl -u ephemres-api` | `Session evicted`, `quota exceeded`, `HTTPException: 401` | Check multi-worker session store sync (Section 2.I). |
| **Browser shows "Unable to connect" / HTTP 502** | `/var/log/nginx/error.log`<br>`journalctl -u ephemres-api` | `connect() failed (111: Connection refused)`, `Uvicorn running` | Verify Uvicorn socket listening on `127.0.0.1:8000`. |
| **Probe sweeps failing with permission errors** | `journalctl -u ephemres-engine` | `Operation not permitted`, `socket.error: [Errno 1]` | Re-apply `setcap cap_net_raw+ep` (Section 2.A). |
| **Account locked / HTTP 403 on login** | `/var/log/ephemres/ephemres-api.log` | `Account temporarily locked`, `record_failed_attempt` | Wait for 15m window or run `reset-admin-password.sh` (Section 2.B). |
| **Live SSE cards not updating / split-brain** | `/var/log/ephemres/ephemres-api.log` | `TelemetryRelay: listener error`, `FastStoreEventBroker` | Verify PostgreSQL `LISTEN` or FastStore `/dev/shm` IPC state. |
| **Historical charts or SLA blank** | `/var/log/ephemres/ephemres-api.log` | `timedatectl`, `clock skew`, `unknown_seconds` | Synchronize NTP clock and timezone (Section 2.G). |

---

## 2. Common Issues & Solutions

### A. ICMP Polling Fails with "Operation Not Permitted" / Raw Socket Permission Denied
* **Cause**: The `ephemres-engine` daemon requires raw socket capability (`CAP_NET_RAW`) to craft ICMP packets without running as `root`. If Python runs under a virtualenv, the capability must be placed on the real, resolved Python binary target (not a symlink) as well as the traceroute binary.
* **Fix**:
  ```bash
  # Resolve symlinked python binary in virtual environment and grant raw socket access
  PYTHON_BIN=$(readlink -f /opt/ephemres/backend/venv/bin/python3)
  sudo setcap cap_net_raw+ep "${PYTHON_BIN}"
  
  # Ensure traceroute also retains raw network capabilities
  sudo setcap cap_net_raw+ep $(command -v traceroute)
  
  # Verify capabilities
  getcap "${PYTHON_BIN}" $(command -v traceroute)
  
  # Restart monitoring engine
  sudo systemctl restart ephemres-engine
  ```

### B. User Account Temporarily Locked Out (HTTP 403)
* **Cause**: 5 consecutive failed login attempts from a single IP within a 15-minute window trigger an IP-scoped security lockout.
* **Fix**:
  - Wait 15 minutes for the automated sliding lockout window to expire.
  - Or reset the user's password directly from the server CLI:
    ```bash
    sudo /opt/ephemres/deploy/reset-admin-password.sh <username> <new_password>
    ```

### C. Forgot Admin Password / Out-of-Band Recovery
* **Fix**:
  ```bash
  cd /opt/ephemres/deploy
  sudo ./reset-admin-password.sh <username> <new_password>
  ```

### D. TimescaleDB Chunk Compression Verification
* **Check Compression Status**:
  ```bash
  sudo -u postgres psql -d netmon -c "
    SELECT hypertable_name, total_chunks, number_compressed_chunks 
    FROM timescaledb_information.hypertable_compression_stats;
  "
  ```
* **Manually Trigger Compression on Older Chunks**:
  ```bash
  sudo -u postgres psql -d netmon -c "
    SELECT compress_chunk(c) 
    FROM show_chunks('endpoint_events', older_than => INTERVAL '7 days') c;
  "
  ```

### E. Frontend Shows "Unable to connect to Ephemres Server" (HTTP 502 / Connection Refused)
* **Diagnosis**:
  1. Check if the Uvicorn Native ASGI daemon is running locally:
     ```bash
     curl -I http://127.0.0.1:8000/api/v1/health
     ```
  2. Check Nginx reverse proxy configuration and error logs:
     ```bash
     sudo nginx -t
     sudo tail -n 50 /var/log/nginx/error.log
     ```
  3. Restart API service and Nginx:
     ```bash
     sudo systemctl restart ephemres-api nginx
     ```

### F. Diagnostic Traceroutes Failing or Taking Too Long
* **Cause**: The target network or endpoint is dropping UDP/ICMP probe packets, or local firewall blocks outbound traceroutes.
* **Diagnosis**:
  ```bash
  # Test traceroute manually from server CLI
  traceroute -n -w 2 -m 15 <target_ip>
  ```
* **Note**: In Ephemres, traceroute timeouts are handled gracefully and anonymous hops (`* * *`) are rendered safely without crashing topology calculations.

### G. Endpoint Telemetry, RTT Trends, or Transition Logs Appear Blank
* **Symptoms**:
  - Dashboard endpoint cards display live operational status and latency.
  - Clicking into **Endpoint Detail View** shows:
    - Blank state transition logs (*"No state transitions recorded in this period"*).
    - Blank RTT latency trend chart (*"No RTT data available for this period"*).
    - Missing state timeline history.
* **Cause**:
  - **Host Timezone Misconfiguration or Clock Skew**: The server's timezone or system clock is out of sync with the operational region or client browser. Because telemetry queries query events within strict time boundaries (`start_time <= end_dt`), a server clock lagging behind real time or configured with an incorrect timezone offset causes newly recorded events to fall outside the query window.
* **Fix**:
  1. Inspect the server's current time, timezone, and NTP synchronization:
     ```bash
     timedatectl status
     ```
  2. Set the correct regional timezone (e.g., `Europe/London`, `America/New_York`, `UTC`, `Africa/Lagos`):
     ```bash
     sudo timedatectl set-timezone <Your/Region_Timezone>
     ```
  3. Ensure Network Time Protocol (NTP) synchronization is enabled:
     ```bash
     sudo timedatectl set-ntp on
     ```
  4. Confirm that the system clock is synchronized:
     ```bash
     timedatectl
     ```
  5. Restart the monitoring daemons so queries and event logging immediately align with the synchronized clock:
     ```bash
     sudo systemctl restart ephemres-engine ephemres-api
     ```

### H. Engine Startup Crash: TimescaleDB Tuple Decompression Limit Exceeded (`max_tuples_decompressed_per_dml_transaction`)
* **Symptoms**:
  - `systemctl status ephemres-engine` shows repeated crash loops with exit status `1/FAILURE` upon daemon restart.
  - `journalctl -u ephemres-engine` displays:
    ```text
    asyncpg.exceptions.InternalServerError: current limit: 100000, tuples decompressed: 317367
    HINT: Consider increasing timescaledb.max_tuples_decompressed_per_dml_transaction or setting it to 0.
    [SQL: UPDATE endpoint_events SET end_time = ... WHERE end_time IS NULL]
    ```
* **Cause**:
  - `endpoint_events` is a TimescaleDB hypertable with an automated chunk compression policy for records older than 7 days.
  - When the engine restarts after an outage or extended operations where unclosed historical events accumulated in compressed chunks, updating rows without bounding the query forces TimescaleDB to decompress all historical chunks across database history.
  - TimescaleDB's default safety limit (`max_tuples_decompressed_per_dml_transaction = 100000`) halts the update with an `InternalServerError`, causing `ephemres-engine` to fail during `resolve_startup_state()`.
* **Fix**:
  1. Configure the decompression limit threshold to 0 (unlimited) at the database level:
     ```bash
     sudo -u postgres psql -d netmon -c "ALTER DATABASE netmon SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0;"
     ```
  2. Sanitize legacy historical unclosed events trapped in compressed chunks:
     ```bash
     sudo -u postgres psql -d netmon -c "
     DO \$\$
     BEGIN
         IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
             PERFORM set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', '0', false);
         END IF;
         UPDATE endpoint_events
         SET end_time = start_time,
             duration_seconds = 0
         WHERE end_time IS NULL
           AND start_time < NOW() - INTERVAL '7 days';
     END \$\$;"
     ```
  3. Restart the monitoring engine:
     ```bash
     sudo systemctl restart ephemres-engine
     ```

### I. Admin Repeatedly Logged Out After Switching Storage Driver (Multi-Worker Session Store Desynchronization)
* **Symptoms**:
  - The administrator toggles **Performance Mode** (FastStore In-RAM IPC) in the Admin Settings console.
  - The administrator is immediately logged out back to the login page.
  - Upon logging back in, navigating to the **Admin Settings** tab immediately kicks the administrator back to `/login` on every attempt.
  - The issue persists until the API daemon is restarted.
* **Cause**:
  - In production, `ephemres-api` operates under Uvicorn with multiple worker processes (`--workers 2`).
  - Worker processes have isolated memory spaces. In legacy releases prior to v3.1.3s, updating `performance_mode` via `PATCH /api/v1/settings` re-initialized the storage driver only in the worker process that handled the HTTP request.
  - The other worker remained connected to PostgreSQL, creating an in-memory split-brain state where Worker 1 validated sessions against FastStore while Worker 2 validated sessions against PostgreSQL.
  - Navigating to Admin Settings triggers concurrent HTTP requests. Any request routed to the desynchronized worker returned `HTTP 401 Unauthorized`, causing the frontend router interceptor to clear credentials and redirect to `/login`.
* **Fix**:
  1. **Immediate Service Realignment**:
     Restart the API daemon to synchronize all Uvicorn worker processes from the persisted database settings:
     ```bash
     sudo systemctl restart ephemres-api
     ```
  2. **Permanent Resolution**:
     Ephemres incorporates cluster-wide PostgreSQL `LISTEN`/`NOTIFY` synchronization (`SYSTEM_SETTINGS_SYNC`) and bidirectional warm session migration, eliminating worker split-brain states automatically without service restarts.

### J. PostgreSQL Connection Pool Exhaustion (`TooManyConnectionsError`)
* **Symptoms**:
  - `ephemres-api` or `ephemres-engine` logs display:
    ```text
    asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients already
    ```
  - Incoming HTTP requests receive `HTTP 500 Internal Server Error`.
* **Cause**:
  - The total number of pooled connections requested across all Uvicorn workers and engine background tasks exceeds PostgreSQL's configured `max_connections` ceiling.
* **Fix**:
  1. Inspect PostgreSQL's active connection count and states:
     ```bash
     sudo -u postgres psql -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
     ```
  2. Increase `max_connections` in PostgreSQL configuration (`/etc/postgresql/<version>/main/postgresql.conf`), setting it to 150 or higher:
     ```text
     max_connections = 150
     ```
  3. Reload PostgreSQL:
     ```bash
     sudo systemctl reload postgresql
     ```

### K. FastStore Shared-Memory IPC Inspection & Diagnostics (`/dev/shm`)
* **Symptoms**:
  - NetFlow telemetry not reaching rollup tables, or real-time event broadcasts stalling when Performance Mode is active.
* **Diagnosis**:
  1. Inspect FastStore shared-memory database file existence, permissions, and size:
     ```bash
     ls -lh /dev/shm/ephemres_faststore.db*
     ```
  2. Query FastStore internal tables and stream backlog using the standard `sqlite3` CLI:
     ```bash
     # Inspect queued NetFlow stream batches
     sqlite3 /dev/shm/ephemres_faststore.db "SELECT stream_key, COUNT(*) AS queued_batches FROM stream_entries GROUP BY stream_key;"

     # Inspect active cached sessions
     sqlite3 /dev/shm/ephemres_faststore.db "SELECT count(*) FROM session_entries WHERE expires_at > unixepoch();"
     ```
  3. If the database file is corrupted or locked by an orphaned process:
     ```bash
     sudo systemctl stop ephemres-api ephemres-engine ephemres-flowd
     rm -f /dev/shm/ephemres_faststore.db*
     sudo systemctl start ephemres-api ephemres-engine ephemres-flowd
     ```

---

## 3. Database Disaster Recovery

For complete details on cross-version restorations (including TimescaleDB internal chunk remapping, schema drift reconciliation, and `$argon2id$` to `$scrypt$` credential migrations), refer to the comprehensive [Disaster Recovery Runbook](file:///opt/ephemres/deploy/RESTORE.md).

### Creating an Immediate Manual Backup
```bash
mkdir -p /var/backups/ephemres
sudo PGPASSWORD="netmon_secure_password" pg_dump -h 127.0.0.1 -U netmon_user -d netmon -F p -f /var/backups/ephemres/manual_backup_$(date +%Y%m%d_%H%M%S).sql
```

### Restoring from a Backup File (Automated Cross-Version Engine)
```bash
# Execute the automated cross-version restoration utility
sudo bash /opt/ephemres/deploy/restore.sh \
  --backup-file /var/backups/ephemres/<backup_file_name>.sql \
  --admin-password "<temporary_admin_password>"
```

### Emergency Platform Verification Checklist
Following any restoration or major incident, verify platform health across all layers:
```bash
# 1. Daemon service health
sudo systemctl status ephemres-api ephemres-engine ephemres-flowd nginx

# 2. REST API & schema version response
curl -s http://127.0.0.1:8000/api/v1/health | jq .
curl -s http://127.0.0.1:8000/api/v1/version | jq .

# 3. ICMP raw socket capability check
getcap $(readlink -f /opt/ephemres/backend/venv/bin/python3) $(command -v traceroute)

# 4. FastStore IPC shared memory check
ls -l /dev/shm/ephemres_faststore.db*
```
