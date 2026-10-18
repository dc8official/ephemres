#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ephemres Network Monitoring Platform v3.3.2.v - Production Installer
# Supports: Debian 12+, Ubuntu 22.04+
# Usage: sudo bash deploy/install.sh [--dry-run]
# ============================================================

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "DRY RUN MODE: No changes will be made."
fi

DB_PASS=""
SECRET_KEY=""
DOMAIN_NAME=""

# Self-locate project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Installation target directory
INSTALL_DIR="/opt/ephemres"
VENV_DIR="/opt/ephemres/venv"
CONFIG_DIR="/etc/ephemres"
LOG_DIR="/var/log/ephemres"
BACKUP_DIR="/var/backups/ephemres"
ENV_FILE="$CONFIG_DIR/ephemres.env"
CONFIG_FILE="$CONFIG_DIR/config.toml"

# ============================================================
print_header() {
    echo ""
    echo "========================================================"
    echo "  $1"
    echo "========================================================"
}

run() {
    # Executes a command or prints it in dry-run mode
    local description="$1"
    shift
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] $description"
        echo "          Command: $*"
    else
        echo "--> $description"
        "$@"
    fi
}

check_root() {
    if [ "$EUID" -ne 0 ] && [ "$DRY_RUN" = false ]; then
        echo "Error: This script must be run as root."
        echo "Usage: sudo bash deploy/install.sh"
        exit 1
    fi
}

# ============================================================
print_header "Step 1: Checking root privileges"
check_root

# ============================================================
print_header "Step 2: Installing prerequisite tools"

PREREQS="curl wget gnupg lsb-release ca-certificates apt-transport-https"
if [ "$DRY_RUN" = false ]; then
    apt-get update -qq
    apt-get install -y $PREREQS
else
    echo "[DRY RUN] Would install prerequisites: $PREREQS"
fi

# ============================================================
print_header "Step 3: Adding required APT repositories"

# NodeSource Node.js 20
if [ ! -f "/etc/apt/sources.list.d/nodesource.list" ]; then
    run "Adding NodeSource Node.js 20 repository" \
        bash -c "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
else
    echo "NodeSource repository already configured."
fi

# PostgreSQL PGDG
if [ ! -f "/etc/apt/sources.list.d/pgdg.list" ]; then
    if [ "$DRY_RUN" = false ]; then
        echo "--> Adding PostgreSQL PGDG repository"
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
            | gpg --dearmor \
            -o /usr/share/keyrings/postgresql-keyring.gpg
        echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] \
https://apt.postgresql.org/pub/repos/apt \
$(lsb_release -cs)-pgdg main" \
            | tee /etc/apt/sources.list.d/pgdg.list
    else
        echo "[DRY RUN] Would add PostgreSQL PGDG repository"
    fi
else
    echo "PostgreSQL PGDG repository already configured."
fi

# TimescaleDB
if [ ! -f "/etc/apt/sources.list.d/timescaledb.list" ]; then
    if [ "$DRY_RUN" = false ]; then
        echo "--> Adding TimescaleDB repository"
        wget -qO - \
            https://packagecloud.io/timescale/timescaledb/gpgkey \
            | gpg --dearmor \
            -o /usr/share/keyrings/timescaledb-keyring.gpg

        OS_ID=$(lsb_release -is | tr '[:upper:]' '[:lower:]')
        OS_CODENAME=$(lsb_release -cs)

        if [ "$OS_ID" = "debian" ]; then
            REPO_PATH="debian"
        else
            REPO_PATH="ubuntu"
        fi

        echo "deb [signed-by=/usr/share/keyrings/timescaledb-keyring.gpg] \
https://packagecloud.io/timescale/timescaledb/${REPO_PATH}/ \
${OS_CODENAME} main" \
            | tee /etc/apt/sources.list.d/timescaledb.list
    else
        echo "[DRY RUN] Would add TimescaleDB repository"
    fi
else
    echo "TimescaleDB repository already configured."
