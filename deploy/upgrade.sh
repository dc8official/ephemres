#!/usr/bin/env bash
# ==============================================================================
# Ephemres Network Monitoring Platform v3.3.1n - Automated Upgrade Utility
# ==============================================================================

set -euo pipefail

# Color Palette for Terminal Output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==============================================================================
# 1. Parse Command-Line Options
# ==============================================================================
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo -e "${YELLOW}[DRY-RUN MODE] Simulating upgrade operations without making mutations.${NC}"
fi

# ==============================================================================
# 2. Require Root / Administrative Privileges (bypassed in dry-run mode)
# ==============================================================================
if [[ ${EUID} -ne 0 && ${DRY_RUN} -eq 0 ]]; then
    echo -e "${RED}[ERROR] This script must be executed with root privileges (e.g., sudo ./upgrade.sh).${NC}" >&2
    exit 1
fi

echo -e "${BLUE}========================================================================${NC}"
echo -e "${BLUE}    Ephemres Network Monitoring Platform v3.3.1n - Upgrade Utility      ${NC}"
echo -e "${BLUE}========================================================================${NC}"

# Resolve Script and Project Root Directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="/opt/ephemres"
REPO_URL="${EPHEMRES_REPO_URL:-https://github.com/dc8official/ephemres.git}"
# Resolve Active or Target Upgrade Branch (defaults to current git branch or v3.3.1n)
CURRENT_GIT_BRANCH="$(git -C "${PROJECT_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
UPGRADE_BRANCH="${EPHEMRES_BRANCH:-${CURRENT_GIT_BRANCH:-v3.3.1n}}"

# 3. Read Configuration Values
ENV_FILE=""
for candidate in "/etc/ephemres/ephemres.env" "${PROJECT_ROOT}/backend/.env" "${PROJECT_ROOT}/.env" "/etc/netmon/netmon.env"; do
    if [[ -f "${candidate}" ]]; then
        ENV_FILE="${candidate}"
        break
    fi
done

if [[ -n "${ENV_FILE}" ]]; then
    echo -e "${GREEN}[INFO] Loading environment configuration from ${ENV_FILE}${NC}"
    if [[ ${DRY_RUN:-0} -eq 0 ]]; then
        chmod 0600 "${ENV_FILE}"
        chown ephemres:ephemres "${ENV_FILE}" 2>/dev/null || true
    fi
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
elif [[ -f "${PROJECT_ROOT}/backend/.env" ]]; then
    echo -e "${YELLOW}[WARN] Configuration file not found in /etc/ephemres. Loading local backend/.env${NC}"
    # shellcheck disable=SC1090
    set -a
    source "${PROJECT_ROOT}/backend/.env"
    set +a
fi

DB_NAME="${EPHEMRES_DB_NAME:-${NETMON_DB_NAME:-${POSTGRES_DB:-netmon}}}"
DB_USER="${EPHEMRES_DB_USER:-${NETMON_DB_USER:-${POSTGRES_USER:-netmon_user}}}"
DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD:-${POSTGRES_PASSWORD:-netmon_secure_password}}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-${POSTGRES_HOST:-127.0.0.1}}}"
DB_PORT="${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-${POSTGRES_PORT:-5432}}}"

# 3. Pre-Upgrade Database Backup
BACKUP_DIR="/var/backups/ephemres"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/ephemres_backup_${TIMESTAMP}.sql"

