# Ephemres User & Operator Guide — Version 3.3.5

Welcome to the Ephemres Network Monitoring Platform v3.3.5 user guide. This document explains how to navigate the web dashboard, use the interactive topology visualizer, monitor fleet Traffic Analytics and WAN edge capacity, configure multi-channel enterprise alert notifications, use the CSV/PDF report generators, and manage system settings.

---

## 1. Authentication & Session Security

Ephemres provides enterprise-grade session protection:
* **Browser Password Autofill:** The login page supports native browser credential managers (Chrome, Edge, Safari, Firefox, Bitwarden, 1Password) for 1-click authentication.
* **Idle Timeout (v3.3.6):** You are signed out after a period without activity, set per account by an administrator. Only clicks, key presses and moving between pages count as activity: a dashboard left open on a screen still times out, because its automatic refreshes don't count. Admin accounts choose 15 minutes, 30 minutes (default) or 1 hour; viewer accounts 30 minutes, 1, 2 (default) or 4 hours.
* **Admin 12-Hour Limit:** An admin session also ends 12 hours after sign-in, even while it is in use.
* **Session Limit per Account:** Each account may be signed in on a set number of devices at once (default 2, from 1 to 10). Signing in on one more device signs out the oldest session.
* **Why You Were Signed Out:** The sign-in page says why the last session ended: after N minutes without activity, because the account signed in on another device, because the password was changed, or because an administrator ended it.
* **One Account per Person:** Give each colleague a named account rather than sharing `admin`. With a shared account, one person signing in can sign another out mid-presentation (the session limit), and the audit log can't tell who did what.
* **IP-Scoped Lockouts:** Brute-force protection isolates failed attempts by `<Client_IP>:<Username>`, ensuring legitimate users on other networks are never locked out.
* **Forced Initial Password Reset:** First-time logins require setting a secure replacement password before platform access is granted.

---

## 2. Real-Time Dashboard Overview (`/`)

The v3.3.5 dashboard provides instantaneous fleet telemetry with URL-persisted filters and severity-first sorting:

### Global Network Health KPI Strip
* **Summary Ribbon:** Displays total monitored devices, counts of `UP`, `UNSTABLE`, `DOWN`, and **Not Monitored** (passive flow exporters or paused devices), plus the aggregate **Fleet SLA %** (computed strictly across ICMP-monitored endpoints).
* **Interactive Filter Pills:** Clicking any state tile filters the endpoint list and updates the URL query parameters so filtered views can be bookmarked or shared.

