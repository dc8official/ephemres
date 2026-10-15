# Ephemres Architecture Overview — Version 3.3.1n

Ephemres v3.3.1n is architected with a decoupled, asynchronous design engineered for high-concurrency telemetry collection, passive network flow telemetry ingestion (NetFlow v5, NetFlow v9, and IPFIX), per-interface throughput accounting, real-time Server-Sent Events (SSE), dual-driver storage acceleration, enterprise multi-channel notifications, crossing-free topology visualization, and enterprise security governance.

---

## Core System Components

### 1. Data Access & Repository Layer
* **SQLAlchemy 2.0 Async ORM:** All database interactions are mediated by strongly-typed declarative models inheriting from `DeclarativeBase` (`backend/app/models/`).
* **Repository Pattern:** Business logic is decoupled from data persistence through dedicated repositories (`EndpointRepository`, `EventRepository`, `IncidentRepository`, `UserRepository`, `SettingRepository`, `FlowRepository`).
* **SQL-Level Pagination:** All listing endpoints enforce SQL `LIMIT` and `OFFSET` queries, preventing memory bloat on large fleets.
* **Pydantic Settings:** System configuration is centrally validated through typed Pydantic models.

### 2. Concurrency Sweeper & Timing Budget (The Poller)
* **Cycle Timing Budget (5 pings @ 8.0s):** Monitoring probes are tuned to execute 5 pings with an 8.0-second timeout, guaranteeing probe pass completion in ~32 seconds.
* **28-Second Headroom Window:** Leaves 28 seconds of idle headroom before the minute boundary, eliminating thundering herds and connection pool contention.
* **Startup Jitter:** Workers inject 0–2000ms randomized offset at cycle initialization to disperse network bursts.
* **Dynamic In-Memory Registry:** `EndpointRegistry` maintains a thread-safe, concurrent registry allowing sub-minute endpoint additions, updates, and removals with zero engine downtime.

### 3. Dual-Driver Storage Architecture
The platform supports pluggable, dual-driver storage via `StorageDriverManager`:
* **PostgreSQL-Native Driver:**
  - `PostgresSessionStore`: Manages authenticated sessions within the `user_sessions` PostgreSQL table.
  - `PostgresEventBroker`: Uses PostgreSQL `LISTEN / NOTIFY` asynchronous channels to publish and broadcast telemetry events.
* **Redis Acceleration Driver:**
  - `RedisSessionStore`: Caches active sessions in Redis key-value storage with automatic TTL expiry.
  - `RedisEventBroker`: Uses Redis Pub/Sub channels for high-throughput, low-latency inter-process messaging.
* **Multi-Worker Settings & Driver Cluster Synchronization (`SYSTEM_SETTINGS_SYNC`):**
  When running under multiple Uvicorn workers, modifying active storage settings via the Admin UI triggers a PostgreSQL `NOTIFY system_settings_sync` signal. All listening worker processes dynamically reload driver configurations and synchronize active session stores without service downtime or cluster restarts.
* **Bidirectional Warm Session State Migration:**
  When switching active drivers between PostgreSQL and Redis, active sessions are dynamically read and migrated to the destination backend in real time, guaranteeing zero operator session termination or abrupt logouts.

### 4. Real-Time Telemetry & Server-Sent Events (SSE)
* **SSE Endpoint (`GET /api/v1/events/stream`):** Delivers continuous telemetry directly to web browsers using `text/event-stream`.
* **Event Envelopes:** Emits structured JSON events on every monitoring transition (`STATE_TRANSITION`, `NODE_STATE_CHANGE`, `RCA_INCIDENT`).
* **Connection Heartbeat:** Emits a 15-second heartbeat comment (`: heartbeat\n\n`) to keep proxy and reverse-proxy connections alive.

### 5. Multi-Protocol Synthetic Monitoring & SSRF Defense
* **Synthetic Probes:**
  - `TCP_PORT`: Validates service port reachability and TCP 3-way handshake latency.
  - `HTTP_STATUS`: Validates HTTP/HTTPS response codes and round-trip time.
  - `SSL_EXPIRY`: Inspects remote TLS certificates and computes days until expiration.