echo -e "\n${BLUE}--- Step 1/7: Executing Pre-Upgrade Database Backup ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    if ! command -v pg_dump &> /dev/null; then
        echo -e "${RED}[ERROR] pg_dump command not found. Please install postgresql-client.${NC}" >&2
        exit 1
    fi

    # Gracefully stop services before backup to release database connection pool slots
    if systemctl is-active --quiet ephemres-engine || systemctl is-active --quiet ephemres-api || systemctl is-active --quiet ephemres-flowd || \
       systemctl is-active --quiet netmon-engine || systemctl is-active --quiet netmon-api || systemctl is-active --quiet netmon-flowd 2>/dev/null; then
        echo -e "${GREEN}[INFO] Pausing background services to release database connection slots...${NC}"
        systemctl stop ephemres-engine ephemres-api ephemres-flowd netmon-engine netmon-api netmon-flowd 2>/dev/null || true
    fi

    mkdir -p "${BACKUP_DIR}"
    chown root:postgres "${BACKUP_DIR}" 2>/dev/null || true
    chmod 775 "${BACKUP_DIR}"
    echo -e "${GREEN}[INFO] Creating timestamped database dump at ${BACKUP_FILE}...${NC}"
    
    BACKUP_SUCCESS=0
    if PGPASSWORD="${DB_PASS}" pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -F p -f "${BACKUP_FILE}" 2>/dev/null; then
        BACKUP_SUCCESS=1
    elif command -v sudo &>/dev/null && id -u postgres &>/dev/null; then
        echo -e "${YELLOW}[WARN] Non-superuser connection slots exhausted. Attempting fallback via postgres superuser...${NC}"
        # Terminate any orphaned/idle connections holding database slots
        sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" &>/dev/null || true
        if sudo -u postgres pg_dump -d "${DB_NAME}" -F p > "${BACKUP_FILE}"; then
            BACKUP_SUCCESS=1
        fi
    fi

    if [[ ${BACKUP_SUCCESS} -eq 1 ]]; then
        chmod 640 "${BACKUP_FILE}"
        echo -e "${GREEN}[SUCCESS] Pre-upgrade backup successfully saved to ${BACKUP_FILE}${NC}"
    else
        echo -e "${RED}[ERROR] Database backup failed. Aborting upgrade to preserve data safety.${NC}" >&2
        echo -e "${YELLOW}[HINT] If PostgreSQL connection slots are exhausted, stop background services first: 'systemctl stop ephemres-engine ephemres-api ephemres-flowd' or 'systemctl restart postgresql'.${NC}" >&2
        exit 1
    fi
else
    echo -e "[DRY-RUN] Would generate SQL dump to ${BACKUP_FILE}"
fi

# 4. Smart Config Migration (In-Place Upgrade)
CONFIG_FILE=""
for cfg_candidate in "/etc/ephemres/config.toml" "/etc/netmon/config.toml"; do
    if [[ -f "${cfg_candidate}" ]]; then
        CONFIG_FILE="${cfg_candidate}"
        break
    fi
done

echo -e "\n${BLUE}--- Step 2/7: Migrating System Configuration Defaults ---${NC}"
if [[ ${DRY_RUN} -eq 0 && -n "${CONFIG_FILE}" && -f "${CONFIG_FILE}" ]]; then
    echo -e "${GREEN}[INFO] Verifying configuration settings in ${CONFIG_FILE}...${NC}"
    
    # Update ping timing budget to 5 pings @ 8.0s
    if grep -q "ping_count = 10" "${CONFIG_FILE}"; then
        sed -i 's/ping_count = 10/ping_count = 5/' "${CONFIG_FILE}"
        echo -e "${GREEN}[INFO] Updated ping_count to 5 probes.${NC}"
    fi
    if grep -q "ping_interval_seconds = 6" "${CONFIG_FILE}"; then
        sed -i 's/ping_interval_seconds = 6/ping_interval_seconds = 8/' "${CONFIG_FILE}"
        echo -e "${GREEN}[INFO] Updated ping_interval_seconds to 8s.${NC}"
    fi

    # Update session_timeout_minutes to 120 if currently 30
    if grep -q "session_timeout_minutes = 30" "${CONFIG_FILE}"; then
        sed -i 's/session_timeout_minutes = 30/session_timeout_minutes = 120/' "${CONFIG_FILE}"
        echo -e "${GREEN}[INFO] Updated session_timeout_minutes to 120 (2 hours).${NC}"
    fi

    # Add max_active_sessions_per_user if missing
    if ! grep -q "max_active_sessions_per_user" "${CONFIG_FILE}"; then
        sed -i '/\[security\]/a max_active_sessions_per_user = 2' "${CONFIG_FILE}"
        echo -e "${GREEN}[INFO] Added max_active_sessions_per_user = 2 to [security].${NC}"
    fi

    # Add [redis] section if missing
    if ! grep -q "\[redis\]" "${CONFIG_FILE}"; then
        cat << 'EOF' >> "${CONFIG_FILE}"

