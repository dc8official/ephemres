# LNMP Deployment & Operations Guide — Version 3.1.35s

This guide details the procedures for installing, maintaining, and upgrading the Network Monitoring Platform (LNMP) v3.1.35s on a production Linux server.

---

## 1. System Requirements

- **OS:** Ubuntu 22.04 LTS or 24.04 LTS (Debian 12+ also supported)
- **Hardware (Minimum for Production):** 2 vCPUs, 2 GB RAM, 10–25 GB SSD.
- **Dependencies:** PostgreSQL 14+ with TimescaleDB, Redis 6+ (for Memory Acceleration mode), Node.js 18+, Python 3.10+, Nginx.
- **Time Synchronization & Timezone:** Accurate system clock via NTP (`systemd-timesyncd` or `chrony`) and server timezone configured to match the operational region. Critical for TimescaleDB telemetry partitioning, RTT graph rendering, and SLA calculations.
- **Network Permissions:** The `netmon-engine` daemon requires `CAP_NET_RAW` capability to send raw ICMP packets.

---

## 2. Initial Installation

### Pre-Installation: System Timezone & Clock Synchronization

> [!IMPORTANT]
> LNMP relies on strict time-window boundaries for calculating uptime percentages, transition logs, and RTT telemetry graphs. Ensure the server's timezone matches your operations region and NTP time sync is active before launching LNMP services:
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
git clone https://github.com/dc8official/lnmp.git
cd lnmp/deploy
./install.sh
```

**What the installer executes:**
1. Installs system packages: `python3-pip`, `postgresql`, `timescaledb`, `redis-server`, `nginx`, `traceroute`, `libcap2-bin`.
2. Creates dedicated system user `netmon` and virtual environment at `/opt/netmon/venv`.
3. Compiles the Vue 3 production bundle (`npm run build`).
4. Generates `/etc/netmon/netmon.env` and `/etc/netmon/config.toml` with v3.1.35s defaults.
5. Sets network capabilities: `setcap cap_net_raw+ep $(command -v traceroute)`.
6. Enables and starts systemd units (`netmon-api`, `netmon-engine`, `redis-server`, `nginx`).

---

## 3. Upgrading to Version 3.1.35s (Zero Historical Data Loss)

To perform an in-place upgrade to v3.1.35s:

```bash
cd ~/lnmp
git pull origin v3.1.35s
sudo ./deploy/upgrade.sh
```

*(Alternatively, if upgrading directly on the host without a local git clone, you can run `sudo bash /opt/netmon/noop/deploy/upgrade.sh`)*

### Automated Upgrade Lifecycle Steps:
1. **Pre-Upgrade Database Backup**: Automatically creates a timestamped SQL backup at `/var/backups/netmon/netmon_backup_<TIMESTAMP>.sql` before any changes. If the backup fails, the upgrade aborts immediately.
2. **System Dependencies & Redis**: Installs missing packages (`redis-server`, `traceroute`, `libcap2-bin`, verifies Python package `httpx>=0.27.0`) and ensures Redis is enabled and started (`systemctl enable --now redis-server`).
3. **Network Capabilities**: Sets `CAP_NET_RAW` capabilities on `traceroute` for unprivileged ICMP discovery.
4. **Smart Config Migration**: Patches `/etc/netmon/config.toml` with v3.1 defaults (5 pings @ 8s, 120-minute session timeout, 2-session limit, `[redis]` and `[alerting]` sections).
5. **Daemon Pause**: Gracefully stops `netmon-engine` and `netmon-api`.
6. **Code & Dependency Sync**: Pulls latest codebase, updates Python dependencies, and compiles frontend assets.
7. **Forward Alembic Migrations**: Runs `alembic upgrade head` applying migration `0007_v3_1_alert_channels_and_delivery.py` to create `alert_channels` and `alert_delivery_logs` while preserving all historical TimescaleDB chunks.
8. **Unit Refresh & Daemon Restart**: Reloads systemd daemon, enables auto-start on boot, and starts `redis-server`, `netmon-api`, `netmon-engine`, and `nginx`.
9. **Health Verification**: Verifies API health and version endpoints (`/api/v1/version`).

---

## 4. Disaster Recovery & Backup Restoration

For complete database restoration procedures from timestamped backups, refer to the **[Disaster Recovery & Database Restoration Runbook](deploy/RESTORE.md)**.

Quick restore summary:
```bash
sudo systemctl stop netmon-engine netmon-api
set -a && source /etc/netmon/netmon.env && set +a
PGPASSWORD="${NETMON_DB_PASSWORD}" psql -h 127.0.0.1 -U netmon_user -d netmon -f /var/backups/netmon/netmon_backup_<TIMESTAMP>.sql
cd /opt/netmon/noop/backend && /opt/netmon/venv/bin/alembic upgrade head
sudo systemctl restart redis-server netmon-api netmon-engine nginx
```

---

## 5. Storage Driver Configuration

In `/etc/netmon/config.toml`:

```toml
[redis]
host = "127.0.0.1"
port = 6379
db = 0
enabled = true
performance_mode = false  # Set to true for Redis in-memory acceleration
```

* **Standard Mode (`performance_mode = false`):** Uses PostgreSQL for session storage and `LISTEN / NOTIFY` for event broadcasting.
* **Memory Acceleration Mode (`performance_mode = true`):** Uses Redis for sub-millisecond session validation and Redis Pub/Sub for high-throughput event broadcasting.

---

## 6. Service Management & Logging

```bash
# Check service status
sudo systemctl status netmon-api netmon-engine redis-server