* **SSRF Protection:** `validate_probe_target()` enforces strict IP filtering, blocking loopback (`127.0.0.0/8`, `::1`) and cloud metadata (`169.254.169.254`, `169.254.0.0/16`) targets.

### 6. High-Fidelity Route Diagnostics & RCA Engine
* **High-Fidelity Traceroute:** Uses `traceroute -n -q 2 -w 3 -m 30 -I` with `CAP_NET_RAW` capability for accurate ICMP route tracing and multi-probe latency parsing.
* **Layer-2 Auto-Bypass:** Automatically skips redundant multi-hop traceroutes for devices located on the same direct Layer-2 subnet broadcast domain.
* **Topological RCA (`INFERRED_DOWN`):** When an upstream aggregation node fails, downstream dependent devices are marked `INFERRED_DOWN`, preventing cascading alert storms.

### 7. Interactive Crossing-Free Topology Map
* **4-Phase Sugiyama & Gansner Framework**:
  1. **Phase 1: BFS DAG Longest-Path Layering (`Level(v) = max(Level(u) + 1)`)**: Assigns every node to its exact discrete hop depth tier.
  2. **Phase 2: Crossing Reduction (`edgeMinimization: true`)**: Sugiyama barycenter reordering of sibling nodes to eliminate overlapping diagonal links.
  3. **Phase 3: Coordinate Assignment (`blockShifting: true`, `parentCentralization: true`)**: Gansner coordinate alignment centering parents directly over child clusters.
  4. **Phase 4: Directional Spline Routing (`cubicBezier`)**: Tangent-constrained spline channels with Horizontal (`LR`) ⇄ Vertical (`UD`) switching.
* **Frozen Physics Real-Time Recoloring:** Updates node state colors in place on SSE `NODE_STATE_CHANGE` events with `physics: { enabled: false }` to prevent canvas shaking.

### 8. Enterprise Alerting Subsystem & Decoupling Invariant
* **Decoupling Invariant:** Outbound webhooks and email notifications can experience unpredictable network delays, TLS handshakes, or third-party API rate limits (100ms – 10s+). Running alert dispatching inside the `ephemres-engine` monitoring sweep would stall raw ICMP socket reading and blow the critical 32-second sweep budget. To preserve deterministic timing, the **Alert Dispatcher operates entirely within the `ephemres-api` service as an independent asynchronous background task pool**.
* **Inter-Process Decoupling Pipeline:**
  1. `ephemres-engine` executes ICMP/TCP probes and publishes transitions to `EventBroker` (`LISTEN/NOTIFY` or Redis Pub/Sub) in sub-millisecond time.
  2. `TelemetryRelay` running in `ephemres-api` receives the event and enqueues it into an in-memory `asyncio.Queue`.
  3. `AlertDispatcher` pulls from the queue and applies multi-layer filtering:
     - **Scope Filter:** Evaluates target scope (`all` vs. explicit endpoint UUIDs) and severity bitmasks.
     - **RCA Cascade Suppression:** Suppresses individual downstream endpoint notifications when an upstream parent node generates an active `RCA_INCIDENT`.
     - **Flapping Cooldown:** Suppresses alerts if an endpoint exhibits $\ge 3$ state changes within a 10-minute sliding window (max 1 alert per 5 minutes per endpoint/severity).
     - **Cryptographic & SSRF Defense:** Derives AES-256-GCM decryption keys via HKDF-SHA256 and validates outbound destination IPs against loopback and cloud metadata ranges.
     - **Polyglot Formatters:** Adapts payload to Microsoft Teams (Adaptive Cards v1.4 & HTML fallback), Discord (Rich Embeds), Slack (Block Kit), or SMTP TLS 1.2+ MIME.
     - **Delivery Audit Logging:** Persists asynchronous results, HTTP response codes, and round-trip latencies into `alert_delivery_logs`.

