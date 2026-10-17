# Ephemres User & Operator Guide — Version 3.3.2.v

Welcome to the Ephemres Network Monitoring Platform v3.3.2.v user guide. This document explains how to navigate the web dashboard, use the interactive topology visualizer, monitor fleet bandwidth telemetry, configure multi-channel enterprise alert notifications, use the CSV report customizer, interpret multi-protocol probes, and manage system settings.

---

## 1. Authentication & Session Security

Ephemres v3.3.2.v provides enterprise-grade session protection:
* **Browser Password Autofill:** The login page supports native browser credential managers (Chrome, Edge, Safari, Firefox, Bitwarden, 1Password) for 1-click authentication.
* **Sliding 2-Hour Inactivity Timeout:** Sessions slide forward on active requests. If idle for 120 minutes, sessions expire automatically.
* **Concurrent Device Quotas:** Accounts are allowed up to 2 active sessions (managed via FIFO rotation).
* **IP-Scoped Lockouts:** Brute-force protection isolates failed attempts by `<Client_IP>:<Username>`, ensuring legitimate users on other networks are never locked out.
* **Forced Initial Password Reset:** First-time logins require setting a secure replacement password before platform access is granted.

---

## 2. Real-Time Dashboard Overview (`/`)

The v3.3.0 dashboard provides instantaneous fleet telemetry:

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

## 4. Endpoint Details & Perspective Tabs (`/endpoints/:id`)

Clicking any endpoint opens its detailed forensic view with an accessible perspective switcher:
* **Tab 1: Health & Diagnostics:**
  - **24-Hour Telemetry Graphs:** Interactive latency and packet loss curves with TimescaleDB continuous aggregate baselines.
  - **Multi-Protocol Synthetic Metrics:** View TCP connect latency, HTTP response codes, and SSL certificate expiration remaining days.
  - **High-Fidelity Traceroutes:** View hop-by-hop latency breakdowns and identify failure transit boundaries.
* **Tab 2: Flow Telemetry & Bandwidth:**
  - **Endpoint Traffic Volume:** Ingress vs. Egress stacked bandwidth curves specific to this device.
  - **Top Communicating Peers:** Ranks remote internal and external IPs sending or receiving traffic with this endpoint.
  - **Application Breakdown:** Protocol and service port distribution attributed to this endpoint.

---

## 5. Admin Settings Console (`/settings`)

Administrators can configure platform behavior across five dedicated tabs:
1. **🔔 Alert Channels:** Master alerting engine on/off switch, alert channel inventory, live diagnostic test triggers, and historical delivery audit logs.
2. **⚡ Performance & Telemetry:** Storage driver selector (PostgreSQL-Native vs. Redis Acceleration) and dynamic concurrency metrics.
3. **🌊 Flow Telemetry:** Master NetFlow/IPFIX collection toggle, listening ports (2055, 4739), packet sampling multiplier, and live **Redis Pre-Flight Check** validation.
4. **🛡️ Security & Discovery:** Layer-2 subnet auto-bypass toggle, sliding session timeouts, and IP lockout thresholds.
5. **👥 User Governance:** User creation, role management (`ADMIN` / `VIEWER`), and credential reset actions.

---

## 6. Configuring Enterprise Alert Channels

Ephemres v3.3.2.v includes an asynchronous, non-blocking notification dispatcher supporting major collaboration platforms and corporate email.

### Microsoft Teams Workflows Setup
Microsoft Teams supports two integration patterns:
1. **Flow Bot Adaptive Cards (Recommended)**:
   - In Microsoft Teams, navigate to your channel and choose **Workflows** -> **Post to a channel when a webhook request is received**.
   - Copy the generated Webhook URL into Ephemres under **Alert Channels** -> **Add Channel** (Provider: `Microsoft Teams`).
   - Set Card Style to `Adaptive Card (v1.4)`. The alert will render rich facts, status colors, and incident metrics.
2. **"Post as User" Fallback (For Restricted Tenants)**:
   - If your Microsoft 365 tenant administrator has disabled bots (`BotDisabledByAdmin`), configure the workflow to "Post as User" instead of "Flow Bot".
   - In Ephemres, select **HTML (Simple Webhook)** card style. The dispatcher will transmit a pre-rendered HTML payload compatible with standard user webhooks.

### Discord Webhooks Setup
1. In Discord, navigate to **Server Settings** -> **Integrations** -> **Webhooks** -> **New Webhook**.
2. Select your channel and click **Copy Webhook URL**.
3. In Ephemres, select provider `Discord`, paste the URL, and select desired severities (`DOWN`, `UNSTABLE`). Notifications render with color-coded sidebars.

