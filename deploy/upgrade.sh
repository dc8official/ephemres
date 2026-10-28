#!/usr/bin/env bash
# ==============================================================================
# Ephemres Network Monitoring Platform v3.3.5 - Automated Upgrade Utility
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
echo -e "${BLUE}    Ephemres Network Monitoring Platform v3.3.5 - Upgrade Utility        ${NC}"
echo -e "${BLUE}========================================================================${NC}"

# Resolve Script and Project Root Directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=backup_lib.sh
source "${SCRIPT_DIR}/backup_lib.sh"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="/opt/ephemres"
REPO_URL="${EPHEMRES_REPO_URL:-https://github.com/dc8official/ephemres.git}"
# Resolve Active or Target Upgrade Branch (defaults to current git branch or v3.3.5)
CURRENT_GIT_BRANCH="$(git -C "${PROJECT_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
UPGRADE_BRANCH="${EPHEMRES_BRANCH:-${CURRENT_GIT_BRANCH:-v3.3.5}}"
UPGRADE_LOG="${EPHEMRES_UPGRADE_LOG:-/var/log/ephemres/upgrade.log}"

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
        # The legacy /etc/netmon/netmon.env stays owned by netmon so the old install can still be
        # started for a rollback; the migrated copy in /etc/ephemres is owned by ephemres.
        if [[ "${ENV_FILE}" != "/etc/netmon/netmon.env" ]]; then
            chown ephemres:ephemres "${ENV_FILE}" 2>/dev/null || true
        fi
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

# Legacy netmon layout (v3.2.0 / v3.3.0): code in /opt/netmon/noop, venv in /opt/netmon/venv,
# config and env in /etc/netmon, user netmon, units netmon-*. The ephemres units run
# /opt/ephemres/venv as user ephemres and read /etc/ephemres, so such an install is moved over
# (see migrate_legacy_config and migrate_legacy_services below).
LEGACY_NETMON=0
LEGACY_FLOWD_ENABLED=0
if [[ ! -f /etc/ephemres/ephemres.env && -f /etc/netmon/netmon.env ]]; then
    LEGACY_NETMON=1
    echo -e "${YELLOW}[INFO] Legacy netmon install detected (/etc/netmon/netmon.env, no /etc/ephemres/ephemres.env): it will be migrated to the ephemres layout.${NC}"
    # v3.2/v3.3.0 install.sh enabled and started netmon-flowd unconditionally; note it before the
    # upgrade stops and disables the netmon units (see migrate_legacy_services).
    if systemctl is-enabled --quiet netmon-flowd 2>/dev/null; then
        LEGACY_FLOWD_ENABLED=1
    fi
fi

DB_NAME="${EPHEMRES_DB_NAME:-${NETMON_DB_NAME:-${POSTGRES_DB:-netmon}}}"
DB_USER="${EPHEMRES_DB_USER:-${NETMON_DB_USER:-${POSTGRES_USER:-netmon_user}}}"
DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD:-${POSTGRES_PASSWORD:-}}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-${POSTGRES_HOST:-127.0.0.1}}}"
DB_PORT="${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-${POSTGRES_PORT:-5432}}}"

if [[ -z "${DB_PASS}" && ${DRY_RUN} -eq 0 ]]; then
    echo -e "${RED}[ERROR] Database password is unset. Please configure EPHEMRES_DB_PASSWORD in the environment or configuration file.${NC}" >&2
    exit 1
fi

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
    
    # backup_create falls back to the postgres superuser, which still connects through the
    # reserved superuser slots when the application user's connection slots are exhausted.
    if backup_create "${BACKUP_FILE}" "${DB_NAME}" "${DB_USER}" "${DB_PASS}" "${DB_HOST}" "${DB_PORT}"; then
        echo -e "${GREEN}[SUCCESS] Pre-upgrade backup successfully saved to ${BACKUP_FILE}${NC}"
        if backup_save_key "${BACKUP_FILE}" "${INSTALL_DIR}" "${INSTALL_DIR}/venv/bin/python3"; then
            echo -e "${GREEN}[INFO] Encryption key saved to ${BACKUP_FILE}.key${NC}"
        else
            echo -e "${YELLOW}[WARN] Could not determine SECRET_KEY; no key file was saved next to the backup.${NC}"
        fi
    else
        echo -e "${RED}[ERROR] Database backup failed. Aborting upgrade to preserve data safety.${NC}" >&2
        echo -e "${YELLOW}[HINT] If PostgreSQL connection slots are exhausted, stop background services first: 'systemctl stop ephemres-engine ephemres-api ephemres-flowd' or 'systemctl restart postgresql'.${NC}" >&2
        exit 1
    fi