### Sites, Locations, Tags and the Filter Bar (v3.3.6)
* **On each endpoint:** an optional **Site** (e.g. "Abuja"), **Location** within it (e.g. "Building A") and any number of **Tags** (e.g. "mpls router"), all with autocomplete from values already in use. Locations offered are those already used in the chosen site. Typing "abuja" where "Abuja" exists stores "Abuja", so case never splits one site in two. Tags are stored in lower case and can't contain commas, quotes, backslashes or braces. Endpoints from before v3.3.6 keep their old location text as the Location, with no Site yet.
* **Filter bar:** the same bar on the Dashboard, Topology Map and Reports pages: search, Site, Location, Tags and Type. Within one field any value matches (tags "mpls" and "core" show endpoints with either); across fields all must match (Site Abuja and tag "mpls" shows only Abuja's mpls endpoints). The filter lives in the page URL, so a filtered view can be bookmarked or shared, and the header links carry it to the other pages.
* **Tiles under a filter:** the KPI tiles count only the endpoints in the filter, and each tile's small line gives the figure for the whole fleet ("of 7 in all sites"). The tiles still filter by state on top of it.
* **Bulk assign (admins):** tick endpoints in the table or cards (or **Select all N matching** the filter), then use **Set site**, **Set location**, **Add tag** or **Remove tag** in the bar that appears. To put old endpoints under a site, filter on their old location text, select all, and set the site. An empty site or location clears it.
* **Renaming:** **Admin Settings → Organization → Sites, locations and tags** renames a value on every endpoint that has it. To merge two spellings, rename the wrong one to the right one. A location can be renamed within one site only.

### Dual View Switcher
* **Visual Card Grid:** Rich visual cards showcasing live status badges, latency indicators, packet loss, and quick diagnostic links, sorted by severity by default (`DOWN` first).
* **Dense Sortable Data Table:** High-density tabular view displaying Hostname, IP, Detailed Operational State, Average/p95 Latency, 24h RTT Sparkline, Packet Loss %, In-State-Since duration, and 24h SLA Uptime %.

### Real-Time Telemetry Stream
* Connected directly to Server-Sent Events (SSE). An indicator badge in the header confirms connection status.

---

## 3. Interactive Topology Map (`/topology`)

Visualizes the parent-child network hierarchy with crossing-free routing and NetFlow link verification:

### Features & Controls
* **Layout Switcher & Actions Menu:** Toggle between **Vertical View (Top-to-Bottom)** and **Horizontal View (Left-to-Right)**, fit the graph to the viewport, refresh, or open the Admin actions menu (**Discover All Routes**, **Rebuild Graph**).
* **NetFlow Link Verification:** Links traversed by flow-exporting routers are checked every 5 minutes against live traffic next hops:
  - **Confirmed by traffic:** Solid link indicating flow next-hop matches the traceroute hop.
  - **Traffic takes another path (`differs`):** Highlighted link when flows leave for a different next hop than the baseline traceroute.
  - **Inferred from flow data (`inferred`):** Names anonymous `* * *` traceroute hops using the router's exported next-hop address.
* **Frozen-Physics Real-Time Recoloring:** State changes update node colors in real time via SSE without recalculating coordinates or causing canvas movement.
* **Collapsible Legend:** Displays live counts of node states and link verification styles.
* **Failover view (v3.3.6):** an endpoint whose route is not its normal one is labelled **⤳ alternate path since …**; the part of its normal path not in use is drawn faded and dashed. The map redraws on every API worker as routes change.
* **Filter (v3.3.6):** with the filter bar set, the map shows the matching endpoints and the path from Ephemres to them; everything else is hidden. **Path devices outside the filter** chooses how the other devices on those paths look: **Dim** shows them faded; **Hide** leaves out endpoints that don't match and joins the path over them with a dashed line, keeping the routers in between. The choice is remembered in the browser. (Both are offered so they can be compared on real data.)
* **Topological Root Cause Analysis (RCA):**
  - When an upstream transit gateway fails, dependent nodes are automatically classified as `INFERRED_DOWN`, preventing alert fatigue.

---

### Routes, failover and root-cause analysis (v3.3.6)
Each endpoint keeps the path its traffic uses now (**current**) and the path it normally uses (**normal**).
* **When routes are traced:** when the endpoint is added; when an admin clicks Refresh; after it recovers; on the rolling schedule; when flow data shows its traffic moved; when the TTL of its ping replies changes and stays changed for 3 cycles; and when a monitored device its route passes through goes down (re-traced a cycle later if it is still up) or comes back.
* **No flapping rewrites:** a different path counts only when two traces at least 2 minutes apart agree. After a change, at most one log entry per endpoint per 15 minutes; 4 or more changes in an hour mark it **flapping** with one entry until it has been quiet for an hour.
* **Normal path:** a current path held 24 hours becomes the normal one. An admin can make the current path normal at once, or pin it so it never changes on its own.
* **Root-cause analysis** compares a failure with the current path, what traffic used just before the failure, and says when that was not the normal path ("On an alternate path since 14:02 UTC (normal path via 172.16.1.1).").
* **On the endpoint page**, the **Route** panel shows both paths side by side (hops that differ highlighted), any pending or flapping state, and the route-change log, with **Make current path normal**, **Pin as normal** and **Unpin** for admins.

---

## 4. Endpoint Details & Perspective Tabs (`/endpoints/:id`)

Clicking any endpoint opens its detailed forensic view with a live status bar (State, Current Latency, 24h Loss %, Jitter, Time in State, Last Probe) and perspective switcher:
* **Tab 1: Health & Diagnostics:**
  - **Per-Cycle Probe Latency & Loss (`ProbeTrendPanel`):** Interactive time-series chart showing average RTT with a min/max latency band and packet-loss markers across the selected window, backed by `endpoint_probe_samples` and `endpoint_probe_hourly`.
  - **State Timeline & Audit Log:** Visual state timeline and paginated transition history.
  - **High-Fidelity Traceroutes & RCA:** View hop-by-hop latency breakdowns, baseline vs. failure trace comparisons, and **Flow Evidence** showing whether traffic continued flowing across routers after ICMP probes failed.
* **Tab 2: Flow Telemetry & Bandwidth:**
  - **Role Mode Switcher:** View traffic **As exporter** (all traffic traversing this router) or **As participant** (traffic to or from this endpoint).
  - **Bandwidth vs. Correlated View:** Switch between **Bandwidth** (Ingress, Egress, and Transit throughput/packet rate) and **Latency vs. bandwidth** (dual time-aligned charts comparing per-probe ICMP latency/loss directly above flow throughput, filterable by SNMP interface).
  - **Per-Interface Utilization Cards:** Displays average, 95th-percentile (`p95`), and busiest-minute (`peak`) utilization, WAN role badges, and a **Configure Interfaces** modal.
  - **Paths Seen in Flows:** Lists where exporting routers forwarded this endpoint's traffic in the last 24h (`in_if`, `out_if`, `next_hop`, volume, active status) and any sustained route changes recorded over the last 7 days.
  - **Server-Filtered Conversations Panel:** Drill down into conversations through or with this device with instant IP, service port, DSCP, and quick-triage filters (`All`, `Anomalies`, `QoS`, `Bulk`).

---

## 5. Admin Settings Console (`/settings`)

Administrators configure the platform across five tabs (v3.3.6 order and names):
1. **👥 Users & access:** User creation, role management (`ADMIN` / `VIEWER`), password reset, and session revocation. **Edit** sets an account's role, idle timeout and session limit; the table's **Session** column shows the timeout and how many sessions are open against the limit ("1 of 2 sessions"). Click that count to see each session (device, address, when it signed in, when it last saw activity, when it ends if left alone) and to end one, or all of them. On your own account, "End all sessions" keeps the browser you are using. A new timeout applies to the account's open sessions at once; a new session limit applies from its next sign-in. An admin can change their own timeout and limit, but not their own role. **Sign-in protection** holds the brute-force lockout threshold (failed attempts per address and username before a 15-minute lockout).
2. **🌐 Discovery & topology:** Layer-2 subnet auto-bypass for traceroutes; **Parallel route traces** (1–8, default 2: traces through the same router still take turns, and incident and root-cause traces always get 2 more); **Re-trace every** 6, 12 or 24 hours (default 24), spread over the period instead of all at midnight.
3. **📡 Data collection:** NetFlow v5/v9 and IPFIX collector controls (UDP 2055/4739), flow diagnostic preflight, sampling rate, and the FastStore event-routing toggle (`performance_mode`; flows don't depend on it). **SNMP polling** (v3.3.6): the on/off switch, how often each device is polled (30 s to 5 min, default 1 min), and the **SNMP credential sets** (see section 9).
4. **🔔 Alerts:** Master alerting engine toggle, alert channel inventory (Teams, Discord, Slack, Email, Webhook), live diagnostic test triggers, and historical delivery audit logs.
5. **🏢 Organization:** Company name, department / NOC unit, report footer disclaimer, and corporate logo (PNG or JPEG, up to 1 MB) for branded PDF reports, plus renaming sites, locations and tags.

**Saving:** every plain setting on any tab (switches, ports, thresholds, branding) waits for one **Save Changes** bar at the bottom of the page, with **Discard Changes** beside it; a dot on a tab marks its unsaved changes, and leaving the page asks first. Lists save on their own when you confirm their dialog: user accounts, alert channels, and renames. Older links to `?tab=performance` and `?tab=security` open Data collection and Discovery & topology.

---

## 6. Configuring Enterprise Alert Channels

Ephemres v3.3.5 includes an asynchronous, non-blocking notification dispatcher supporting major collaboration platforms and corporate email.

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
3. In Ephemres, select provider `Discord`, paste the URL, and select desired severities (`DOWN`, `RECOVERED`). Notifications render with color-coded sidebars.

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

### Traffic Alerts on WAN Links (v3.3.6)
Ephemres can warn when a WAN link is busy for a while, using the NetFlow/IPFIX traffic it already collects.

* **Which links:** interfaces marked **WAN** in the interface editor (Traffic → Interfaces, or the endpoint page) that have a **link speed** set. A WAN link without a speed shows "Set a speed to watch this link". LAN links are never watched.
* **Turning it on:** Settings → Alerts → **📈 Traffic alerts on WAN links**. The default rule is a threshold (70–95%, default 80%) and a duration (5–60 minutes, default 15). Save with the Save bar.
* **One link's own rule:** under each WAN row in the interface editor, **Traffic alert** is *Default (Settings → Alerts)*, *Custom* (its own % and minutes) or *Off*.
* **When it alerts:**
  * **Traffic high**: inbound or outbound utilisation was above the threshold in **every** minute of the duration. Inbound and outbound alert separately. One alert per busy spell, with no reminders.
  * **Back to normal**: 5 minutes in a row at least 10 points under the threshold (under 70% for an 80% rule). It says how long the link was busy and its peak.
  * **Link silent**: a link that had traffic in the last 24 hours sent no flow data for 15 minutes (only while flow collection is on). **Traffic again** follows when data returns. This usually means the exporter or the link stopped, not that the link is idle.
* **Minutes are checked once a minute,** at 20 seconds past, on whole minutes that ended at least 2 minutes earlier, so late flow records are counted.
* **Which channels get them:** only channels with the traffic severities ticked (**Traffic high**, **Back to normal**, **Link silent**, **Traffic again** under "Traffic on WAN links" in the channel dialog). Existing channels get none until you tick them. The channel's endpoint and subnet filters apply to the exporter. The 5-minute rate limit is per link and direction. The master alerting switch stops these too.
* **Example:** `abj-rtr WAN1: inbound traffic above 80% for 15 min`, with the link speed, average and peak. The delivery log shows these as `TRAFFIC_ALERT` against the link (e.g. `abj-rtr WAN1`).

### Live Diagnostic Validation
Always verify channel reachability before saving:
- In the channel modal, click **Send Diagnostic Test Alert**.
- Ephemres immediately dispatches a synthetic test payload and displays the HTTP status code, server round-trip latency (ms), and any diagnostic error feedback.

---

## 7. Reports, Auditing & Compliance Documentation

Ephemres v3.3.5 provides enterprise-grade reporting workflows for compliance audits, executive SLA reviews, and capacity planning.

### 7.0 Reports for a site, location or tag (v3.3.6)
The filter bar on **Reports & SLA** limits the summary table, the CSV export and the PDF to the endpoints it matches. "All" in the export and PDF dialogs then means all endpoints in the filter; picking endpoints by hand picks among them. The PDF prints the filter under its scope title. Reports use each endpoint's site, location and tags as assigned now, not as they were during the report period, and say so. The table's own search box is gone: search is in the filter bar.

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
1. Navigate to **Settings** (`/settings`) and click the **🏢 Organization** tab.
2. Configure company branding attributes:
   - **Company Name:** Displays prominently in the top header of all PDF reports.
   - **Department / Unit:** Identifies the operational unit (e.g. `Network Operations Center (NOC)`).
   - **Report Footer Disclaimer:** Customized confidentiality or compliance notice rendered at the bottom margin of every page.
   - **Corporate Logo:** Upload a PNG or JPG image (max 1 MB). SVG is not accepted because PDF reports cannot embed it. A real-time preview is rendered instantly.
3. Click **Save Changes** in the bar at the bottom of the page; the next reports use the new branding.

---

## 8. Traffic (`/bandwidth`)

The Traffic page (**Traffic** in the top bar) shows what the routers' flows (NetFlow v5, v9, IPFIX) and SNMP counters say about your links. v3.3.6 rebuilt it around one question: *how full is each WAN link now, and is that usual?* Everything else on the page is "over the last {window}" below that.

### 8.1 What the page covers: filter, device and window
* **Filter bar.** The same filter as the dashboard, reports and map: site, location, tags, type and search. It picks **routers** (devices that export flows or are polled over SNMP); every figure on the page is the traffic those routers saw. Hosts are not filtered on their own.
* **Device picker.** Narrows the page to one router. Devices are grouped by site; one that sends no flows (polled over SNMP only) is marked "(SNMP)". A device outside the filter shows nothing, and the page lets it go when the filter changes. Old links with `?exporter_id=` still open on the device.
* **Window.** 1h, 6h, 24h, 7d or 30d, kept in the address (`?window=24h`) and remembered for the browser tab. Charts are per minute up to 6h and per hour beyond; conversation figures are per minute at 1h and per hour beyond.
* **Unregistered sources.** A router sending flows that isn't enrolled is not in the picker. One line says "2 unregistered sources sending flows"; **Review** lists each with its flows per second and when it was last seen, and admins can **Enroll** it or **Link** it to an existing endpoint. Its flows are not stored until then.
* **Updates.** The page updates itself once a minute, about 15 seconds past the minute, once the last minute's totals are written. The line under the title says when it last updated. **Pause** holds the page still and **Resume** updates it at once; nothing updates while the browser tab is hidden. **↻ Refresh** updates on demand.
* **Counted once.** Traffic between two sites crosses two routers and both report it. Everywhere on the page (and in the PDF) a conversation counts once, at the larger report, however many routers saw it.

### 8.2 Now: each WAN link against its capacity
The top of the page, on every tab, shows each WAN link over the last five complete minutes, grouped by site (sites by name, links without a site last; within a site, the busiest first).

* **Capacity bars.** An **In** and an **Out** bar against the link's speed, with:
  * the **usual range** for this hour drawn as a bracket over the bar;
  * **today's peak** (the busiest minute since midnight, server time) as a tick.
* **Usual range.** Worked out per link from the last 28 days of hourly averages, in the server's timezone (so office hours line up and summer time is followed). With under 21 days of history, hours are grouped as weekday or weekend; from 21 days, by day of week. A range shows once an hour has 3 samples; from 5, the lowest and highest are left out. Until then the link says "No usual range yet".
* **Above usual.** A note such as "In 25% above usual" when a direction is more than 10% over the top of its range.
* **Speed.** Typed in the interface editor, else the port's speed from SNMP when the router is polled. A typed speed that differs from the port's says so: "20 Mb/s, set by hand (port 1 Gb/s)". A link with no speed shows its rates and, for admins, **Set speed**, which opens the interface editor on that link.
* **State.** For a polled router: down ("down since 14:00"), shut down, or "SNMP not answering" while the router doesn't reply.
* **Alerts.** An open traffic alert shows on its link ("Busy since 14:05 (in, peak 97%)"). See *Traffic Alerts on WAN Links* in section 6.
* **Status line.** Under the links: "3 of 3 routers sending flows · flows · average packet size". It turns amber and names the routers when one that exports flows sent nothing in the five minutes.

Links are WAN links when they have the WAN role in the interface editor (section 8.4).

### 8.3 Overview
* **One chart per WAN link** (default), grouped by site, in **% of capacity** with a dashed 100% line, so links of different sizes compare at a glance (a link with no speed is drawn in bits per second and says so). Each chart shows:
  * in and out, per minute up to 6h and per hour beyond;
  * the busiest minute of each hour as a dashed line (hourly charts);
  * the usual range shaded for the busier direction (click the other in the legend to show it);
  * times the link was down (SNMP), shaded.

  Hover for rates, % of capacity, both usual ranges and any down time. Links with an alert or down time open first, then the busiest, at most 6; the others are one row each with a sparkline and open on click. **Show all N charts** opens every one. Your own opens and closes are kept while the page updates.
* **All links together** (`?chart=together`) is the combined chart: **WAN edge** (traffic on WAN links, counted once) or **All flows** (every conversation, split into inbound, outbound, internal and transit), as **Throughput**, **Packets** or **Flows**.
* **Top talkers.** The hosts and outside addresses with the most traffic: **Received** and **Sent** from the host's side, and the **Site** (an endpoint's own site, or the sites of the routers that saw the address). Click one to list its conversations.
* **Applications.** Share of traffic by service port, named in one table shared with the PDF ("QUIC (HTTP/3)" for UDP 443, "MS SQL" for 1433, and so on). Click one to list its conversations.
* **Traffic zones.** Inbound, outbound, internal (east-west) and transit. **Internal** means a private address (RFC 1918, loopback, link-local, IPv6 ULA) or a monitored endpoint, the same rule everywhere. Zones at 0% are hidden.

### 8.4 Interfaces
* **No device picked:** "Interfaces across devices", one table of every router's interfaces in scope:
  * WAN links first, busiest against their speed first;
  * then the other interfaces by traffic (the top 20, **Show all N** for the rest);
  * ports an admin shut down last, greyed.
* **One device picked:** that router's interfaces by index (shut-down ports last), with its chart above.
* **Columns:** site and device (across devices), interface, speed, **Now in** / **Now out** (the same five minutes as section 8.2), **Last {window}: avg / P95 / peak** utilisation (without a speed: the average rates), source (SNMP or Flows) and status. Every column sorts: click once ascending, twice descending, a third time back to the default order.
* **Status.** For a polled router, from SNMP: Up, Down, Shut down, or "SNMP not answering". Down time in the window is spelled out: "down 40 min, Thu 14:00", "down since …", or "down 3 times, 1 h 20 min in all". Routers that only export flows show "Traffic seen" or "Idle".
* **Click a row** across devices to open that router with the interface isolated in the chart.
* **Interface editor (admins).** **⚙ Configure interfaces**, or **✎** on a row, opens the router's editor: a name, the speed (presets from 10 Mb/s to 100 Gb/s or a custom figure; left empty, SNMP's speed is used), the role (WAN or not) and, on WAN links with a speed, the **Traffic alert** choice (Default, Custom % and minutes, or Off). Saving applies at once to the page and to PDF reports.

