#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ephemres Network Monitoring Platform - Netmon Leftover Cleanup (optional)
# Usage: sudo /opt/ephemres/deploy/cleanup-netmon.sh [--apply] [--remove-backups] [--purge-packages]
#
# upgrade.sh moves a v3.2.0 / v3.3.0 server (the netmon layout) to Ephemres but deletes nothing
# of the old install, so a rollback stays possible. Once the upgraded server has run long enough
# to be trusted, this removes what is left: Redis (v3.3.0 needed it, v3.3.5 doesn't), the
# disabled netmon-* units, the netmon sudo rule and logrotate file, /opt/netmon (old code and
# virtualenv), /etc/netmon (which holds a second copy of the database password and secret
# key), /var/log/netmon, /run/netmon and the netmon user.
#
# Without --apply it only reports. The database is never touched: its name (netmon) and role
# (netmon_user) are still in use. Optional extras:
#   --remove-backups  also delete /var/backups/netmon (the old install's database dumps)
#   --purge-packages  also purge redis-server and the PDF libraries v3.3.0 needed, each only
#                     when apt would remove nothing else with it
#
# After --apply there is no rollback to v3.3.0 on this server; the way back is a fresh v3.3.0
# install restored from the pre-upgrade backup (/var/backups/ephemres).
# ============================================================

# Tests run the script against a fake filesystem under this prefix, with stub commands on PATH.
R="${EPHEMRES_CLEANUP_ROOT:-}"

APPLY=false
REMOVE_BACKUPS=false
PURGE_PACKAGES=false
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        --remove-backups) REMOVE_BACKUPS=true ;;
        --purge-packages) PURGE_PACKAGES=true ;;
        -h|--help)
            sed -n '4,23p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "Error: unknown option '$arg' (use --apply, --remove-backups, --purge-packages)." >&2
            exit 2 ;;
    esac
done

if [[ -z "$R" && ${EUID} -ne 0 ]]; then
    echo "Error: This script must be run as root." >&2
    exit 1
fi
# The prefix only redirects paths; systemctl, userdel and apt-get would still act on the real
# host. So it is for unprivileged test runs only.
if [[ -n "$R" && ${EUID} -eq 0 ]]; then
    echo "Error: EPHEMRES_CLEANUP_ROOT is for tests and is refused as root." >&2
    exit 1
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
# No colour codes when the output goes to a file or a pipe (a saved report stays readable).
if [[ ! -t 1 ]]; then
    GREEN='' YELLOW='' RED='' NC=''
fi

refuse() {
    echo -e "${RED}[REFUSED] $*${NC}" >&2
    echo "Nothing was changed." >&2
    exit 1
}

FOUND=0
# do_or_say "<description>" cmd args...: runs the command with --apply, otherwise says what it
# would do. Every change goes through here, so the report lists exactly what --apply does.
do_or_say() {
    local what="$1"; shift
    FOUND=$((FOUND + 1))
    if $APPLY; then
        echo -e "${GREEN}[REMOVE]${NC} ${what}"
        "$@"
    else
        echo -e "${YELLOW}[WOULD REMOVE]${NC} ${what}"
    fi
}

# The same for a change that removes nothing (not counted as a removal).
change_or_say() {
    local what="$1"; shift
    if $APPLY; then
        echo -e "${GREEN}[CHANGE]${NC} ${what}"
        "$@"
    else
        echo -e "${YELLOW}[WOULD CHANGE]${NC} ${what}"
    fi
}

keep() {
    echo -e "[KEEP] $*"
}

# --- Preconditions: only clean up a migrated install that works without the old one ----------

ENV_FILE="$R/etc/ephemres/ephemres.env"
if [[ ! -f "$ENV_FILE" ]]; then
    refuse "$ENV_FILE is missing: this server has not been migrated to the Ephemres layout. Run upgrade.sh first."
fi
# /etc/netmon/netmon.env may be the only other copy of these; never delete it unless the
# Ephemres env file has them.
for key in EPHEMRES_SECRET_KEY EPHEMRES_DB_PASSWORD; do
    if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
        refuse "$ENV_FILE has no ${key}; /etc/netmon/netmon.env may hold the only copy."
    fi
done

for svc in ephemres-api ephemres-engine; do
    if ! systemctl is-active --quiet "$svc"; then
        refuse "$svc is not running. Clean up only once the upgraded install runs on its own."
    fi
done

for svc in netmon-api netmon-engine netmon-flowd; do
    if systemctl is-active --quiet "$svc" 2>/dev/null || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        refuse "$svc is running or enabled, so this server may have been rolled back to the netmon install."
    fi
done

# Without its own config file the app falls back to /etc/netmon/config.toml (app/config.py), and
# with neither it starts on the development template.
if [[ ! -f "$R/etc/ephemres/config.toml" ]]; then
    refuse "$R/etc/ephemres/config.toml is missing, so Ephemres may be reading /etc/netmon/config.toml."
fi

