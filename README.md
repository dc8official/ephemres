# LNMP: Network Monitoring Platform v3.3.0

A high-precision, decoupled network telemetry and monitoring platform combining active multi-protocol health probing with passive high-volume network flow ingestion (NetFlow v5, NetFlow v9, and IPFIX) and per-interface throughput/utilization telemetry. Engineered for continuous endpoint verification, sub-second route diagnostics, adaptive statistical alerting, automated root-cause analysis (RCA), real-time Server-Sent Events (SSE), dual-driver storage acceleration, enterprise multi-channel incident dispatch, and dynamic topology visualization with crossing-free layout routing.

---

## Architectural Overview

The platform is decoupled into independent, modular layers to guarantee continuous telemetry collection regardless of client-side dashboard activity, heavy API load, or temporary network disruptions:

* **Network Flow Telemetry Ingestion (NetFlow v5, NetFlow v9, IPFIX):** Dedicated, decoupled collector daemon (`netmon-flowd`) listening on UDP 2055 (NetFlow) and UDP 4739 (IPFIX) with 4MB kernel socket buffers (`SO_RCVBUF`), zero-allocation binary decoders, dynamic template caching (1,800s TTL), orphan data set ring buffering, and dual-stack IPv4/IPv6 support.
* **Dual-Role $\mathcal{O}(1)$ In-Memory Correlation & Discovery:** In-memory hash indexing providing sub-microsecond matching for both Exporters (primary IP + `flow_exporter_ips` aliases) and Monitored Participants without database lookups; automatic Redis-backed discovery banner for unmapped exporter candidates with 1-click UI alias binding.
* **4-Tier Hierarchical Storage Lifecycle:** Sliding 2-hour Redis Stream shock-absorber (`stream:netflow:raw`) with memory circuit breaker (>85%), 7-day 1-minute TimescaleDB rollups (`flow_minute_rollups` and `flow_interface_minute_rollups`) with columnar compression, 30-day 1-hour continuous aggregates (`flow_hourly_rollups`), and 365-day daily totals (`flow_daily_rollups`).
* **Fleet Bandwidth & Deep-Dive Perspective Tabs:** Fleet-wide `/bandwidth` dashboard featuring real-time KPI ribbon, Chart.js stacked Ingress/Egress area charts, Top Talkers, Application distribution donut, forensic IP-to-IP conversation explorer, and accessible tab switcher in `EndpointDetailView.vue` (ICMP Diagnostics ⇄ Flow Telemetry).
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

* **[v3.3.0 Release Notes](docs/release-notes/v3.3.0.md):** Major release highlights, new features, and upgrade instructions for v3.3.0.
* **[v3.2.0 Release Notes](docs/release-notes/v3.2.0.md):** Network flow telemetry foundation notes.
* **[v3.2 Hybrid Active/Passive Architecture Guide](docs/architecture/v3.2-hybrid-active-passive.md):** Architectural breakdown of active synthetic polling integrated with line-rate NetFlow/IPFIX flow ingestion.
* **[Changelog & Technical Evolution](docs/changelog.md):** Complete release notes and evolutionary milestones from Version 1.0 to Version 3.3.0.
* **[Network Flow Configuration & Telemetry Guide](docs/netflow-guide.md):** Production runbook for NetFlow v5/v9 and IPFIX collector setup, exporter vendor configuration templates (Cisco, Juniper, Mikrotik, Linux `fprobe`, pfSense/OPNsense), timeout standards, and UI alias mapping.
* **[Architecture Deep-Dive](docs/architecture.md):** In-depth analysis of the Repository Layer, Dual-Driver Storage, Concurrency Sweeper, Network Flow Telemetry Pipeline (`netmon-flowd`), and Topology DAG.
* **[Deployment & Operations Guide](docs/deployment.md):** Production installation, automated in-place upgrades (`upgrade.sh`), Redis configuration, and health verification.
* **[Disaster Recovery & Restoration Runbook](deploy/RESTORE.md):** Step-by-step procedures for restoring TimescaleDB backups, running Alembic migrations, and flushing Redis cache.
* **[User & Operator Guide](docs/user-guide.md):** Guide to navigating the Live KPI ribbon, Card/Table view switcher, High-Fidelity traceroutes, Bandwidth Fleet Dashboard, and Admin Settings.
* **[API Reference](docs/api-reference.md):** Complete documentation for REST endpoints, SSE streams (`/events/stream`), pagination parameters, synthetic probe schemas, and bandwidth flow analytics (`/api/v1/bandwidth/*`).
* **[Database & TimescaleDB Deep-Dive](docs/database.md):** Full schema dictionary, hypertable partitioning, 7-day chunk compression, flow rollup hypertables, and continuous aggregate policies.
* **[Security Model & Threat Hardening](docs/security.md):** Authentication matrix, Argon2id password hashing, sliding sessions, lockout defense, Linux capability isolation, and SSRF guardrails.
* **[SLA Calculation Methodology](docs/sla-calculation.md):** Mathematical formulation of uptime availability, flap suppression, and blackout neutralization.
* **[Troubleshooting Runbook](docs/troubleshooting.md):** Step-by-step diagnostic workflows, permission fixes, flow socket buffer sizing, and log inspection.
* **[Developer Guide](docs/developer-guide.md):** Local setup instructions for Vite and Uvicorn, plus guidelines for contributing via Alembic migrations.