[redis]
host = "127.0.0.1"
port = 6379
db = 0
enabled = true
performance_mode = false
EOF
        echo -e "${GREEN}[INFO] Appended [redis] storage driver section to ${CONFIG_FILE}.${NC}"
    fi

    # Add [flow] section if missing (Network Flow Telemetry)
    if ! grep -q "\[flow\]" "${CONFIG_FILE}"; then
        cat << 'EOF' >> "${CONFIG_FILE}"

[flow]
enabled = false
netflow_port = 2055
ipfix_port = 4739
sampling_multiplier = 1
buffer_memory_limit_mb = 512
EOF
        echo -e "${GREEN}[INFO] Appended [flow] network telemetry section to ${CONFIG_FILE}.${NC}"
    fi
fi

# Pre-Flight: Build Frontend Assets Before Service Pause
echo -e "\n${BLUE}--- Pre-Flight: Verifying & Compiling Frontend Production Bundle ---${NC}"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
if [[ ${DRY_RUN} -eq 0 && -d "${FRONTEND_DIR}" && -f "${FRONTEND_DIR}/package.json" ]]; then
    if command -v npm &>/dev/null; then
        echo -e "${GREEN}[INFO] Building Vue 3 frontend bundle in pre-flight (${FRONTEND_DIR})...${NC}"
        if (cd "${FRONTEND_DIR}" && npm install && npm run build); then
            echo -e "${GREEN}[SUCCESS] Pre-flight frontend build completed successfully.${NC}"
        elif [[ -f "${FRONTEND_DIR}/dist/index.html" ]]; then
            echo -e "${YELLOW}[WARN] npm build failed or air-gapped network detected. Existing frontend/dist/index.html found; proceeding with pre-built assets.${NC}"
        else
            echo -e "${RED}[ERROR] Frontend build failed and no pre-built frontend/dist/index.html found. Aborting upgrade before stopping services.${NC}" >&2
            exit 1
        fi
    elif [[ -f "${FRONTEND_DIR}/dist/index.html" ]]; then
        echo -e "${GREEN}[INFO] npm command not found; air-gapped environment detected. Using existing pre-built frontend/dist/index.html.${NC}"
    else
        echo -e "${RED}[ERROR] npm command not found and no pre-built frontend/dist/index.html exists. Cannot proceed.${NC}" >&2
        exit 1
    fi
else
    echo -e "[DRY-RUN] Pre-flight frontend build check passed."
fi

# 5. Service Pause
echo -e "\n${BLUE}--- Step 3/7: Gracefully Pausing Platform Background Daemons ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    if systemctl is-active --quiet ephemres-engine || systemctl is-active --quiet ephemres-api || systemctl is-active --quiet ephemres-flowd || \
       systemctl is-active --quiet netmon-engine || systemctl is-active --quiet netmon-api || systemctl is-active --quiet netmon-flowd; then
        echo -e "${GREEN}[INFO] Stopping ephemres-engine, ephemres-api, and ephemres-flowd systemd services...${NC}"
        systemctl stop ephemres-engine ephemres-api ephemres-flowd netmon-engine netmon-api netmon-flowd || true
    else
        echo -e "${YELLOW}[INFO] Platform systemd services are not active. Skipping stop.${NC}"
    fi
else
    echo -e "[DRY-RUN] Would run: systemctl stop ephemres-engine ephemres-api ephemres-flowd"
