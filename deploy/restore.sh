#!/usr/bin/env bash
# ==============================================================================
# Ephemres Network Monitoring Platform — Database Backup Restore Utility
# Cross-Version Restoration Engine (v3.1.35s, v3.3.0, v3.3.1n -> v3.3.2v+)
# ==============================================================================
set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}==================================================================${NC}"
echo -e "${CYAN} Ephemres Enterprise Database Backup Restoration Utility          ${NC}"
echo -e "${CYAN}==================================================================${NC}"

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: sudo bash deploy/restore.sh [BACKUP_FILE] [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --backup-file <path>     Path to backup .sql or .sql.gz dump"
    echo "  --admin-password <pass>  New admin password, forced change at first login (default: keep backed-up password)"
    echo "  --old-secret-key <key>   Previous SECRET_KEY for re-encrypting alert channels (default: <backup>.key)"
    echo "  --skip-telemetry         Skip restoring hypertable metrics for fast config restore"
    echo "  --allow-incomplete       Restore a backup that pg_dump did not finish (missing tables stay empty)"
    echo "  --dry-run                Simulate restoration without writing changes to database"
    echo "  -h, --help               Show this help message"
    exit 0
fi

# Ensure execution as root or with sudo
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR] This script must be run as root or via sudo.${NC}" >&2
    exit 1
fi

INSTALL_DIR="/opt/ephemres"
BACKUP_FILE=""
ADMIN_PASS="${EPHEMRES_RESTORE_ADMIN_PASSWORD:-}"
OLD_SECRET_KEY="${OLD_SECRET_KEY:-}"
SKIP_TELEMETRY=""
ALLOW_INCOMPLETE=""
DRY_RUN=""

# Parse command line flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backup-file)
            BACKUP_FILE="$2"
            shift 2
            ;;
        --admin-password)
            ADMIN_PASS="$2"
            shift 2
            ;;
        --old-secret-key)
            OLD_SECRET_KEY="$2"
            shift 2
            ;;
        --skip-telemetry)
            SKIP_TELEMETRY="--skip-telemetry"
            shift
            ;;
        --allow-incomplete)
            ALLOW_INCOMPLETE="--allow-incomplete"
            shift
            ;;
        --dry-run)
            DRY_RUN="--dry-run"
            shift
            ;;
        -h|--help)
            echo "Usage: sudo bash deploy/restore.sh [BACKUP_FILE] [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --backup-file <path>     Path to backup .sql or .sql.gz dump"
            echo "  --admin-password <pass>  New admin password, forced change at first login (default: keep backed-up password)"
            echo "  --old-secret-key <key>   Previous SECRET_KEY for re-encrypting alert channels (default: <backup>.key)"
            echo "  --skip-telemetry         Skip restoring hypertable metrics for fast config restore"
            echo "  --allow-incomplete       Restore a backup that pg_dump did not finish (missing tables stay empty)"
            echo "  --dry-run                Simulate restoration without writing changes to database"
            echo "  -h, --help               Show this help message"
            exit 0
            ;;
        *)
            if [[ -z "${BACKUP_FILE}" && ! "$1" =~ ^-- ]]; then
                BACKUP_FILE="$1"
                shift
            else
                echo -e "${RED}[ERROR] Unknown argument: $1${NC}" >&2
                exit 1
            fi
            ;;
    esac
done

