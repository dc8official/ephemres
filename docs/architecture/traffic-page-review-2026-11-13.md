# Traffic page review, 2026-11-13 (v3.3.6 build plan 7.3)

The Traffic page as it stands on branch `v3.3.6`, walked on a scratch stack carrying a week of
realistic traffic. Every finding has a number so Kenneth can answer it point by point
(agree, change it, drop it). Nothing here is built. The 7.2 items are designed after this.

## How the data was made

Three routers, each at a site with its own WAN link:

- **hq-rtr1** (Lagos HQ) is polled over SNMP and exports flows. It has two WAN links:
  - Gi0/0, "MTN fibre 100M", with its speed taken from SNMP;
  - Gi0/1, "Airtel backup", typed in as 20 Mb/s while SNMP reports 1000.
- **abj-rtr1** (Abuja) exports flows only. Its WAN link is "Glo MPLS 20M".
- **ph-rtr1** (Port Harcourt) exports flows only. Its WAN link has no speed set.

Four servers are monitored endpoints: hq-fs1, hq-erp1, hq-dns1 and abj-pos1. Users' PCs
(10.x.20.0/24) are not endpoints, as in a real office.

Traffic follows office hours in WAT (08:00 to 18:00 on weekdays, with a lunch dip), with quiet
nights and weekends. It is made of:

- Microsoft 365, YouTube (QUIC), Windows Update and Zoom;
- the Abuja point of sale (POS) talking to the HQ ERP server across both routers;
- DNS, and a CCTV upload on the backup link;
- a scanner hitting telnet on hq-rtr1.

Three events are planted:

- a nightly backup at 60 Mb/s;
- an ad hoc backup on Wednesday from 15:00 to 16:30 that fills the HQ fibre;
- the Airtel backup link down on Thursday from 14:00 to 14:40, with the SNMP down and up
  events recorded.

A feeder writes each new minute, so the page updates as it would live.

The data is not real in a few ways:

- Nothing is DSCP-marked.
- Each conversation keeps a steady share of the traffic.
- The Wednesday backup is seeded beyond line rate (106 Mb/s on a 100 Mb/s link), where a real
  link would flatten at its capacity.
- The unregistered-exporter banner and callout were not exercised, because they need a live
  flowd.

## What works

- **Interfaces tab.** The **avg / P95 / peak utilisation** bar per interface is the most
  useful thing on the page. It answers "is this link too small" directly.
- **SNMP merging.**
  - The SNMP badge shows where an interface's data comes from.
  - The typed 20 Mb/s speed overrides the 1000 Mb/s that SNMP reports.
  - The HQ chart uses the counters.
- **Utilisation colours in the WAN links table.** The Abuja MPLS at 62% shows in amber.
- **Security tab.** The telnet scanner shows as a SYN scan against hq-rtr1.
- **Updates.** The once-a-minute update with "Updated N s ago" and Pause (7.1) behaves well.

## Findings

### Page-wide

**P1. Two time frames on one page.**

- These always show the **last 5 minutes**, whatever window is picked:
  - the KPI strip;
  - the WAN links table;
  - the Security tiles.
- Everything else follows the picker: the chart, Top talkers, Applications, Zones and
  Conversations.
- At 7d, the tiles say WAN in **53 Mb/s** right above a chart whose average is **19.6 Mb/s**.
  The Security tiles count 125 flows in 5 minutes above a table covering 24 hours.

*Proposal:* split the page into a clearly labelled "Now" area (the headline links) and a
"Over the last …" area. Everything in the second area follows the picker.

**P2. Panels count traffic differently.**

- Traffic that crosses two routers is counted twice in **Applications** and **Traffic zones**,
  but once in **Top talkers** and **Conversations**.
- In 24h, the ERP traffic between Abuja and Lagos shows as:
  - **42.7 GB** under TCP/1433 in Applications, and as East-West;
  - **23.9 GB** in Conversations (each router saw about 22 GB).
- So the percentages in Applications and Zones do not agree with the rest of the page.
- The footnote under the tiles explains double counting for Ingress/Egress/Transit only.

*Proposal:* in the all-routers scope, count once everywhere, using the rule Top talkers
already uses.

**P3. "Transit" means two different things, and the Ingress/Egress/Transit tiles mislead in an
office.**

- **The tiles.** The **Transit** tile (55.8 Mb/s, "no monitored host on either side") is the
  biggest number on the page, because users' PCs are not endpoints. Ingress (9.3) and Egress
  (9.5) cover only the four servers.
- **Traffic zones.** The same traffic counts as North-South there, and **External / Transit
  is 0%**, because private addresses count as internal.

