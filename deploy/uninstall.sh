#!/usr/bin/env bash
# ==============================================================================
# Ephemres Network Monitoring Platform v3.3.2.v - Decommission / Uninstall Utility
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=backup_lib.sh
source "${SCRIPT_DIR}/backup_lib.sh"

# Color Palette
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# Require Root
if [[ ${EUID} -ne 0 ]]; then
    echo -e "${RED}[ERROR] This script must be executed with root privileges (e.g., sudo ./uninstall.sh).${NC}" >&2
    exit 1
fi

echo -e "${RED}========================================================================${NC}"
echo -e "${RED}  Ephemres Network Monitoring Platform v3.3.2.v - Decommission / Uninstall ${NC}"
echo -e "${RED}========================================================================${NC}"
echo -e "${YELLOW}WARNING: This utility will stop and remove all Ephemres system services,${NC}"
echo -e "${YELLOW}disable background monitoring daemons, and remove web server routes.${NC}"
echo ""

# Flags
PURGE_DATA=false
AUTO_CONFIRM=false
FORCE_PURGE=false

for arg in "$@"; do
    case "$arg" in
        --purge-data|--purge-db|--purge|-p)
            PURGE_DATA=true
            ;;
        --yes|-y)
            AUTO_CONFIRM=true
            ;;
        --force-purge)
            PURGE_DATA=true
            FORCE_PURGE=true
            ;;
        --help|-h)
            echo "Usage: sudo ./uninstall.sh [--purge-data] [--yes] [--force-purge]"
            echo "  --purge-data, --purge   Completely drop PostgreSQL database (netmon) and delete all data"
            echo "  --yes, -y               Skip interactive confirmation prompt"
            echo "  --force-purge           Purge even if the safety backup could not be created"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: sudo ./uninstall.sh [--purge-data] [--yes] [--force-purge]"
            exit 1
            ;;
    esac
done

# Safety Confirmation
if [[ "${AUTO_CONFIRM}" != true ]]; then
    read -rp "Are you sure you want to uninstall Ephemres? (Type 'YES' to proceed): " CONFIRM
    if [[ "${CONFIRM}" != "YES" ]]; then
        echo -e "${GREEN}[ABORTED] Uninstallation cancelled. No changes were made.${NC}"
        exit 0
    fi
fi

# Load Database credentials for safety dump if available
ENV_FILE=""
for candidate in "/etc/ephemres/ephemres.env" "/opt/ephemres/backend/.env" "/opt/ephemres/.env" "/etc/netmon/netmon.env"; do
    if [[ -f "${candidate}" ]]; then
        ENV_FILE="${candidate}"
        break
    fi
done

if [[ -n "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

DB_NAME="${EPHEMRES_DB_NAME:-${NETMON_DB_NAME:-${POSTGRES_DB:-netmon}}}"
DB_USER="${EPHEMRES_DB_USER:-${NETMON_DB_USER:-${POSTGRES_USER:-netmon_user}}}"
DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD:-${POSTGRES_PASSWORD:-}}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-${POSTGRES_HOST:-127.0.0.1}}}"
DB_PORT="${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-${POSTGRES_PORT:-5432}}}"

# 1. Final Safety Database Backup
BACKUP_DIR="/var/backups/ephemres"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FINAL_BACKUP="${BACKUP_DIR}/ephemres_decommission_backup_${TIMESTAMP}.sql"
BACKUP_OK=false

echo -e "\n${BLUE}--- Step 1/4: Creating Final Safety Database Backup ---${NC}"
mkdir -p "${BACKUP_DIR}"
if ! command -v pg_dump &>/dev/null; then
    echo -e "${RED}[ERROR] pg_dump not found; no safety backup can be created.${NC}" >&2
else
    echo -e "${GREEN}[INFO] Creating safety dump at ${FINAL_BACKUP}...${NC}"
    if backup_create "${FINAL_BACKUP}" "${DB_NAME}" "${DB_USER}" "${DB_PASS}" "${DB_HOST}" "${DB_PORT}"; then
        BACKUP_OK=true
        echo -e "${GREEN}[SUCCESS] Safety backup saved to ${FINAL_BACKUP}${NC}"
        if backup_save_key "${FINAL_BACKUP}" "/opt/ephemres" "/opt/ephemres/venv/bin/python3"; then
            echo -e "${GREEN}[SUCCESS] Encryption key saved to ${FINAL_BACKUP}.key (restore.sh uses it for alert channel secrets)${NC}"
        else
            echo -e "${YELLOW}[WARN] Could not determine SECRET_KEY; encrypted alert channels will need their secrets re-entered after a restore.${NC}"
        fi
    fi
fi