---

## Technical Stack

* **Backend:** Python 3.10+, FastAPI, SQLAlchemy 2.0 (Async Declarative Models & Repository Layer), Pydantic Settings, Alembic, Native `asyncio`, Argon2id, NetFlow v5/v9 & IPFIX binary protocol decoders
* **Storage & Caching:** PostgreSQL 14+ with TimescaleDB Extension (Hypertables, Continuous Aggregates & Columnar Compression), Redis 6+ (Pub/Sub, Stream Shock-Absorber `stream:netflow:raw` & In-Memory Session Cache Acceleration)
* **Frontend:** Vue 3 (Composition API), Vite, PrimeVue (Aura Theme Preset), Chart.js, `vis-network` (BFS + Sugiyama Crossing Reduction)
* **System Layer:** Linux `systemd` (`netmon-api`, `netmon-engine`, `netmon-flowd`), Native Raw Sockets (`CAP_NET_RAW` capability), System `traceroute`, UDP Datagram Sockets (2055, 4739)
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

### 2. Upgrading to v3.3.0 (Zero Historical Data Loss)

To upgrade an existing installation to Version 3.3.0:

```bash
cd ~/lnmp
git pull origin v3.3.0
sudo ./deploy/upgrade.sh
```

*(Alternatively, if running directly on the production host without a cloned repository, you can execute `sudo bash /opt/netmon/noop/deploy/upgrade.sh`)*

The upgrade utility automatically executes:
1. **Pre-Upgrade Backup**: Dumps a timestamped PostgreSQL SQL backup to `/var/backups/netmon/`.
2. **System Dependencies**: Installs and starts `redis-server`, verifies `httpx`, and sets `CAP_NET_RAW` capabilities on `traceroute`.
3. **Smart Config Migration**: Updates `/etc/netmon/config.toml` defaults (5 pings @ 8s, 120m timeout, Redis, Alerting, and `[flow]` telemetry sections) without overwriting secrets.
4. **Service Pause**: Gracefully pauses background daemons (`netmon-api`, `netmon-engine`, and `netmon-flowd`).
5. **Code & Dependency Sync**: Pulls latest updates, installs Python requirements, and compiles Vue 3 assets.
6. **Alembic Forward Migrations**: Runs `alembic upgrade head` applying migration `0012_v3_3_flow_interface_telemetry.py` to add `device_role` (`ck_endpoints_device_role`) and create the `flow_interface_minute_rollups` hypertable (with 7-day retention) while preserving all historical ICMP/probe metrics and continuous aggregates.
7. **Systemd Unit Refresh & Restart**: Installs `netmon-flowd.service`, reloads systemd daemons, enables auto-start on boot, and restarts `redis-server`, `netmon-api`, `netmon-engine`, and `nginx`. If `[flow].enabled = true`, automatically enables and starts `netmon-flowd` and configures UFW firewall rules for UDP 2055 and UDP 4739.
8. **Health Check**: Validates live API status and version endpoint (`/api/v1/version`).

---

## License & Authorship

Core Architecture designed and authored by **Kenneth Nnorom**.

Website: [kennethnnorom.com](https://kennethnnorom.com) | LinkedIn: [linkedin.com/in/kennethnnorom](https://linkedin.com/in/kennethnnorom)

This project is licensed under the terms of the **Apache License 2.0**. See the [LICENSE](LICENSE) file for complete details.
