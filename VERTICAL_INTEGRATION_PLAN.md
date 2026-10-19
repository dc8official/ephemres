# Flow Telemetry Analytics Workbench: Vertical Integration Plan (v3.3.2v)

## Executive Summary
This document establishes the end-to-end vertical integration plan for upgrading Ephemres' Bandwidth & Flow Telemetry view into a comprehensive **Flow Telemetry Analytics Workbench** on branch `v3.3.2v`. 

The upgrade enriches packet physics metrics (PPS, average packet size, burst profiles), bidirectional directional flow analytics, multi-tab layer-4/QoS/Zone breakdowns, and interactive forensic cross-filtering.

---

## 1. Architecture & Data Flow Overview

```
                      +---------------------------------------+
                      | NetFlow v5/v9 & IPFIX Ingestion UDP   |
                      +---------------------------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | FastStore Wire Buffer Streams         |
                      +---------------------------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | FlowMinuteRollup / Interface Hypertable|
                      +---------------------------------------+
                                          |
              +---------------------------+---------------------------+
              |                           |                           |
              v                           v                           v
+---------------------------+ +-----------------------+ +-----------------------------+
| /bandwidth/overview       | | /bandwidth/traffic-   | | /bandwidth/applications     |
| - total_ingress/egress_pps| |   series              | | - L4 Application Breakdown  |
| - avg_packet_size (B/pkt) | | - ingress_pps /       | | - QoS / DSCP Breakdown      |
| - anomaly_flows_count     | |     egress_pps        | | - Traffic Zone Breakdown    |
| - active_exporters_count  | | - flows_per_sec       | |   (East-West, North-South,  |
+---------------------------+ +-----------------------+ |    External / Transit)      |
                                                        +-----------------------------+
                                          |
                                          v
+-------------------------------------------------------------------------------------+
| frontend/src/views/BandwidthView.vue (Flow Telemetry Analytics Workbench)           |
| - 5-Card Top KPI Metric Strip with Packet Physics classification                    |
| - 3-Way Chart Metric Switcher: Throughput (bps) | Packet Rate (pps) | Flow Rate (fps)|
| - Multi-Tab Breakdown: [ L4 Apps ] [ QoS / DSCP ] [ Traffic Zones ]                 |
| - Interactive Cross-Filtering by Endpoint IP, Service Port, and DSCP Class          |
| - Forensic Conversation Triage Pills (All, Anomalies, QoS Tagged, Bulk Transfers)   |
| - Packet Physics Column with Semantic Flow Profile Badges                           |
+-------------------------------------------------------------------------------------+
```

---

## 2. Step-by-Step Implementation Specifications

### Step 1: Schema Enrichment (`backend/app/schemas/bandwidth.py`)
1. **`BandwidthOverview`**:
   - `total_ingress_pps: float = 0.0`
   - `total_egress_pps: float = 0.0`
   - `avg_packet_size: int = 0`
   - `anomaly_flows_count: int = 0`
2. **`TrafficSeriesPoint`**:
   - `ingress_pps: float = 0.0`
   - `egress_pps: float = 0.0`
   - `flows_per_sec: float = 0.0`
3. **`TopTalkerItem`**:
   - `total_packets: int = 0`
   - `avg_packet_size: int = 0`
4. **`TopConversationItem`**:
   - `total_packets: int = 0`
   - `avg_packet_size: int = 0`
   - `packets_per_flow: float = 0.0`
   - `traffic_profile: str = "Standard"`
     - Derived packet physics classification:
       - `anomaly_hint == "SYN_ONLY"`: `"SYN Scan / Probe"`
       - `anomaly_hint == "TCP_RST"`: `"Reset / Refused"`
       - `avg_packet_size >= 1100 and packets_per_flow >= 10`: `"Bulk Transfer"`
       - `avg_packet_size <= 120 and packets_per_flow <= 3`: `"Micro-Flow / Control"`
       - `dscp == 46 or dst_port in (5060, 5061, 16384)`: `"Real-Time / VoIP"`
       - Otherwise: `"Interactive"`
5. **QoS & Traffic Zone Schemas**:
   - `QoSDistributionItem(BaseModel)`: `dscp: int`, `dscp_label: str`, `total_bytes: int`, `total_packets: int`, `flow_count: int`, `percentage: float`
   - `TrafficZoneItem(BaseModel)`: `zone: str`, `total_bytes: int`, `flow_count: int`, `percentage: float`
   - `ApplicationDistributionResponse`: include `applications`, `qos_classes: List[QoSDistributionItem]`, and `traffic_zones: List[TrafficZoneItem]`.

