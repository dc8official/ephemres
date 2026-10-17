# LNMP: Network Monitoring Platform v3.1.35s (Enterprise LTS)

A high-precision, decoupled network telemetry and monitoring solution designed for continuous endpoint status verification, low-latency multi-protocol polling, adaptive statistical alerting, automated root-cause analysis (RCA), real-time Server-Sent Events (SSE), dual-driver storage acceleration, enterprise multi-channel notifications, and dynamic topology visualization with crossing-free layout routing.

---

## Architectural Overview

The platform is decoupled into independent, modular layers to guarantee continuous telemetry collection regardless of client-side dashboard activity, heavy API load, or temporary network disruptions:

* **Continuous Event Lifecycle & Zero Row Bloat:** Active state events remain open with `end_time = NULL`. Monitoring cycles increment duration and cycle counters in-place on unchanged state instead of generating 1,440 duplicate rows per endpoint per day, completely eliminating row bloat while maintaining sub-second transition fidelity.
* **Uptime Interval Calculus:** Mathematical SLA reporting intersects actual event durations (`clamped_duration`) with the reporting window, accurately factoring open events using `COALESCE(end_time, NOW())`.
* **DNS Rebinding Prevention (Socket DNS Pinning):** Synthetic probes pre-resolve probe target hostnames and pin subsequent TCP/TLS socket handshakes directly to the validated IP address, eliminating TOCTOU DNS rebinding while preserving RFC 1918 private probing.
* **Deterministic Probe Staggering:** Endpoint probes are deterministically distributed across the 60-second window based on endpoint UUID hash (`int.from_bytes(endpoint_id.bytes[:4], "big") % 60`), eliminating minute-boundary thundering herds.
* **Diagnostic Trace & Subprocess Bounding:** Bounded queue depth (max 10 traces) per endpoint under database write semaphores, and concurrency-limited ICMP ping subprocess fallbacks (`asyncio.Semaphore(25)`).
* **Enterprise Security & Credential Governance:** Enforced production secret key rejection (CWE-798), cryptographically secure 96-bit temporary password generation, strict CORS isolation, and decoupled `DecryptionError` exception handling.
* **Session Deduplication & Eviction Resilience:** Enforces atomic session registration and stateless JWT signing, eliminating premature session eviction under multi-session limits (`max_active_sessions_per_user`).
* **Zero-Trust SSRF Defense & Private Relay Support:** Kernel-level connection validation permitting private RFC 1918 and loopback targets for internal corporate SMTP relays while strictly blocking link-local cloud metadata endpoints (`169.254.169.254`).
* **Database Connection Retry Resilience:** Multi-attempt retry loop with backoff on cold startup (`check_database_connection`), preventing systemd service crash loops during host reboots.
* **TimescaleDB Startup Chunk Pruning & Decompression Safety:** Bounds startup event closure queries to the active 7-day uncompressed window, enabling hypertable partition pruning and enforcing transaction overrides (`max_tuples_decompressed_per_dml_transaction = 0`) to prevent decompression limits from failing engine reboots.
* **Multi-Worker Storage Driver Cluster Synchronization:** PostgreSQL `LISTEN`/`NOTIFY` inter-process coordination keeping Uvicorn multi-worker clusters instantly synchronized on active storage backend state transitions (`SYSTEM_SETTINGS_SYNC`).
* **Bidirectional Warm Session State Migration:** Seamless, zero-downtime session transfer between PostgreSQL and Redis, ensuring zero session drop or operator logout when toggling storage drivers.
* **Enterprise Alerting & Notifications Engine:** Asynchronous, non-blocking notification dispatcher pushing state transitions and RCA incidents across Microsoft Teams (Adaptive Cards v1.4 & HTML fallback), Discord (Rich Embeds), Slack (Block Kit), Generic Webhooks, and direct hardened SMTP Email with socket-level SSRF defense, AES-256-GCM encryption at rest, flapping cooldown, and cascade suppression.
* **Interactive CSV Column Customizer:** Dynamic telemetry export allowing operators to customize exported metrics while strictly enforcing locked, non-negotiable device identity columns (`Hostname`, `IP_Address`).
* **SQLAlchemy 2.0 ORM & Repository Pattern:** Clean data access layer separating business logic from database operations, eliminating raw SQL queries and implementing SQL-level pagination (`limit`, `offset`) across all entities.
* **5-Ping @ 8.0s Concurrency Sweeper:** Polling engine tuned with a spacious **28-second headroom window** before the minute boundary and 0–2000ms randomized startup jitter, completely eliminating connection contention and thundering herds.
* **Dynamic In-Memory Endpoint Registry:** Concurrent-safe registry supporting zero-downtime, sub-minute dynamic additions, updates, and deletions of monitored targets without service restarts.
* **High-Fidelity Route Diagnostics:** Upgraded traceroute engine (`traceroute -n -q 2 -w 3 -m 30 -I`) with multi-probe latency parsing, raw ICMP capability isolation (`CAP_NET_RAW`), and Layer-2 subnet auto-bypass.
* **Real-Time Server-Sent Events (SSE) Stream:** Asynchronous event broker streaming telemetry envelopes (`STATE_TRANSITION`, `NODE_STATE_CHANGE`, `RCA_INCIDENT`) and 15-second heartbeat keep-alives via `GET /api/v1/events/stream`.
* **Multi-Protocol Synthetic Probes:** Lightweight async probes verifying TCP port reachability, HTTP/HTTPS status code validation, and SSL/TLS certificate expiry with strict SSRF defense.
* **Dual-Driver Storage Architecture:** Seamlessly switches between **PostgreSQL-Native** (`LISTEN/NOTIFY` + table sessions) and **Redis-Accelerated** (Pub/Sub + in-memory sessions) drivers via configuration and admin settings.
* **Interactive Crossing-Free Topology Map:** Vue 3 Vis-Network visualizer implementing **BFS DAG Longest-Path Layering** (`Level(v) = max(Level(u) + 1)`), **Sugiyama (1981)** barycenter crossing reduction, **Gansner (1993)** coordinate alignment, **frozen-physics real-time recoloring**, and **Horizontal (LR) ⇄ Vertical (UD)** layout switching.
* **Enterprise Frontend & Accessibility Overhaul:** High-contrast monochrome design system, top summary KPI ribbon with instant filter pills, **Dual View Switcher** (Visual Card Grid vs. Dense Sortable Table), tabular monospace numbers, and WCAG 2.1 AA keyboard focus indicators.
* **TimescaleDB Compression & Retention:** 7-day chunk compression (90%+ disk savings), automated continuous aggregates, and daily automated 90-day retention cleanup.
* **Enterprise Logging Architecture:** Python `RotatingFileHandler` with bounded disk quotas (~150MB total footprint) and strict permission isolation (`0640 netmon:netmon`), preventing log exhaustion on high-volume production deployments.
* **Security & Session Governance:** Sliding 2-hour inactivity timeouts, token-based concurrent session quotas (max 2 active sessions with FIFO rotation), and IP-scoped failed login lockouts (`<Client_IP>:<Username>`).