### 8.5 Conversations
A conversation is source, destination, protocol and service port over the window, filtered, counted and paged on the server.
* **Quick filters:** All, Anomalies, QoS tagged and Bulk, each with its count over every matching conversation (not just the page shown). **Bulk** means full-size packets (1100 B on average or more), seen for at least 30 minutes, at 5 Mb/s or more while seen: backups and replication, not a large download.
* **Sort** by volume, flows or packets. Type an address or a port in the box, or click an address, port or QoS class in a row, to filter by it; the filters show as chips you can remove.
* **Site.** Each row names the sites of the routers that saw it; hover for the routers ("Seen by abj-rtr1 (Abuja), hq-rtr1 (Lagos HQ)"). A conversation between two sites lists both. Its volume is counted once.
* **Profile.** Bulk Transfer, Download, Real-Time / VoIP, Micro-Flow / Control, SYN Scan / Probe, Reset / Refused, or Interactive, with the average packet size and packets per flow. TCP flags show only on suspicious rows.

### 8.6 QoS
Share of traffic by DSCP class, and the QoS-marked conversations. The tab's count is the number of marked classes; on a network where nothing is DSCP-marked the tab says so in one line.

### 8.7 Security
* **Tiles** over the picked window and scope: suspicious conversations, and the flows in them out of all flows.
* **Suspicious conversations.** SYN without ACK over the whole conversation (possible scans) or RST without FIN (refused connections), sorted by flow count. Each row also gives:
  * **First / last seen** in the window, in server time: to the minute at 1h, to the hour beyond (the first hour it was seen in, and the end of the last);
  * the **router** that saw it, under its site, and the **WAN link** it arrived on when that router has only one. Flows are stored per router, not per interface, so with two WAN links the link isn't known.

