# Ephemres Deployment & Operations Guide — Version 3.3.2.v

This guide details the procedures for installing, maintaining, and upgrading the Ephemres Network Monitoring Platform v3.3.2.v on a production Linux server under the **Vertical Integration Architecture**.

---

## 1. System Requirements

- **OS:** Ubuntu 22.04 LTS or 24.04 LTS (Debian 12+ also supported)
- **Hardware (Minimum for Production):** 2 vCPUs, 2 GB RAM, 10–25 GB SSD (edge); 4+ vCPUs, 8 GB RAM, 50 GB+ NVMe (large scale).
- **Core Dependencies:**
  - **PostgreSQL 14+ with TimescaleDB**
  - **Python 3.10+** (with virtual environment)
  - **Node.js 18+ & npm** (for frontend asset compilation)
  - **Nginx** (reverse proxy & TLS termination)
  - **Linux `/dev/shm`** (standard in-RAM POSIX shared memory for FastStore SQLite WAL)
- **Zero External Daemons:** `redis-server` is **NOT** required. FastStore runs entirely in-process in `/dev/shm`.
- **Zero Heavy C-Libraries:** `libpango-1.0-0`, `libcairo2`, and `pypdf` are **NOT** required. PDF reports are emitted directly by `app.core.pdf`.
- **Time Synchronization & Timezone:** Accurate system clock via NTP (`systemd-timesyncd` or `chrony`) and server timezone configured to match the operational region. Critical for TimescaleDB telemetry partitioning, RTT graph rendering, flow rollup boundaries, and SLA calculations.
- **Network Permissions:** The `ephemres-engine` daemon requires `CAP_NET_RAW` capability to send raw ICMP packets. `ephemres-flowd` binds UDP 2055 and UDP 4739.

---

## 2. Initial Installation

### Pre-Installation: System Timezone & Clock Synchronization

> [!IMPORTANT]
> Ephemres relies on strict time-window boundaries for calculating uptime percentages, transition logs, RTT telemetry graphs, and flow rollups. Ensure the server's timezone matches your operations region and NTP time sync is active before launching Ephemres services:
>
> ```bash
> # 1. Set regional timezone (e.g., Europe/London, America/New_York, UTC, Africa/Lagos)
> sudo timedatectl set-timezone <Your/Region_Timezone>
>
> # 2. Enable NTP time synchronization
> sudo timedatectl set-ntp on
>
> # 3. Confirm active timezone and NTP synchronization status
> timedatectl status
> ```

### Automated Installation

The initial installation is fully automated via `install.sh`:

```bash
sudo -i
git clone -b v3.3.2v https://github.com/dc8official/ephemres.git
cd ephemres/deploy
./install.sh
```

**What the installer executes:**
1. Installs minimal system packages: `python3-pip`, `postgresql-16`, `timescaledb-2-postgresql-16`, `nginx`, `traceroute`, `libcap2-bin`, `nodejs`.
2. Creates dedicated system user `ephemres` and minimal virtual environment at `/opt/ephemres/backend/venv` (installing only `uvicorn` and `asyncpg`).
3. Compiles the Vue 3 production bundle (`npm run build`).
4. Generates `/etc/ephemres/ephemres.env` and `/etc/ephemres/config.toml` with v3.3.2.v defaults.
5. Runs native async database migrations (`python3 -m app.core.migrator upgrade head`).
6. Sets network capabilities: `setcap cap_net_raw+ep $(readlink -f /opt/ephemres/backend/venv/bin/python3)`.
7. Enables and starts systemd units (`ephemres-api`, `ephemres-engine`, `ephemres-flowd` if enabled, `nginx`).

---

## 3. Upgrading to Version 3.3.2.v (Zero Historical Data Loss)

To perform an in-place upgrade to v3.3.2.v:

```bash
cd ~/ephemres
git pull origin v3.3.2v
sudo ./deploy/upgrade.sh
```

