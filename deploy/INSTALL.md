# LNMP Version 3.3.0 — Installation & Deployment Quick Start

This guide provides direct, step-by-step instructions to install, configure, upgrade, and decommission the **LNMP Network Monitoring Platform v3.3.0**.

---

## 1. Prerequisites

- **OS:** Ubuntu 22.04 LTS or 24.04 LTS (x86_64)
- **Privileges:** Root access (`sudo -i`)
- **Hardware:** 1–2 vCPUs, 2 GB RAM, 10–25 GB SSD

---

## 2. Fresh Installation

Elevate to root and run the automated installer:

```bash
sudo -i
git clone https://github.com/dc8official/lnmp.git
cd lnmp/deploy
chmod +x *.sh
./install.sh
```

**Post-Install Verification:**
```bash
# Verify systemd services are active and enabled on boot
systemctl status netmon-api netmon-engine netmon-flowd

# Verify API health
curl -s http://127.0.0.1:8000/api/v1/health
```

---

## 3. Zero-Downtime Upgrade

To upgrade an existing installation without data loss:

```bash
# Option A: From your git clone directory
cd ~/lnmp/deploy
sudo ./upgrade.sh

# Option B: Directly inside /opt/netmon/noop
cd /opt/netmon/noop
sudo git pull
sudo ./deploy/upgrade.sh
```

---

## 4. Decommission / Uninstall

To remove the platform, disable systemd services, and remove Nginx web routing:

```bash
cd /opt/netmon/noop/deploy
sudo ./uninstall.sh
```

---

## 5. Password Reset (CLI)

If you need to reset an admin or user password from the server console:

```bash
sudo /opt/netmon/noop/deploy/reset-admin-password.sh <username> <new_password>
```