fi

if [ "$DRY_RUN" = false ]; then
    apt-get update -qq
fi

# ============================================================
print_header "Step 4: Installing system packages"

PACKAGES="postgresql-16 postgresql-client-16 \
timescaledb-2-postgresql-16 timescaledb-tools \
python3-venv python3-pip traceroute iputils-tracepath iputils-ping libcap2-bin \
nodejs nginx certbot python3-certbot-nginx"

MISSING=""
for pkg in $PACKAGES; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        MISSING="$MISSING $pkg"
    fi
done

if [ -n "$MISSING" ]; then
    run "Installing packages: $MISSING" apt-get install -y $MISSING
else
    echo "All required packages already installed."
fi

# Enable unprivileged ICMP / raw socket capabilities for traceroute if available
TRACEROUTE_PATH=$(command -v traceroute || true)
if [ -n "$TRACEROUTE_PATH" ]; then
    TRACEROUTE_BIN="$(readlink -f "$TRACEROUTE_PATH" 2>/dev/null || echo "$TRACEROUTE_PATH")"
    if [ -f "$TRACEROUTE_BIN" ] && command -v setcap &>/dev/null; then
        run "Setting network capabilities for traceroute ($TRACEROUTE_BIN)" setcap cap_net_raw+ep "$TRACEROUTE_BIN" || true
    fi
fi

# ============================================================
print_header "Step 5: Detecting Python version"

PYTHON_BIN=""
for ver in 3.13 3.12 3.11 3.10; do
    if command -v "python$ver" &>/dev/null; then
        PYTHON_BIN="python$ver"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Python 3.10 or higher not found."
    echo "Install python3.10, python3.11, or python3.12 before running this script."
    exit 1
fi

echo "Using $PYTHON_BIN ($($PYTHON_BIN --version))"

# ============================================================
print_header "Step 6: Creating system user and directories"

if id "ephemres" &>/dev/null; then
    echo "User 'ephemres' already exists."
else
    run "Creating system user 'ephemres'" \
        useradd -r -s /usr/sbin/nologin ephemres
fi

for dir in /opt/ephemres "$CONFIG_DIR" "$LOG_DIR" "$BACKUP_DIR" /run/ephemres; do
    if [ ! -d "$dir" ]; then
        run "Creating directory $dir" mkdir -p "$dir"
        run "Setting ownership: $dir" chown ephemres:ephemres "$dir"
    else
        echo "Directory $dir already exists."
    fi
done

# ============================================================
print_header "Step 7: Copying project files"

if [ ! -d "$INSTALL_DIR" ]; then
    run "Creating project directory $INSTALL_DIR" \
        mkdir -p "$INSTALL_DIR"
    run "Copying project files to $INSTALL_DIR" \
        rsync -a \
        --exclude='.git' \
        --exclude='frontend/node_modules' \
        --exclude='backend/venv' \
        --exclude='tests' \
        --exclude='pytest.ini' \
        --exclude='scratch' \
        --exclude='.env' \
        --exclude='backend/.env' \
        --exclude='certs' \
        "$PROJECT_ROOT/" "$INSTALL_DIR/"
    run "Setting project ownership" \
        chown -R ephemres:ephemres "$INSTALL_DIR"
else
    echo "Project files already exist at $INSTALL_DIR."
    echo "Updating project files..."
    run "Syncing project files" \
        rsync -a --delete \
        --exclude='.git' \
        --exclude='frontend/node_modules' \
        --exclude='backend/venv' \
        --exclude='tests' \
        --exclude='pytest.ini' \
        --exclude='scratch' \
        --exclude='.env' \
        --exclude='backend/.env' \
        --exclude='certs' \
        "$PROJECT_ROOT/" "$INSTALL_DIR/"
    run "Setting project ownership" \
        chown -R ephemres:ephemres "$INSTALL_DIR"
fi

# Purge any stale bytecode from previous runs to ensure clean imports
find "$INSTALL_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

