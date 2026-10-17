# LNMP User & Operator Guide — Version 3.1.35s

Welcome to the LNMP Network Monitoring Platform v3.1.35s user guide. This document explains how to navigate the web dashboard, use the interactive topology visualizer, configure multi-channel enterprise alert notifications, use the CSV report customizer, interpret multi-protocol probes, and manage system settings.

---

## 1. Authentication & Session Security

LNMP v3.1.35s provides enterprise-grade session protection:
* **Session Deduplication & Eviction Resilience:** Enforces atomic session registration and stateless JWT signing, ensuring concurrent administrator sessions are not prematurely evicted under multi-session limits (`max_active_sessions_per_user`).
* **Browser Password Autofill:** The login page supports native browser credential managers (Chrome, Edge, Safari, Firefox, Bitwarden, 1Password) for 1-click authentication.
* **Sliding 2-Hour Inactivity Timeout:** Sessions slide forward on active requests. If idle for 120 minutes, sessions expire automatically.
* **Concurrent Device Quotas:** Accounts are allowed up to 2 active sessions (managed via FIFO rotation).
* **IP-Scoped Lockouts:** Brute-force protection isolates failed attempts by `<Client_IP>:<Username>`, ensuring legitimate users on other networks are never locked out.
* **Forced Initial Password Reset:** First-time logins require setting a secure replacement password before platform access is granted.

---

## 2. Real-Time Dashboard Overview (`/`)

The v3.1.35s dashboard provides instantaneous fleet telemetry:

### Global Network Health KPI Strip
* **Summary Ribbon:** Displays total monitored devices, count of `🟢 UP`, `🟡 UNSTABLE`, `🔴 DOWN` devices, and the aggregate **Fleet SLA %**.
* **Interactive Filter Pills:** Clicking any state card (e.g. `🔴 DOWN`) instantly filters the endpoint list without triggering a page reload.

### Dual View Switcher
* **Visual Card Grid:** Rich visual cards showcasing live status badges, latency indicators, packet loss bars, and quick diagnostic links.
* **Dense Sortable Data Table:** High-density tabular view displaying Hostname, IP, Detailed Operational State, Average Latency, Packet Loss %, and 30-Day SLA Uptime %. Columns are sortable for rapid fleet triage.

### Real-Time Telemetry Stream
* Connected directly to Server-Sent Events (SSE). An indicator badge in the top right confirms connection status (`🟢 Live SSE` / `🟡 Reconnecting...`).

---

## 3. Interactive Topology Map (`/topology`)

Visualizes the parent-child network hierarchy with crossing-free routing:

### Features & Controls
* **Layout Switcher:** Toggle between **Vertical View (Top-to-Bottom)** and **Horizontal View (Left-to-Right)**.
* **Frozen-Physics Real-Time Recoloring:** State changes update node colors in real time via SSE without recalculating coordinates or causing canvas movement.
* **Dynamic Legend Badges:** Glowing count badges in the legend show live counts of Root, UP, UNSTABLE, DOWN, and Transit nodes.
* **Topological Root Cause Analysis (RCA):**
  - When an upstream transit gateway fails, dependent nodes are automatically classified as `INFERRED_DOWN`, preventing alert fatigue.

---

## 4. Endpoint Details & Synthetic Probes (`/endpoints/:id`)

Clicking any endpoint opens its diagnostic view:
* **24-Hour Telemetry Graphs:** Interactive latency and packet loss curves with TimescaleDB continuous aggregate baselines.
* **Multi-Protocol Synthetic Metrics:** View TCP connect latency, HTTP response codes, and SSL certificate expiration remaining days.
* **High-Fidelity Traceroutes:** View hop-by-hop latency breakdowns and identify failure transit boundaries.

---

## 5. Admin Settings Console (`/settings`)