*Proposal:*
- Use the zones rule (internal = monitored or private) everywhere.
- Take Ingress/Egress/Transit out of the headline. They stay available under "All flows".

**P4. The scope list holds flow exporters only.**

- It is a flat list with emoji and an IP per router.
- There is no site grouping, and it does not use the shared site/tags filter from phase 2.
- A router polled over **SNMP that does not export flows never appears** anywhere on this
  page. I found this in the code; it was not tried in the browser.

*Proposal (7.2 "scope list"):*
- Scope is the shared filter (site, tags), plus an optional single device.
- The device list is grouped by site and includes SNMP-polled devices.
- Unregistered sources leave the list and appear as a notice with Enroll / Link.

**P5. Ports are named unevenly.**

- "HTTPS" appears **twice** in Applications (TCP 443, and UDP 443, which is QUIC).
- Common ports have no name: 1433 (MS SQL), 8801 (Zoom), 873 (rsync), 23 (Telnet).

*Proposal:* name UDP 443 "QUIC (HTTP/3)" and extend the port-name table. This is small.

### Overview

**O1. The WAN links table leads with the wrong measure.**

- The bar shows each link's **share of all WAN traffic**. That compares the Lagos fibre (60%)
  with the Abuja MPLS (23%), which nobody acts on.
- Utilisation against capacity, the measure that matters, is a small number at the right.

*Proposal (7.2 "headline"):*
- One row per WAN link with a **capacity bar**: now, the usual range for this hour, and today's
  peak.
- Rows are sorted by utilisation and grouped by site.
- A link with no speed shows a "Set speed" button rather than a tooltip.

**O2. The chart adds every site's WAN links into one line.**

- Wednesday's full HQ fibre appears as a spike to 106 Mb/s on an axis with no capacity line. It
  does not read as "a link was full for 90 minutes".
- The 40-minute Airtel outage on Thursday does not show at all: the link is small and the
  chart uses hourly averages.
- The chart shows no link events, although SNMP now records them.

*Proposal:* one small chart per WAN link, each with:
- its capacity line;
- the "usual" band (7.2);
- down/up markers from SNMP.

The combined chart stays as an option.

**O3. Long windows lose peaks.**

- 24h, 7d and 30d plot hourly **averages**. The 7d "peak 58.3 Mb/s" is the busiest hour's
  average; the busy minutes inside it are gone.
- Minute data is kept for 7 days.

*Proposal:* plot the hourly peak (or P95) alongside the average. This needs a max column in
the hourly aggregates, which is a migration.

**O4. Several tiles answer questions nobody asks first.**

- These sit at the same weight as the WAN tiles:
  - average packet size ("Mixed / Standard MTU");
  - exporters sending ("3 of 3");
  - flow count.

*Proposal:* move them to a small status line, or to the exporter health in Settings.

**O5. Top talkers columns are confusing.**

- "Ingress / Egress" are from the **host's** side. For Microsoft (52.97.146.2), "Egress
  124 GB" means it sent us 124 GB, but it reads as our egress.
- External and user addresses are bare IPs, with no site.

*Proposal:*
- Rename the columns "Received / Sent" and add a Site column.
- Name hints (reverse DNS or a small table of well-known services) are a separate decision.

**O6. Traffic zones has a row that is always zero.**

- "External / Transit" is 0% on every branch router.
- The double counting from P2 also applies here.

*Proposal:* hide empty zones.

### Interfaces

**I1. The tab needs a router picked first.**

*Proposal (7.2 "interfaces"):* in the all-routers scope, show one table of interfaces across
every device:
- WAN links first;
- columns: site, device, now, avg / P95 / peak utilisation, and source (SNMP or flow);
- sortable;
- following the shared filter.

**I2. Status ignores SNMP.**

- The Status column says "Active" (traffic was seen). SNMP's oper status is not used.
- Thursday's 40-minute Airtel outage is not mentioned.
- The shut-down Gi0/3 is not listed.

*Proposal:*
- When a device is polled, Status comes from SNMP (up / down / shut down).
- Show recent down time on the row ("down 40 min, Thu 14:00").

**I3. "Ingress rate / Egress rate" are averages over the window.**

At 24h they show 15.3 / 9.3 Mb/s, which looks like a rate right now.

*Proposal:* label them "avg" or show "now" next to them.

### Conversations

**C1. No router or site column.** "52.97.146.2 → 10.1.20.12" is Abuja, but the table does
not say so.

*Proposal:* add a Site column and filter.

**C2. "Bulk Transfer" is decided by packet size alone.**

- 11 of 19 conversations are "Bulk", including every Microsoft 365, YouTube and Windows
  Update download. The Bulk filter therefore does not single out the backup job.

