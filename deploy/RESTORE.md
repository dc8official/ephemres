# Ephemres v3.3.5 — Disaster Recovery & Database Restoration Runbook

This runbook describes the standard operating procedures for restoring the Ephemres database and platform configuration from timestamped backup archives across current (`v3.3.5`, `v3.3.2v`), Long-Term Support (`v3.3.0`), and legacy (`v3.1.35s`, `v3.3.1n`) releases.

> [!NOTE]
> **Version Support Policy**: `v3.3.5` is the active current standard, `v3.3.2v` remains supported (upgrade to `v3.3.5` recommended), and `v3.3.0` is the supported Enterprise LTS track. `v3.1.35s` has reached End of Support; `app.core.restore` continues to read `v3.1.35s` backups so operators can migrate directly to `v3.3.0` or `v3.3.5`.

---

## 1. Why Raw `psql -f backup.sql` Fails Across Versions

Restoring a legacy PostgreSQL/TimescaleDB dump into Ephemres using raw `psql -f` often results in fatal errors due to three architectural factors:

1. **TimescaleDB Internal Hypertable Chunk IDs:**
   `pg_dump` dumps hypertable data as `COPY _timescaledb_internal._hyper_<id>_<chunk>_chunk (...) FROM stdin;`. On a freshly provisioned or migrated TimescaleDB instance, these internal chunk table identifiers do not exist, causing PostgreSQL to abort the restoration.
2. **Schema Drift Across Releases:**
   - **`v3.1.35s` (migration `0007`)**: Lacks `flow_exporter_ips`, `flow_interface_aliases`, and `device_role` on `endpoints`, and does not have the `flow_minute_rollups` or `flow_interface_minute_rollups` tables.
   - **`v3.3.0` / `v3.3.1n`**: Feature different column orders and omit recently introduced telemetry rollups.
   - Raw `psql` fails when a backup dump includes columns that no longer exist or omits newly enforced `NOT NULL` constraints.
3. **Password Hashing Algorithm Transition (`$argon2id$` $\rightarrow$ `$scrypt$`):**
   - Pre-`v3.3.2v` backups store `$argon2id$v=19$...` password hashes in `users.password_hash`.
   - `v3.3.2v+` uses Python standard library `hashlib.scrypt` (`ScryptPasswordHasher`, `$scrypt$...`) exclusively and does not authenticate legacy `$argon2id$` hashes.
   - Restoring raw user rows leaves accounts unable to authenticate unless explicitly migrated.

To solve this, Ephemres provides a dedicated Cross-Version Database Restoration Engine (`app.core.restore` / `deploy/restore.sh`).

---

## 2. Backup Archive Locations

Automated pre-upgrade and decommissioning backups are generated in plaintext SQL format at:
```bash
/var/backups/ephemres/ephemres_backup_<YYYYMMDD_HHMMSS>.sql                  # upgrade.sh
/var/backups/ephemres/ephemres_decommission_backup_<YYYYMMDD_HHMMSS>.sql     # uninstall.sh
```
*(Legacy backups from earlier versions may be located in `/var/backups/netmon/` or `/root/`)*

Each backup is only kept once `pg_dump` has finished it (its last lines contain
`-- PostgreSQL database dump complete`); `uninstall.sh` refuses `--purge-data` when the backup fails,
unless `--force-purge` is given. Next to each backup, a `<backup>.key` file (`0600`) holds the
encryption key of the install that made it. Keep the two together: `restore.sh` needs the key to
re-encrypt alert channel secrets once the old install and its `/etc/ephemres` are gone.

To list all available backups sorted by modification time:
```bash
ls -lht /var/backups/ephemres/ /var/backups/netmon/ 2>/dev/null || true
```

---

## 3. Workflow A: Automated Cross-Version Restoration (Recommended)

The automated script [`deploy/restore.sh`](restore.sh) orchestrates the Python restoration engine (`app.core.restore`), safely resolving schema drift, hypertable chunk mappings, and credential conversions.

### Basic Syntax
```bash
sudo bash /opt/ephemres/deploy/restore.sh [BACKUP_FILE] [OPTIONS]
```

### Options and Parameters
| Flag | Description | Default |
| :--- | :--- | :--- |
| `--backup-file <path>` | Path to plaintext `.sql` or gzipped `.sql.gz` backup archive | Auto-detected / Interactive prompt |
| `--admin-password <pass>` | New password for the `admin` account, which must be changed at first login. Applied whatever the backup's era (see step 5 below). A value on the command line is visible to other local users in the process list; prefer the prompt or `EPHEMRES_RESTORE_ADMIN_PASSWORD` | Interactive prompt; leave blank to keep the backed-up admin password |
| `--old-secret-key <key>` | Previous `SECRET_KEY` for re-encrypting alert channel webhooks (or set `OLD_SECRET_KEY`) | `<backup>.key`, else an earlier netmon install's env files; without a key, encrypted channels are restored disabled |
| `--skip-telemetry` | Skip historical hypertable metrics for fast configuration restore | `false` |
| `--allow-incomplete` | Restore a backup that lacks `pg_dump`'s completion trailer (it was cut short; its later tables, including users and endpoints, are missing) | Refused |
| `--dry-run` | Parse and validate backup without committing changes to PostgreSQL | `false` |