---

## 9. SNMP Polling (v3.3.6)

With SNMP, Ephemres reads each device's interface names, speeds and up/down status itself, and notices reboots. You no longer have to type interface names, and link speeds come from the device.

1. **Credential sets.** Settings → Data collection → **SNMP credential sets** → **Add credential set**. A set is named (for example "Branch v3" or "Meraki") and holds:
   - a v2c community, or a v3 user with its authentication and privacy protocols;
   - **Use the authentication password for privacy too** for devices that take one password for both (Meraki, UniFi);
   - under **Advanced**: port, timeout, retries and context name.

   Enter a device address and press **Test** to see what that device answers before saving: interfaces, 64-bit counters, LLDP/CDP neighbours, and what one read costs. Passwords are never shown again after saving: leave a password field empty when editing to keep it. Deleting a set stops polling its endpoints.
2. **Assign sets to endpoints.** In the endpoint form (**SNMP credentials**), or for many at once: select endpoints on the dashboard and use **Set SNMP** in the selection bar. **None** turns SNMP off for them.
3. **Switch SNMP on.** Settings → Data collection → **SNMP polling**, then **Save Changes**.

**What you see**
- **The endpoint page's SNMP panel:**
  - whether the device answers, or why not ("No reply", "Credentials refused");
  - the last answer, the vendor, and what the device answers;
  - each interface with its description, speed and status;
  - recent interface events: up, down, added, removed, renumbered, and device reboots.

  **Probe now** (admin) reads the whole device again at its next turn.