else
    echo -e "[DRY-RUN] Would generate SQL dump to ${BACKUP_FILE}"
fi

# 3b. Legacy netmon install: user, directories, env file and config for the ephemres units.
# Runs after the backup so nothing changes before a verified dump exists. The /etc/netmon files
# are copied, never moved; together with the pre-upgrade backup they allow a rollback.
migrate_legacy_config() {
    echo -e "\n${BLUE}--- Migrating legacy netmon configuration to /etc/ephemres ---${NC}"
    if ! id ephemres &>/dev/null; then
        useradd -r -s /usr/sbin/nologin ephemres
    fi
    local dir
    for dir in /etc/ephemres /var/log/ephemres /var/backups/ephemres /run/ephemres; do
        mkdir -p "${dir}"
        chown ephemres:ephemres "${dir}"
    done

    # NETMON_* keys are written again as EPHEMRES_* with the same values (as install.sh writes
    # both), so the database password and the key that encrypts alert channel secrets carry over.
    # A NETMON_CONFIG_PATH would point the ephemres units back at /etc/netmon, which user ephemres
    # cannot read; the config now lives in /etc/ephemres/config.toml, so that key is dropped.
    local new_env="/etc/ephemres/ephemres.env"
    {
        echo "# Migrated from /etc/netmon/netmon.env by upgrade.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        awk '/^NETMON_CONFIG_PATH=/ { next }
             /^NETMON_[A-Za-z0-9_]+=/ { key = $0; sub(/=.*/, "", key); sub(/^NETMON_/, "EPHEMRES_", key);
                                        print key substr($0, index($0, "=")) }
             { print }' /etc/netmon/netmon.env
    } > "${new_env}.tmp"
    chmod 0600 "${new_env}.tmp"
    chown ephemres:ephemres "${new_env}.tmp"
    mv -f "${new_env}.tmp" "${new_env}"
    echo -e "${GREEN}[INFO] Created ${new_env} from /etc/netmon/netmon.env.${NC}"

    if [[ -f /etc/netmon/config.toml && ! -f /etc/ephemres/config.toml ]]; then
        sed 's#^log_dir = "/var/log/netmon"#log_dir = "/var/log/ephemres"#' /etc/netmon/config.toml > /etc/ephemres/config.toml
        chown root:ephemres /etc/ephemres/config.toml
        chmod 640 /etc/ephemres/config.toml
        echo -e "${GREEN}[INFO] Copied /etc/netmon/config.toml to /etc/ephemres/config.toml (log_dir -> /var/log/ephemres).${NC}"
    fi
}

if [[ ${LEGACY_NETMON} -eq 1 ]]; then
    if [[ ${DRY_RUN} -eq 0 ]]; then
        migrate_legacy_config
    else
        echo -e "[DRY-RUN] Would create user ephemres, /etc/ephemres/ephemres.env (from /etc/netmon/netmon.env) and /etc/ephemres/config.toml (from /etc/netmon/config.toml)"
    fi
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

    # Add [faststore] section if missing
    if ! grep -q "\[faststore\]" "${CONFIG_FILE}" && ! grep -q "\[redis\]" "${CONFIG_FILE}"; then
        cat << 'EOF' >> "${CONFIG_FILE}"