### Slack Block Kit Setup
1. In Slack, create an **Incoming Webhook** in your Slack App administration console.
2. Paste the `https://hooks.slack.com/services/...` URL into Ephemres.
3. Select provider `Slack`. Alerts render structured Block Kit sections with device status and telemetry.

### Direct Hardened SMTP Email Setup
Ephemres connects directly to external mail servers or internal relays without requiring a local postfix/sendmail agent:
- **Host & Port:** Cloud relays (`smtp.office365.com`, `smtp.gmail.com` on Port `587`) or on-premise internal relays (`localhost`, `127.0.0.1`, RFC 1918 subnets on Port `25`/`587`).
- **Security:** Requires STARTTLS or SSL/TLS 1.2+ for remote servers.
- **Credentials:** Username and Application-Specific Password (encrypted at rest via AES-256-GCM).
- **Recipients:** Comma-separated list of operational email addresses.

### Live Diagnostic Validation
Always verify channel reachability before saving:
- In the channel modal, click **Send Diagnostic Test Alert**.
- Ephemres immediately dispatches a synthetic test payload and displays the HTTP status code, server round-trip latency (ms), and any diagnostic error feedback.

---

## 7. Reports, Auditing & Compliance Documentation

Ephemres v3.3.2.v provides enterprise-grade reporting workflows for compliance audits, executive SLA reviews, and capacity planning.

### 7.1 CSV Telemetry Export Stream
Operators and Administrators can export raw or customized time-series telemetry to CSV:
1. Navigate to **Reports & SLA** (`/reports`) and click **Export Telemetry**.
2. The configuration modal allows selecting target endpoints, time range, and metric columns:
   - **Locked Identity Columns:** `Hostname` and `IP Address` are mandatory and automatically preserved.
   - **Toggleable Metrics:** `Endpoint ID`, `Device Type`, `Timestamp`, `Operational State`, `Detailed State`, `Packet Success Rate / Loss %`, and `Avg Latency (RTT ms)`.
   - **Quick Actions:** Use **Select All** for a complete data dump, or **Reset to Standard** to restore default operational fields.
3. Click **Download CSV Stream** to initiate keyset-paginated streaming export protected against CSV formula injection (`=`, `+`, `-`, `@`).

### 7.2 Executive PDF Reports Generation (`/reports`)
Generate print-perfect, air-gapped ISO A4 PDF audit documents signed with cryptographic checksum headers:
1. On the **Reports & SLA** page (`/reports`), click **📄 Generate PDF Report**.
2. Choose one of the three audit templates:
   - **Template A — Availability & SLA Performance:** Compiles objective uptime percentages, cumulative downtime durations, packet loss, average latency (RTT), and recorded outage incident ledger with Root Cause Analysis (RCA).
   - **Template B — Bandwidth & Interface Capacity:** Summarizes exporter routers, per-interface directional bandwidth accounting, line-speed capacity utilization with color-coded warning/critical thresholds, and cross-router forensic top conversations.
   - **Template C — Executive Master Audit Dossier:** Comprehensive 4-part dossier consolidating availability, outage ledgers with RCA, interface capacity metrics, and top transit conversations into a single executive document.
3. **Select Scope:** Choose between **All Monitored Endpoints** or select individual devices via the target checklist.
4. **Select Time Window:** Choose from quick presets (**24 Hours**, **7 Days**, **30 Days**) or configure a **Custom Date Range** with UTC start and end bounds.
5. **Root Cause Analysis (RCA):** Toggle the **Include Root Cause Analysis (RCA)** checkbox to embed diagnostic summaries directly into the outage incident ledger.
6. Click **Generate PDF Report** to render and download the document.
   - **Immutability:** The output PDF is compiled directly by the native PDF engine (`app.core.pdf`) with vector formatting, preventing post-export editing.
   - **Integrity Verification:** The server provides a SHA-256 hash in the `X-Report-SHA256` HTTP header for verification without cluttering printed pages.

### 7.3 Organization Profile & Corporate Report Branding
Administrators can customize corporate identity settings to brand all compiled PDF reports:
1. Navigate to **Settings** (`/settings`) and click the **🏢 Organization & Branding** tab.
2. Configure company branding attributes:
   - **Company Name:** Displays prominently in the top header of all PDF reports.
   - **Department / Unit:** Identifies the operational unit (e.g. `Network Operations Center (NOC)`).
   - **Report Footer Disclaimer:** Customized confidentiality or compliance notice rendered at the bottom margin of every page.
   - **Corporate Logo:** Upload a PNG, JPG, or SVG image (max 1 MB / 2 MB Base64 data URI). A real-time preview is rendered instantly.
