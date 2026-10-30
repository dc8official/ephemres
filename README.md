# Ephemres: Autonomous Network Observability Platform v3.3.5

> **Vertical Integration Architecture — Zero-Dependency Native Core**

A high-precision, decoupled network telemetry and autonomous monitoring platform combining active multi-protocol health probing with passive high-volume network flow ingestion (NetFlow v5, NetFlow v9, and IPFIX), WAN edge capacity accounting, per-cycle probe telemetry, and NetFlow-assisted route discovery. Engineered for continuous endpoint verification, sub-second route diagnostics, adaptive statistical alerting, automated root-cause analysis (RCA), real-time Server-Sent Events (SSE), in-RAM accelerated storage, enterprise multi-channel incident dispatch, and dynamic topology visualization with crossing-free layout routing.

Beginning in **Version 3.3.2.v** and expanded in **Version 3.3.5**, Ephemres operates on a **Vertically Integrated Architecture**: eliminating 13 heavy external runtime frameworks, third-party ORMs, and daemon dependencies (`fastapi`, `starlette`, `pydantic`, `sqlalchemy`, `alembic`, `redis`, `psycopg2-binary`, `httpx`, `jinja2`, `weasyprint`, `pypdf`, `psutil`, `argon2-cffi`) in favor of **12 zero-dependency native engines** residing directly in `backend/app/core/`. The entire platform runs on the pure Python Standard Library plus just two lightweight production drivers: `uvicorn` (ASGI HTTP server) and `asyncpg` (PostgreSQL/TimescaleDB binary protocol driver).

---

## 🏛️ The Vertical Integration Breakthrough

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                           Ephemres Application Layer                           │
│     Active ICMP Poller  •  Passive Flow Daemon (flowd)  •  REST & SSE API      │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼────────────────────────────────────────┐
│                   Layer 0: Native Zero-Dependency Core Engines                 │
│                              (backend/app/core/)                               │
│                                                                                │
│   • asgi.py        Pure-stdlib ASGI 3.0 router, DI, lifespan & middleware      │
│   • orm.py         Zero-dependency async SQL query compiler & unit-of-work     │
│   • schema.py      Pure-stdlib validation, type coercion & JSON serialization  │
│   • faststore.py   In-RAM /dev/shm SQLite WAL engine with Redis async API      │
│   • migrator.py    Native async TimescaleDB/PostgreSQL DDL migration engine    │
│   • crypto.py      Pure-stdlib Scrypt key derivation & constant-time validation│
│   • restore.py     Cross-version database backup restoration engine            │
│   • http_client.py RFC-compliant SSRF-hardened async HTTP socket client        │
│   • netlink.py     Pure Linux RTNETLINK binary parser & /proc diagnostics      │
│   • pdf.py         Pure-stdlib PostScript/PDF binary document generator        │
│   • template.py    Pure-stdlib Jinja2-compatible template compiler             │
│   • watchdog.py    Zero-dependency systemd sd_notify UNIX socket client        │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼────────────────────────────────────────┐
│                   Minimal Production Drivers (Only 2 Packages)                  │
│                   • uvicorn (HTTP Server)  • asyncpg (Database)                │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Architectural & Operational Benchmark Matrix

| Dimension | Legacy Architecture (`v3.3.1n`) | Vertical Integration (`v3.3.5`) | Operational Benefit |
| :--- | :--- | :--- | :--- |
| **Python Dependencies** | 15 external packages (~150MB wheels) | **2 packages** (`uvicorn`, `asyncpg`) | Eliminates dependency rot, transitive CVEs, and installation breakages. |
| **Startup Time** | 2.1–2.4 s until the API answers (code load alone ~2.0 s) | **1.0–1.7 s** until the API answers (code load alone ~0.4 s): about twice as fast | Quicker process restarts and test cycles. |
| **Worker Memory Footprint** | ~155 MB per worker | **~47 MB per worker** (about 70% less) | Enables deployment on edge nodes and ultra-low-memory containers. |
| **External Daemons** | PostgreSQL + TimescaleDB + `redis-server` | **PostgreSQL + TimescaleDB** (Redis eliminated) | FastStore runs in `/dev/shm` SQLite WAL mode; zero daemon failure modes. |
| **System Shared Libraries** | `libpango`, `libharfbuzz`, `cffi`, `python3-dev` | **Zero external C-libraries** | Pure stdlib implementation runs on any vanilla Linux distribution. |
| **ORM & SQL Overhead** | Heavy SQLAlchemy mapper metamodels | **Direct native asyncpg SQL compilation** | Zero-reflection query generation with sub-millisecond execution. |
| **Schema Validation** | Pydantic v2 Rust binary extension | **Pure-stdlib `app.core.schema` engine** | Native Python typing with zero binary ABI incompatibilities. |