# 1. Discover or Prompt for Backup File
if [[ -z "${BACKUP_FILE}" ]]; then
    echo -e "\n${BLUE}[INFO] Scanning system for existing backups...${NC}"
    DISCOVERED=()
    for search_dir in /var/backups/ephemres /var/backups/netmon /root; do
        if [[ -d "${search_dir}" ]]; then
            while IFS= read -r -d $'\0' file; do
                DISCOVERED+=("$file")
            done < <(find "${search_dir}" -maxdepth 2 \( -name "*.sql" -o -name "*.sql.gz" \) -print0 2>/dev/null)
        fi
    done

    if [[ ${#DISCOVERED[@]} -eq 1 ]]; then
        echo -e "${GREEN}Found backup:${NC} ${DISCOVERED[0]}"
        read -r -p "Restore from this backup? [Y/n]: " CONFIRM_RESTORE
        if [[ "${CONFIRM_RESTORE}" =~ ^[Nn] ]]; then
            read -r -p "Enter path to backup file: " BACKUP_FILE
        else
            BACKUP_FILE="${DISCOVERED[0]}"
        fi
    elif [[ ${#DISCOVERED[@]} -gt 1 ]]; then
        echo -e "${YELLOW}Discovered multiple backup archives:${NC}"
        for i in "${!DISCOVERED[@]}"; do
            echo -e "  [$((i + 1))] ${DISCOVERED[$i]}"
        done
        echo -e "  [$(( ${#DISCOVERED[@]} + 1 ))] Specify a custom file path"
        read -r -p "Select a backup [1-$(( ${#DISCOVERED[@]} + 1 ))]: " SELECTION
        if [[ "$SELECTION" -ge 1 && "$SELECTION" -le ${#DISCOVERED[@]} ]]; then
            BACKUP_FILE="${DISCOVERED[$((SELECTION - 1))]}"
        else
            read -r -p "Enter path to backup file: " BACKUP_FILE
        fi
    else
        read -r -p "Enter path to backup file: " BACKUP_FILE
    fi
fi

if [[ ! -f "${BACKUP_FILE}" ]]; then
    echo -e "${RED}[ERROR] Backup file not found: ${BACKUP_FILE}${NC}" >&2
    exit 1
fi
echo -e "${GREEN}[OK] Selected backup archive:${NC} ${BACKUP_FILE}"

# 2. The previous SECRET_KEY (for alert channel secrets) is found by the restore engine itself:
# --old-secret-key / OLD_SECRET_KEY, else <backup>.key saved by uninstall.sh / upgrade.sh, else the
# env files of an earlier netmon install. The current /etc/ephemres/ephemres.env is never a
# candidate: it holds this install's key, which the engine already tries.

# 3. Prompt for Admin Temporary Password
if [[ -z "${ADMIN_PASS}" ]]; then
    read -r -s -p "New admin password (leave blank to keep the password from the backup): " ADMIN_PASS
    echo ""
fi

# Locate Python binary
PYTHON_BIN=""
for candidate in \
    "${INSTALL_DIR}/venv/bin/python" \
    "${INSTALL_DIR}/backend/venv/bin/python" \
    "./backend/venv/bin/python" \
    "$(which python3 2>/dev/null)"; do
    if [[ -x "${candidate}" ]]; then
        PYTHON_BIN="${candidate}"
        break
    fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo -e "${RED}[ERROR] Python runtime environment not found.${NC}" >&2
    exit 1
fi

# 4. Stop Daemons Prior to Live Restore
if [[ -z "${DRY_RUN}" ]]; then
    echo -e "\n${BLUE}[INFO] Pausing platform services for database consistency...${NC}"
    systemctl stop ephemres-api ephemres-engine ephemres-flowd 2>/dev/null || true
fi

# 5. Execute Restore Engine
echo -e "\n${BLUE}[INFO] Executing schema-aware database restoration...${NC}"
# Secrets go to the restore engine through its environment, not argv: argv is world-readable
# in the process list (/proc/<pid>/cmdline), the environment only to root and the same user.
RESTORE_STATUS=0
EPHEMRES_RESTORE_ADMIN_PASSWORD="${ADMIN_PASS}" OLD_SECRET_KEY="${OLD_SECRET_KEY}" \
PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend:.:backend" "${PYTHON_BIN}" -m app.core.restore \
    --backup-file "${BACKUP_FILE}" \
    ${SKIP_TELEMETRY} \
    ${ALLOW_INCOMPLETE} \
    ${DRY_RUN} || RESTORE_STATUS=$?

# 6. Restart Platform Services, also after a failed restore so the platform does not stay down
if [[ -z "${DRY_RUN}" ]]; then
    echo -e "\n${BLUE}[INFO] Clearing stale FastStore shared-memory state and restarting platform services...${NC}"
    systemctl restart ephemres-api ephemres-engine 2>/dev/null || true
    systemctl is-enabled ephemres-flowd &>/dev/null && systemctl restart ephemres-flowd 2>/dev/null || true
fi

if [[ ${RESTORE_STATUS} -ne 0 ]]; then
    echo -e "${RED}==================================================================${NC}"
    echo -e "${RED} Restoration FAILED (see the error above). Services were restarted. ${NC}"
    echo -e "${RED}==================================================================${NC}"
    exit "${RESTORE_STATUS}"
fi

if [[ -z "${DRY_RUN}" ]]; then
    echo -e "${GREEN}==================================================================${NC}"
    echo -e "${GREEN} Restoration Complete! Platform is operational.                   ${NC}"
    echo -e "${GREEN} Admin login: see the restore summary above for the password state. ${NC}"
    echo -e "${GREEN}==================================================================${NC}"
else
    echo -e "${YELLOW}Dry run complete. No database changes were applied.${NC}"
fi