### 9. Network Flow Telemetry Ingestion Pipeline (`ephemres-flowd`)
* **Autonomous Ingestion Daemon:** `ephemres-flowd` runs as an independent `systemd` daemon completely isolated from `ephemres-engine` and `ephemres-api`. Even under massive multi-gigabit traffic bursts, ICMP polling and REST API responsiveness remain completely unperturbed.
* **Async Datagram Sockets & Buffer Allocation:** Listens on UDP port 2055 (NetFlow v5/v9) and UDP port 4739 (IPFIX) with a 4MB kernel receive buffer (`SO_RCVBUF = 4 * 1024 * 1024`), eliminating UDP packet drops under highpps microbursts.
* **Zero-Allocation Binary Decoders:** High-throughput `struct.unpack_from` byte-slice parsers with dynamic template caching (1,800s TTL) for NetFlow v9 and IPFIX options/flow records. Orphan data records received before templates are buffered in an in-memory ring buffer (up to 2,000 records) and flushed once the template arrives.
* **Dual-Role $\mathcal{O}(1)$ In-Memory Correlator:**
  - Maintains pre-indexed hash maps for sub-microsecond matching without SQL queries.
  - **Exporter Resolution:** Resolves exporter IP against endpoint primary IPs and `flow_exporter_ips` secondary aliases. Unknown exporter IPs are registered into a Redis sliding set (`netflow:unmatched_exporters`) with a 24-hour TTL, triggering the Unmatched Exporter Discovery Banner in the UI.
  - **Traffic Participant Resolution:** Resolves source and destination IPs against monitored inventory endpoints to attribute traffic to internal infrastructure vs. external internet.
* **Port Normalization & Cardinality Shield:** Normalizes ephemeral client ports (1024–65535) unless recognized as well-known server services (e.g. 8080, 8443, 3306, 5432, 6379, 9200) into a single aggregate representation. This prevents high-entropy ephemeral client ports from exploding TimescaleDB hypertable B-tree index sizes.
* **Redis Stream Shock-Absorber (`stream:netflow:raw`):** Ingested flows are streamed to Redis Streams using `XADD` with sliding window trimming (`MAXLEN ~100000`). A memory circuit breaker monitors Redis memory usage (>85%) and applies exponential backoff or sampling adjustments to safeguard host stability.
* **4-Tier Hierarchical Storage Lifecycle:**
  1. *Tier 1 (Real-Time Raw Buffer):* Redis Stream (`stream:netflow:raw`), sliding 2-hour retention for real-time forensic inspection.
  2. *Tier 2 (1-Minute Rollups):* TimescaleDB hypertable `flow_minute_rollups`, uncompressed for 24 hours, compressed via TimescaleDB columnar compression after 1 day (85%+ disk space reduction), dropped after 7 days via `drop_chunks`.
  3. *Tier 3 (Hourly Aggregates):* TimescaleDB continuous aggregate `flow_hourly_rollups`, refreshed every hour, retained for 30 days.
  4. *Tier 4 (Daily Aggregates):* TimescaleDB continuous aggregate `flow_daily_rollups`, refreshed daily, retained for 365 days for long-term capacity planning.

---

## Architectural Diagram

