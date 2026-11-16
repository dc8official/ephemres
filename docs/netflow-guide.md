# Ephemres Network Flow Telemetry Configuration & Operations Guide (v3.3.6)

This operational manual details the architecture, firewall configuration, vendor router setup, and troubleshooting procedures for **Network Flow Telemetry Ingestion (NetFlow v5, NetFlow v9, IPFIX and, since v3.3.6, sFlow v5)** and **NetFlow Route Augmentation** in Ephemres.

---

## 1. Architectural Overview

Ephemres provides real-time, high-volume flow ingestion, per-interface throughput and WAN edge accounting, and flow-assisted route discovery completely isolated from ICMP polling daemons:

```
+-------------------------------------------------------------------------------+
|                        ROUTER / SWITCH / FIREWALL                             |
|  (Cisco IOS-XE, Juniper Junos, Fortinet, Mikrotik, Linux softflowd)           |
+---------------------------------------+---------------------------------------+
                                        |
        UDP 2055 (NetFlow v5/v9) / UDP 4739 (IPFIX) / UDP 6343 (sFlow v5)
                                        v
+-------------------------------------------------------------------------------+
|                    EPHEMRES COLLECTOR DAEMON (ephemres-flowd)                 |
|  - Async Datagram Listeners (4MB SO_RCVBUF)                                   |
|  - Zero-alloc binary parsers (v5, v9 template cache, IPFIX RFC 7011/7012)     |
|  - sFlow v5 samples, each scaled by its own sampling rate (v3.3.6)            |
|  - Parsed flows handed straight to the aggregator (no FastStore buffer)      |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|                  CORRELATOR & MICRO-BATCH AGGREGATOR                          |
|  - Dual-role O(1) in-memory lookup table (Exporters & Monitored Participants) |
|  - Port normalizer (collapses ephemeral ports 1024-65535 to preserve index)  |
|  - Unmatched exporter candidate discovery (15-minute TTL in FastStore)        |
|  - 1-minute micro-batch accumulator with bulk UPSERT                          |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|                    TIMESCALEDB MULTI-TIER ROLLUPS & PATHS                     |
|  - flow_minute_rollups (Hypertable: 1-day chunk, 1d compression, 90d retain) |
|  - flow_interface_minute_rollups (Hypertable: 7d retain) & hourly (365d)      |
|  - flow_hourly_rollups (30d retain) & flow_daily_rollups (365d, real-time)    |
|  - flow_path_observations (30d retain) & flow_route_changes (90d retain)      |
+-------------------------------------------------------------------------------+
```

---

## 2. Recommended Configuration Best Practices

To ensure maximum telemetry fidelity, eliminate bursty traffic artifacts, and prevent orphan packet discards, network administrators should adhere to these standardized baseline settings on all flow-exporting devices:

### 2.1 Active Flow Timeout (Crucial for 1-Minute Rollups)
* **Recommended Setting:** `60 seconds` (1 minute).
* **Operational Rationale:** Routers and firewalls maintain state for long-running connections (such as multi-gigabyte file transfers, database backups, or video streams). If the active timeout is left at the vendor default of 30 minutes, the router only exports an aggregate byte counter once every 30 minutes, producing misleading, massive bandwidth spikes followed by flatlines. Configuring `active-timeout 60` forces the device to flush flow progress every minute, guaranteeing smooth, continuous, and accurate 1-minute rollups in Ephemres.

### 2.2 Inactive Flow Timeout
* **Recommended Setting:** `15 seconds`.
* **Operational Rationale:** Inactive timeout determines how long the exporter waits before concluding that an idle TCP or UDP conversation has terminated. A 15-second inactive timeout flushes finished flows promptly to the collector while preventing the router's internal flow cache tables from becoming exhausted during high connection rates.