---

## Detailed Documentation Suite

For comprehensive guides, references, and operational procedures, refer to the `docs/` directory:

* **[Changelog & Technical Evolution](docs/changelog.md):** Complete release notes and evolutionary milestones from Version 1.5 to Version 3.1.35s.
* **[Architecture Deep-Dive](docs/architecture.md):** In-depth analysis of the Repository Layer, Dual-Driver Storage, Concurrency Sweeper, and Topology DAG.
* **[Deployment & Operations Guide](docs/deployment.md):** Production installation, automated in-place upgrades (`upgrade.sh`), Redis configuration, and health verification.
* **[Disaster Recovery & Restoration Runbook](deploy/RESTORE.md):** Step-by-step procedures for restoring TimescaleDB backups, running Alembic migrations, and flushing Redis cache.
* **[User & Operator Guide](docs/user-guide.md):** Guide to navigating the Live KPI ribbon, Card/Table view switcher, High-Fidelity traceroutes, and Admin Settings.
* **[API Reference](docs/api-reference.md):** Complete documentation for REST endpoints, SSE streams (`/events/stream`), pagination parameters, and synthetic probe schemas.
* **[Database & TimescaleDB Deep-Dive](docs/database.md):** Full schema dictionary, hypertable partitioning, 7-day chunk compression, and continuous aggregate policies.
* **[Security Model & Threat Hardening](docs/security.md):** Authentication matrix, Argon2id password hashing, sliding sessions, lockout defense, and Linux capability isolation.
* **[SLA Calculation Methodology](docs/sla-calculation.md):** Mathematical formulation of uptime availability, flap suppression, and blackout neutralization.
* **[Troubleshooting Runbook](docs/troubleshooting.md):** Step-by-step diagnostic workflows, permission fixes, and log inspection.
* **[Developer Guide](docs/developer-guide.md):** Local setup instructions for Vite and Uvicorn, plus guidelines for contributing via Alembic migrations.

---

## Technical Stack