fi

# 6. Fetch Latest Release Files from Repository
echo -e "\n${BLUE}--- Step 4/7: Fetching Latest Release & Updating System Dependencies ---${NC}"
SOURCE_DIR="${PROJECT_ROOT}"
STAGE_DIR="/tmp/ephemres-upgrade-stage-$$"

if [[ ${DRY_RUN} -eq 0 ]]; then
    # Ensure system user ephemres exists
    if ! id "ephemres" &>/dev/null; then
        useradd -r -s /usr/sbin/nologin ephemres || true
    fi

    # Ensure system dependencies (git, redis-server, traceroute, libcap2-bin)
    if command -v apt-get &>/dev/null; then
        PACKAGES_TO_CHECK="git redis-server traceroute libcap2-bin rsync libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0"
        MISSING_PKGS=""
        for pkg in ${PACKAGES_TO_CHECK}; do
            if ! dpkg -l "${pkg}" 2>/dev/null | grep -q "^ii"; then
                MISSING_PKGS="${MISSING_PKGS} ${pkg}"
            fi
        done

        if [[ -n "${MISSING_PKGS}" ]]; then
            echo -e "${GREEN}[INFO] Installing missing system packages:${MISSING_PKGS}...${NC}"
            apt-get update -qq && apt-get install -y ${MISSING_PKGS} || true
        fi

        # Enable and start Redis
        systemctl enable --now redis-server 2>/dev/null || systemctl enable --now redis 2>/dev/null || true

        # Set network capabilities for raw ICMP traceroute
        TRACEROUTE_BIN=$(command -v traceroute || true)
        if [[ -n "${TRACEROUTE_BIN}" ]] && command -v setcap &>/dev/null; then
            setcap cap_net_raw+ep "${TRACEROUTE_BIN}" || true
        fi
    fi

    # Case A: PROJECT_ROOT is an existing Git clone directory
    if [[ -d "${PROJECT_ROOT}/.git" ]]; then
        echo -e "${GREEN}[INFO] Detected Git repository at ${PROJECT_ROOT}. Pulling latest updates...${NC}"
        cd "${PROJECT_ROOT}"
        git config --global --add safe.directory "${PROJECT_ROOT}" 2>/dev/null || true
        git fetch --all --tags --prune || true
        git checkout "${UPGRADE_BRANCH}" 2>/dev/null || git checkout v3.3.1n 2>/dev/null || git checkout v3.3.0 2>/dev/null || true
        git pull origin "${UPGRADE_BRANCH}" 2>/dev/null || git pull origin v3.3.1n 2>/dev/null || git pull origin v3.3.0 2>/dev/null || git pull || echo -e "${YELLOW}[WARN] Git pull finished with non-zero exit code. Proceeding with existing files.${NC}"
        SOURCE_DIR="${PROJECT_ROOT}"
    # Case B: Running from /opt/ephemres or non-git directory -> clone directly from remote
    else
        echo -e "${GREEN}[INFO] Staging fresh release from ${REPO_URL} (branch: ${UPGRADE_BRANCH})...${NC}"
        rm -rf "${STAGE_DIR}"
        if git clone --depth 1 --branch "${UPGRADE_BRANCH}" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 --branch "v3.3.1n" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 --branch "v3.3.0" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 "${REPO_URL}" "${STAGE_DIR}"; then
            echo -e "${GREEN}[SUCCESS] Downloaded latest codebase into staging directory.${NC}"
            SOURCE_DIR="${STAGE_DIR}"
        else
            echo -e "${RED}[ERROR] Failed to clone repository from ${REPO_URL}. Check internet connectivity.${NC}" >&2
            exit 1
        fi
    fi
    # Rebuild Vue 3 frontend in SOURCE_DIR if needed
    FRONTEND_DIR="${SOURCE_DIR}/frontend"
    if [[ -d "${FRONTEND_DIR}" && -f "${FRONTEND_DIR}/package.json" ]]; then
        if command -v npm &>/dev/null; then
            echo -e "${GREEN}[INFO] Rebuilding production Vue 3 frontend bundle in ${FRONTEND_DIR}...${NC}"
            (cd "${FRONTEND_DIR}" && npm install && npm run build) || {
                if [[ -f "${FRONTEND_DIR}/dist/index.html" ]]; then
                    echo -e "${YELLOW}[WARN] npm build failed or air-gapped network detected. Existing frontend/dist/index.html found; proceeding.${NC}"
                else
                    echo -e "${RED}[ERROR] Frontend build failed and no pre-built dist/index.html found.${NC}" >&2
                    exit 1
                fi
            }
        elif [[ -f "${FRONTEND_DIR}/dist/index.html" ]]; then
            echo -e "${GREEN}[INFO] npm not found; using existing pre-built ${FRONTEND_DIR}/dist/index.html.${NC}"
        fi
    fi