### Step-by-Step Restoration Procedure
```bash
# 1. Execute the automated restore runner
#    (prompts for the new admin password with hidden input; leave blank to keep the backed-up one)
sudo bash /opt/ephemres/deploy/restore.sh --backup-file /var/backups/ephemres/ephemres_backup_20261015_120000.sql

# 2. Verify platform service status
sudo systemctl status ephemres-api ephemres-engine ephemres-flowd

# 3. Log into the web interface
# URL: http://<server-ip>/
# User: admin
# Password: TempAdminSecure123!
# Note: You will be prompted to choose a new permanent password immediately upon login.
```

### What `app.core.restore` Executes Automatically:
1. **Daemon Quiescence:** Temporarily halts `ephemres-api`, `ephemres-engine`, and `ephemres-flowd` to prevent concurrent write collisions and stale session cache hits.
2. **Schema Introspection:** Inspects the live PostgreSQL `information_schema` on the target database.
3. **Hypertable Re-mapping:** Translates internal `_hyper_*_chunk` COPY blocks directly into the parent hypertable targets (`public.endpoint_events`, `public.endpoint_probe_samples`, `public.flow_minute_rollups`, `public.flow_interface_minute_rollups`).
4. **Column Intersection:** Dynamically intersects backup dump column lists with target table schemas, safely loading available fields while applying database defaults for missing columns.
5. **Credential Migration:**
   - `admin` with `--admin-password` given (or typed at the prompt): password set to that value with `must_change_password = TRUE`, whatever hash the backup holds. If the backup has no `admin` row, the existing `admin` account in the target database is updated instead.
   - `admin` without a password: a backed-up `$scrypt$` password is kept unchanged. A legacy (pre-`v3.3.2v`, non-scrypt) admin hash is replaced with a random temporary password, recorded in the report below.
   - Every other account with a legacy (non-`$scrypt$`) hash: assigned a random temporary password with `must_change_password = TRUE`. Accounts that already have `$scrypt$` hashes keep their backed-up passwords.
   - Temporary passwords are written to a root-only (`0600`) report, `/opt/ephemres/legacy_password_resets.txt` (override with `EPHEMRES_RESET_REPORT`). The restore summary prints its path and states which of the admin cases above applied. Distribute the passwords securely, then delete the file.
6. **Continuous Aggregate Rebuild:** Refreshes `flow_hourly_rollups`, `flow_daily_rollups`, `endpoint_probe_hourly` and the node baselines over the whole restored history; their policies only look back 3, 7, 3 and 28 days. Aggregates are rebuilt from the restored source rows, so history older than a source's retention (minute flows 90 days, interface minutes 7 days, probe samples 30 days) is not restored into `flow_daily_rollups`, `flow_interface_hourly_rollups` or `endpoint_probe_hourly`.
7. **In-RAM Storage Flush:** Removes any stale FastStore WAL / IPC shared-memory files (`/dev/shm/ephemres_faststore*`).
8. **Daemon Resumption:** Restarts API and engine daemons, also when the restore failed, so the platform does not stay down.

---

## 4. Workflow B: Clean Database Rebuild & Same-Version Manual Restore

Use this workflow only if performing a clean manual rebuild or restoring a backup generated from the identical `v3.3.5` release.

### Step 1: Stop Services
```bash
sudo systemctl stop ephemres-engine ephemres-api ephemres-flowd
```

### Step 2: Load Environment Credentials
```bash
set -a
if [ -f /etc/ephemres/ephemres.env ]; then
    source /etc/ephemres/ephemres.env
elif [ -f /opt/ephemres/backend/.env ]; then
    source /opt/ephemres/backend/.env
fi
set +a

DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD}}"
DB_USER="${EPHEMRES_DB_USER:-${NETMON_DB_USER:-netmon_user}}"
DB_NAME="${EPHEMRES_DB_NAME:-${NETMON_DB_NAME:-netmon}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-127.0.0.1}}"
DB_PORT="${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-5432}}"
```

### Step 3: Re-create Clean Database Target
```bash
PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d postgres -c "CREATE DATABASE ${DB_NAME};"
PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
```

### Step 4: Run Forward Schema Migrations
```bash
cd /opt/ephemres/backend
source /opt/ephemres/venv/bin/activate
export PYTHONPATH=/opt/ephemres:/opt/ephemres/backend

python3 -m app.core.migrator upgrade head
```

### Step 5: Restore Data Using Schema Engine
```bash
# Optional: new admin password (hidden input, kept out of the process list); skip to keep the backed-up one
read -rs -p "New admin password: " EPHEMRES_RESTORE_ADMIN_PASSWORD && export EPHEMRES_RESTORE_ADMIN_PASSWORD
python3 -m app.core.restore \
    --backup-file "/var/backups/ephemres/ephemres_backup_YYYYMMDD_HHMMSS.sql"
unset EPHEMRES_RESTORE_ADMIN_PASSWORD
```

### Step 6: Clear FastStore In-RAM Shared Memory
```bash
rm -f /dev/shm/ephemres_faststore*
```

### Step 7: Restart Ephemres Daemons
```bash
sudo systemctl restart ephemres-api ephemres-engine
sudo systemctl is-enabled ephemres-flowd &>/dev/null && sudo systemctl restart ephemres-flowd || true
```

### Step 8: Verify Service Health
```bash
curl -s http://127.0.0.1:8000/api/v1/health | jq .
curl -s http://127.0.0.1:8000/api/v1/version | jq .
```

Expected output:
```json
{
  "status": "ok",
  "version": "3.3.5"
}
```