```mermaid
flowchart TD
    subgraph UI_Layer["Frontend & UI Layer (Vue 3 / Vite)"]
        A["Dashboard View (KPI Strip & Dual View)"]
        T["Topology Canvas (Frozen Physics)"]
        BW["Bandwidth Dashboard (/bandwidth)"]
        S["Admin Settings Console (/settings)"]
    end

    subgraph SSE_Stream["Real-Time Event Streaming"]
        E_STREAM["SSE Stream (/api/v1/events/stream)"]
        A -.->|Subscribes| E_STREAM
        T -.->|Subscribes| E_STREAM
        BW -.->|Subscribes| E_STREAM
    end

    subgraph API_Layer["FastAPI Service Layer"]
        B["FastAPI REST & SSE Router"]
        AUTH["Async Argon2id Auth & Session Guard"]
        REPO["Repository Layer (SQLAlchemy 2.0)"]
    end

    subgraph Storage_Drivers["Pluggable Storage Drivers"]
        MGR["StorageDriverManager"]
        PS["PostgresSessionStore / PostgresEventBroker"]
        RS["RedisSessionStore / RedisEventBroker"]
        R_STREAM["Redis Stream (stream:netflow:raw)"]
        MGR --> PS
        MGR --> RS
    end

    subgraph Monitoring_Core["Monitoring & Diagnostic Engines"]
        ENG["Monitoring Engine (5 pings @ 8s / 28s Headroom)"]
        REG["Dynamic EndpointRegistry"]
        SYN["Synthetic Probes (TCP / HTTP / SSL + SSRF)"]
        TR["High-Fidelity Traceroute & L2 Auto-Bypass"]
        RCA["RCA Topology & Inference Engine"]
    end

    subgraph Flow_Ingestion["Flow Telemetry Ingestion (ephemres-flowd)"]
        FLOWD["Flow Collector Daemon (UDP 2055 / 4739)"]
        CORR["Dual-Role O(1) Correlator"]
        FLOWD --> CORR
        CORR --> R_STREAM
    end

    subgraph Database_Layer["PostgreSQL & TimescaleDB"]
        DB[("PostgreSQL 14+ with TimescaleDB")]
        HT[("Hypertables (Metrics & Flow Rollups)")]
        CAG[("Continuous Aggregates (Hourly & Daily)")]
        DB --> HT
        DB --> CAG
    end

    E_STREAM --> B
    B --> AUTH
    B --> REPO
    REPO --> DB
    B --> MGR
    B --> R_STREAM

    ENG --> REG
    ENG --> SYN
    ENG -.->|Triggers on Drop| TR
    TR -.->|Informs| RCA
    ENG --> REPO
    ENG --> MGR

    FLOWD --> REPO
    R_STREAM --> DB
```

---

## Alert Dispatcher Subsystem Diagram

```mermaid
flowchart TD
    subgraph Engine_Sweep["ephemres-engine (Strict 32s Sweeper Budget)"]
        PROBE["ICMP / TCP Probes"]
        TRANS["State Classifier"]
        BROKER_PUB["EventBroker.publish()"]
        PROBE --> TRANS --> BROKER_PUB
    end

    BROKER_PUB -->|Inter-Process Channel| BROKER_SUB

    subgraph API_Process["ephemres-api (Decoupled Async Background Task)"]
        BROKER_SUB["TelemetryRelay Listener"]
        ASYNC_Q["asyncio.Queue (alert_queue)"]
        DISPATCHER["AlertDispatcher Worker"]
        
        BROKER_SUB -->|Enqueue in <0.1ms| ASYNC_Q
        ASYNC_Q -->|Worker Dequeue| DISPATCHER

        subgraph Pipeline["Multi-Layer Alert Processing Pipeline"]
            MASTER{"alerting_enabled?"}
            SCOPE{"Target Scope & Severity Match?"}
            RCA_SUPP{"Upstream RCA Active?"}
            FLAP_SUPP{"Flapping (<5m Cooldown)?"}
            SSRF_CRYPTO["SSRF DNS Pinning & AES-256 Decrypt"]
            FORMATTER["Polyglot Formatters (Teams / Discord / Slack / SMTP)"]
            DISPATCH_CALL["Async httpx / direct smtplib"]

            DISPATCHER --> MASTER
            MASTER -->|Yes| SCOPE
            SCOPE -->|Match| RCA_SUPP
            RCA_SUPP -->|No Gateway Incident| FLAP_SUPP
            FLAP_SUPP -->|Clear| SSRF_CRYPTO
            SSRF_CRYPTO --> FORMATTER
            FORMATTER --> DISPATCH_CALL
        end

        AUDIT[("alert_delivery_logs")]
        DISPATCH_CALL -->|Result / Latency / Status| AUDIT
        RCA_SUPP -.->|Suppressed Log| AUDIT
        FLAP_SUPP -.->|Suppressed Log| AUDIT
    end

    subgraph External_Destinations["Outbound Alert Destinations"]
        TEAMS["Microsoft Teams (Adaptive Card / HTML)"]
        DISCORD["Discord (Rich Embeds)"]
        SLACK["Slack (Block Kit)"]
        EMAIL["Corporate SMTP Relay (TLS 1.2+)"]
        WEBHOOK["Generic HTTPS Webhook"]

        DISPATCH_CALL --> TEAMS
        DISPATCH_CALL --> DISCORD
        DISPATCH_CALL --> SLACK
        DISPATCH_CALL --> EMAIL
        DISPATCH_CALL --> WEBHOOK
    end
```