*(Alternatively, if upgrading directly on the host without a local git clone, you can run `sudo bash /opt/ephemres/deploy/upgrade.sh`)*

### Automated Upgrade Lifecycle Steps:
1. **Pre-Upgrade Database Backup**: Automatically creates a timestamped SQL backup at `/var/backups/ephemres/ephemres_backup_<TIMESTAMP>.sql` before any changes. If the backup fails, the upgrade aborts immediately.
2. **System Dependencies Cleanup**: Verifies essential packages (`traceroute`, `libcap2-bin`) without installing legacy bloatware (`redis-server`, `libpango-1.0-0`).
3. **Network Capabilities**: Sets `CAP_NET_RAW` capabilities on the python binary (`/opt/ephemres/backend/venv/bin/python3`) and `traceroute` for unprivileged ICMP discovery.
4. **Smart Config Migration**: Patches `/etc/ephemres/config.toml` with v3.3.2.v defaults (5 pings @ 8s, 120-minute session timeout, 2-session limit, FastStore in-RAM storage, `[alerting]`, and `[flow]` sections).
5. **Daemon Pause**: Gracefully stops `ephemres-flowd`, `ephemres-engine`, and `ephemres-api`.
6. **Code & Dependency Sync**: Pulls latest codebase, cleans obsolete dependencies from the virtual environment, installs minimal requirements (`uvicorn`, `asyncpg`), and compiles frontend assets.
7. **Native Async Migrations**: Runs `python3 -m app.core.migrator upgrade head` to apply all database revisions while preserving all historical TimescaleDB hypertable data.
8. **Unit Refresh & Daemon Restart**: Refreshes `ephemres-api.service`, `ephemres-engine.service`, and `ephemres-flowd.service`, reloads systemd daemons, enables auto-start on boot, and restarts `ephemres-api`, `ephemres-engine`, and `nginx`. If flow ingestion is enabled, automatically starts `ephemres-flowd` and configures UFW rules for UDP 2055 and UDP 4739.
9. **Health Verification**: Verifies API health and version endpoints (`/api/v1/version`).

---

## 4. Disaster Recovery & Backup Restoration (Same-Version or Cross-Version `v3.1.35s` / `v3.3.0` / `v3.3.1n` $\rightarrow$ `v3.3.2v+`)

Ephemres includes an automated schema-reconciling restore engine (`deploy/restore.sh` / `python3 -m app.core.restore`) that safely migrates backups from `v3.1.35s`, `v3.3.0`, `v3.3.1n`, or `v3.3.2v+`:

```bash
# Interactive restore (prompts for a new Admin password if legacy $argon2id$ hashes are detected)
sudo bash /opt/ephemres/deploy/restore.sh /var/backups/ephemres/ephemres_backup_YYYYMMDD.sql.gz

# Non-interactive / automated restore with explicit admin password reset
sudo bash /opt/ephemres/deploy/restore.sh /var/backups/ephemres/ephemres_backup_YYYYMMDD.sql.gz \
  --admin-password 'NewSecureAdminPassword123!'
```

See [`deploy/RESTORE.md`](../deploy/RESTORE.md) for full details on cross-version column reconciliation, TimescaleDB chunk remapping, and `SECRET_KEY` migration.

---

## 5. Storage Driver & Flow Configuration

Ephemres configuration is declared in `/etc/ephemres/config.toml`:

```toml
[database]
host = "localhost"
port = 5432
name = "netmon"
user = "netmon_user"

[monitoring]
interval_seconds = 60
ping_count = 5
ping_interval_seconds = 8
state_confirmation_cycles = 3
retention_days = 730

[api]
host = "127.0.0.1"
port = 8000
allowed_origins = ["https://monitor.yourdomain.com"]

[security]
session_timeout_minutes = 120
max_active_sessions_per_user = 2
hsts_enabled = true

[redis]
# FastStore shared-memory configuration (in-RAM /dev/shm)
host = "127.0.0.1"
port = 6379
db = 0
enabled = true
performance_mode = false

[flow]
enabled = false
netflow_port = 2055
ipfix_port = 4739
sampling_multiplier = 1
buffer_memory_limit_mb = 512
```