- **Interface names and speeds everywhere:** the name you typed, else the device's interface description, else its interface name; the speed you typed, else the device's. The interface editors show the device's values greyed as defaults ("From SNMP: 1 Gbps"). Type a value only to override it: a contracted 50 Mb link on a 1 Gb port should keep 50. WAN/LAN stays yours to set. A WAN link with an SNMP speed is watched by traffic alerts.
- **Renumbered interfaces:** when a device renumbers its interfaces after a reboot, your typed entries move with them (matched by name), and the move is logged on the endpoint page.
- **Interface traffic from the counters (step 2):** where a device answers SNMP, its interface charts and table, WAN totals and chart, traffic alerts and PDF interface figures use its counters instead of flow figures.
  - Counters are exact, where flows are sampled and miss what the router does not export.
  - Such interfaces carry an **SNMP** badge.
  - Flows still give conversations, top talkers and applications, and remain the source for devices without SNMP.
  - The switch is per minute: minutes from before SNMP was switched on (or while the device did not answer) still show flow figures, so there is no gap and nothing is counted twice.
  - The newest minute fills in at the next poll.
  - Errors and discards are recorded too.
  - A link with no flow export can now raise **Traffic high** from its counters. **Link silent** stays a flow alarm: it means the router stopped exporting flows.