---

## Network Flow Telemetry Ingestion Pipeline Diagram

```mermaid
flowchart TD
    subgraph Network_Exporters["Network Infrastructure (Routers, Switches & Firewalls)"]
        R1["Cisco / Juniper / Mikrotik / pfSense / Linux"]
        R1 -->|NetFlow v5/v9 (UDP 2055)| UDP_2055["UDP Port 2055"]
        R1 -->|IPFIX (UDP 4739)| UDP_4739["UDP Port 4739"]
    end

    subgraph Ingestion_Daemon["Autonomous Daemon: ephemres-flowd"]
        SO_RCV["4MB Kernel Socket Buffer (SO_RCVBUF)"]
        UDP_2055 --> SO_RCV
        UDP_4739 --> SO_RCV

        DECODER["Zero-Allocation Binary Parser (struct.unpack_from)"]
        SO_RCV --> DECODER

        subgraph Parser_Internals["Decoder & Template Engine"]
            TMPL_CACHE["Template Cache (1,800s TTL)"]
            RING_BUF["Orphan Data Set Ring Buffer (2,000 pkts)"]
            NORM["Ephemeral Port Normalizer (>1024 to single bucket)"]
            DECODER --> TMPL_CACHE
            DECODER --> RING_BUF
            DECODER --> NORM
        end

        subgraph Correlator["Dual-Role In-Memory Correlator (O(1))"]
            EXP_MAP["Exporter Index (Primary IP + flow_exporter_ips)"]
            PART_MAP["Traffic Participant Index (Monitored Endpoints)"]
            UNMATCHED["Redis Set: netflow:unmatched_exporters (24h TTL)"]
            
            NORM --> EXP_MAP
            NORM --> PART_MAP
            EXP_MAP -.->|Unmapped IP| UNMATCHED
        end

        CIRCUIT["Memory Circuit Breaker (>85% Redis Mem)"]
        NORM --> CIRCUIT
    end

    subgraph Storage_Tiers["4-Tier Hierarchical Storage Lifecycle"]
        CIRCUIT -->|XADD (MAXLEN ~100k)| TIER1["Tier 1: Redis Stream (stream:netflow:raw, 2h TTL)"]
        
        TIER1 -->|Rollup Worker (60s Batch)| TIER2["Tier 2: TimescaleDB flow_minute_rollups (7d Retention)"]
        TIER2 -.->|After 1 Day| TIER2_COMP["TimescaleDB Columnar Compression (85%+ Savings)"]
        
        TIER2 -->|Continuous Aggregate| TIER3["Tier 3: flow_hourly_rollups (30d Retention)"]
        TIER3 -->|Continuous Aggregate| TIER4["Tier 4: flow_daily_rollups (365d Retention)"]
    end

    subgraph UI_API["Operator Presentation Layer"]
        API["FastAPI Bandwidth Router (/api/v1/bandwidth/*)"]
        TIER1 -.->|Forensic Top-Talkers| API
        TIER2 -->|Traffic Series & App Mix| API
        TIER3 -->|Multi-Day Trends| API
        TIER4 -->|Annual Capacity Planning| API
        UNMATCHED -.->|Discovery Banner| API

        DASH["Bandwidth Dashboard (/bandwidth)"]
        EP_TAB["Endpoint Detail (Flow Telemetry Tab)"]
        API --> DASH
        API --> EP_TAB
    end
```
