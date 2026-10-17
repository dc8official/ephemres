# Ephemres v3.3.2.v — Disaster Recovery & Database Restoration Runbook

This runbook describes the standard operating procedures for restoring the Ephemres v3.3.2.v database and application environment from timestamped backups (including legacy v3.3.1n and v3.3.0 backups).

---

## 1. Backup File Locations

Automated pre-upgrade and decommissioning backups are generated in plaintext SQL format at:
```bash
/var/backups/ephemres/ephemres_backup_<YYYYMMDD_HHMMSS>.sql
```
*(Legacy backups from earlier versions may be found in `/var/backups/netmon/`)*

To list available backups sorted by date:
```bash
ls -lht /var/backups/ephemres/ /var/backups/netmon/ 2>/dev/null || true
```

---

## 2. Prerequisites Before Restoration

1. Ensure the PostgreSQL and TimescaleDB server services are active:
   ```bash
   sudo systemctl status postgresql
   ```
2. Stop the monitoring engine, API daemons, and flow telemetry collector to prevent write contention during data restoration:
   ```bash
   sudo systemctl stop ephemres-engine ephemres-api ephemres-flowd netmon-engine netmon-api netmon-flowd 2>/dev/null || true
   ```
3. Load database credentials from the environment file:
   ```bash
   set -a
   if [ -f /etc/ephemres/ephemres.env ]; then
       source /etc/ephemres/ephemres.env
   elif [ -f /opt/ephemres/backend/.env ]; then
       source /opt/ephemres/backend/.env
   elif [ -f /etc/netmon/netmon.env ]; then
       source /etc/netmon/netmon.env
   fi
   set +a
   ```

---

## 3. Database Restoration Procedure

### Step 1: Re-create Clean Database Target (Optional / Clean Restore)
If performing a full database rebuild:
```bash
DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD}}"
DB_USER="${EPHEMRES_DB_USER:-${NETMON_DB_USER:-netmon_user}}"
DB_NAME="${EPHEMRES_DB_NAME:-${NETMON_DB_NAME:-netmon}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-127.0.0.1}}"

PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -U "${DB_USER}" -d postgres -c "CREATE DATABASE ${DB_NAME};"
PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
```

### Step 2: Ingest Backup SQL Dump
```bash
RESTORE_FILE="/var/backups/ephemres/ephemres_backup_YYYYMMDD_HHMMSS.sql"

PGPASSWORD="${DB_PASS}" psql \
  -h "${DB_HOST}" \
  -p "${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-5432}}" \
  -U "${DB_USER}" \
  -d "${DB_NAME}" \
  -f "${RESTORE_FILE}"
```

### Step 3: Run Forward Migrations
Ensure all schema tables, views, and continuous aggregates are up to date:
```bash
cd /opt/ephemres/backend
PYTHONPATH=/opt/ephemres:/opt/ephemres/backend /opt/ephemres/venv/bin/python3 -m app.core.migrator upgrade head
```

### Step 4: Clear FastStore In-RAM Session Cache (If Active)
If FastStore In-RAM storage was active, remove any persisted WAL file in `/dev/shm`:
```bash
rm -f /dev/shm/ephemres_faststore.db*
```

### Step 5: Restart Ephemres Daemons
```bash
sudo systemctl restart ephemres-api ephemres-engine nginx
sudo systemctl is-enabled ephemres-flowd &>/dev/null && sudo systemctl restart ephemres-flowd || true
```

### Step 6: Verify Service Health
```bash
curl -s http://127.0.0.1:8000/api/v1/health | jq .
curl -s http://127.0.0.1:8000/api/v1/version | jq .
```
Expected response:
```json
{
  "status": "ok",
  "version": "3.3.2.v"
}
```