- **Neighbours (step 3):** the SNMP panel lists what each device reports over LLDP and CDP:
  - the local port, the neighbour, its port and its address;
  - **monitored** when the neighbour is one of your endpoints (matched by address, then MAC, then name);
  - **Add as endpoint** for the others, which opens the add form filled in.
- **Switches on the map:** devices traceroute cannot place (on the server's own subnet, previously hung straight under the root) now hang from the neighbour that links them to the rest of the network. An access switch sits under its core switch, which sits under the router.
  - These links are drawn dotted in teal and labelled **Link found by LLDP/CDP**.
  - A parent you set by hand still wins.
  - Root-cause analysis follows them: when a switch and the devices behind it go down, the incident names the switch, and those devices are listed as its symptoms.
- **WAN link down / up:** a WAN link going down or coming back sends **Link down** / **Link up** to the alert channels that tick them (Settings → Alerts → channel → *WAN links (SNMP)*; none get them until ticked).
- **Re-trace on link down:** a link going down or up on a device other endpoints route through re-traces those routes after one cycle, as when the device itself goes down. The device is recognised by any of its addresses, not only the monitored one.

**How often:**
- Status every poll interval.
- Names and speeds when the device reports a change, and every 15 minutes.
- The whole device after a reboot, on Probe now, after 3 failed polls, and daily.

Each device is polled one request at a time, and polls are spread over the interval. See the SNMP guide for device setup and troubleshooting.