### 2.3 Template Refresh Interval (NetFlow v9 & IPFIX)
* **Recommended Setting:** Every `60 seconds` or every `1,000 packets`.
* **Operational Rationale:** Unlike NetFlow v5 which features a static 48-byte record structure, NetFlow v9 and IPFIX decouple flow schemas using dynamic templates. If an exporting router reboots or switches supervisors without immediately sending template definitions, the collector cannot decode data flowsets. Although Ephemres maintains an in-memory template cache with a 1,800-second TTL and buffers orphan flowsets, configuring a 60-second template refresh interval guarantees zero orphan data drops and instant cold-start recovery.

### 2.4 Export ToS / DSCP for QoS Analytics
* **Recommended Setting:** Include the IPv4 Type-of-Service byte in every exported record: NetFlow v9 field type **5** (`SRC_TOS`) or IPFIX Information Element **5** (`ipClassOfService`). NetFlow v5 always carries it.
* **Operational Rationale:** The QoS / DSCP breakdown on the Bandwidth dashboard, the endpoint flow view, and PDF reports is computed from this field only. Exporters whose template omits it are counted as DSCP 0 (`CS0` / best effort), so the QoS panels show everything as best effort. Check your platform's flow-record or template options for a ToS/DSCP field; the Cisco example below adds `match ipv4 tos`.

### 2.5 Export the Next Hop for Route Augmentation
* **Recommended Setting:** Include the next-hop address in every record: NetFlow v9 field type **15** (`IPV4_NEXT_HOP`) / **62** (`IPV6_NEXT_HOP`), or IPFIX Information Element **15** (`ipNextHopIPv4Address`) / **62** (`ipNextHopIPv6Address`). NetFlow v5 always carries it. Keep the input and output interface fields as well.
* **Operational Rationale:** Traceroute stays Ephemres' route of record. Flows add the path real traffic takes: for every flow to or from a monitored endpoint, Ephemres records the exporter's interfaces and next hop (`flow_path_observations`). With the next hop present:
  * the Topology map marks traceroute links **confirmed by traffic**, flags links where **traffic takes another path**, and names hops that do not answer traceroute (`* * *`);
  * a change of the dominant path, held for 3 minutes, is recorded as a route change and triggers a traceroute of just that endpoint (only while it is UP, at most once per 15 minutes per endpoint and 10 per minute overall);
  * an RCA records which path the endpoint's traffic last took and whether traffic still flowed after probes started failing.

  Without it, Endpoint Detail still lists the interfaces used, but paths cannot be compared with traceroute. A traceroute hop is matched to an exporter by its IP or one of its exporter IP aliases; traceroute shows a router's ingress address, so add interface addresses as aliases where they differ. Tunables in `config.toml` `[flow]`: `route_change_minutes` (default 3) and `route_ecmp_share` (default 0.2: two or more paths each above this share are load balancing, not a change).

### 2.6 Persistent Export Source IP (`Loopback0`)
* **Recommended Setting:** Always bind the exporter source address to a stable virtual interface:
  * Cisco: `source Loopback0`
  * Juniper: `inline-jflow { source-address <Loopback0_IP>; }`
  * Linux: bind to static management IP.
* **Operational Rationale:** When a router has multiple egress uplinks or dynamic routing protocols (BGP/OSPF/ECMP), packet routes can change. If the flow export source is not explicitly pinned to a Loopback IP, the router's egress interface IP will be used as the exporter IP, causing Ephemres to detect spurious "unmatched exporters". Binding to `Loopback0` guarantees deterministic $\mathcal{O}(1)$ correlation.