run "Marking install directory as git safe directory" \
    git config --global --add safe.directory "$INSTALL_DIR"

# ============================================================
print_header "Step 8: Creating Python virtual environment"

if [ ! -d "$VENV_DIR" ]; then
    run "Creating virtual environment with $PYTHON_BIN" \
        $PYTHON_BIN -m venv "$VENV_DIR"
    run "Setting venv ownership" \
        chown -R ephemres:ephemres "$VENV_DIR"
fi

run "Upgrading pip to latest version" \
    sudo -H -u ephemres "$VENV_DIR/bin/pip" install \
    --timeout 120 \
    --upgrade pip

run "Installing Python dependencies" \
    sudo -H -u ephemres "$VENV_DIR/bin/pip" install \
    --timeout 120 \
    --retries 5 \
    -r "$INSTALL_DIR/backend/requirements.txt"

# Set network capabilities on the virtualenv python binary
PY3_TARGET="$(readlink -f "$VENV_DIR/bin/python3" 2>/dev/null || true)"
if [ -n "$PY3_TARGET" ] && [ -f "$PY3_TARGET" ] && command -v setcap &>/dev/null; then
    run "Setting network raw socket capabilities for Python binary ($PY3_TARGET)" \
        setcap cap_net_raw+ep "$PY3_TARGET" || true
fi

# ============================================================
print_header "Step 9: Building Vue frontend"

# Ensure vite executable exists; reinstall if missing or corrupt
if [ ! -x "$INSTALL_DIR/frontend/node_modules/.bin/vite" ]; then
    run "Installing frontend npm dependencies (including devDependencies)" \
        bash -c "cd $INSTALL_DIR/frontend && npm install --include=dev --no-update-notifier"
fi

# Build frontend production bundle
if [ "$DRY_RUN" = false ]; then
    echo "--> Building Vue frontend for production"
    if ! (cd "$INSTALL_DIR/frontend" && npm run build); then
        if [ -f "$INSTALL_DIR/frontend/dist/index.html" ]; then
            echo "--> [WARN] npm run build failed, but pre-built dist/index.html is present. Proceeding with existing bundle."
        else
            echo "Error: Failed to build Vue frontend and no pre-built assets found in $INSTALL_DIR/frontend/dist." >&2
            exit 1
        fi
    fi
else
    echo "[DRY RUN] Would build Vue frontend for production (npm run build)"
fi

run "Setting frontend ownership" \
    chown -R ephemres:ephemres "$INSTALL_DIR/frontend/dist"

# ============================================================
print_header "Step 10: Configuration"

if [ -f "$ENV_FILE" ]; then
    echo "Environment file already exists at $ENV_FILE."
    echo "Skipping configuration prompts."
    source "$ENV_FILE"
    if [ "$DRY_RUN" = false ]; then
        chmod 0600 "$ENV_FILE"
        chown ephemres:ephemres "$ENV_FILE" 2>/dev/null || true
    fi
else
    if [ "$DRY_RUN" = false ]; then
        echo ""
        echo "Configuring Ephemres Platform..."
        echo "These will be stored in $ENV_FILE"
        echo ""

        # Auto-generate DB password
        DB_PASS=$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 16)
        echo "Database password auto-generated."

        # Set default admin password to 'admin'
        ADMIN_PASS="admin"
        echo "Default admin password configured as 'admin'."

        SECRET_KEY=$(openssl rand -hex 32)
        echo "Secret key auto-generated."

        # Auto-detect IP address
        PRIMARY_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || ip route get 1 2>/dev/null | awk '{print $NF;exit}' || echo "localhost")
        echo "Detected primary IP address: $PRIMARY_IP"
        DOMAIN_NAME=$PRIMARY_IP
        echo "Server domain/IP configured to: $DOMAIN_NAME"

        cat > "$ENV_FILE" <<EOF
