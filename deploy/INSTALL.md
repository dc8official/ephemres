# Ephemres v3.3.2.v — Installation & Deployment Quick Start

This guide provides direct, step-by-step instructions to install, configure, upgrade, and decommission the **Ephemres Network Monitoring Platform v3.3.2.v**.

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
git clone -b v3.3.2v https://github.com/dc8official/ephemres.git /opt/ephemres
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
