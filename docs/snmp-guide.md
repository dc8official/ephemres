# Ephemres SNMP Guide (v3.3.6)

SNMP in v3.3.6 is being built in two steps (build plan section 8.1):

1. **Device survey (built).** A command that reads, from one device, everything the planned SNMP
   poller would read: system information, interfaces with their counters, IPv4 addresses, and
   LLDP and CDP neighbours. It shows what each device really answers before the poller's
   scope is fixed.
2. **Poller (step 1 built).** `ephemres-snmpd`, its own service like `ephemres-flowd`.
   - Built: shared, encrypted credential sets; interface names, speeds and up/down status;
     reboot detection.
   - Also built: interface traffic from the counters (step 2); neighbours on the map, in
     root-cause analysis, WAN link-down alerts and link-down re-traces (step 3).

   Settings, credential sets and the endpoint panel are described in the user guide, section 9.
   The design is vendor-neutral (`docs/architecture/snmp-vendor-research.md`): each device's
   capability profile is learned on first contact, not assumed from its vendor.

Everything is hand-written (zero-dependency rule): BER encoding, SNMP v2c and v3, and the v3
security model, using the same OpenSSL binding as the rest of Ephemres.

---

## 1. What the survey supports

| | Supported |
| :--- | :--- |
| Versions | v2c, v3 |
| v3 security levels | noAuthNoPriv, authNoPriv, authPriv |
| v3 authentication | MD5, SHA (SHA-1), SHA-224, SHA-256, SHA-384, SHA-512 (RFC 3414, RFC 7860) |
| v3 privacy | `aes` (AES-128, RFC 3826); `aes192` / `aes256` (net-snmp-style keys: net-snmp `AES-256`, FortiGate `aes256`); `aes192c` / `aes256c` (Cisco-style keys: Cisco, HPE Aruba AOS-S, FortiGate `aes256cisco`, net-snmp `AES-256-C`); `3des`; `des` (only where the server's OpenSSL still provides it) |
| v3 extras | One password for authentication and privacy (`--priv-same`, or Enter at the privacy prompt: Meraki and UniFi need this); a context name (`--context`) |
| Not supported | v1; traps; SET |
| MIBs read | SNMPv2-MIB system group, HOST-RESOURCES `hrSystemUptime`, SNMP-FRAMEWORK engine boots/time, IF-MIB `ifTable` + `ifXTable` (with `ifCounterDiscontinuityTime`), IP-MIB `ipAddrTable`, LLDP-MIB (LLDP-V2-MIB when LLDP-MIB is empty), CISCO-CDP-MIB `cdpCacheTable` |

AES-192/256 has no standard: vendors stretch the key in two incompatible ways, so the survey
offers both. Each family's way is listed in `docs/architecture/snmp-vendor-research.md`. If one
gets "no reply ... it answered SNMPv3 discovery", try the other.

It only reads: it never changes anything on the device.

## 2. Running it

Run it on the Ephemres server: the poller will sit at the same place in the network, so the
same firewall rules and ACLs apply.

```bash
cd /opt/ephemres
sudo -u ephemres env PYTHONPATH=/opt/ephemres/backend:/opt/ephemres \
    /opt/ephemres/venv/bin/python -m monitoring.snmp.survey 10.20.0.1 --json /tmp/branch1-mx.json
```

SNMPv3:

```bash
sudo -u ephemres env PYTHONPATH=/opt/ephemres/backend:/opt/ephemres \
    /opt/ephemres/venv/bin/python -m monitoring.snmp.survey 10.20.0.1 \
    --version 3 --user ephemres --auth sha --priv aes --json /tmp/branch1-mx.json
```

- **Secrets are never typed on the command line,** where shell history and `ps` would keep
  them. The survey asks for the community or the v3 passwords. To script it, set
  `EPHEMRES_SNMP_COMMUNITY`, `EPHEMRES_SNMP_AUTH_PASSWORD` and `EPHEMRES_SNMP_PRIV_PASSWORD`
  instead. The report never contains them.
- **One password for both:** `--priv-same`, or press Enter at the privacy prompt.
- **Options:** `--context` (SNMPv3 context name), `--port` (default 161), `--timeout` seconds per try (2), `--retries` (1),
  `--max-repetitions` per GETBULK (20), `--no-bulk` (GETNEXT only, for agents with a broken
  GETBULK), `--interval` seconds between the two counter reads (10; `0` skips that part).
- **Exit status:** `0` when the device answered, `2` when it did not or refused the credentials.

The survey reads the octet counters twice, `--interval` seconds apart, to show they move. That
is the only waiting it does: a typical device takes a few seconds plus the interval.

## 3. Reading the result

The text report shows the system information, one line per interface (name, alias, speed,
admin/oper status, whether 64-bit counters exist, error counters), which interfaces moved
traffic during the interval, the device's IPv4 addresses, the LLDP and CDP neighbours, and
**Findings**:

- **64-bit counters on N of M interfaces.** The poller needs them on WAN links: a 32-bit octet
  counter wraps in under 6 minutes at 100 Mb/s.
- **ifHighSpeed / ifName / ifAlias.** Speed above 4 Gb/s, stable interface names (the poller
  keys interfaces on the name as well as the number, because numbers can change after a
  reboot), and the description set on the device.
- **Reboot clocks answered.** `sysUpTime` resets when the SNMP agent restarts; `hrSystemUptime`
  and engine boots show a real reboot.
- **Counter reset marker.** `ifCounterDiscontinuityTime` lets the poller drop a traffic sample
  when the device reset an interface's counters.
- **LLDP / CDP.** Neighbours found, and which LLDP MIB answered, or "not answered".
- **Columns the device never answered.** Some agents drop a whole request when it includes a
  table they cannot serve. The survey then asks for each column on its own, so only that
  column is lost, and lists it here.
- **Survey cost.** Requests, seconds, timeouts: what one poll of this device will cost.

`--json FILE` writes the full report. It holds the device's names, addresses, interfaces and
neighbours, but no secrets. It ends with a **profile**: what the poller would remember about
the device, so that regular polls ask only for what it answers, and in a way that works:
- which groups answered;
- whether GETBULK works, and the repetitions that worked;
- the columns to ask for one at a time;
- the reply time.

## 4. Turning SNMP on, read-only

Use a read-only community or v3 user, and allow only the Ephemres server's address. Prefer
v3 with SHA and AES. Menu names change between firmware versions, so check your device's own
documentation for the exact place.

**Cisco IOS / IOS XE**

```
snmp-server group EPHEMRES v3 priv
snmp-server user ephemres EPHEMRES v3 auth sha <auth-password> priv aes 128 <priv-password>
snmp-server ifindex persist
lldp run
```

(`snmp-server community <community> RO <acl>` for v2c.) `ifindex persist` keeps interface
numbers across reboots. CDP is on by default; `lldp run` turns LLDP on.

**MikroTik RouterOS**

```
/snmp set enabled=yes
/snmp community add name=ephemres security=private authentication-protocol=SHA1 \
    authentication-password=<auth-password> encryption-protocol=AES encryption-password=<priv-password> \
    addresses=<ephemres-server>/32
/ip neighbor discovery-settings set discover-interface-list=all protocol=cdp,lldp,mndp
```

In RouterOS a v3 user is a community with `security=private`. Remove or restrict the default
`public` community.

**Cisco Meraki (MX, MS, MR)**

Local polling of the device itself is set per network under **Network-wide → Configure → General
→ Reporting** (v1/v2c community or v3 user). This is not the organisation-wide SNMP, which the
Meraki cloud serves and which does not show the device's own interfaces. Points from Meraki's
documentation:

- v3 uses SHA-1 or SHA-2 with DES or AES-128, and **one password for both**. Give the survey the
  same password at both prompts.
- Devices refuse polling from non-local networks until you allow it. On an MX, add the Ephemres
  server under **Security & SD-WAN → Configure → Firewall → WAN appliance services** (SNMP).
- MS switches and MR access points **do not answer at all** for data they do not support, so some
  parts of the survey may report "no reply" while the rest works.

**Sophos Firewall**

Enable the SNMP agent and add a community or v3 user under **Administration → SNMP**. Then allow
SNMP for the zone the Ephemres server is in, under **Administration → Device access**. Without
that rule the firewall drops the requests silently, and the survey reports "no reply". Sophos v3
offers MD5, SHA-256 or SHA-512 (there is no plain SHA-1) with AES or DES: use `--auth sha256` or
`--auth sha512` with `--priv aes`. Third-party guides report a 12-character minimum for the
passwords.

**Other families**

Ubiquiti UniFi, Juniper, Aruba, Cumulus, Fortinet and Palo Alto are covered in
`docs/architecture/snmp-vendor-research.md`: which v3 options each offers, where SNMP must be
bound to a management VRF, and known quirks.

## 5. When it does not answer

| Survey says | Usually means |
| :--- | :--- |
| `no reply ... after 2 tries` | Wrong community (devices stay silent rather than refusing), SNMP off, an ACL or firewall in the way, or the agent bound to another address |
| `no reply ...; it answered SNMPv3 discovery, so ...` | The device is there and speaks v3, but ignored the request: the user, protocols or passwords do not match. RouterOS answers every v3 mistake this way, and Cisco answers a wrong privacy protocol this way |
| `ICMP port unreachable` | Nothing listens on that port |
| `unknown user name` | The v3 user does not exist on the device (Cisco IOS; RouterOS stays silent instead) |
| `wrong digest` | Wrong authentication password or protocol (MD5 vs SHA) |
| `unknown context` | The `--context` name does not exist on the device; leave it empty unless the device needs one |
| `unsupported security level` | No such user at this level, or the device cannot do v3 privacy at all. An IOS router without the security licence answers every authPriv request this way: use authNoPriv (`--auth` without `--priv`) |
| `decryption error` | Wrong privacy password or protocol (AES vs DES) |
| `replies failed the v3 authentication check` | Replies came back but did not verify: usually the authentication protocol differs |
| `SNMPv3 passwords must be at least 8 characters` | RFC 3414's minimum; devices refuse shorter ones |

## 6. Lab results (GNS3, 2026-11-12)

Surveyed in a throwaway GNS3 lab: a Cisco IOSv 15.9(3)M2 router, a Cisco vIOS-L2 switch and a
MikroTik CHR 7.8, cabled to each other and to the server's network. Meraki and Sophos are still
to be surveyed on real branch hardware.

| | IOSv router | vIOS-L2 switch | RouterOS CHR 7.8 |
| :--- | :--- | :--- | :--- |
| v2c | yes | yes | yes |
| v3 authPriv | **no**: the image has no SNMPv3 privacy (security licence); authNoPriv works | SHA+AES and MD5+DES | SHA+AES |
| 64-bit counters | all physical ports (not Null0) | all physical ports and VLAN interfaces | all ports |
| Speed | `ifHighSpeed` | `ifHighSpeed` | `ifHighSpeed` (virtual ports report 10 Gb/s, unplugged ports 0) |
| `ifAlias` | the interface description | the interface description | empty |
| Reboot clocks | `sysUpTime`, engine boots | `sysUpTime`, engine boots | `sysUpTime`, `hrSystemUptime` |
| LLDP | neighbours with management addresses | neighbours with management addresses | neighbours, but no management addresses |
| CDP | neighbours | neighbours | speaks CDP but has no CDP MIB |
| One full read | 11 to 13 requests, under 0.1 s | 13 to 15 requests, under 0.1 s | 15 to 17 requests, about 4 s (see quirks) |

Quirks found, and what the code does about them:

- **RouterOS drops any request that asks for `lldpRemManAddrTable` together with other
  columns,** silently. Walks now fall back to one column at a time after a multi-column request
  goes unanswered. Each read of such a device still pays the timeout once (about 4 s with the
  defaults): the poller should remember, per device, which columns to walk alone.
- **RouterOS returns extra varbinds (and `noSuchObject`) in GETNEXT replies.** Walks ignore what
  does not belong to the column asked for.
- **Wrong v3 settings are reported differently by each vendor:** see section 5. RouterOS never
  reports them, so the survey and the poller explain a v3 timeout after a successful discovery.
- **Port names differ between protocols:** CDP gives `GigabitEthernet0/1`, LLDP and `ifName` give
  `Gi0/1`. Matching a neighbour's port to that device's interfaces has to try `ifName` and
  `ifDescr`. LLDP's local port numbers are not interface numbers either.
- GETBULK cut the requests for a full read from 33 (GETNEXT) to 13.

## 7. The poller (ephemres-snmpd)

- **Turning it on.**
  1. Add a credential set in Settings → Data collection; use **Test** first.
  2. Assign the set to endpoints: in the endpoint form, or **Set SNMP** in the dashboard
     selection bar.
  3. Switch **SNMP polling** on.

  The service is always running and idles while SNMP is off.
- **Each poll** (every 30 s to 5 min, default 1 min):
  - one GET of the device's clocks (`sysUpTime`, engine boots, `ifTableLastChange`);
  - one walk of interface admin/oper status.

  A clock going back is a reboot: the device is probed again. `ifTableLastChange` moving re-reads
  the interface list.
- **Probe:** the survey's reads, on first contact, after a reboot, on **Probe now**, after 3
  failed polls in a row, and daily. It refreshes the device profile, which says what to ask for
  and how: GETBULK or GETNEXT, and which columns to ask for one at a time. A device that
  silently drops one table therefore pays the timeout once, not on every poll.
- **Status on the endpoint page:** `ok`, `partial` (some columns never answer), `no_reply`,
  `auth_failed` (with the device's reason, or "it answered SNMPv3 discovery, so the user name,
  protocols or passwords probably do not match"), `error`.
- **Interface names and speeds:** the typed name, else the device's description (`ifAlias`),
  else `ifName`; the typed speed, else `ifHighSpeed`. When interface numbers change after a
  reboot, typed entries move with their interface (matched by unique name), and the move is
  logged.
- **Traffic (step 2):**
  - Every poll also reads each interface's counters, in the same walk as the status:
    - octets and packets, 64-bit where the device has them, 32-bit otherwise;
    - errors and discards;
    - `ifCounterDiscontinuityTime`.
  - The difference from the last reading becomes per-minute traffic in
    `snmp_interface_minute_rollups`, timed by the device's own uptime clock and shared across
    the minutes it spans.
  - A difference is dropped when:
    - the counters were reset (discontinuity time changed, a 64-bit counter went back, the
      device rebooted);
    - the readings are more than three poll intervals apart;
    - it would mean more than 1.5 x the link speed.

    A 32-bit counter that went back wrapped, and is added up.
  - The readings are saved, so a restart of snmpd loses nothing.
  - 32-bit-only devices are fine on slow links; at 100 Mb/s a 32-bit octet counter wraps in
    under 6 minutes, so keep the poll interval short there.
- **Neighbours (step 3):**
  - Read on each probe, and with each inventory read when the device answered LLDP or CDP at its
    probe; the device's IPv4 addresses and sysName are read at the same time.
  - Each neighbour is matched to a monitored endpoint by, in order:
    1. any of its management addresses (an endpoint's address, its exporter addresses, or an
       address its SNMP reported);
    2. its chassis MAC against the MACs of known interfaces;
    3. its system name without the domain.
  - When a device's matched neighbours change, every API worker is told to rebuild its map.
- **Link events (step 3):** the alert leader claims interface up/down events every 10 seconds,
  each exactly once.
  - WAN links send `LINK_DOWN` / `LINK_UP` to channels that ticked them.
  - Every link change asks the scheduler leader to re-trace the routes through that device: the
    transit trigger's wait, its check that the endpoint is still up, and its cooldown all apply.
  - Events older than 15 minutes (a leader that was down a while) are marked handled without
    acting.
- **Load:** one request at a time per device, at most 32 devices at once, polls spread over the
  interval, and a database pool of 5.