EPHEMRES_DB_PASSWORD=$DB_PASS
NETMON_DB_PASSWORD=$DB_PASS
EPHEMRES_SECRET_KEY=$SECRET_KEY
NETMON_SECRET_KEY=$SECRET_KEY
DOMAIN_NAME=$DOMAIN_NAME
DEFAULT_ADMIN_PASSWORD=$ADMIN_PASS
EOF
        chmod 0600 "$ENV_FILE"
        chown ephemres:ephemres "$ENV_FILE" 2>/dev/null || true
        # Keep backend/.env in sync for convenience
        cp "$ENV_FILE" "$INSTALL_DIR/backend/.env"
        chmod 0600 "$INSTALL_DIR/backend/.env"
        chown ephemres:ephemres "$INSTALL_DIR/backend/.env"
        echo "Environment file created at $ENV_FILE"
    else
        DB_PASS="dryrun_password"
        ADMIN_PASS="admin"
        SECRET_KEY="dryrun_secret"
        DOMAIN_NAME="monitor.example.com"
        echo "[DRY RUN] Would auto-generate DB password, set admin password to 'admin', generate secret key, and auto-detect IP address"
    fi
fi

# Read values for subsequent steps
if [ "$DRY_RUN" = false ]; then
    DB_PASS=$(grep -E '^(EPHEMRES|NETMON)_DB_PASSWORD' "$ENV_FILE" | head -n1 | cut -d'=' -f2-)
    SECRET_KEY=$(grep -E '^(EPHEMRES|NETMON)_SECRET_KEY' "$ENV_FILE" | head -n1 | cut -d'=' -f2-)
    DOMAIN_NAME=$(grep DOMAIN_NAME "$ENV_FILE" | cut -d'=' -f2-)
    if grep -q DEFAULT_ADMIN_PASSWORD "$ENV_FILE"; then
        ADMIN_PASS=$(grep DEFAULT_ADMIN_PASSWORD "$ENV_FILE" | cut -d'=' -f2-)
    else
        ADMIN_PASS="admin"
    fi
else
    DB_PASS="dryrun_password"
    SECRET_KEY="dryrun_secret"
    DOMAIN_NAME="monitor.example.com"
    ADMIN_PASS="admin"
fi

# ============================================================
print_header "Step 11: Creating configuration file"

if [ ! -f "$CONFIG_FILE" ]; then
    run "Copying config template" \
        cp "$INSTALL_DIR/deploy/config.template.toml" "$CONFIG_FILE"
    run "Setting config ownership" \
        chown root:ephemres "$CONFIG_FILE"
    run "Setting config permissions" \
        chmod 640 "$CONFIG_FILE"
    echo "Config file created at $CONFIG_FILE"
    echo "Review and edit $CONFIG_FILE if needed."
else
    echo "Config file already exists at $CONFIG_FILE"
fi

# ============================================================
print_header "Step 12: Setting up PostgreSQL"

if [ "$DRY_RUN" = false ]; then
    # Tune TimescaleDB
    if command -v timescaledb-tune &>/dev/null; then
        echo "--> Tuning PostgreSQL for TimescaleDB"
        timescaledb-tune --quiet --yes
        systemctl restart postgresql
    fi

    # Create database user
    DB_PASS_ESCAPED=$(echo "$DB_PASS" | sed "s/'/''/g")
    if ! sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='netmon_user'" \
        | grep -q 1; then
        echo "--> Creating PostgreSQL user 'netmon_user'"
        sudo -u postgres psql -c \
            "CREATE USER netmon_user WITH PASSWORD '$DB_PASS_ESCAPED';"
    else
        echo "PostgreSQL user 'netmon_user' already exists."
        # Update password in case it changed
        sudo -u postgres psql -c \
            "ALTER USER netmon_user WITH PASSWORD '$DB_PASS_ESCAPED';"
    fi

    # Create database
    if ! sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_database WHERE datname='netmon'" \
        | grep -q 1; then
        echo "--> Creating database 'netmon'"
        sudo -u postgres psql -c \
            "CREATE DATABASE netmon OWNER netmon_user;"
    else
        echo "Database 'netmon' already exists."
    fi

    # Enable TimescaleDB extension
    echo "--> Enabling TimescaleDB extension"
    sudo -u postgres psql -d netmon -c \
        "CREATE EXTENSION IF NOT EXISTS timescaledb;"
    echo "--> Configuring TimescaleDB decompression safety threshold"
    sudo -u postgres psql -d netmon -c \
        "ALTER DATABASE netmon SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0;" 2>/dev/null || true