Startup time and memory were measured on v3.3.0 against v3.3.5, running the API as its systemd unit does (uvicorn, 2 workers), two runs each. "Until the API answers" is the time from launch to the first successful `/api/v1/health` response; "code load" is the time to import the application.

---

## ⚡ Core System Architecture

1. **Network Flow Telemetry Ingestion (`ephemres-flowd`):** Autonomous collector daemon listening on UDP 2055 (NetFlow v5/v9) and UDP 4739 (IPFIX) with 4MB kernel socket buffers (`SO_RCVBUF`), zero-allocation binary decoders (`struct.unpack_from`), dynamic template caching (1,800s TTL), and orphan data set ring buffering.
2. **FastStore In-RAM Accelerated Storage:** Pure-stdlib SQLite `WAL` mode storage residing in `/dev/shm` with `synchronous=OFF`, delivering a drop-in async Redis client replacement (`FastStoreClient`) supporting Key-Value with expiry, Sets, Sorted Sets (ZSET sliding 24h prune), high-volume Streams (`XADD`, `XREADGROUP`, `XACK`, `XTRIM`), and inter-process Pub/Sub.
3. **Dual-Role $\mathcal{O}(1)$ In-Memory Correlation:** In-memory hash indexing mapping exporter IP addresses (primary endpoint IP + `flow_exporter_ips` secondary aliases) and traffic participants without database roundtrips.
4. **4-Tier Hierarchical Storage Lifecycle:** FastStore Stream shock-absorber (`stream:netflow:raw`), 1-minute TimescaleDB rollups (`flow_minute_rollups`, 90-day retention with columnar compression; `flow_interface_minute_rollups`, 7-day retention), real-time 1-hour continuous aggregates (`flow_hourly_rollups`, 30-day retention; `flow_interface_hourly_rollups`, 365-day retention with busiest-minute peaks), and 365-day daily totals (`flow_daily_rollups`).
5. **Traffic Analytics, WAN Edge Totals & Per-Interface Telemetry:** Multi-view Traffic Analytics cockpit (`Overview`, `Interfaces`, `Conversations`, `QoS`, `Security`), server-side conversation forensics, average/p95/peak link utilization against provisioned speeds, WAN interface role (`role: "wan"`) counting network-edge traffic once at the perimeter, and 1-click exporter enrollment at `/bandwidth`.
6. **NetFlow Route Discovery & Topology Augmentation:** Decodes IPv4/IPv6 next-hop addresses from NetFlow v5/v9 and IPFIX (`flow_path_observations`), detects sustained route shifts vs. ECMP load-balancing (`flow_route_changes`), triggers targeted re-traceroutes (`ROUTE_HINT`) for `UP` endpoints, verifies topology links (`confirmed`, `differs`, `inferred` silent `* * *` hops), and attaches flow evidence to RCA incidents.
7. **Enterprise Alerting Subsystem:** Independent background task pool pushing state transitions and RCA incidents across Microsoft Teams (Adaptive Cards v1.4 & HTML fallback), Discord (Rich Embeds), Slack (Block Kit), Generic Webhooks, and direct hardened SMTP Email with socket-level SSRF defense, Scrypt encryption at rest, flapping cooldown, and cascade suppression.
8. **Native Async ORM (`app.core.orm`):** Zero-dependency data access layer supporting declarative models, column constraints, relationship navigation, transactional sessions, and query compilation (`select`, `insert`, `update`, `delete`, `func`, `case`, `text`) directly over `asyncpg`.
9. **Native Async Migrations (`app.core.migrator`):** In-tree schema migration engine executing all platform revisions (`0001`–`0017`) sequentially with transactional DDL compilation, eliminating `alembic` and `psycopg2`.
10. **5-Ping @ 8.0s Concurrency Sweeper & Per-Cycle Probe History:** Each endpoint sends 5 pings 8.0s apart (2.0s reply timeout) once per 60-second cycle (~32s typical, 42s worst case), staggered deterministically across the minute by UUID. Every cycle records min/avg/max RTT, jitter, and packet loss into `endpoint_probe_samples` (30d retention) and `endpoint_probe_hourly` (365d retention).
11. **High-Fidelity Route Diagnostics:** Upgraded traceroute engine (`traceroute -n -q 2 -w 3 -m 30 -I`) with multi-probe latency parsing, raw ICMP capability isolation (`CAP_NET_RAW`), and Layer-2 subnet auto-bypass.
12. **Real-Time Server-Sent Events (SSE):** Asynchronous event broker streaming telemetry envelopes (`STATE_TRANSITION`, `NODE_STATE_CHANGE`, `RCA_INCIDENT`, `ROUTE_CHANGE`) and 15-second heartbeat keep-alives via `GET /api/v1/events/stream`.
13. **Pure-Stdlib PDF Compliance Engine (`app.core.pdf`):** PostScript/PDF binary emitter compiling executive compliance reports (Availability/SLA, Bandwidth/Capacity, Master Dossier) with corporate branding, Base64 logos, and zero external binary dependencies.
14. **Interactive Crossing-Free Topology Map:** Vue 3 Vis-Network visualizer implementing **BFS DAG Longest-Path Layering** (`Level(v) = max(Level(u) + 1)`), **Sugiyama (1981)** barycenter crossing reduction, **Gansner (1993)** coordinate alignment, **frozen-physics real-time recoloring**, NetFlow link verification (`confirmed`, `differs`, `inferred`), and **Horizontal (LR) ⇄ Vertical (UD)** layout switching.

