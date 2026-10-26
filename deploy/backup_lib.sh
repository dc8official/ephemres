#!/usr/bin/env bash
# ==============================================================================
# Ephemres - shared database backup helpers (sourced by uninstall.sh and upgrade.sh)
# ==============================================================================
# A backup only counts once pg_dump has finished every table, and it is kept together with the
# key that encrypts alert channel secrets: restore.sh needs that key to re-encrypt them once the
# old install (and /etc/ephemres) is gone.

# pg_dump writes this trailer only after the last table; a dump cut short by an error, a killed
# connection or a full disk ends without it.
BACKUP_COMPLETE_MARKER="-- PostgreSQL database dump complete"

# backup_dump_is_complete <file>
backup_dump_is_complete() {
    [[ -s "$1" ]] && tail -n 20 "$1" | grep -qF -- "${BACKUP_COMPLETE_MARKER}"
}

# backup_create <file> <db name> <db user> <db password> <db host> <db port>
# Dumps as the application user, then (as root) as the postgres superuser. <file> only appears
# once a dump is complete; otherwise pg_dump's errors are printed and the function returns 1.
backup_create() {
    local file="$1" name="$2" user="$3" pass="$4" host="$5" port="$6"
    local partial="${file}.partial" errors="${file}.errors"
    rm -f "${partial}" "${errors}"

    # -w: never prompt for a password; a missing or wrong one fails instead of hanging.
    if ! { PGPASSWORD="${pass}" pg_dump -w -h "${host}" -p "${port}" -U "${user}" -d "${name}" -F p \
            -f "${partial}" 2>"${errors}" && backup_dump_is_complete "${partial}"; }; then
        rm -f "${partial}"
        if [[ ${EUID} -eq 0 ]] && command -v sudo &>/dev/null && id -u postgres &>/dev/null; then
            echo "[INFO] Retrying the dump as the postgres superuser..."
            sudo -u postgres pg_dump -d "${name}" -F p 2>>"${errors}" > "${partial}" || true
        fi
    fi

    if ! backup_dump_is_complete "${partial}"; then
        echo "[ERROR] pg_dump did not produce a complete backup of '${name}':" >&2
        if [[ -s "${errors}" ]]; then
            sed 's/^/    /' "${errors}" >&2
        else
            echo "    (no error output; the dump ended without pg_dump's completion trailer)" >&2
        fi
        rm -f "${partial}" "${errors}"
        return 1
    fi

    rm -f "${errors}"
    chmod 600 "${partial}"
    mv -f "${partial}" "${file}"
}

# backup_save_key <backup file> <app dir> <python>
# Writes <backup file>.key (0600) with the effective encryption key: the application's own view
# of it when its Python runs (a config.toml crypto_key wins over SECRET_KEY), else the sourced
# EPHEMRES_SECRET_KEY / NETMON_SECRET_KEY. Returns 1 when no key could be determined.
backup_save_key() {
    local file="$1" app_dir="$2" python_bin="$3" key=""
    if [[ -n "${python_bin}" && -x "${python_bin}" && -d "${app_dir}/backend" ]]; then
        key="$(cd "${app_dir}/backend" && PYTHONPATH="${app_dir}:${app_dir}/backend" "${python_bin}" -c \
            'from app.config import settings; print(settings.security.effective_crypto_key)' 2>/dev/null || true)"
    fi
    key="${key:-${EPHEMRES_SECRET_KEY:-${NETMON_SECRET_KEY:-}}}"
    if [[ -z "${key}" ]]; then
        return 1
    fi
    (umask 077 && printf 'EPHEMRES_SECRET_KEY=%s\n' "${key}" > "${file}.key")
}
