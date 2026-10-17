# LNMP SLA Calculation & Availability Methodology

This document outlines the mathematical principles, state machine transitions, and outage neutralization formulas used by LNMP to calculate Service Level Agreement (SLA) uptime.

---

## 1. High-Precision 5-Ping Sub-Cycle Polling

Within every 60-second window aligned to the absolute minute mark (with `0–2000ms` randomized startup jitter), the LNMP monitoring daemon sends:
* **5 ICMP echo requests** spaced **8 seconds apart** (`Interval = 8.0s`, active sweep window `~32s`, leaving a **28-second headroom window** before the next minute boundary).
* **Packet Loss & Latency Metrics**:
  - `failed_count` (`0 <= k <= 5`)
  - `packet_loss_percent` = `(k / 5) * 100%`
  - `avg_rtt_ms`, `min_rtt_ms`, `max_rtt_ms` (calculated across successful replies).

---

## 2. Dual-State Health Engine

To prevent momentary packet jitter from skewing SLA uptime, LNMP separates operational state from detailed packet health:

```
[Packet Health / Detailed State]
  - UP              (0 packet drops / 0% loss, normal RTT)
  - UP-UNSTABLE     (1 to 2 packet drops / 20%–40% loss or high Z-score jitter)
  - DOWN-UNSTABLE   (3 to 4 packet drops / 60%–80% loss)
  - DOWN            (5 packet drops / 100% loss)

[Production Availability / Operational State]
  - UP              (0 to 2 packet drops; endpoint is operational and contributes to SLA uptime)
  - DOWN            (3 to 5 packet drops; endpoint is unavailable and counts as SLA outage once confirmed)
```

---

## 3. In-Memory Flap Suppression (N=3)

State transitions require **3 consecutive cycles (180 seconds)** of sustained condition before the operational state switches from `UP` to `DOWN` or `DOWN` to `UP`:

```mermaid
stateDiagram-v2
    [*] --> UP
    UP --> DOWN_PENDING : 1st cycle 100% loss
    DOWN_PENDING --> DOWN_PENDING : 2nd cycle 100% loss
    DOWN_PENDING --> DOWN : 3rd cycle 100% loss (Outage Confirmed)
    DOWN_PENDING --> UP : Any packet reply (Flap Suppressed)
    
    DOWN --> UP_PENDING : 1st cycle packet reply
    UP_PENDING --> UP_PENDING : 2nd cycle packet reply
    UP_PENDING --> UP : 3rd cycle packet reply (Recovery Confirmed)
```

---

## 4. Honest SLA Mathematics & Blackout Neutralization

### The Problem with Traditional SLA Calculators
If the monitoring server itself loses power or undergoes scheduled maintenance, traditional monitoring tools record the missing intervals as "outages" for all monitored clients, skewing SLA reports.

### The LNMP Blackout Neutralization Formula
LNMP computes availability exclusively over monitored lifetime intervals:

```
SLA Availability (%) = [ T_online / (T_monitored_lifetime - T_blackout_neutralized) ] * 100
```

Where:
* `T_online`: Cumulative minutes where `operational_state = 'UP'`.
* `T_monitored_lifetime`: Total elapsed minutes since the endpoint was onboarded.
* `T_blackout_neutralized`: Server downtime or excluded maintenance periods during which the monitoring engine was offline.

This guarantees mathematically sound SLA compliance reports for client reporting, audits, and executive reviews.