---

## 📚 Detailed Documentation Suite

For complete architectural references, operational runbooks, and developer guides, refer to the `docs/` directory:

* **[v3.3.5 Release Notes](docs/release-notes/v3.3.5.md):** Complete release notes for Traffic Analytics, Per-Cycle Probe History, WAN Edge Totals, and NetFlow Route Augmentation.
* **[v3.3.2.v Release Notes](docs/release-notes/v3.3.2.v.md):** Architectural release notes detailing the 6 stages of the Vertical Integration Architecture.
* **[Changelog & Technical Evolution](docs/changelog.md):** Complete evolutionary matrix and milestone chronicle from Version 1.0 to Version 3.3.5.
* **[Architecture Deep-Dive](docs/architecture.md):** Comprehensive breakdown of the 12 native core engines, data pipelines, and concurrency sweeper.
* **[Developer Guide](docs/developer-guide.md):** Guidelines for contributing with `app.core.orm`, `app.core.schema`, `app.core.asgi`, and `FastStoreClient`.
* **[Deployment & Operations Guide](docs/deployment.md):** Production deployment runbook, `/dev/shm` shared memory tuning, and zero-Redis service supervision.
* **[Network Flow Configuration & Telemetry Guide](docs/netflow-guide.md):** Runbook for NetFlow v5/v9 and IPFIX setup, exporter vendor templates (Cisco, Juniper, Mikrotik, Linux `fprobe`, pfSense/OPNsense), and interface metrics.
* **[Database & TimescaleDB Deep-Dive](docs/database.md):** Full schema dictionary, hypertable partitioning, 7-day chunk compression, flow rollup hypertables, and continuous aggregate policies.
* **[Disaster Recovery & Restoration Runbook](deploy/RESTORE.md):** Procedures for restoring TimescaleDB backups and executing native async migrations.
* **[Security Model & Threat Hardening](docs/security.md):** Authentication matrix, Scrypt password hashing, sliding sessions, lockout defense, Linux capability isolation, and SSRF guardrails.
* **[SLA Calculation Methodology](docs/sla-calculation.md):** Mathematical formulation of uptime availability, flap suppression, and blackout neutralization.
* **[Troubleshooting Runbook](docs/troubleshooting.md):** Step-by-step diagnostic workflows, permission fixes, flow socket buffer sizing, and log inspection.
* **[User & Operator Guide](docs/user-guide.md):** Guide to navigating the Live KPI ribbon, Card/Table view switcher, High-Fidelity traceroutes, Bandwidth Fleet Dashboard, and Admin Settings.
* **[API Reference](docs/api-reference.md):** Complete documentation for REST endpoints, SSE streams (`/events/stream`), and bandwidth flow analytics.

---

## 💻 Technical Stack

### Core Stack
* **Backend Runtime**: Python 3.11+ (Strict `asyncio` event loop)
* **Web Framework**: Native ASGI (`app.core.asgi` — Zero-Dependency FastAPI/Starlette Replacement)
* **Validation & Config**: Native Schema Engine (`app.core.schema` & `app.config` — Zero-Dependency Pydantic Replacement)
* **Database Layer**: PostgreSQL 16 + TimescaleDB (`asyncpg` + `app.core.orm` — Zero-Dependency SQLAlchemy Replacement)
* **IPC & Streaming**: FastStore (`app.core.faststore` — Shared-Memory `/dev/shm` SQLite WAL Engine replacing Redis)
* **Security & Crypto**: Scrypt password hashing (`hashlib.scrypt`, `app.services.auth_service`), AES-256-GCM secret encryption with HKDF-SHA256 key derivation (`app.core.crypto`, via the system OpenSSL `libcrypto`), HS256 JWT session tokens, and a stdlib DER certificate-expiry parser for SSL probes (`monitoring/synthetic.py`)
* **Networking & Harvesters**: Raw Socket ICMP (`monitoring/ping.py`), Native Linux RTNETLINK & `/proc/net` Parser (`app.core.netlink`), Native Async HTTP/1.1 + SSRF Guard (`app.core.http_client`)
* **Reporting Engine**: Native Binary PDF 1.4 Generator with PNG/JPEG XObject embedding (`app.core.pdf`) & Native AST Template Engine (`app.core.template`)
* **Frontend**: Vue 3 (Composition API), Vite, Pinia, PrimeVue (Aura Theme), Custom CSS Design System, Native `fetch` Client (Zero Axios/CDN dependencies), Chart.js