### 2.7 Router Clock (NTP)
* **Recommended Setting:** NTP on every exporting router, with the clock in UTC or with the timezone set correctly.
* **Operational Rationale:** NetFlow v5/v9 and IPFIX date each flow by the router's own clock. Since v3.3.6 Ephemres checks that clock against the arrival time of every export packet:
  * within 2 minutes, the router's times are used as they are;
  * further out (no NTP, or local time set as UTC), the flow's times are moved by the difference, so the traffic still appears at the right minute. flowd logs the offset once an hour per router: `The clock of flow exporter 10.1.0.1 is 1 h 0 min ahead of this server's. Its flows are stored at the corrected time; set NTP on the router.`
  * a flow start that is still in the future, or more than a day old, is stored at the minute it arrived.

  The correction is only as good as the Ephemres server's own clock, so the server needs NTP too ([Deployment §2](deployment.md)). sFlow carries no flow time and has always been dated by arrival. Fix the router's clock when you see the log line: its own logs and its SNMP traps are dated by the same clock.

---

## 3. Firewall & Port Requirements

Ensure that the Ephemres host permits incoming UDP datagrams from your network infrastructure:

| Protocol | Default Port | Usage | Transport |
| :--- | :--- | :--- | :--- |
| **NetFlow v5 / v9** | `UDP 2055` | Cisco, Mikrotik, Linux, Fortinet flow export | UDP |
| **IPFIX** | `UDP 4739` | IANA standard flow export (Juniper, OVS, VyOS) | UDP |
| **sFlow v5** (v3.3.6) | `UDP 6343` | Sampled packet headers from switches and hosts (Arista, Juniper EX/QFX, Aruba, hsflowd) | UDP |

### UFW Configuration (Ubuntu/Debian)

```bash
# Allow NetFlow v5/v9 from internal subnets
sudo ufw allow from 10.0.0.0/8 to any port 2055 proto udp comment 'NetFlow Ingestion'
sudo ufw allow from 172.16.0.0/12 to any port 2055 proto udp comment 'NetFlow Ingestion'
sudo ufw allow from 192.168.0.0/16 to any port 2055 proto udp comment 'NetFlow Ingestion'

# Allow IPFIX from internal subnets
sudo ufw allow from 10.0.0.0/8 to any port 4739 proto udp comment 'IPFIX Ingestion'
sudo ufw allow from 172.16.0.0/12 to any port 4739 proto udp comment 'IPFIX Ingestion'
sudo ufw allow from 192.168.0.0/16 to any port 4739 proto udp comment 'IPFIX Ingestion'

# Allow sFlow from internal subnets (v3.3.6; upgrade.sh opens 6343 itself when flow ingestion is on)
sudo ufw allow from 10.0.0.0/8 to any port 6343 proto udp comment 'sFlow Ingestion'
```

---

## 4. Router & Device Configuration Examples

### 4.1 Cisco IOS-XE (Flexible NetFlow - NetFlow v9)

```cisco
! 1. Define Flow Record
flow record EPHEMRES-FLOW-RECORD
 match ipv4 source address
 match ipv4 destination address
 match ipv4 protocol
 match ipv4 tos
 match transport source-port
 match transport destination-port
 match interface input
 match interface output
 collect routing next-hop address ipv4
 collect counter bytes long
 collect counter packets long
 collect timestamp sys-uptime first
 collect timestamp sys-uptime last
 collect transport tcp flags
!
! 2. Define Flow Exporter (Point to Ephemres host)
flow exporter EPHEMRES-EXPORTER
 destination 192.168.1.10
 source Loopback0
 transport udp 2055
 export-protocol netflow-v9
 template data timeout 60
!
! 3. Define Flow Monitor
flow monitor EPHEMRES-MONITOR
 record EPHEMRES-FLOW-RECORD
 exporter EPHEMRES-EXPORTER
 cache timeout active 60
 cache timeout inactive 15
!
! 4. Apply to WAN/Core Interfaces
interface GigabitEthernet0/0/0
 ip flow monitor EPHEMRES-MONITOR input
 ip flow monitor EPHEMRES-MONITOR output