# Dropping the database without a backup is unrecoverable: stop before changing anything.
if [[ "${BACKUP_OK}" != true && "${PURGE_DATA}" == true && "${FORCE_PURGE}" != true ]]; then
    echo -e "${RED}[ABORTED] The safety backup failed, so the database will not be purged. Nothing was changed.${NC}" >&2
    echo -e "${YELLOW}Fix the error above and re-run, or pass --force-purge to delete the data without a backup.${NC}" >&2
    exit 1
fi

# 2. Stop and Disable Systemd Services
echo -e "\n${BLUE}--- Step 2/4: Stopping & Removing Systemd Services ---${NC}"
systemctl stop ephemres-api ephemres-engine ephemres-flowd netmon-api netmon-engine netmon-flowd 2>/dev/null || true
systemctl disable ephemres-api ephemres-engine ephemres-flowd netmon-api netmon-engine netmon-flowd 2>/dev/null || true

rm -f /etc/systemd/system/ephemres-api.service /etc/systemd/system/ephemres-engine.service /etc/systemd/system/ephemres-flowd.service
rm -f /etc/systemd/system/netmon-api.service /etc/systemd/system/netmon-engine.service /etc/systemd/system/netmon-flowd.service
rm -f /etc/sudoers.d/ephemres /etc/sudoers.d/netmon
systemctl daemon-reload
echo -e "${GREEN}[SUCCESS] Ephemres background services disabled and removed from systemd.${NC}"

# 3. Clean Nginx Configuration
echo -e "\n${BLUE}--- Step 3/4: Removing Web Server Routing ---${NC}"
rm -f /etc/nginx/sites-enabled/ephemres /etc/nginx/sites-enabled/netmon
rm -f /etc/nginx/sites-available/ephemres /etc/nginx/sites-available/netmon
if command -v nginx &>/dev/null; then
    nginx -t 2>/dev/null && systemctl reload nginx || true
fi
echo -e "${GREEN}[SUCCESS] Nginx web routing removed.${NC}"

# 4. Optional Application Directory and Database Cleanup
echo -e "\n${BLUE}--- Step 4/4: Application Directory & Database ---${NC}"

if [[ "${PURGE_DATA}" == false && "${AUTO_CONFIRM}" == false ]]; then
    if [[ "${BACKUP_OK}" != true ]]; then
        echo -e "${YELLOW}[INFO] Keeping the PostgreSQL database: there is no safety backup of it.${NC}"
    else
        read -rp "Do you want to delete the PostgreSQL database and all monitoring data? [y/N]: " PURGE_PROMPT
        if [[ "${PURGE_PROMPT,,}" == "y" || "${PURGE_PROMPT,,}" == "yes" ]]; then
            PURGE_DATA=true
        fi
    fi
fi

if [[ "${PURGE_DATA}" == true ]]; then
    echo -e "${RED}[WARN] Dropping PostgreSQL database '${DB_NAME}' and role '${DB_USER}'...${NC}"
    if command -v psql &>/dev/null; then
        sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" 2>/dev/null || true
        sudo -u postgres psql -c "DROP DATABASE IF EXISTS ${DB_NAME};" 2>/dev/null || true
        sudo -u postgres psql -c "DROP USER IF EXISTS ${DB_USER};" 2>/dev/null || true
        echo -e "${GREEN}[SUCCESS] Database '${DB_NAME}' and user '${DB_USER}' removed.${NC}"
    fi
    rm -rf /opt/ephemres /opt/netmon /etc/ephemres /etc/netmon /run/ephemres /var/log/ephemres
    echo -e "${GREEN}[INFO] Purged application directories, logs, and configuration files.${NC}"
else
    if [[ "${AUTO_CONFIRM}" == false ]]; then
        read -rp "Do you want to delete application files in /opt/ephemres? [y/N]: " REMOVE_FILES
        if [[ "${REMOVE_FILES,,}" == "y" || "${REMOVE_FILES,,}" == "yes" ]]; then
            rm -rf /opt/ephemres /opt/netmon
            echo -e "${GREEN}[INFO] Removed application directories.${NC}"
        fi
    fi
fi

echo -e "\n${GREEN}========================================================================${NC}"
echo -e "${GREEN}   [UNINSTALL COMPLETE] Ephemres v3.3.2.v platform decommissioned.        ${NC}"
if [[ "${BACKUP_OK}" == true ]]; then
    echo -e "${GREEN}   Safety Database Backup Retained At: ${FINAL_BACKUP}${NC}"
    if [[ -f "${FINAL_BACKUP}.key" ]]; then
        echo -e "${GREEN}   Encryption key (keep with the backup):  ${FINAL_BACKUP}.key${NC}"
    fi
else
    echo -e "${YELLOW}   No safety database backup was created.${NC}"
fi
echo -e "${GREEN}========================================================================${NC}"