* **Backend:** Python 3.10+, FastAPI, SQLAlchemy 2.0 (Async Declarative Models & Repository Layer), Pydantic Settings, Alembic, Native `asyncio`, Argon2id
* **Storage & Caching:** PostgreSQL 14+ with TimescaleDB Extension, Redis 6+ (Pub/Sub & Session Cache Acceleration)
* **Frontend:** Vue 3 (Composition API), Vite, PrimeVue (Aura Theme Preset), Chart.js, `vis-network` (BFS + Sugiyama Crossing Reduction)
* **System Layer:** Linux `systemd` (Auto-Start Enabled), Native Raw Sockets (`CAP_NET_RAW` capability), System `traceroute`
* **Logging:** Python `RotatingFileHandler` (~150MB bounded footprint) + `systemd-journald`

---

## Recommended System Specifications

### Hardware Sizing Matrix

| Deployment Scale | Monitored Endpoints | CPU Cores | Memory (RAM) | Storage (SSD) | Recommended Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Small / Edge** | Up to 100 | 1 vCPU | 2 GB RAM | 10 GB SSD | Home lab, edge monitoring, small office networks. |
| **Medium Enterprise** | 100 – 500 | 2 vCPUs | 4 GB RAM | 25 GB SSD | Branch networks, regional datacenter monitoring. |
| **Large Scale** | 500 – 2,000+ | 4+ vCPUs | 8 GB+ RAM | 50 GB+ NVMe | Multi-site enterprise datacenters & ISP backbones. |

---

## Getting Started (Production Deployment)

### Pre-Installation Requirement: System Timezone & NTP Time Synchronization

> [!IMPORTANT]
> **Timezone Alignment & Clock Synchronization**:
> LNMP telemetry analysis, uptime SLA calculation, and real-time state transition logging rely on precise timestamp filtering (`start_time <= end_dt`). If the server's timezone or system clock is out of sync with your operational region or client endpoints, newly captured telemetry events, RTT trends, and transition logs may appear blank or be excluded from query windows.
>
> Before installing or starting LNMP services, configure your host's regional timezone and enable network time synchronization (NTP):
>
> ```bash
> # Set the server timezone to your operational region (e.g. Europe/London, America/New_York, UTC, Africa/Lagos)
> sudo timedatectl set-timezone <Your/Region_Timezone>
>
> # Enable Network Time Protocol (NTP) synchronization
> sudo timedatectl set-ntp on
>
> # Verify time synchronization and active timezone
> timedatectl status
> ```

### 1. Fresh Installation

```bash
sudo -i
git clone https://github.com/dc8official/lnmp.git
cd lnmp/deploy
./install.sh
```

### 2. Upgrading to v3.1.35s (Zero Historical Data Loss)

To upgrade an existing installation to Version 3.1.35s:

```bash
cd ~/lnmp
git pull origin v3.1.35s
sudo ./deploy/upgrade.sh
```

*(Alternatively, if running directly on the production host without a cloned repository, you can execute `sudo bash /opt/netmon/noop/deploy/upgrade.sh`)*

The upgrade utility automatically executes:
1. **Pre-Upgrade Backup**: Dumps a timestamped PostgreSQL SQL backup to `/var/backups/netmon/`.
2. **System Dependencies**: Installs and starts `redis-server`, verifies `httpx`, and sets `CAP_NET_RAW` capabilities on `traceroute`.
3. **Smart Config Migration**: Updates `/etc/netmon/config.toml` defaults (5 pings @ 8s, 120m timeout, Redis and Alerting sections) without overwriting secrets.
4. **Service Pause**: Gracefully pauses background daemons.
5. **Code & Dependency Sync**: Pulls latest updates, installs Python requirements (`httpx>=0.27.0`), and compiles Vue 3 assets.
6. **Alembic Forward Migrations**: Runs `alembic upgrade head` applying migration `0007_v3_1_alert_channels_and_delivery.py` to create `alert_channels` and `alert_delivery_logs` while preserving all TimescaleDB hypertables and continuous aggregates.
7. **Systemd Unit Refresh & Restart**: Reloads daemons, enables auto-start, and restarts `redis-server`, `netmon-api`, `netmon-engine`, and `nginx`.
8. **Health Check**: Validates live API status and version endpoint (`/api/v1/version`).

---

## License & Authorship

Core Architecture designed and authored by **Kenneth Nnorom**.

Website: [kennethnnorom.com](https://kennethnnorom.com) | LinkedIn: [linkedin.com/in/kennethnnorom](https://linkedin.com/in/kennethnnorom)

This project is licensed under the terms of the **Apache License 2.0**. See the [LICENSE](LICENSE) file for complete details.