# Live stream API server logs
sudo journalctl -u netmon-api -f

# Live stream monitoring engine logs
sudo journalctl -u netmon-engine -f

# View rotating log files
tail -f /var/log/netmon/api.log
tail -f /var/log/netmon/engine.log
tail -f /var/log/netmon/error.log
```

---

## 7. Egress Firewall Rules (Enterprise Alerting)

LNMP v3.1.28s dispatches outbound incident notifications directly to external endpoints. Enterprise network administrators must configure egress firewall policies allowing outbound traffic from the LNMP host:

| Destination Service | Protocol & Ports | Purpose |
| :--- | :--- | :--- |
| **Microsoft Teams Workflows** | TCP 443 (HTTPS) | Webhook incident payload dispatch (`*.office.com`, `*.microsoft.com`) |
| **Discord Webhooks** | TCP 443 (HTTPS) | Rich embed alerts (`discord.com`, `discordapp.com`) |
| **Slack Webhooks** | TCP 443 (HTTPS) | Block Kit notifications (`hooks.slack.com`) |
| **Corporate SMTP Relays** | TCP 587 (STARTTLS), TCP 465 (SMTPS), or TCP 25 | Outbound alert emails to corporate exchange / cloud mail gateways |
| **External NTP Servers** | UDP 123 | Continuous system clock synchronization |

> [!NOTE]
> All outbound HTTP webhook calls are strictly validated by the LNMP SSRF defense engine, rejecting destination IP addresses resolving to loopback (`127.0.0.0/8`, `::1`) or link-local cloud metadata (`169.254.169.254`).

---

## 8. Database Migration 0007 Execution Procedure

If executing migrations manually outside of `upgrade.sh`:

```bash
cd /opt/netmon/noop/backend

# Activate python virtual environment and set path
source /opt/netmon/venv/bin/activate
export PYTHONPATH=/opt/netmon/noop:/opt/netmon/noop/backend

# Inspect pending migrations
alembic current
alembic heads

# Apply migration 0007 (creates alert_channels and alert_delivery_logs)
alembic upgrade head

# Verify table creation in PostgreSQL
sudo -u postgres psql -d netmon -c "\dt alert*"
```
