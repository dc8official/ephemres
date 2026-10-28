# Ephemres v3.3.5 — Installation & Deployment Quick Start

This guide provides direct, step-by-step instructions to install, configure, upgrade, and decommission the **Ephemres Network Monitoring Platform v3.3.5**.

---

## 1. Prerequisites

- **OS:** Ubuntu 22.04 / 24.04 LTS or Debian 12+ (x86_64). The installer uses `apt`; RHEL-family distributions are not supported by `install.sh`.
- **Python:** Python 3.11+. Ubuntu 24.04 (3.12) and Debian 12 (3.11) meet this out of the box; on Ubuntu 22.04 (3.10) install `python3.11` and `python3.11-venv` before running the installer.
- **Privileges:** Root access (`sudo -i`)
- **Hardware:** 1–2 vCPUs, 2 GB RAM, 10–25 GB SSD

---

## 2. Fresh Installation

Elevate to root and run the automated installer:

```bash
sudo -i
git clone -b v3.3.5 https://github.com/dc8official/ephemres.git /opt/ephemres
cd /opt/ephemres/deploy
./install.sh
```

**Post-Install Verification:**
```bash
# Verify systemd services are active and enabled on boot
systemctl status ephemres-api ephemres-engine ephemres-flowd

# Verify API health
curl -s http://127.0.0.1:8000/api/v1/health
```

---

## 3. Zero-Downtime Upgrade

To upgrade an existing installation without data loss:

```bash
sudo bash /opt/ephemres/deploy/upgrade.sh
```
`upgrade.sh` fetches the code itself: run from a git checkout it pulls that checkout (branch from `EPHEMRES_BRANCH`, else the checkout's current branch); run from a copy without `.git` it shallow-clones the release branch from GitHub into a staging directory. No manual `git pull` is needed.

If a service does not come up, the script stops with `UPGRADE INCOMPLETE` and a non-zero exit instead of reporting success. The database is already migrated at that point; the message names the pre-upgrade backup and the `systemctl` / `journalctl` commands to inspect the services.

### From v3.2.0 or v3.3.0 (netmon layout)

These releases installed into `/opt/netmon/noop` with `/opt/netmon/venv`, `/etc/netmon`, user `netmon` and `netmon-*` services, so `/opt/ephemres/deploy/upgrade.sh` does not exist yet. Clone v3.3.5 and run its upgrade script; it moves the install to the Ephemres layout:

```bash
git clone --branch v3.3.5 https://github.com/dc8official/ephemres.git ~/ephemres
sudo bash ~/ephemres/deploy/upgrade.sh
```

The script detects the old layout (`/etc/netmon/netmon.env` and no `/etc/ephemres/ephemres.env`) and, after taking the pre-upgrade backup:

* creates the `ephemres` user, `/etc/ephemres/ephemres.env` (the `NETMON_*` keys written again as `EPHEMRES_*` with the same values, so the database password and the key that encrypts alert channel secrets carry over) and `/etc/ephemres/config.toml` (`log_dir` moved to `/var/log/ephemres`);
* builds a new `/opt/ephemres/venv`, migrates the database (`0012` to `0018`), disables the `netmon-*` units and points the existing `/etc/nginx/sites-available/netmon` site at `/opt/ephemres` (the original is saved as `netmon.pre-ephemres`; certificates and local edits are kept);
* starts `ephemres-api` and `ephemres-engine` and checks that the API answers `/api/v1/health`.

Passwords cannot be carried over: v3.2/v3.3.0 stored argon2 hashes. Every user, the admin included, gets a random temporary password listed in `/opt/ephemres/legacy_password_resets.txt` (root only; the upgrade ends with the command to read the admin's), and everyone must change their password at the next login. The install-time `DEFAULT_ADMIN_PASSWORD` is not reused.

`/opt/netmon`, `/etc/netmon` and the disabled `netmon-*` units are left in place. The database itself is migrated, so a rollback means re-enabling the `netmon-*` units and restoring the pre-upgrade backup; remove the old files once the new install is confirmed. A fresh v3.3.5 install followed by `deploy/restore.sh` with a backup of the old server is a supported alternative.

---

## 4. Decommission / Uninstall

To remove the platform, disable systemd services, and remove Nginx web routing:

```bash
cd /opt/ephemres/deploy
sudo ./uninstall.sh
```

**Complete Data Purge (Clean Slate):**
To also drop the PostgreSQL database (`netmon`), remove the database role (`netmon_user`), and purge all configuration and logs:
```bash
sudo ./uninstall.sh --purge-data
```

---

## 5. Password Reset (CLI)

If you need to reset an admin password from the server console:

```bash
sudo /opt/ephemres/deploy/reset-admin-password.sh <new_password>
```