```

---

### 4.2 Juniper Junos (inline-jflow - IPFIX)

```junos
services {
    flow-monitoring {
        version-ipfix {
            template EPHEMRES-IPFIX-TEMPLATE {
                flow-active-timeout 60;
                flow-inactive-timeout 15;
                template-refresh-rate {
                    packets 1000;
                    seconds 60;
                }
                ipv4-template;
            }
        }
    }
}
forwarding-options {
    sampling {
        instance EPHEMRES-SAMPLING {
            input {
                rate 1;
            }
            family inet {
                output {
                    flow-server 192.168.1.10 {
                        port 4739;
                        version-ipfix {
                            template EPHEMRES-IPFIX-TEMPLATE;
                        }
                    }
                    inline-jflow {
                        source-address 10.255.255.1;
                    }
                }
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0 {
            family inet {
                sampling {
                    input;
                    output;
                }
            }
        }
    }
}
```

---

### 4.3 Fortinet FortiOS (NetFlow v9)

```fortios
config system netflow
    set collector-ip 192.168.1.10
    set collector-port 2055
    set source-ip 10.1.1.1
    set active-flow-timeout 1
    set inactive-flow-timeout 15
    set template-tx-timeout 1
end

# Enable NetFlow export on firewall policies
config firewall policy
    edit 1
        set netflow-sampler both
    next
end
```

---

### 4.4 Mikrotik RouterOS v7 (Traffic Flow - NetFlow v9)

```routeros
/ip traffic-flow
set enabled=yes active-flow-timeout=1m inactive-flow-timeout=15s

/ip traffic-flow target
add dst-address=192.168.1.10 port=2055 version=9
```

---

### 4.5 Linux Host / Server (softflowd)

Install `softflowd` to export NetFlow directly from a Linux gateway or hypervisor:

```bash
sudo apt-get install -y softflowd

# Run softflowd listening on eth0, exporting NetFlow v9 to Ephemres
sudo softflowd -i eth0 -n 192.168.1.10:2055 -v 9 -m 8192 -t maxlife=60s
```

---

### 4.6 pfSense / OPNsense Firewall (softflowd)

On FreeBSD-based firewalls (pfSense and OPNsense), flow telemetry is provided via the high-performance `softflowd` service:

#### pfSense Configuration:
1. Navigate to **System** -> **Package Manager** -> **Available Packages**.
2. Search for `softflowd` and click **Install**.
3. Navigate to **Services** -> **softflowd**:
   - **Interface:** Select your internal gateway interfaces (e.g., `LAN`, `VLAN10`).
   - **Host:** Enter the Ephemres server IP (e.g., `192.168.1.10`).
   - **Port:** `2055`.
   - **NetFlow Version:** `9`.
   - **Max Flows:** `8192`.
   - **Hop Limit:** `64`.
   - **Tracking Level:** `Full (track IP, protocol, and transport ports)`.
   - **Active Timeout:** `60` seconds.
   - **Inactive Timeout:** `15` seconds.
4. Click **Save** to start exporting flows.

#### OPNsense Configuration:
1. Navigate to **System** -> **Firmware** -> **Plugins**.
2. Search for `os-softflowd` and click **+** to install.
3. Navigate to **Services** -> **Softflowd**:
   - Check **Enable**.
   - **Listen Interfaces:** Select monitored ingress interfaces (`LAN`, `DMZ`).
   - **Export Destination:** Set to `<EPHEMRES_IP>:2055` (e.g., `192.168.1.10:2055`).
   - **NetFlow Version:** `9`.
   - **Active Timeout:** `60` seconds.
   - **Inactive Timeout:** `15` seconds.
4. Click **Apply** to activate.

### 4.7 sFlow v5 (Switches and Hosts, v3.3.6)

sFlow is not NetFlow with another header. A device does not export flows: it exports one in every N packets it forwards (the sampling rate), each as the first bytes of the packet. What that means in Ephemres:

* **Scaling is per sample.** Each sampled packet counts as N packets and its IP length times N bytes, with N taken from the sample itself. Devices with different rates can report to one server, and the global sampling multiplier (Settings → Data collection) is not applied on top.
* **Figures are estimates.** They get better with volume; a conversation of a few packets may not be sampled at all. The Traffic page says so while any exporter sends sampled traffic.
* **What is read:** flow samples (compact and expanded) carrying a raw packet header (Ethernet, IPv4 or IPv6) or the pre-decoded sampled IPv4/IPv6 record, plus the extended router record for the next hop. Counter samples are skipped: interface counters come from SNMP polling ([SNMP guide](snmp-guide.md)).
* **Who sent it:** the exporter is the address the datagrams come from, not the agent address inside them. Enroll or map that address like any other exporter (section 6); datagrams from an unknown address are not read.
* **Time:** sFlow carries no flow time, so samples are dated when they arrive.
* **Port:** UDP 6343 by default (`[flow] sflow_port`, or the sFlow port in Settings). sFlow sent to the NetFlow or IPFIX port is recognised too.

A sampling rate of 1 in 1,024 suits 1 Gb/s links and 1 in 4,096 or more suits 10 Gb/s and above; a lower rate (more samples) is finer and costs the device more. Typical configurations follow. Command names differ between software versions, so check them against your platform's reference.

**Arista EOS**
```text
sflow sample 1024
sflow destination <EPHEMRES_IP> 6343
sflow source-interface Loopback0
sflow run
```

**Juniper EX / QFX**
```text
set protocols sflow collector <EPHEMRES_IP> udp-port 6343
set protocols sflow sample-rate ingress 1024
set protocols sflow source-ip <LOOPBACK_IP>
set protocols sflow interfaces ge-0/0/0.0
```

**Linux host (hsflowd, `/etc/hsflowd.conf`)**
```text
sflow {
  sampling = 1024
  collector { ip = <EPHEMRES_IP> udp = 6343 }
  pcap { dev = eth0 }
}
```

---

## 5. Enabling Flow Telemetry in Ephemres

1. Log in to the Ephemres Web Interface as an **Administrator**.
2. Open **Settings** -> **Data collection**.
3. In the **Network Flow Telemetry Engine (NetFlow, IPFIX & sFlow)** card:
   - Switch the toggle on. This opens the preflight, which checks that FastStore opens and the flow UDP ports are free (it also reports `rmem_max`). When it passes, click **Activate Flow Telemetry**; the badge then reads **FLOW INGESTION ACTIVE** and the port fields appear. Performance Mode is not needed for flows. **Run Diagnostic Preflight** runs the same check on its own.
   - Ensure the ports (NetFlow: `2055`, IPFIX: `4739`, sFlow: `6343`) match what your exporters send to.
   - Click **Save Changes**. Saving runs the preflight again (a failure is refused with its reason), then starts `ephemres-flowd` and enables it at boot.
4. Check that the service is running. The API starts it through the sudo rule `install.sh` and `upgrade.sh` write to `/etc/sudoers.d/ephemres`; without that rule, start it yourself:
   ```bash
   sudo systemctl status ephemres-flowd
   sudo systemctl enable --now ephemres-flowd
   ```

---

## 6. Exporter IP Mapping & Alias Management

When network devices export flows from loopback interfaces, secondary management subnets, or CARP VIPs, Ephemres's automatic correlator tracks them as **Unmatched Exporters**:

1. Open the **Bandwidth** fleet dashboard from the top navigation bar.
2. If unmatched exporters are detected, an alert banner appears:
   > `⚠️ Unmatched Flow Exporter Detected: 10.255.255.1 is streaming NetFlow but is not registered to an endpoint.`
3. Click **Map Exporter IP**:
   - Select the corresponding managed device from the dropdown.
   - Click **Bind Exporter IP**.
4. The secondary IP is added to the endpoint's `flow_exporter_ips` array and immediately synchronized to the in-memory correlator via FastStore / PostgreSQL sync (`channel:registry_sync`).

> **Flows from an unmatched exporter are not stored.** flowd drops packets from any sender that is not an endpoint's primary IP or one of its `flow_exporter_ips` before parsing them, so nobody who can reach UDP 2055/4739 can fill the flow buffer or the database. The sender still shows in the banner, with its flow rate (for IPFIX, packets per second, since IPFIX headers carry no record count). Its traffic appears from the moment it is mapped or enrolled. Flows stored before this change under the placeholder exporter (`00000000-…`) stay until retention removes them.

---

## 7. Verification & Troubleshooting

### Check Daemon Health
```bash
sudo systemctl status ephemres-flowd
sudo journalctl -u ephemres-flowd -n 100 -f
```

### Verify Packet Reception
```bash
# Verify NetFlow packets on UDP 2055
sudo tcpdump -ni any udp port 2055 -c 5

# Verify IPFIX packets on UDP 4739
sudo tcpdump -ni any udp port 4739 -c 5

# Verify sFlow datagrams on UDP 6343
sudo tcpdump -ni any udp port 6343 -c 5
```

### Check What flowd Receives, Stores and Loses
flowd logs a summary every minute. Flows go from the collector to the aggregator in memory, so
the only places flows can be lost are logged there:
```bash
sudo journalctl -u ephemres-flowd --since "-15 min" | grep "last minute"
```
* `last minute N flows received, M rollup rows stored`: normal.
* `LOST N datagrams (kernel socket buffer full: flowd too slow)`: flowd can't keep up with the
  exporters. Raise `net.core.rmem_max` (flowd asks for a 4 MB socket buffer) or reduce what the
  exporters send (sampling).
* `LOST N flows (flow buffer full: <reason>)`: flows held for the database filled
  `flow.buffer_memory_limit_mb`, and new conversations are being dropped. The reason says which:
  * `the database is not accepting rollup writes (<error>)`: PostgreSQL is down or refusing
    writes; check PostgreSQL.
  * `rollup writes are not keeping up with new conversations (the last flush wrote N rows in X s)`:
    the database is up but too slow for the number of new conversations (a single TimescaleDB
    manages roughly 15,000 to 20,000 rollup rows/s). Reduce what the exporters send (sampling,
    fewer exporters) or give the database more resources.
* `rollup writes are falling behind (the last flush wrote N rows in X s, flushes are 10 s apart)`:
  nothing is lost yet, but a flush takes longer than the gap between flushes, so the buffer is
  growing and will fill if this goes on. Same remedies as above.

Since v3.3.6 flowd also says when it leaves something out or corrects it:
```bash
sudo journalctl -u ephemres-flowd --since "-1 hour" | grep -E "left out|refused|clock of flow exporter"
```
* `Flow record from <ip> left out: a value no real flow has (bytes=... packets=... protocol=... ports=...)`:
  a record with more than 2^50 bytes or packets, a protocol above 255 or a port above 65535.
  Logged at most once a minute with the count since flowd started. A steady stream from one
  router points at a broken export template on it (a field declared with the wrong length).
* `Flow rollup write refused for a value in it (...)`, then `Flow rollup row refused by the database and left out: ...`
  and `N flow rollup rows left out at this flush (...); the other M were stored`: the database
  refused a batch for a value in it, and flowd wrote the batch again in pieces, dropping only
  the refused rows. Everything else was stored. Report the logged row if this appears.
* `The clock of flow exporter <ip> is ... ahead of / behind this server's`: see section 2.7.

### Which Data a View Shows
Each Traffic page view, the endpoint Bandwidth & Flow tab and the PDF bandwidth report read a minute, hourly or daily table depending on the window, and the longer-term tables keep less detail (no addresses, and the daily one no endpoints). See [Database §3](database.md#3-4-tier-hierarchical-storage-lifecycle) for what each table keeps and which one each view reads.

### Query Database Rollups
```bash
sudo -u postgres psql -d netmon -c "SELECT bucket, COUNT(*), SUM(bytes) FROM flow_minute_rollups GROUP BY bucket ORDER BY bucket DESC LIMIT 5;"
```