else
    echo "[DRY RUN] Would create PostgreSQL user, database, and TimescaleDB extension"
fi

# ============================================================
print_header "Step 13: Running database migrations"

# Purge any stale python bytecode in backend before executing migrations
find "$INSTALL_DIR/backend" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

run "Running native database migrations" \
    sudo -H -u ephemres bash -c "
        export EPHEMRES_DB_PASSWORD='$DB_PASS' &&
        export NETMON_DB_PASSWORD='$DB_PASS' &&
        export EPHEMRES_SECRET_KEY='$SECRET_KEY' &&
        export NETMON_SECRET_KEY='$SECRET_KEY' &&
        export PYTHONPATH='$INSTALL_DIR/backend:$INSTALL_DIR' &&
        cd '$INSTALL_DIR/backend' &&
        "$VENV_DIR/bin/python3" -m app.core.migrator upgrade head
    "

# ============================================================
print_header "Step 14: Creating default admin user"

if [ "$DRY_RUN" = false ]; then
    run "Seeding default admin user" \
        sudo -u ephemres \
            EPHEMRES_DB_PASSWORD="$DB_PASS" \
            NETMON_DB_PASSWORD="$DB_PASS" \
            EPHEMRES_SECRET_KEY="$SECRET_KEY" \
            NETMON_SECRET_KEY="$SECRET_KEY" \
            DEFAULT_ADMIN_PASSWORD="$ADMIN_PASS" \
            PYTHONPATH="$INSTALL_DIR/backend:$INSTALL_DIR" \
            "$VENV_DIR/bin/python3" -m app.seed_admin
else
    echo "[DRY RUN] Would create default admin user (admin / $ADMIN_PASS)"
fi

# ============================================================
print_header "Step 15: Installing systemd services"

for service in ephemres-engine ephemres-api ephemres-flowd; do
    src="$INSTALL_DIR/deploy/${service}.service"
    dst="/etc/systemd/system/${service}.service"
    if [ ! -f "$dst" ] || ! diff -q "$src" "$dst" > /dev/null 2>&1; then
        run "Installing $service" cp "$src" "$dst"
    else
        echo "$service already up to date."
    fi
done

run "Reloading systemd daemon" systemctl daemon-reload
run "Enabling ephemres-engine" systemctl enable ephemres-engine
run "Enabling ephemres-api" systemctl enable ephemres-api

FLOW_ENABLED=false
if grep -A 5 "\[flow\]" "$CONFIG_FILE" 2>/dev/null | grep -q "enabled = true"; then
    FLOW_ENABLED=true
fi

if [ "$FLOW_ENABLED" = true ]; then
    run "Enabling ephemres-flowd" systemctl enable ephemres-flowd
fi

# Grant ephemres user sudo privilege to start/stop ephemres-flowd dynamically from web UI
if [ "$DRY_RUN" = false ] && [ -d "/etc/sudoers.d" ]; then
    cat << 'EOF' > /etc/sudoers.d/ephemres
ephemres ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ephemres-flowd, /usr/bin/systemctl stop ephemres-flowd, /usr/bin/systemctl restart ephemres-flowd, /usr/bin/systemctl is-active ephemres-flowd, /usr/bin/systemctl enable ephemres-flowd, /usr/bin/systemctl disable ephemres-flowd
EOF
    chmod 0440 /etc/sudoers.d/ephemres
fi