*Proposal:* call a conversation bulk only when it has large packets **and** sustained volume.
Or rename the label "Large packets".

**C3. Request and reply are separate rows.** User → Microsoft and Microsoft → user are listed
apart.

*Proposal:* pair the two directions in one row. This is a bigger change and could come later.

**C4. Visual noise.**

- "156608.2 pkts/flow" shows decimals.
- "FIN SYN PSH ACK" appears on every normal TCP row.

*Proposal:* round the numbers, and show flags only when they are unusual (SYN only, RST).

### QoS

**Q1. On a network with no DSCP marking, the tab is empty.** It shows "Best Effort 100%" and
an empty table, yet the tab badge says "QoS (1)", which suggests something to look at.

*Proposal:* count only marked classes in the badge. When nothing is marked, show one line
("Nothing is DSCP-marked on these links").

### Security

**S1. The tiles and the table cover different windows** (see P1). The scan's row also does not
say which link it arrived on or since when.

*Proposal:* the window follows the picker; add first and last seen, and the arrival link.

## How this maps to the 7.2 items

| 7.2 item | Findings |
|---|---|
| Headline: each WAN link against capacity | O1, O2, O4, P1, P3 |
| Usual versus now | O1 (band on each link), O2 (band on each link's chart), O3 (peaks) |
| Scope list and shared filter | P4 |
| Interfaces across devices | I1, I2, I3 |

New, outside 7.2: P2 (count once), P5 (port names), O5, O6, C1–C4, Q1, S1.

## Kenneth's answers (2026-11-13)

All 22 findings were gone through with Kenneth. He chose the proposal in every case except
C3, which is parked.

| # | Decision |
|---|---|
| P1 | A clearly labelled **"Now"** area at the top (the WAN links headline). Everything below says "Over the last …" and follows the picker, the Security tiles included. |
| P2 | In the all-routers scope, **count once everywhere**, using the rule Top talkers already uses. Applications and Traffic zones change. |
| P3 | Internal means **monitored or private (RFC 1918)** everywhere. Ingress/Egress/Transit leave the headline; they stay available under the chart's "All flows" mode. |
| P4 | Scope is the **phase 2 shared filter (site, tags) plus an optional single device**. Devices are grouped by site, and SNMP-polled devices are included. Unregistered sources move out of the list into a notice with Enroll / Link. |
| P5 | UDP 443 is named **"QUIC (HTTP/3)"**. The port-name table gains 1433, 8801, 873, 23 and other common services. |
| O1 | **One row per WAN link with a capacity bar.** The bar shows now, the usual range for this hour and today's peak. Rows are grouped by site and sorted by utilisation. A link with no speed shows a "Set speed" button. |
| O2 | **One small chart per WAN link**, each with its capacity line, the usual band and SNMP down/up markers. The combined chart stays as a toggle. |
| O3 | Each hour's **busiest minute** is stored in the hourly aggregates (a migration). It is drawn as a faint line above the average and is used for the "peak" figures. |
| O4 | Packet size, exporters sending and flow count move to **one quiet status line**. It turns amber when an exporter stops sending. |
| O5 | Top talkers columns become **"Received / Sent"** (from the host's side), and a **Site** column is added. |
| O6 | Zone rows at 0% are hidden. |
| I1 | In the all-routers scope, one table of interfaces across devices: **WAN links first, then the busiest other interfaces**. Columns: site, device, now, avg / P95 / peak utilisation, source. The table is sortable, follows the shared filter, and has a toggle to show all. |
| I2 | For a polled device, Status comes from **SNMP** (up / down / shut down), with **recent down time** on the row within the window. Shut-down interfaces are listed greyed at the bottom. Flow-only devices keep "traffic seen". |
| I3 | A **"Now"** in/out column (last 5 minutes, per P1). The window's average sits in the utilisation column, labelled as such. |
| C1 | A **Site column** (router on hover) and the shared filter. A conversation seen by two routers shows both sites. |
| C2 | A conversation is **Bulk** only when it has large packets **and** moves data steadily. Other conversations with large packets are "Download". |
| C3 | **Later** (build plan 11a). Pairing directions touches the conversation queries, the rollups and the PDF. |
| C4 | Packets per flow are rounded. Flags are shown only on SYN-only or RST rows. |
| Q1 | The QoS badge counts only marked classes. With nothing marked, the tab shows one line: "Nothing is DSCP-marked on these links". |
| S1 | The Security tiles follow the picked window. Each suspicious row gives **first and last seen** and the **link it arrived on**. Refined the same day: the router that saw it, and the WAN link only when that router has a single one, because conversations are not stored per interface. |