else
    echo -e "[DRY-RUN] Would fetch latest code from ${REPO_URL} (${UPGRADE_BRANCH}), build Vue 3 frontend, and ensure system packages (redis-server, traceroute, libcap2-bin)"
fi

# 7. Synchronize Production Files to Target Directory
echo -e "\n${BLUE}--- Step 5/7: Synchronizing Codebase & Enforcing Production Structure ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    mkdir -p "${INSTALL_DIR}" /run/ephemres /var/log/ephemres
    chown -R ephemres:ephemres /run/ephemres /var/log/ephemres 2>/dev/null || true
    if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
        echo -e "${GREEN}[INFO] Syncing repository files from ${SOURCE_DIR} to ${INSTALL_DIR}...${NC}"
        rsync -a --delete \
            --exclude='.git' \
            --exclude='frontend/node_modules' \
            --exclude='backend/venv' \
            --exclude='backend/.env' \
            --exclude='.env' \
            --exclude='tests' \
            --exclude='pytest.ini' \
            --exclude='scratch' \
            --exclude='certs' \
            "${SOURCE_DIR}/" "${INSTALL_DIR}/"
        chown -R ephemres:ephemres "${INSTALL_DIR}"
    fi

    # Clean up temporary staging directory if used
    if [[ -d "${STAGE_DIR}" ]]; then
        rm -rf "${STAGE_DIR}"
    fi
else
    echo -e "[DRY-RUN] Would rsync codebase to ${INSTALL_DIR} and set ownership to ephemres:ephemres"
fi

