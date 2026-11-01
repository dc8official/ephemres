#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ephemres Network Monitoring Platform - Audit Log Correction (optional)
# Usage: sudo /opt/ephemres/deploy/repair-audit-history.sh [--apply] [--since YYYY-MM-DD]
#
# Before v3.3.5, each closed period in the audit log kept the health and latency of its
# last minute instead of the whole period. This corrects those figures. Without --apply it
# only reports what would change. See /opt/ephemres/docs/troubleshooting.md,
# "Audit log health and latency from before v3.3.5".
# ============================================================

if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root."
    exit 1
fi

ENV_FILE=""
for candidate in "/etc/ephemres/ephemres.env" "/opt/ephemres/backend/.env" "/opt/ephemres/.env"; do
    if [ -f "$candidate" ]; then
        ENV_FILE="$candidate"
        break
    fi
done

if [ -z "$ENV_FILE" ]; then
    echo "Error: Ephemres configuration file not found (/etc/ephemres/ephemres.env, /opt/ephemres/backend/.env, /opt/ephemres/.env)."
    echo "Run this on a server where Ephemres v3.3.5 or later is installed."
    exit 1
fi

DB_PASS=$(grep -E '^EPHEMRES_DB_PASSWORD' "$ENV_FILE" | head -n1 | cut -d'=' -f2- || true)
SECRET_KEY=$(grep -E '^EPHEMRES_SECRET_KEY' "$ENV_FILE" | head -n1 | cut -d'=' -f2- || true)

APP_DIR="/opt/ephemres"
PYTHON_BIN="${APP_DIR}/venv/bin/python3"

sudo -u ephemres \
    EPHEMRES_DB_PASSWORD="$DB_PASS" \
    EPHEMRES_SECRET_KEY="$SECRET_KEY" \
    PYTHONPATH="$APP_DIR/backend:$APP_DIR" \
    "$PYTHON_BIN" -m monitoring.repair_event_stats "$@"
