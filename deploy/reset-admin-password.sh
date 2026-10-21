#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ephemres Network Monitoring Platform - Admin Password Resetter
# Usage: sudo bash deploy/reset-admin-password.sh [new_password]
# ============================================================

if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root."
    exit 1
fi

ENV_FILE=""
for candidate in "/etc/ephemres/ephemres.env" "/opt/ephemres/backend/.env" "/opt/ephemres/.env" "/etc/netmon/netmon.env"; do
    if [ -f "$candidate" ]; then
        ENV_FILE="$candidate"
        break
    fi
done

if [ -z "$ENV_FILE" ]; then
    echo "Error: Ephemres configuration file not found (/etc/ephemres/ephemres.env, /opt/ephemres/backend/.env, /opt/ephemres/.env)."
    echo "Please ensure the platform is installed before resetting the password."
    exit 1
fi

DB_PASS=$(grep -E '^(EPHEMRES|NETMON)_DB_PASSWORD' "$ENV_FILE" | head -n1 | cut -d'=' -f2- || true)
SECRET_KEY=$(grep -E '^(EPHEMRES|NETMON)_SECRET_KEY' "$ENV_FILE" | head -n1 | cut -d'=' -f2- || true)

NEW_PASS="${1:-}"
if [ -z "$NEW_PASS" ]; then
    read -s -p "Enter new admin password (leave blank to auto-generate): " NEW_PASS
    echo ""
    # Blank input: leave NEW_PASS empty so app.seed_admin generates and prints a random password.
fi

RUN_USER="ephemres"
if ! id "ephemres" &>/dev/null && id "netmon" &>/dev/null; then
    RUN_USER="netmon"
fi

PYTHON_BIN="/opt/ephemres/venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ] && [ -f "/opt/netmon/venv/bin/python3" ]; then
    PYTHON_BIN="/opt/netmon/venv/bin/python3"
fi

APP_DIR="/opt/ephemres"
if [ ! -d "$APP_DIR" ] && [ -d "/opt/netmon/noop" ]; then
    APP_DIR="/opt/netmon/noop"
fi

sudo -u "$RUN_USER" \
    EPHEMRES_DB_PASSWORD="$DB_PASS" \
    NETMON_DB_PASSWORD="$DB_PASS" \
    EPHEMRES_SECRET_KEY="$SECRET_KEY" \
    NETMON_SECRET_KEY="$SECRET_KEY" \
    DEFAULT_ADMIN_PASSWORD="$NEW_PASS" \
    FORCE_RESET_ADMIN="true" \
    PYTHONPATH="$APP_DIR/backend:$APP_DIR" \
    "$PYTHON_BIN" -m app.seed_admin

echo ""
echo "--------------------------------------------------------"
echo "  Updated Admin Credentials"
echo "  Username: admin"
if [ -n "$NEW_PASS" ]; then
    echo "  Password: $NEW_PASS"
else
    echo "  Password: (auto-generated; shown in the [seed] line above)"
fi
echo "  You will be required to change it at first login."
echo "--------------------------------------------------------"
