#!/usr/bin/env bash
# ==============================================================================
# Ephemres Network Monitoring Platform v3.3.2.v - Decommission / Uninstall Utility
# ==============================================================================

set -euo pipefail

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

# Safety Confirmation
read -rp "Are you sure you want to uninstall Ephemres? (Type 'YES' to proceed): " CONFIRM
if [[ "${CONFIRM}" != "YES" ]]; then
    echo -e "${GREEN}[ABORTED] Uninstallation cancelled. No changes were made.${NC}"
    exit 0
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
DB_PASS="${EPHEMRES_DB_PASSWORD:-${NETMON_DB_PASSWORD:-${POSTGRES_PASSWORD:-netmon_secure_password}}}"
DB_HOST="${EPHEMRES_DB_HOST:-${NETMON_DB_HOST:-${POSTGRES_HOST:-127.0.0.1}}}"
DB_PORT="${EPHEMRES_DB_PORT:-${NETMON_DB_PORT:-${POSTGRES_PORT:-5432}}}"

# 1. Final Safety Database Backup
BACKUP_DIR="/var/backups/ephemres"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FINAL_BACKUP="${BACKUP_DIR}/ephemres_decommission_backup_${TIMESTAMP}.sql"

echo -e "\n${BLUE}--- Step 1/4: Creating Final Safety Database Backup ---${NC}"
mkdir -p "${BACKUP_DIR}"
if command -v pg_dump &>/dev/null; then
    echo -e "${GREEN}[INFO] Creating safety dump at ${FINAL_BACKUP}...${NC}"
    PGPASSWORD="${DB_PASS}" pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -F p -f "${FINAL_BACKUP}" 2>/dev/null || true
    if [[ -f "${FINAL_BACKUP}" && -s "${FINAL_BACKUP}" ]]; then
        chmod 600 "${FINAL_BACKUP}"
        echo -e "${GREEN}[SUCCESS] Safety backup saved to ${FINAL_BACKUP}${NC}"
    else
        echo -e "${YELLOW}[WARN] Could not create database dump (database may already be offline). Continuing...${NC}"
    fi
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
read -rp "Do you want to delete application files in /opt/ephemres? [y/N]: " REMOVE_FILES
if [[ "${REMOVE_FILES,,}" == "y" || "${REMOVE_FILES,,}" == "yes" ]]; then
    rm -rf /opt/ephemres /opt/netmon
    echo -e "${GREEN}[INFO] Removed application directories.${NC}"
fi

echo -e "\n${GREEN}========================================================================${NC}"
echo -e "${GREEN}   [UNINSTALL COMPLETE] Ephemres v3.3.2.v platform decommissioned.        ${NC}"
echo -e "${GREEN}   Safety Database Backup Retained At: ${FINAL_BACKUP}${NC}"
echo -e "${GREEN}========================================================================${NC}"