---

## 🚀 Getting Started (Production Deployment)

### 1. Host Requirements
* **Operating System:** Ubuntu 22.04 / 24.04 LTS or Debian 12+ (x86_64). The installer uses `apt`; RHEL-family distributions are not supported by `install.sh`.
* **Kernel & Memory:** Linux Kernel 4.x+ with write access to `/dev/shm` (standard POSIX shared memory).
* **Database:** PostgreSQL with TimescaleDB 2. The installer provisions PostgreSQL 16 and TimescaleDB from the official PGDG and Timescale repositories; manual installs need PostgreSQL 14+.
* **Python:** Python 3.11+. Ubuntu 24.04 (3.12) and Debian 12 (3.11) meet this out of the box; on Ubuntu 22.04 (3.10) install `python3.11` and `python3.11-venv` before running the installer.
* **System Packages:** `traceroute`, Nginx, Node.js 20 (installed by `install.sh`). **No `redis-server`, `libpango`, or C-compilers required.**

### 2. Timezone & NTP Time Synchronization

> [!IMPORTANT]
> **Timezone Alignment & Clock Synchronization**:
> Ephemres telemetry analysis, uptime SLA calculation, and real-time state transition logging rely on precise timestamp filtering (`start_time <= end_dt`). Configure your host's regional timezone and enable network time synchronization (NTP) before starting services:
>
> ```bash
> sudo timedatectl set-timezone <Your/Region_Timezone>
> sudo timedatectl set-ntp on
> timedatectl status
> ```

### 3. Fresh Installation

```bash
sudo -i
git clone -b v3.3.5 https://github.com/dc8official/ephemres.git /opt/ephemres
cd /opt/ephemres/deploy
./install.sh
```

### 4. Upgrading to v3.3.5 (Zero Historical Data Loss)

To upgrade an existing installation (`v3.3.0` LTS or `v3.3.2.v`) to Version 3.3.5:

```bash
sudo bash /opt/ephemres/deploy/upgrade.sh
```
`upgrade.sh` fetches the code itself: run from a git checkout it pulls that checkout (branch from `EPHEMRES_BRANCH`, else the checkout's current branch); run from a copy without `.git` it shallow-clones the release branch from GitHub into a staging directory. No manual `git pull` is needed.

The upgrade utility automatically:
1. **Pre-Upgrade Backup:** Dumps a timestamped PostgreSQL SQL backup to `/var/backups/ephemres/`.
2. **Removes Obsolete Daemons:** Safely stops and disables `redis-server` (if previously installed for legacy Ephemres versions).
3. **Synchronizes Dependencies:** Installs the minimal 2-package requirements (`uvicorn`, `asyncpg`) in <3 seconds.
4. **Compiles Frontend:** Builds production Vue 3 assets via Vite.
5. **Executes Native Migrations:** Runs `migrator.py` applying forward schema migrations (`0001`–`0017`) while preserving all historical telemetry and continuous aggregates.
6. **Restarts Services:** Reloads `ephemres-api`, `ephemres-engine`, and `ephemres-flowd` under systemd.
7. **Health Verification:** Confirms `/api/v1/version` and `/api/v1/health` report services are healthy.

---

## 🔬 Related Research & Companion Projects

* **[Ephemres Observer Lab](https://github.com/dc8official/ephemres-observer-lab.git)** — An independent companion research repository exploring network observability experiments based on Ephemres. *(Note: `ephemres-observer-lab` is a standalone research project; the core Ephemres production codebase is not influenced by or dependent on experimental work in the lab.)*

---

## 📜 License & Authorship

Core Architecture designed and authored by **Kenneth Nnorom**.

Website: [kennethnnorom.com](https://kennethnnorom.com) | LinkedIn: [linkedin.com/in/kennethnnorom](https://linkedin.com/in/kennethnnorom)

This project is licensed under the terms of the **Apache License 2.0**. See the [LICENSE](LICENSE) file for complete details.