3. Click **Save Organization Settings** to apply changes immediately across all future report generations.

---

## 8. Fleet Bandwidth Monitoring & Flow Analytics (`/bandwidth`)

Ephemres v3.3.2.v introduces passive network flow ingestion supporting NetFlow v5, NetFlow v9, and IPFIX along with per-interface telemetry and link utilization tracking. The dedicated `/bandwidth` dashboard provides complete visibility into network traffic volumes, protocol usage, router interfaces, and top communicating entities.

### Accessing the Bandwidth Dashboard
Click the **🌊 Bandwidth** item in the main navigation header or navigate directly to `/bandwidth`.

### 1. Fleet KPI Ribbon
At the top of the dashboard, five real-time KPI cards present current network traffic state (evaluated over a sliding 5-minute window):
* **Total Ingress Bandwidth:** Real-time incoming throughput in bits per second (`Mbps` / `Gbps`).
* **Total Egress Bandwidth:** Real-time outgoing throughput in bits per second.
* **Active Exporters:** Count of network routers/switches actively streaming flow datagrams to the collector.
* **Total Flows Ingested:** Cumulative count of discrete flow sessions processed in the selected window.
* **Unmatched Candidates:** Count of unrecognized exporter IPs streaming flows to Ephemres.

### 2. Time Window Selection
Select your analysis horizon using the time range switcher:
* `1h` (High-resolution 1-minute intervals)
* `6h` (5-minute rolling averages)
* `24h` (Hourly trend analysis)
* `7d` (Daily capacity curves)
* `30d` (Monthly utilization summaries)

### 3. Stacked Traffic Series Chart
An interactive Chart.js area chart visualizes total Ingress vs. Egress throughput over time:
* Hover over any data point to inspect exact bandwidth rates in Mbps.
* Toggle the **Ingress** or **Egress** legend pills to isolate unidirectional traffic.

### 4. Top Talkers & Forensic IP Conversations
The dashboard features two forensic traffic tables:
* **Top Endpoints:** Identifies the highest-consuming devices on the network. For devices enrolled in Ephemres inventory, the system automatically resolves and displays the friendly hostname alongside its IP address, total bytes transferred, and flow count.
* **Top Conversations:** Uncovers high-volume IP-to-IP communication pairs. Displays Source IP, Destination IP, Protocol name (TCP, UDP, ICMP), Destination Port (e.g. 443 / HTTPS, 5432 / PostgreSQL), total bytes transferred, and session counts.

### 5. Application Protocol Distribution
A visual donut chart aggregates network consumption by application service and port:
* Recognizes standard enterprise traffic profiles (HTTPS, HTTP, SSH, DNS, NTP, SNMP, MySQL, PostgreSQL, Redis, Elasticsearch).
* Ranks services by percentage share of total network volume to quickly identify bandwidth hogs or unauthorized file transfer activity.

### 6. Active Exporters & Direct Interface Configuration
The **Flow Exporters & Interface Telemetry** section lists all networking equipment actively reporting to Ephemres:
* Displays device hostname, primary IP address, and active interface counts.
* **Per-Interface Telemetry Grid:** Shows interface index (`#1`), configured alias (e.g. `WAN-Fiber`), line speed capacity (e.g. `1 Gbps`), average ingress/egress throughput (bps), peak traffic rates, and color-coded capacity utilization bars.
* **Direct Admin Interface Configuration:**
  * Administrators can click the **[ ⚙ Configure Interfaces ]** button at the top of the interface table, or click the inline quick-edit **✎** icon next to any interface row.
  * In the modal, administrators can assign human-readable interface names (e.g. `Gi0/0/0 - Core Uplink`) and select standard line speed presets (**10 Mbps**, **100 Mbps**, **1 Gbps**, **10 Gbps**, **40 Gbps**, **100 Gbps**) or enter a **Custom Mbps** value.
  * Saving configurations applies immediately, dynamically recalculating utilization meters across the dashboard and PDF reports.

### 7. Unmatched Exporter Discovery Banner
When a network device begins exporting NetFlow/IPFIX to Ephemres from an IP address not yet enrolled in the platform's inventory:
* An **amber warning banner** automatically appears across the top of the Bandwidth dashboard:
  > **⚠️ Unmatched Flow Exporters Detected**: Flows were received from `172.16.50.1` which is not mapped to any known device.
* Administrators can click **"Map Exporter"** directly within the banner. A modal prompts the operator to select an existing endpoint to bind the exporter IP to its `flow_exporter_ips` array.
* The mapping takes effect immediately ($\mathcal{O}(1)$ in-memory reload) without restarting daemons or dropping active flow streams.