> [!NOTE]
> **Performance Mode Toggling**:
> **Performance Mode** (switching between Standard PostgreSQL Mode and high-velocity FastStore `/dev/shm` shared-memory streaming) is managed live from the **Admin Settings UI $\rightarrow$ Performance & Telemetry** tab (`PATCH /api/v1/settings`), which instantly synchronizes across all worker processes via PostgreSQL `LISTEN`/`NOTIFY`.

---

## 6. Service Management & Logging

```bash
# Check service status across all daemons
sudo systemctl status ephemres-api ephemres-engine ephemres-flowd nginx

# Live stream API server logs
sudo journalctl -u ephemres-api -f

# Live stream monitoring engine logs
sudo journalctl -u ephemres-engine -f

# Live stream flow collector logs
sudo journalctl -u ephemres-flowd -f

# View rotating log files
tail -f /var/log/ephemres/ephemres-api.log
tail -f /var/log/ephemres/ephemres-engine.log
tail -f /var/log/ephemres/ephemres-flowd.log
tail -f /var/log/ephemres/error.log
```

---

## 7. Firewall Rules (Egress Alerts & Ingress Flow Telemetry)

### Ingress Firewall Policies (Passive Telemetry Collection)
Routers and firewalls must be permitted to stream flow datagrams to the Ephemres collector host:

| Flow Protocol | Protocol & Port | Source | Purpose |
| :--- | :--- | :--- | :--- |
| **NetFlow v5 / v9** | UDP 2055 | Exporter Routers / Switches | Raw NetFlow datagram collection |
| **IPFIX** | UDP 4739 | Exporter Routers / Firewalls | Raw IPFIX datagram collection |

```bash
# Example UFW commands to allow flow ingestion from your management subnet:
sudo ufw allow proto udp from 10.0.0.0/16 to any port 2055 comment 'Ephemres NetFlow v5/v9 Ingestion'
sudo ufw allow proto udp from 10.0.0.0/16 to any port 4739 comment 'Ephemres IPFIX Ingestion'
```

### Egress Firewall Policies (Enterprise Alerting)
Ephemres dispatches outbound incident notifications directly to external endpoints:

| Destination Service | Protocol & Ports | Purpose |
| :--- | :--- | :--- |
| **Microsoft Teams Workflows** | TCP 443 (HTTPS) | Webhook incident payload dispatch (`*.office.com`, `*.microsoft.com`) |
| **Discord Webhooks** | TCP 443 (HTTPS) | Rich embed alerts (`discord.com`, `discordapp.com`) |
| **Slack Webhooks** | TCP 443 (HTTPS) | Block Kit notifications (`hooks.slack.com`) |
| **Corporate SMTP Relays** | TCP 587 (STARTTLS), TCP 465 (SMTPS), or TCP 25 | Outbound alert emails to corporate exchange / cloud mail gateways |
| **External NTP Servers** | UDP 123 | Continuous system clock synchronization |

---

## 8. Database Migration Execution Procedure

If executing migrations manually outside of `upgrade.sh`:

```bash
cd /opt/ephemres/backend

# Activate python virtual environment and set path
source /opt/ephemres/backend/venv/bin/activate
export PYTHONPATH=/opt/ephemres:/opt/ephemres/backend

# Inspect pending migrations
python3 -m app.core.migrator current
python3 -m app.core.migrator heads

# Apply pending migrations
python3 -m app.core.migrator upgrade head

# Verify table and hypertable creation in PostgreSQL
sudo -u postgres psql -d netmon -c "\dt flow*"
sudo -u postgres psql -d netmon -c "SELECT hypertable_name FROM timescaledb_information.hypertables;"
```