---

### Step 2: Backend Query Upgrades (`backend/app/routers/bandwidth.py`)
1. **`GET /bandwidth/overview`**:
   - Aggregate `total_packets`, `ingress_packets`, and `egress_packets` over the 5-minute sliding window.
   - Compute `total_ingress_pps = round(ingress_packets / 300.0, 2)` and `total_egress_pps = round(egress_packets / 300.0, 2)`.
   - Compute `avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0`.
   - Query anomaly flow bursts (TCP protocol 6 with RST or SYN-only flags) over the 5-minute window.
2. **`GET /bandwidth/traffic-series`**:
   - For `FlowInterfaceMinuteRollup`: sum `in_packets`, `out_packets`, and `flow_count`, populating `ingress_pps`, `egress_pps`, and `flows_per_sec` over 60s buckets.
   - For standard rollups (`FlowMinuteRollup`, `FlowHourlyRollup`, `FlowDailyRollup`): sum directional packets and flow counts, calculating `ingress_pps`, `egress_pps`, and `flows_per_sec` based on bucket interval.
3. **`GET /bandwidth/top-talkers`**:
   - Default query `limit` updated to 25 (range 1-200).
   - In Top Endpoints, select and sum `packets` across ingress/egress to populate `total_packets` and `avg_packet_size`.
   - In Top Conversations, select and sum `packets`, computing `avg_packet_size`, `packets_per_flow`, and deriving `traffic_profile`.
4. **`GET /bandwidth/applications`**:
   - Accept query parameters `exporter_id: Optional[UUID] = Query(None)` and `endpoint_id: Optional[UUID] = Query(None)` and scope all queries when provided.
   - Run QoS/DSCP aggregation by `tos` (`(tos >> 2)`), computing byte volume, packet volume, flow count, and percentage.
   - Run Traffic Zone aggregation across `src_ip`, `dst_ip`, `src_endpoint_id`, and `dst_endpoint_id` categorizing into:
     - `East-West (Internal)`
     - `North-South Egress`
     - `North-South Ingress`
     - `External / Transit`

---

### Step 3: API Service Updates (`frontend/src/services/api.js`)
- Update `getApplicationDistribution(window = '1h', exporterId = null, endpointId = null)` to forward `exporter_id` and `endpoint_id` query parameters.
- Ensure `getTopTalkers` supports up to limit 200 (passing default limit 25).

---

### Step 4: Frontend Workbench Upgrade (`frontend/src/views/BandwidthView.vue`)
1. **5-Card Top KPI Strip**:
   - Total Ingress Rate + PPS subtitle
   - Total Egress Rate + PPS subtitle
   - Packet Physics Card (`avg_packet_size B/pkt` + Bulk/Mixed/Control classification)
   - Active Flow Exporters (`active_exporters_count / totalEndpointsCount`)
   - Monitored Flows & Anomaly badge (`anomaly_flows_count SYN/RST bursts`)
2. **Main Time-Series Chart 3-Way Mode Switcher**:
   - Pill buttons in chart header: `Throughput (bps)`, `Packet Rate (pps)`, `Flow Rate (fps)`.
   - Dynamic Y-axis scale formatting and tooltip formatting matching selected metric mode.
3. **Multi-Tab Telemetry Breakdown Card**:
   - Tab 1: `L4 Apps` (Doughnut chart + clickable port items cross-filtering conversations).
   - Tab 2: `QoS / DSCP` (DSCP breakdown bars + clickable items cross-filtering conversations).
   - Tab 3: `Traffic Zones` (East-West vs. North-South directional breakdown).
4. **Interactive Cross-Filtering & Forensic Triage**:
   - Clickable top communicating endpoints filter conversations to that IP with active chip.
   - Quick triage pills: `All`, `Anomalies`, `QoS Tagged`, `Bulk Transfers`.
   - Packet Physics column in Top Conversations table with semantic profile badges.

---

### Step 5: Testing & Quality Verification
- Backend tests in `tests/backend/test_flow_pipeline_hardening.py` asserting schema fields and calculations.
- Production bundle verification via `npm --prefix frontend run build`.