[faststore]
host = "127.0.0.1"
port = 6379
db = 0
enabled = true
performance_mode = false
EOF
        echo -e "${GREEN}[INFO] Appended [faststore] storage driver section to ${CONFIG_FILE}.${NC}"
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
        if (cd "${FRONTEND_DIR}" && npm install --no-update-notifier && npm run build); then
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

    # Ensure system dependencies (git, traceroute, libcap2-bin, rsync)
    if command -v apt-get &>/dev/null; then
        PACKAGES_TO_CHECK="git traceroute libcap2-bin rsync"
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

        # Set network capabilities for raw ICMP traceroute
        TRACEROUTE_PATH=$(command -v traceroute || true)
        if [[ -n "${TRACEROUTE_PATH}" ]]; then
            TRACEROUTE_BIN="$(readlink -f "${TRACEROUTE_PATH}" 2>/dev/null || echo "${TRACEROUTE_PATH}")"
            if [[ -f "${TRACEROUTE_BIN}" ]] && command -v setcap &>/dev/null; then
                setcap cap_net_raw+ep "${TRACEROUTE_BIN}" || true
            fi
        fi
    fi

    # Case A: PROJECT_ROOT is an existing Git clone directory
    if [[ -d "${PROJECT_ROOT}/.git" ]]; then
        echo -e "${GREEN}[INFO] Detected Git repository at ${PROJECT_ROOT}. Pulling latest updates...${NC}"
        cd "${PROJECT_ROOT}"
        git config --global --add safe.directory "${PROJECT_ROOT}" 2>/dev/null || true
        git fetch --all --tags --prune || true
        git checkout "${UPGRADE_BRANCH}" 2>/dev/null || git checkout v3.3.5 2>/dev/null || git checkout v3.3.2v 2>/dev/null || git checkout v3.3.1n 2>/dev/null || true
        git pull origin "${UPGRADE_BRANCH}" 2>/dev/null || git pull origin v3.3.5 2>/dev/null || git pull origin v3.3.2v 2>/dev/null || git pull origin v3.3.1n 2>/dev/null || git pull || echo -e "${YELLOW}[WARN] Git pull finished with non-zero exit code. Proceeding with existing files.${NC}"
        SOURCE_DIR="${PROJECT_ROOT}"
    # Case B: Running from /opt/ephemres or non-git directory -> clone directly from remote
    else
        echo -e "${GREEN}[INFO] Staging fresh release from ${REPO_URL} (branch: ${UPGRADE_BRANCH})...${NC}"
        rm -rf "${STAGE_DIR}"
        if git clone --depth 1 --branch "${UPGRADE_BRANCH}" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 --branch "v3.3.5" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 --branch "v3.3.2v" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
           git clone --depth 1 --branch "v3.3.1n" "${REPO_URL}" "${STAGE_DIR}" 2>/dev/null || \
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
            (cd "${FRONTEND_DIR}" && npm install --no-update-notifier && npm run build) || {
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
    echo -e "[DRY-RUN] Would fetch latest code from ${REPO_URL} (${UPGRADE_BRANCH}), build Vue 3 frontend, and ensure system packages (traceroute, libcap2-bin)"
fi

# 7. Synchronize Production Files to Target Directory
echo -e "\n${BLUE}--- Step 5/7: Synchronizing Codebase & Enforcing Production Structure ---${NC}"
if [[ ${DRY_RUN} -eq 0 ]]; then
    mkdir -p "${INSTALL_DIR}" /run/ephemres /var/log/ephemres
    chown -R ephemres:ephemres /run/ephemres /var/log/ephemres 2>/dev/null || true
    if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
        echo -e "${GREEN}[INFO] Syncing repository files from ${SOURCE_DIR} to ${INSTALL_DIR}...${NC}"
        # --delete removes anything the source lacks, so runtime state that only exists in
        # ${INSTALL_DIR} (the venv, .env files, the password reset report) must be excluded.
        # Patterns are anchored to the transfer root: unanchored names also match deep inside
        # the venv and leave half-deleted directories behind.
        rsync -a --delete \
            --exclude='/.git' \
            --exclude='/frontend/node_modules' \
            --exclude='/venv' \
            --exclude='/.venv' \
            --exclude='/backend/venv' \
            --exclude='/backend/.env' \
            --exclude='/.env' \
            --exclude='/legacy_password_resets.txt' \
            --exclude='/tests' \
            --exclude='/pytest.ini' \
            --exclude='/scratch' \
            --exclude='/certs' \
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
    if [[ ${LEGACY_NETMON} -eq 1 ]]; then
        # The ephemres units start /opt/ephemres/venv; the old /opt/netmon/venv stays for a rollback.
        VENV_PATH="/opt/ephemres/venv"
    elif [[ -d "/opt/ephemres/venv" ]]; then
        VENV_PATH="/opt/ephemres/venv"
    elif [[ -d "/opt/netmon/venv" ]]; then
        VENV_PATH="/opt/netmon/venv"
    elif [[ -d "${INSTALL_DIR}/backend/venv" ]]; then
        VENV_PATH="${INSTALL_DIR}/backend/venv"
    elif [[ -d "${PROJECT_ROOT}/.venv" ]]; then
        VENV_PATH="${PROJECT_ROOT}/.venv"
    fi

    # Upgrades before v3.3.2v synced with --delete and emptied /opt/ephemres/venv, leaving a
    # directory without bin/python3 or bin/pip. Rebuild it instead of failing with services stopped.
    if [[ "${VENV_PATH}" == "/opt/ephemres/venv" ]] && [[ ! -x "${VENV_PATH}/bin/python3" || ! -x "${VENV_PATH}/bin/pip" ]]; then
        VENV_PYTHON=""
        # Python 3.11 is the floor: app.config imports the stdlib tomllib module.
        for ver in 3.14 3.13 3.12 3.11; do
            if command -v "python${ver}" &>/dev/null; then
                VENV_PYTHON="python${ver}"
                break
            fi
        done
        if [[ -z "${VENV_PYTHON}" ]]; then
            echo -e "${RED}[ERROR] ${VENV_PATH} is incomplete and no Python 3.11+ interpreter was found to rebuild it.${NC}" >&2
            exit 1
        fi
        if [[ ${LEGACY_NETMON} -eq 1 ]]; then
            echo -e "${GREEN}[INFO] Creating ${VENV_PATH} with ${VENV_PYTHON} for the ephemres units...${NC}"
        else
            echo -e "${YELLOW}[WARN] ${VENV_PATH} is missing its interpreter or pip; rebuilding it with ${VENV_PYTHON}...${NC}"
        fi
        "${VENV_PYTHON}" -m venv --clear "${VENV_PATH}"
        chown -R ephemres:ephemres "${VENV_PATH}"
    fi

    if [[ -n "${VENV_PATH}" && -f "${INSTALL_DIR}/backend/requirements.txt" ]]; then
        echo -e "${GREEN}[INFO] Upgrading Python dependencies in ${VENV_PATH}...${NC}"
        "${VENV_PATH}/bin/pip" install --upgrade pip
        "${VENV_PATH}/bin/pip" install -r "${INSTALL_DIR}/backend/requirements.txt" --upgrade

        # Earlier releases ran `setcap cap_net_raw+ep` on the resolved venv interpreter, which is the
        # system-wide Python. The units now grant CAP_NET_RAW via AmbientCapabilities, so remove that
        # host-wide file capability, but only if it is exactly the one the installer set.
        PY3_REAL="$(readlink -f "${VENV_PATH}/bin/python3" 2>/dev/null || true)"
        if [[ -n "${PY3_REAL}" ]] && [[ -f "${PY3_REAL}" ]] && command -v getcap &>/dev/null && command -v setcap &>/dev/null; then
            PY3_CAPS="$(getcap "${PY3_REAL}" 2>/dev/null | awk '{print $NF}')"
            # libcap >= 2.48 prints "cap_net_raw=ep"; older versions print "= cap_net_raw+ep".
            if [[ "${PY3_CAPS}" == "cap_net_raw=ep" || "${PY3_CAPS}" == "cap_net_raw+ep" ]]; then
                echo -e "${GREEN}[INFO] Removing legacy host-wide cap_net_raw file capability from ${PY3_REAL}...${NC}"
                setcap -r "${PY3_REAL}" || true
            elif [[ -n "${PY3_CAPS}" ]]; then
                echo -e "${YELLOW}[WARN] ${PY3_REAL} has file capabilities (${PY3_CAPS}) not set by Ephemres; leaving them unchanged.${NC}"
            fi
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
            if ! PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.seed_admin; then
                echo -e "${RED}[ERROR] Default admin seeding failed.${NC}" >&2
                exit 1
            fi

            echo -e "${GREEN}[INFO] Checking and resetting legacy password hashes...${NC}"
            RESET_REPORT="/opt/ephemres/legacy_password_resets.txt"
            PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.reset_legacy_passwords
            if [[ -s "${RESET_REPORT}" ]]; then
                RESET_COUNT=$(wc -l < "${RESET_REPORT}")
                echo -e "${YELLOW}========================================================================${NC}"
                echo -e "${YELLOW}[NOTICE] ${RESET_COUNT} users had legacy password hashes and were assigned temporary passwords.${NC}"
                echo -e "${YELLOW}See ${RESET_REPORT} (root-only). Distribute and delete it.${NC}"
                echo -e "${YELLOW}========================================================================${NC}"
            fi
        fi

        # TimescaleDB Hypertable Decompression Safety & Legacy Row Sanitization
        echo -e "${GREEN}[INFO] Applying TimescaleDB hypertable decompression safety configuration...${NC}"
        if command -v psql &>/dev/null; then
            mkdir -p "$(dirname "${UPGRADE_LOG}")" || true
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
            END \$\$;" 2>>"${UPGRADE_LOG}" || true

            PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c \
                "ALTER DATABASE \"${DB_NAME}\" SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0;" 2>>"${UPGRADE_LOG}" || true
        fi
    fi
else
    echo -e "[DRY-RUN] Would upgrade pip dependencies, compile Vue 3 assets, execute app.core.migrator upgrade head, and apply TimescaleDB decompression tuning"
fi

# upgrade_failed <reason>: the database is already migrated at this point, so say exactly that,
# where the backup is, and how to look at the services, then stop with a non-zero exit.
upgrade_failed() {
    echo -e "${RED}========================================================================${NC}" >&2
    echo -e "${RED}[ERROR] UPGRADE INCOMPLETE: $1${NC}" >&2
    echo -e "${RED}The database schema is already upgraded. Pre-upgrade backup: ${BACKUP_FILE}${NC}" >&2
    echo -e "${YELLOW}Inspect: systemctl status ephemres-api ephemres-engine; journalctl -u ephemres-api -u ephemres-engine -n 50${NC}" >&2
    echo -e "${RED}========================================================================${NC}" >&2
    exit 1
}

# verify_services_running [extra units...]: ephemres-api, ephemres-engine and any extra units
# must be active and the API must answer /api/v1/health within about 60 seconds.
verify_services_running() {
    local attempt unit health_ok=0
    for attempt in $(seq 1 30); do
        sleep 2
        if systemctl is-active --quiet ephemres-api && systemctl is-active --quiet ephemres-engine; then
            if ! command -v curl &>/dev/null; then
                health_ok=1
                break
            fi
            if curl -fsS --max-time 3 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
                health_ok=1
                break
            fi
        fi
    done
    for unit in ephemres-api ephemres-engine "$@"; do
        if ! systemctl is-active --quiet "${unit}"; then
            upgrade_failed "${unit} is not running after the restart."
        fi
    done
    if [[ ${health_ok} -ne 1 ]]; then
        upgrade_failed "ephemres-api is running but http://127.0.0.1:8000/api/v1/health did not answer."
    fi
}

# migrate_legacy_services: retire the netmon-* units (stopped and disabled; the files stay so a
# rollback with the pre-upgrade backup is possible) and point the existing netmon nginx site at /opt/ephemres. The site keeps its own
# certificates and any local edits; only the /opt/netmon/noop paths change.
migrate_legacy_services() {
    echo -e "${GREEN}[INFO] Disabling legacy netmon-api, netmon-engine and netmon-flowd units...${NC}"
    systemctl stop netmon-api netmon-engine netmon-flowd 2>/dev/null || true
    systemctl disable netmon-api netmon-engine netmon-flowd 2>/dev/null || true

    local site="/etc/nginx/sites-available/netmon"
    if [[ -f "${site}" ]] && grep -q "/opt/netmon/noop" "${site}"; then
        cp -p "${site}" "${site}.pre-ephemres"
        sed -i 's#/opt/netmon/noop#/opt/ephemres#g' "${site}"
        if command -v nginx &>/dev/null && ! nginx -t 2>>"${UPGRADE_LOG}"; then
            cp -p "${site}.pre-ephemres" "${site}"
            upgrade_failed "nginx rejected ${site} after pointing it at /opt/ephemres (restored the original; see ${UPGRADE_LOG})."
        fi
        echo -e "${GREEN}[INFO] Pointed nginx site ${site} at /opt/ephemres (original saved as ${site}.pre-ephemres).${NC}"
    fi

    # v3.2/v3.3.0 collected flows whenever netmon-flowd was enabled; v3.3.5 starts ephemres-flowd
    # only when flow ingestion is switched on, which also needs FastStore performance mode. A server
    # that was receiving flows keeps collecting them: set both, as the Settings page would.
    if [[ ${LEGACY_FLOWD_ENABLED} -eq 1 ]]; then
        local recent_flows
        recent_flows=$(PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -tAc \
            "SELECT EXISTS (SELECT 1 FROM flow_minute_rollups WHERE bucket > now() - INTERVAL '7 days')
                 OR EXISTS (SELECT 1 FROM flow_interface_minute_rollups WHERE bucket > now() - INTERVAL '7 days');" \
            2>>"${UPGRADE_LOG}" || true)
        if [[ "${recent_flows}" == "t" ]]; then
            if PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 -qc \
                "INSERT INTO app_settings (setting_key, setting_value) VALUES ('performance_mode', 'true'), ('flow_ingestion_enabled', 'true')
                 ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = now();" \
                >/dev/null 2>>"${UPGRADE_LOG}"; then
                echo -e "${GREEN}[INFO] netmon-flowd was receiving flows: flow ingestion (with FastStore performance mode) stays on under ephemres-flowd.${NC}"
            else
                echo -e "${YELLOW}[WARN] netmon-flowd was receiving flows, but flow ingestion could not be switched on (see ${UPGRADE_LOG}). Enable FastStore and Flow Ingestion in Settings to resume collection.${NC}"
            fi
        else
            echo -e "${YELLOW}[INFO] netmon-flowd was enabled but received no flows in the last 7 days: flow ingestion stays off (enable it in Settings when needed).${NC}"
        fi
    fi

    # install.sh keeps a copy in backend/.env (rsync never touches it); keep a migrated install
    # identical so tools run by hand from /opt/ephemres/backend see the same settings.
    if [[ ! -f "${INSTALL_DIR}/backend/.env" ]]; then
        cp /etc/ephemres/ephemres.env "${INSTALL_DIR}/backend/.env"
        chmod 0600 "${INSTALL_DIR}/backend/.env"
        chown ephemres:ephemres "${INSTALL_DIR}/backend/.env"
    fi

    if [[ -f "${INSTALL_DIR}/deploy/logrotate.conf" && ! -f /etc/logrotate.d/ephemres && -d /etc/logrotate.d ]]; then
        cp "${INSTALL_DIR}/deploy/logrotate.conf" /etc/logrotate.d/ephemres
    fi
}

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

    if [[ ${LEGACY_NETMON} -eq 1 ]]; then
        migrate_legacy_services
    fi

    echo -e "${GREEN}[INFO] Reloading systemd daemons and enabling auto-start on boot...${NC}"
    systemctl daemon-reload
    systemctl enable ephemres-api ephemres-engine || true
    if ! systemctl restart ephemres-api ephemres-engine; then
        upgrade_failed "systemctl could not start ephemres-api / ephemres-engine."
    fi
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

    if [[ "${FLOW_ENABLED}" == "true" ]]; then
        verify_services_running ephemres-flowd
    else
        verify_services_running
    fi
    echo -e "${GREEN}[SUCCESS] Primary platform services (ephemres-api, ephemres-engine) are active, enabled on boot, and the API answers its health check.${NC}"
else
    echo -e "[DRY-RUN] Would run: systemctl enable & restart ephemres-api ephemres-engine (and ephemres-flowd if enabled)"
fi

echo -e "\n${GREEN}========================================================================${NC}"
echo -e "${GREEN}   [UPGRADE COMPLETE] Ephemres v3.3.5 Platform upgraded successfully!     ${NC}"
echo -e "${GREEN}   Pre-Upgrade Database Backup Saved At: ${BACKUP_FILE}${NC}"
echo -e "${GREEN}========================================================================${NC}"
if [[ ${LEGACY_NETMON} -eq 1 ]]; then
    echo -e "${YELLOW}[NOTICE] This install was migrated from the netmon layout. The old /opt/netmon, /etc/netmon and${NC}"
    echo -e "${YELLOW}         netmon-* units (now disabled) were kept: with the pre-upgrade backup above they allow a${NC}"
    echo -e "${YELLOW}         rollback (the database itself is migrated). Remove them once you are satisfied.${NC}"
    echo -e "${YELLOW}[NOTICE] Passwords from v3.2/v3.3.0 cannot be carried over: users were given temporary passwords${NC}"
    echo -e "${YELLOW}         (see the seeding output above and /opt/ephemres/legacy_password_resets.txt).${NC}"
fi