# ============================================================
print_header "Step 16: Configuring Nginx"

NGINX_CONF="/etc/nginx/sites-available/ephemres"

if [ "$DRY_RUN" = false ]; then
    CERT_DIR="/etc/ssl/certs"
    KEY_DIR="/etc/ssl/private"
    mkdir -p "$CERT_DIR" "$KEY_DIR"
    
    if [ ! -f "$CERT_DIR/ephemres.crt" ] || [ ! -f "$KEY_DIR/ephemres.key" ]; then
        echo "--> Generating self-signed SSL certificate for $DOMAIN_NAME"
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$KEY_DIR/ephemres.key" \
            -out "$CERT_DIR/ephemres.crt" \
            -subj "/C=US/ST=State/L=City/O=Ephemres/CN=$DOMAIN_NAME" 2>/dev/null
        chmod 600 "$KEY_DIR/ephemres.key"
        chmod 644 "$CERT_DIR/ephemres.crt"
        echo "Self-signed certificate generated successfully."
    else
        echo "SSL certificate and key already exist."
    fi
fi

if [ ! -f "$NGINX_CONF" ]; then
    if [ "$DRY_RUN" = false ]; then
        echo "--> Generating Nginx configuration for $DOMAIN_NAME"
        sed "s/YOUR_DOMAIN/$DOMAIN_NAME/g" \
            "$INSTALL_DIR/deploy/nginx.conf.template" \
            > "$NGINX_CONF"
        ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/ephemres
        rm -f /etc/nginx/sites-enabled/netmon
        nginx -t && systemctl reload nginx
        echo "Nginx configuration applied."
    else
        echo "[DRY RUN] Would generate Nginx config for $DOMAIN_NAME"
    fi
else
    echo "Nginx configuration already exists."
fi

# ============================================================
print_header "Step 17: Configuring logrotate"

LOGROTATE_DST="/etc/logrotate.d/ephemres"
if [ ! -f "$LOGROTATE_DST" ]; then
    run "Installing logrotate config" \
        cp "$INSTALL_DIR/deploy/logrotate.conf" "$LOGROTATE_DST"
else
    echo "Logrotate config already exists."
fi

# ============================================================
print_header "Step 18: Starting services"

run "Starting ephemres-api" systemctl start ephemres-api
run "Starting ephemres-engine" systemctl start ephemres-engine

if [ "$FLOW_ENABLED" = true ]; then
    run "Starting ephemres-flowd" systemctl start ephemres-flowd
else
    echo "Flow telemetry is disabled in settings. ephemres-flowd service installed and ready for UI activation."
fi

# ============================================================
print_header "Installation Complete"

if [ "$DRY_RUN" = true ]; then
    echo "DRY RUN complete. No changes were made."
else
    echo ""
    echo "Ephemres v3.3.2.v is now running."
    echo ""
    echo "  Dashboard:  https://$DOMAIN_NAME"
    echo "  API docs:   https://$DOMAIN_NAME/api/docs"
    echo ""
    echo "--------------------------------------------------------"
    echo "  DEFAULT ADMIN CREDENTIALS"
    echo "  Username: admin"
    echo "  Password: $ADMIN_PASS"
    echo ""
    echo "  You will be required to change this password"
    echo "  on your first login."
    echo "--------------------------------------------------------"
    echo "  DATABASE CREDENTIALS"
    echo "  Username: netmon_user"
    echo "  Database: netmon"
    echo "  Stored in: $ENV_FILE"
    echo "--------------------------------------------------------"
    echo ""
    echo "  Service status:"
    echo "  systemctl status ephemres-api"
    echo "  systemctl status ephemres-engine"
    echo "  systemctl status ephemres-flowd"
    echo ""
    echo "  Logs:"
    echo "  journalctl -u ephemres-api -f"
    echo "  journalctl -u ephemres-engine -f"
    echo "  journalctl -u ephemres-flowd -f"
    echo ""
fi
echo "========================================================"