# Anything still pointing at the old paths would break when they go: the units and their
# drop-ins (an Environment= or ExecStart=), the Ephemres env and config files (a
# *_CONFIG_PATH or log_dir), and the nginx sites nginx loads (a root or certificate path).
# Comments don't count: the migrated env file says which netmon file it came from.
for f in "$R"/etc/systemd/system/ephemres-*.service "$R"/etc/systemd/system/ephemres-*.service.d/*.conf \
         "$R"/etc/ephemres/* "$R"/opt/ephemres/backend/.env \
         "$R"/etc/nginx/sites-enabled/* "$R"/etc/nginx/conf.d/*.conf; do
    [[ -f "$f" ]] || continue
    hit=$(grep -vE '^[[:space:]]*#' "$f" 2>/dev/null | grep -oE '/(opt|etc|var/log)/netmon' | head -n1 || true)
    if [[ -n "$hit" ]]; then
        refuse "$f still refers to ${hit}."
    fi
done

if $APPLY; then
    echo "Removing the netmon install's leftovers."
else
    echo "Report only: nothing will be changed. Run again with --apply to remove what is listed."
fi
echo

# Counts client connections to the local redis-server (see the Redis section below).
redis_clients() {
    local ports paths tcp unix
    ports=$( { ss -Hltnp 2>/dev/null || true; } | awk '/"redis-server"/ { sub(/.*:/, "", $4); print $4 }' | sort -u)
    paths=$( { ss -Hlxp 2>/dev/null || true; } | awk '/"redis-server"/ { print $5 }' | sort -u)
    tcp=$( { ss -Htnp state established 2>/dev/null || true; } \
        | awk '/"redis-server"/ { sub(/.*:/, "", $3); print $3 }' | grep -cxF -f <(printf '%s\n' "$ports" | sed '/^$/d') || true)
    unix=$( { ss -Hxp state established 2>/dev/null || true; } \
        | awk '/"redis-server"/ { print $4 }' | grep -cxF -f <(printf '%s\n' "$paths" | sed '/^$/d') || true)
    echo $(( ${tcp:-0} + ${unix:-0} ))
}

# --- Redis --------------------------------------------------------------------------------------

REDIS_RETIRED=false
if systemctl cat redis-server.service >/dev/null 2>&1; then
    # Another application on this host may use Redis. Ephemres never connects to it, so any
    # client connection belongs to someone else. A client is a connection that arrived on one
    # of Redis's listening TCP ports or its listening unix socket: whatever port or socket the
    # config sets. Redis's own stdout and stderr, streamed to journald over a unix socket, are
    # not. A client that connects only now and then (a cron job) is not seen; the report says so.
    clients=$(redis_clients)
    if [[ "${clients:-0}" -gt 0 ]]; then
        keep "redis-server: ${clients} client connection(s) open, so something on this host still uses it."
    elif systemctl is-enabled --quiet redis-server 2>/dev/null || systemctl is-active --quiet redis-server 2>/dev/null; then
        do_or_say "redis-server: stop it and disable it at boot (Ephemres v3.3.5 does not use Redis; no client is connected now, but check nothing else on this host uses it now and then)" \
            systemctl disable --now redis-server
        REDIS_RETIRED=true
    else
        REDIS_RETIRED=true
    fi
fi

# --- Units, sudo rule, logrotate ----------------------------------------------------------------

UNITS_REMOVED=false
for svc in netmon-api netmon-engine netmon-flowd; do
    unit="$R/etc/systemd/system/${svc}.service"
    if [[ -f "$unit" ]]; then
        do_or_say "$unit" rm -f "$unit"
        UNITS_REMOVED=true
    fi
done
if $UNITS_REMOVED && $APPLY; then
    systemctl daemon-reload
    systemctl reset-failed netmon-api netmon-engine netmon-flowd 2>/dev/null || true
fi

# Lets the netmon user start and stop netmon-flowd without a password; nothing needs it now.
[[ -f "$R/etc/sudoers.d/netmon" ]] && do_or_say "$R/etc/sudoers.d/netmon (sudo rule for the netmon user)" rm -f "$R/etc/sudoers.d/netmon"
[[ -f "$R/etc/logrotate.d/netmon" ]] && do_or_say "$R/etc/logrotate.d/netmon" rm -f "$R/etc/logrotate.d/netmon"

# --- nginx: only files no enabled site uses ------------------------------------------------------

# upgrade.sh either replaced the netmon site (it is kept, unlinked) or edited it in place (it is
# still the live site). Only the unlinked one, and the copy saved before an in-place edit, go.
site_in_use() {
    local target="$1" f
    for f in "$R"/etc/nginx/sites-enabled/*; do
        [[ -e "$f" ]] || continue
        if [[ "$(readlink -f "$f")" == "$(readlink -f "$target")" ]]; then
            return 0
        fi
    done
    return 1
}
for site in "$R/etc/nginx/sites-available/netmon" "$R/etc/nginx/sites-available/netmon.pre-ephemres"; do
    [[ -f "$site" ]] || continue
    if site_in_use "$site"; then
        keep "$site: it is the live nginx site (linked from sites-enabled)."
    else
        do_or_say "$site (unused nginx site)" rm -f "$site"
    fi
done

# --- Directories --------------------------------------------------------------------------------

[[ -d "$R/opt/netmon" ]] && do_or_say "$R/opt/netmon (v3.3.0 code and virtualenv)" rm -rf "$R/opt/netmon"
[[ -d "$R/etc/netmon" ]] && do_or_say "$R/etc/netmon (old config, and a copy of the database password and secret key)" rm -rf "$R/etc/netmon"
[[ -d "$R/var/log/netmon" ]] && do_or_say "$R/var/log/netmon (old logs)" rm -rf "$R/var/log/netmon"
[[ -d "$R/run/netmon" ]] && do_or_say "$R/run/netmon" rm -rf "$R/run/netmon"

if [[ -d "$R/var/backups/netmon" ]]; then
    if $REMOVE_BACKUPS; then
        do_or_say "$R/var/backups/netmon (database dumps taken by the netmon install)" rm -rf "$R/var/backups/netmon"
    else
        keep "$R/var/backups/netmon: old database dumps (add --remove-backups to delete them)."
    fi
fi

# --- The netmon user -----------------------------------------------------------------------------

if getent passwd netmon >/dev/null 2>&1; then
    if pgrep -u netmon >/dev/null 2>&1; then
        keep "user netmon: it still runs processes ($(pgrep -u netmon | tr '\n' ' ' || true))."
    else
        # Kept dumps would otherwise belong to a deleted UID, which a later user could be given.
        if [[ -d "$R/var/backups/netmon" ]] && ! $REMOVE_BACKUPS; then
            change_or_say "$R/var/backups/netmon: owner netmon -> root (the dumps stay)" \
                chown -R root:root "$R/var/backups/netmon"
        fi
        do_or_say "user netmon" userdel netmon
    fi
fi

# --- Packages (only with --purge-packages) -------------------------------------------------------

installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

# What `apt-get purge <pkgs>` would remove, as sorted names.
apt_would_remove() {
    { apt-get -s purge "$@" 2>/dev/null || true; } | awk '$1 == "Purg" || $1 == "Remv" { print $2 }' | sort -u
}

# Purges the installed packages of a group, leaving out any whose removal would take a package
# outside what is being purged (a library something else on the host uses). Repeats until the
# set purges alone, so the report and --apply agree whatever the dependency order.
purge_group() {
    local pkg candidates="" outside changed
    for pkg in "$@"; do
        installed "$pkg" && candidates="${candidates}${pkg} "
    done
    [[ -n "$candidates" ]] || return 0
    changed=true
    while $changed && [[ -n "$candidates" ]]; do
        changed=false
        for pkg in $candidates; do
            # shellcheck disable=SC2086 # $candidates is a space-separated package list
            outside=$(apt_would_remove "$pkg" | grep -vxF -f <(tr ' ' '\n' <<<"$candidates" | sed '/^$/d') | paste -sd' ' || true)
            if [[ -n "$outside" ]]; then
                keep "package $pkg: purging it would also remove ${outside}"
                candidates=$(tr ' ' '\n' <<<"$candidates" | grep -vxF "$pkg" | sed '/^$/d' | tr '\n' ' ' || true)
                changed=true
                break
            fi
        done
    done
    for pkg in $candidates; do
        do_or_say "package $pkg" true
    done
    if $APPLY && [[ -n "$candidates" ]]; then
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq $candidates >/dev/null
    fi
}

# WeasyPrint's libraries; Ephemres v3.3.5 builds PDFs itself.
PDF_LIBS="libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 libharfbuzz0b"
if $PURGE_PACKAGES; then
    if $REDIS_RETIRED; then
        purge_group redis-server redis-tools
    elif installed redis-server; then
        keep "package redis-server: Redis is still in use (see above)."
    fi
    # shellcheck disable=SC2086
    purge_group $PDF_LIBS
else
    leftover=""
    for pkg in redis-server $PDF_LIBS; do
        installed "$pkg" && leftover="${leftover}${pkg} "
    done
    if [[ -n "$leftover" ]]; then
        keep "packages ${leftover}(add --purge-packages to remove the ones nothing else needs)."
    fi
fi

# --- Summary -------------------------------------------------------------------------------------

echo
keep "the database netmon and role netmon_user: Ephemres still uses them."
if [[ -f "$R/opt/ephemres/legacy_password_resets.txt" ]]; then
    echo -e "${YELLOW}[NOTICE] $R/opt/ephemres/legacy_password_resets.txt still holds temporary passwords in plain text.${NC}"
    echo -e "${YELLOW}         Delete it once every user has theirs.${NC}"
fi
if [[ $FOUND -eq 0 ]]; then
    echo "Nothing left to remove."
elif $APPLY; then
    echo -e "${GREEN}Done: ${FOUND} item(s) removed. A rollback to v3.3.0 now needs a fresh v3.3.0 install restored from the pre-upgrade backup.${NC}"
else
    echo "${FOUND} item(s) would be removed. After --apply a rollback to v3.3.0 needs a fresh v3.3.0 install restored from the pre-upgrade backup."
fi