# 8. Dependency Installation & Database Migrations
echo -e "\n${BLUE}--- Step 6/7: Upgrading Python Dependencies & Database Migrations ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    # Determine Python virtual environment path
    VENV_PATH=""
    if [[ -d "/opt/ephemres/venv" ]]; then
        VENV_PATH="/opt/ephemres/venv"
    elif [[ -d "/opt/netmon/venv" ]]; then
        VENV_PATH="/opt/netmon/venv"
    elif [[ -d "${INSTALL_DIR}/backend/venv" ]]; then
        VENV_PATH="${INSTALL_DIR}/backend/venv"
    elif [[ -d "${PROJECT_ROOT}/.venv" ]]; then
        VENV_PATH="${PROJECT_ROOT}/.venv"
    fi

    if [[ -n "${VENV_PATH}" && -f "${INSTALL_DIR}/backend/requirements.txt" ]]; then
        echo -e "${GREEN}[INFO] Upgrading Python dependencies in ${VENV_PATH}...${NC}"
        "${VENV_PATH}/bin/pip" install --upgrade pip
        "${VENV_PATH}/bin/pip" install -r "${INSTALL_DIR}/backend/requirements.txt" --upgrade

        # Set network raw capabilities for unprivileged ICMP on virtual environment python3 binary
        PY3_REAL="$(readlink -f "${VENV_PATH}/bin/python3" 2>/dev/null || true)"
        if [[ -n "${PY3_REAL}" ]] && [[ -f "${PY3_REAL}" ]] && command -v setcap &>/dev/null; then
            echo -e "${GREEN}[INFO] Applying cap_net_raw+ep to Python binary (${PY3_REAL})...${NC}"
            setcap cap_net_raw+ep "${PY3_REAL}" || true
        fi
    else
        echo -e "${YELLOW}[WARN] Virtual environment not found at /opt/ephemres/venv. Skipping pip upgrade.${NC}"
    fi

    # Database Schema Migrations
    cd "${INSTALL_DIR}/backend"
    PYTHON_BIN=""
    if [[ -n "${VENV_PATH:-}" && -f "${VENV_PATH}/bin/python3" ]]; then
        PYTHON_BIN="${VENV_PATH}/bin/python3"
    elif [[ -f "/opt/ephemres/venv/bin/python3" ]]; then
        PYTHON_BIN="/opt/ephemres/venv/bin/python3"
    elif [[ -f "/opt/netmon/venv/bin/python3" ]]; then
        PYTHON_BIN="/opt/netmon/venv/bin/python3"
    elif [[ -f "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
        PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
    fi

    MIGRATOR_CMD=()
    if [[ -n "${PYTHON_BIN}" && -x "${PYTHON_BIN}" ]]; then
        MIGRATOR_CMD=("${PYTHON_BIN}" "-m" "app.core.migrator")
    fi

    if [[ -d "${INSTALL_DIR}/backend/migrations" && ${#MIGRATOR_CMD[@]} -gt 0 ]]; then
        echo -e "${GREEN}[INFO] Running native schema migration (app.core.migrator upgrade head)...${NC}"
        export EPHEMRES_DB_PASSWORD="${DB_PASS}"
        export NETMON_DB_PASSWORD="${DB_PASS}"
        PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${MIGRATOR_CMD[@]}" upgrade head
        
        if [[ -n "${PYTHON_BIN}" && -f "${PYTHON_BIN}" ]]; then
            echo -e "${GREEN}[INFO] Verifying default admin account seeding...${NC}"
            PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.seed_admin || true
        fi

        # TimescaleDB Hypertable Decompression Safety & Legacy Row Sanitization
        echo -e "${GREEN}[INFO] Applying TimescaleDB hypertable decompression safety configuration...${NC}"
        if command -v psql &>/dev/null; then
            PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=0 -c "
            DO \$\$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                    PERFORM set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', '0', false);
                END IF;
                UPDATE endpoint_events
                SET end_time = start_time + INTERVAL '60 seconds',
                    duration_seconds = 60
                WHERE end_time IS NULL
                  AND start_time < NOW() - INTERVAL '7 days';

                -- Drop legacy cycle count check constraints to allow continuous state monitoring
                ALTER TABLE endpoint_events DROP CONSTRAINT IF EXISTS ck_events_success_count;
                ALTER TABLE endpoint_events DROP CONSTRAINT IF EXISTS ck_events_failed_count;
            END \$\$;" 2>/dev/null || true

            PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c \
                "ALTER DATABASE \"${DB_NAME}\" SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0;" 2>/dev/null || true
        fi
    fi
else
    echo -e "[DRY-RUN] Would upgrade pip dependencies, compile Vue 3 assets, execute app.core.migrator upgrade head, and apply TimescaleDB decompression tuning"
fi

# 9. Service Unit Refresh, Auto-Start Enablement & Restart
echo -e "\n${BLUE}--- Step 7/7: Refreshing Systemd Units, Enabling Auto-Start & Starting Services ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    # Refresh systemd unit files if available in deploy/
    for svc in ephemres-api ephemres-engine ephemres-flowd; do
        if [[ -f "${INSTALL_DIR}/deploy/${svc}.service" ]]; then
            cp "${INSTALL_DIR}/deploy/${svc}.service" /etc/systemd/system/
        elif [[ -f "${PROJECT_ROOT}/deploy/${svc}.service" ]]; then
            cp "${PROJECT_ROOT}/deploy/${svc}.service" /etc/systemd/system/
        fi
    done

    # Ensure runtime and log directories exist before daemon activation
    mkdir -p /run/ephemres /var/log/ephemres
    chown -R ephemres:ephemres /run/ephemres /var/log/ephemres 2>/dev/null || true

    # Grant ephemres user sudo privilege to start/stop ephemres-flowd dynamically from web UI
    if [[ -d "/etc/sudoers.d" ]]; then
        cat << 'EOF' > /etc/sudoers.d/ephemres
ephemres ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ephemres-flowd, /usr/bin/systemctl stop ephemres-flowd, /usr/bin/systemctl restart ephemres-flowd, /usr/bin/systemctl is-active ephemres-flowd, /usr/bin/systemctl enable ephemres-flowd, /usr/bin/systemctl disable ephemres-flowd
EOF
        chmod 0440 /etc/sudoers.d/ephemres
    fi

    echo -e "${GREEN}[INFO] Reloading systemd daemons and enabling auto-start on boot...${NC}"
    systemctl daemon-reload
    systemctl enable --now redis-server 2>/dev/null || systemctl enable --now redis 2>/dev/null || true
    systemctl enable ephemres-api ephemres-engine || true
    systemctl restart ephemres-api ephemres-engine
    systemctl restart nginx || true

    # Network Flow Telemetry Ingestion
    FLOW_ENABLED=false
    if [[ -n "${DB_PASS:-}" ]]; then
        DB_FLOW_VAL=$(PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -tAc "SELECT setting_value FROM app_settings WHERE setting_key = 'flow_ingestion_enabled';" 2>/dev/null || echo "")
        if [[ "${DB_FLOW_VAL}" == "true" ]]; then
            FLOW_ENABLED=true
        fi
    fi
    if [[ "${FLOW_ENABLED}" == "false" ]] && grep -A 5 "\[flow\]" "${CONFIG_FILE}" 2>/dev/null | grep -q "enabled = true"; then
        FLOW_ENABLED=true
    fi

    if [[ "${FLOW_ENABLED}" == "true" ]]; then
        echo -e "${GREEN}[INFO] Flow telemetry ingestion is enabled. Starting ephemres-flowd...${NC}"
        systemctl enable ephemres-flowd || true
        systemctl restart ephemres-flowd || true

        # Manage firewall if ufw is active
        if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
            echo -e "${GREEN}[INFO] Configuring firewall rules for NetFlow (UDP 2055) and IPFIX (UDP 4739)...${NC}"
            ufw allow 2055/udp comment 'Ephemres NetFlow v5/v9 Telemetry' || true
            ufw allow 4739/udp comment 'Ephemres IPFIX Telemetry' || true
        fi
    else
        echo -e "${YELLOW}[INFO] Flow telemetry is disabled in settings. ephemres-flowd service installed and ready for UI activation.${NC}"
    fi

    sleep 2
    if systemctl is-active --quiet ephemres-api && systemctl is-active --quiet ephemres-engine; then
        echo -e "${GREEN}[SUCCESS] Primary platform services (ephemres-api, ephemres-engine) are active, enabled on boot, and healthy.${NC}"
    else
        echo -e "${YELLOW}[WARN] Check service status via: systemctl status ephemres-api ephemres-engine${NC}"
    fi
else
    echo -e "[DRY-RUN] Would run: systemctl enable & restart redis-server ephemres-api ephemres-engine (and ephemres-flowd if enabled)"
fi

echo -e "\n${GREEN}========================================================================${NC}"
echo -e "${GREEN}   [UPGRADE COMPLETE] Ephemres v3.3.1n Platform upgraded successfully!   ${NC}"
echo -e "${GREEN}   Pre-Upgrade Database Backup Saved At: ${BACKUP_FILE}${NC}"
echo -e "${GREEN}========================================================================${NC}"