Administrators can configure platform behavior across four dedicated tabs:
1. **🔔 Alert Channels:** Master alerting engine on/off switch, alert channel inventory, live diagnostic test triggers, and historical delivery audit logs.
2. **⚡ Performance & Storage:** Storage driver selector (PostgreSQL-Native vs. Redis Acceleration) and dynamic concurrency metrics.
3. **🛡️ Security & Discovery:** Layer-2 subnet auto-bypass toggle, sliding session timeouts, and IP lockout thresholds.
4. **👥 User Governance:** User creation, role management (`ADMIN` / `VIEWER`), and credential reset actions.

---

## 6. Configuring Enterprise Alert Channels

LNMP v3.1.35s includes an asynchronous, non-blocking notification dispatcher supporting major collaboration platforms and corporate email.

### Microsoft Teams Workflows Setup
Microsoft Teams supports two integration patterns:
1. **Flow Bot Adaptive Cards (Recommended)**:
   - In Microsoft Teams, navigate to your channel and choose **Workflows** -> **Post to a channel when a webhook request is received**.
   - Copy the generated Webhook URL into LNMP under **Alert Channels** -> **Add Channel** (Provider: `Microsoft Teams`).
   - Set Card Style to `Adaptive Card (v1.4)`. The alert will render rich facts, status colors, and incident metrics.
2. **"Post as User" Fallback (For Restricted Tenants)**:
   - If your Microsoft 365 tenant administrator has disabled bots (`BotDisabledByAdmin`), configure the workflow to "Post as User" instead of "Flow Bot".
   - In LNMP, select **HTML (Simple Webhook)** card style. The dispatcher will transmit a pre-rendered HTML payload compatible with standard user webhooks.

### Discord Webhooks Setup
1. In Discord, navigate to **Server Settings** -> **Integrations** -> **Webhooks** -> **New Webhook**.
2. Select your channel and click **Copy Webhook URL**.
3. In LNMP, select provider `Discord`, paste the URL, and select desired severities (`DOWN`, `UNSTABLE`). Notifications render with color-coded sidebars.

### Slack Block Kit Setup
1. In Slack, create an **Incoming Webhook** in your Slack App administration console.
2. Paste the `https://hooks.slack.com/services/...` URL into LNMP.
3. Select provider `Slack`. Alerts render structured Block Kit sections with device status and telemetry.

### Direct Hardened SMTP Email Setup
LNMP connects directly to external mail servers or internal relays without requiring a local postfix/sendmail agent:
- **Host & Port:** Cloud relays (`smtp.office365.com`, `smtp.gmail.com` on Port `587`) or on-premise internal relays (`localhost`, `127.0.0.1`, RFC 1918 subnets on Port `25`/`587`).
- **Security:** Requires STARTTLS or SSL/TLS 1.2+ for remote servers.
- **Credentials:** Username and Application-Specific Password (encrypted at rest via AES-256-GCM).
- **Recipients:** Comma-separated list of operational email addresses.

### Live Diagnostic Validation
Always verify channel reachability before saving:
- In the channel modal, click **Send Diagnostic Test Alert**.
- LNMP immediately dispatches a synthetic test payload and displays the HTTP status code, server round-trip latency (ms), and any diagnostic error feedback.

---

## 7. Reports & CSV Column Customizer

LNMP v3.1.35s allows operators to tailor exported telemetry CSV files for audits and compliance reviews:

1. Navigate to **Reports & SLA** (`/reports`) and click **Export Telemetry**.
2. The interactive 820px configuration modal allows selecting target endpoints, time range, and metric columns:
   - **Locked Columns (Always Included):** `Hostname` and `IP Address` are non-negotiable device identity columns and cannot be deselected.
   - **Toggleable Metrics:** `Endpoint ID`, `Device Type`, `Timestamp`, `Operational State`, `Detailed State`, `Packet Success Rate / Loss %`, and `Avg Latency (RTT ms)`.
   - **Quick Actions:** Use **Select All** for a complete data dump, or **Reset to Standard** to restore default operational fields.
3. Click **Download CSV Stream** to initiate key-set paginated, streaming export protected against CSV formula injection.
