"""ephemres-snmpd's work: probe, inventory and status for every endpoint with a credential set.

v3.3.6 build plan 8.3, step 1. One DevicePoller per endpoint keeps its SnmpClient open (an
SNMPv3 engine is discovered once and its clock followed) and its learned profile, so polls
ask only for what the device answers, in a way that works (profile.py). SnmpPoller schedules
them: each device has its own offset in the interval, so polls spread over the minute instead
of firing together, and at most MAX_CONCURRENT devices are worked on at once.

Per device, every poll interval:
- the clocks: sysUpTime, engine boots, ifTableLastChange. A clock going back is a reboot: the
  device is probed again. ifTableLastChange moving means interfaces were added, removed or
  renamed: the inventory is read again.
- interface admin and oper status. A change is an interface event (up / down).
The full probe (the survey's reads, read_device) runs on first contact, after a reboot, when an
admin asks (Probe now), after FAILURES_BEFORE_PROBE failed polls in a row, and daily; it also
rewrites the inventory. Otherwise the inventory is re-read every INVENTORY_SECONDS.

Step 2 (counters): every poll also reads the interface traffic counters, together with the
status in one walk. Deltas between two readings become bytes, packets, errors and discards per
minute in snmp_interface_minute_rollups (see counter_delta and spread for the rules); the
Traffic page, WAN totals, traffic alerts and the PDF prefer these over flow figures for the
interfaces that have them (services/interface_traffic.py).

Step 3 (neighbours): each probe, and each inventory read when the device answers LLDP or CDP,
also reads its neighbours and its own addresses; they are matched to monitored endpoints
(match_neighbour) and stored in snmp_neighbours, which places switches on the map
(services/topology.py) and feeds root-cause analysis. Interface up/down events are acted on by
the alert leader (alerts, re-traces), not here.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from monitoring.snmp import ber, mib
from monitoring.snmp.client import SnmpClient, SnmpError, SnmpSecurityError, SnmpTimeout
from monitoring.snmp.profile import DeviceProfile
from monitoring.snmp.survey import Survey, read_device, text_of

logger = logging.getLogger("ephemres.snmpd")

MAX_CONCURRENT = 32
INVENTORY_SECONDS = 900
PROFILE_SECONDS = 86400
FAILURES_BEFORE_PROBE = 3
TARGET_RELOAD_SECONDS = 30
EVENT_RETENTION_DAYS = 90

IF_TABLE_LAST_CHANGE = ber.parse_oid("1.3.6.1.2.1.31.1.5.0")
SYS_UPTIME = mib.SYSTEM["sysUpTime"]
ENGINE_BOOTS = mib.SYSTEM["snmpEngineBoots"]
STATUS_COLUMNS = {"ifAdminStatus": mib.IF_TABLE["ifAdminStatus"], "ifOperStatus": mib.IF_TABLE["ifOperStatus"]}
DISCONTINUITY = mib.IF_X_TABLE["ifCounterDiscontinuityTime"]

# Per-minute amounts, in the order of snmp_interface_minute_rollups' columns.
FIELDS = ("in_bytes", "out_bytes", "in_packets", "out_packets", "in_errors", "out_errors", "in_discards", "out_discards")
COUNTER32 = 2 ** 32
MAX_GAP_INTERVALS = 3  # a delta over a longer gap says too little about any one minute
RATE_SLACK = 1.5  # a delta above link speed x this is a counter glitch, not traffic


@dataclass
class Sample:
    """One reading of an interface's counters."""
    at: float  # wall clock, epoch seconds: which minutes the delta belongs to
    uptime: Optional[int]  # the device's sysUpTime (1/100 s) at the reading: the time base
    disc: Optional[int]  # ifCounterDiscontinuityTime
    hc: bool  # 64-bit octets and packets
    values: List[Optional[int]]  # FIELDS order

    def to_json(self) -> List[Any]:
        return [self.at, self.uptime, self.disc, self.hc, self.values]

    @classmethod
    def from_json(cls, raw: Any) -> Optional["Sample"]:
        try:
            at, uptime, disc, hc, values = raw
            return cls(float(at), uptime, disc, bool(hc), list(values))
        except (TypeError, ValueError):
            return None


def counter_delta(prev: Sample, cur: Sample, interval: float, speed_mbps: Optional[int]) -> Optional[List[int]]:
    """The amounts between two readings, or None when they cannot be trusted:
    - the counters were reset (ifCounterDiscontinuityTime changed, a 64-bit counter went back);
    - the readings are too far apart (more than MAX_GAP_INTERVALS polls) or not in order;
    - the octets imply more than RATE_SLACK x the link speed (a glitch, or two 32-bit wraps).
    A 32-bit counter that went back wrapped once (Counter32 rolls over at 2^32): that is added.
    The device's own uptime is the time base when it moved forward, else our clock."""
    if prev.disc is not None and cur.disc is not None and prev.disc != cur.disc:
        return None
    if prev.hc != cur.hc:
        return None
    wall = cur.at - prev.at
    if prev.uptime is not None and cur.uptime is not None and cur.uptime > prev.uptime:
        elapsed = (cur.uptime - prev.uptime) / 100.0
    else:
        elapsed = wall
    if elapsed <= 0 or wall <= 0 or wall > MAX_GAP_INTERVALS * interval + 30:
        return None
    amounts: List[int] = []
    for i, (a, b) in enumerate(zip(prev.values, cur.values)):
        if a is None or b is None:
            amounts.append(0)
            continue
        d = b - a
        if d < 0:
            # Octets and packets are 64-bit when hc (they do not wrap in practice: going back
            # means a reset); errors and discards are always 32-bit.
            if cur.hc and i < 4:
                return None
            d += COUNTER32
        amounts.append(d)
    if speed_mbps:
        rate = max(amounts[0], amounts[1]) * 8 / elapsed
        if rate > speed_mbps * 1_000_000 * RATE_SLACK:
            return None
    return amounts


def spread(start: float, end: float, amounts: List[int]) -> Dict[datetime, List[int]]:
    """Shares a delta between the minutes its readings span, by time in each. Whole numbers:
    the rounding left over goes to the last minute, so the totals stay exact."""
    out: Dict[datetime, List[int]] = {}
    if end <= start:
        return out
    total = end - start
    minute = int(start // 60) * 60
    given = [0] * len(amounts)
    buckets = []
    while minute < end:
        overlap = min(end, minute + 60) - max(start, minute)
        if overlap > 0:
            buckets.append((minute, overlap))
        minute += 60
    for n, (minute, overlap) in enumerate(buckets):
        if n == len(buckets) - 1:
            share = [a - g for a, g in zip(amounts, given)]
        else:
            share = [int(a * overlap / total) for a in amounts]
            given = [g + x for g, x in zip(given, share)]
        if any(share):
            out[datetime.fromtimestamp(minute, tz=timezone.utc)] = share
    return out


@dataclass
class Target:
    """An endpoint to poll, as the database describes it right now."""
    endpoint_id: str
    address: str
    name: str
    credential_id: str
    credential_version: str  # the set's updated_at: a change rebuilds the client
    credentials: Any  # monitoring.snmp.client CommunityAuth / UserAuth
    port: int
    timeout: float
    retries: int
    probe_requested_at: Optional[datetime] = None


@dataclass
class InterfaceRow:
    if_index: int
    if_name: Optional[str]
    if_descr: Optional[str]
    if_alias: Optional[str]
    if_type: Optional[int]
    speed_mbps: Optional[int]
    mac: Optional[str]
    admin_status: Optional[str]
    oper_status: Optional[str]
    last_change: Optional[int]
    hc_counters: bool

    @property
    def label(self) -> str:
        return self.if_name or self.if_descr or f"if{self.if_index}"


def interface_rows(survey_interfaces: List[Dict[str, Any]]) -> List[InterfaceRow]:
    """The survey's interface entries as rows for snmp_interfaces."""
    rows = []
    for r in survey_interfaces:
        speed = r.get("ifHighSpeed")
        if not speed and r.get("ifSpeed"):
            # ifSpeed is bits per second and tops out at 4.29 Gb/s; ifHighSpeed is Mb/s.
            speed = r["ifSpeed"] // 1_000_000
        mac = r.get("ifPhysAddress")
        rows.append(InterfaceRow(
            if_index=int(r["ifIndex"]),
            if_name=text_of(r.get("ifName")) or None,
            if_descr=text_of(r.get("ifDescr")) or None,
            if_alias=text_of(r.get("ifAlias")) or None,
            if_type=r.get("ifType"),
            speed_mbps=int(speed) if speed else None,
            mac=":".join(f"{b:02x}" for b in mac) if isinstance(mac, bytes) and mac else None,
            admin_status=mib.IF_ADMIN_STATUS.get(r.get("ifAdminStatus")),
            oper_status=mib.IF_OPER_STATUS.get(r.get("ifOperStatus")),
            last_change=r.get("ifLastChange"),
            hc_counters="ifHCInOctets" in r,
        ))
    return rows


def renumbered(old: Dict[int, str], new: Dict[int, str]) -> List[Tuple[int, int, str]]:
    """(old index, new index, name) for interfaces whose number changed but whose name did not.

    Interface numbers may change after a reboot (only some devices keep them), while names do
    not. Only names that are unique on both sides count, so two ports that share a description
    are never confused."""
    def unique(m: Dict[int, str]) -> Dict[str, int]:
        seen: Dict[str, List[int]] = {}
        for idx, name in m.items():
            if name:
                seen.setdefault(name, []).append(idx)
        return {name: idxs[0] for name, idxs in seen.items() if len(idxs) == 1}

    before, after = unique(old), unique(new)
    return sorted((before[n], after[n], n) for n in before.keys() & after.keys() if before[n] != after[n])


def remap_aliases(aliases: Dict[str, Any], moves: List[Tuple[int, int, str]]) -> Tuple[Dict[str, Any], List[Tuple[int, int, str]]]:
    """Moves manual interface entries (flow_interface_aliases) with their interface. A move is
    skipped when the new number already has its own manual entry that is not moving away."""
    moving_from = {str(o) for o, _, _ in moves}
    result = {k: v for k, v in aliases.items() if k not in moving_from}
    done = []
    for old_idx, new_idx, name in moves:
        entry = aliases.get(str(old_idx))
        if entry is None:
            continue
        if str(new_idx) in result:
            # Its slot is taken by an entry that stays: keep both where they were.
            result[str(old_idx)] = entry
            continue
        result[str(new_idx)] = entry
        done.append((old_idx, new_idx, name))
    return result, done


def norm_mac(value: Any) -> Optional[str]:
    """12 lower-case hex digits for anything that looks like a MAC ("0C:7D:74:B4:00:00",
    "0c7d.74b4.0000", "0c-7d-..."), else None. LLDP chassis IDs come in all of these."""
    if not value:
        return None
    hexdigits = "".join(ch for ch in str(value).lower() if ch in "0123456789abcdef")
    cleaned = str(value).lower().replace(":", "").replace("-", "").replace(".", "").replace(" ", "")
    return hexdigits if len(hexdigits) == 12 and cleaned == hexdigits else None


def norm_name(value: Any) -> Optional[str]:
    """A device name without its domain, lower-case: "abj-sw1.branch.local" -> "abj-sw1".
    Addresses are left whole (an IPv4 address is not a name with a domain)."""
    if not value:
        return None
    name = str(value).strip().lower()
    if not name:
        return None
    parts = name.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return name
    return parts[0] or None


@dataclass
class MatchIndex:
    """What identifies each monitored endpoint, for matching the neighbours devices report."""
    addresses: Dict[str, str] = field(default_factory=dict)  # IP -> endpoint id
    macs: Dict[str, str] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)


def match_neighbour(row: Dict[str, Any], index: MatchIndex, self_id: str) -> Tuple[Optional[str], Optional[str]]:
    """(endpoint id, how) for a neighbour, trying the strongest evidence first: a management
    address, then the chassis MAC, then the system name. Never the device itself."""
    for addr in row.get("remote_addresses") or []:
        ep = index.addresses.get(str(addr))
        if ep and ep != self_id:
            return ep, "address"
    mac = norm_mac(row.get("remote_chassis"))
    if mac and index.macs.get(mac) not in (None, self_id):
        return index.macs[mac], "mac"
    name = norm_name(row.get("remote_name"))
    if name and index.names.get(name) not in (None, self_id):
        return index.names[name], "name"
    return None, None


def neighbour_rows(lldp: Optional[Dict[str, Any]], cdp: Optional[Dict[str, Any]],
                   interfaces: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The survey's LLDP and CDP neighbours as snmp_neighbours rows. LLDP-MIB's local port is a
    port number, not an ifIndex, so it is matched to an interface by its description or name."""
    by_label: Dict[str, int] = {}
    for idx, info in interfaces.items():
        for key in ("name", "descr", "alias"):
            if info.get(key):
                by_label.setdefault(str(info[key]).lower(), idx)
    rows: List[Dict[str, Any]] = []
    if lldp and lldp.get("answered"):
        protocol = "lldp-v2" if lldp.get("mib") == "LLDP-V2-MIB" else "lldp"
        for nb in lldp.get("neighbours") or []:
            local_idx = nb.get("local_ifIndex") or by_label.get(str(nb.get("local_port") or "").lower())
            rows.append({
                "protocol": protocol, "local_if_index": local_idx, "local_port": nb.get("local_port") or None,
                "remote_name": nb.get("remote_name") or None, "remote_chassis": nb.get("remote_chassis") or None,
                "remote_port": nb.get("remote_port") or None, "remote_port_desc": nb.get("remote_port_desc") or None,
                "remote_platform": (nb.get("remote_description") or None),
                "remote_addresses": [a for a in nb.get("remote_addresses") or [] if a],
            })
    if cdp and cdp.get("answered"):
        for nb in cdp.get("neighbours") or []:
            idx = nb.get("local_ifIndex")
            info = interfaces.get(idx) or {}
            rows.append({
                "protocol": "cdp", "local_if_index": idx, "local_port": info.get("name"),
                "remote_name": nb.get("remote_name") or None, "remote_chassis": None,
                "remote_port": nb.get("remote_port") or None, "remote_port_desc": None,
                "remote_platform": nb.get("remote_platform") or None,
                "remote_addresses": [nb["remote_address"]] if nb.get("remote_address") else [],
            })
    for r in rows:
        r["nkey"] = "|".join(str(x or "") for x in (
            r["protocol"], r["local_if_index"] or r["local_port"], r["remote_chassis"] or r["remote_name"], r["remote_port"]))[:600]
        for key in ("local_port", "remote_name", "remote_chassis", "remote_port", "remote_port_desc", "remote_platform"):
            if r[key] is not None:
                r[key] = str(r[key])[:255]
    # The same neighbour reported twice in one table (a device listing a port twice): keep one.
    unique: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        unique.setdefault(r["nkey"], r)
    return list(unique.values())


class Store:
    """The poller's reads and writes, kept in one place (text() SQL, as the rest of monitoring)."""

    def __init__(self, session_factory: Callable[[], Any],
                 announce: Optional[Callable[[str, str], Awaitable[None]]] = None):
        self.session_factory = session_factory
        # Called when a device's matched neighbours change, so every API worker rebuilds its map
        # (the daemon passes a broker publish; tests leave it out).
        self.announce = announce

    async def load_targets(self) -> List[Target]:
        from app.core.orm import text
        from app.services.snmp_credentials import _COLUMNS, _from_row

        cols = ", ".join("c." + c for c in _COLUMNS)
        async with self.session_factory() as db:
            res = await db.execute(text(f"""
                -- Aliased: ORM rows are keyed by column name, and the set's id would collide.
                SELECT e.id AS ep_id, host(e.ip_address) AS ep_address, e.hostname AS ep_name,
                       s.probe_requested_at AS ep_probe_requested_at, {cols}
                FROM endpoints e
                JOIN snmp_credentials c ON c.id = e.snmp_credential_id
                LEFT JOIN snmp_device_state s ON s.endpoint_id = e.id
                WHERE e.endpoint_status = 'ACTIVE'
            """))
            targets = []
            for row in res.fetchall():
                row = tuple(row)  # ORM rows index but do not slice
                cred = _from_row(row[4:])
                try:
                    creds = cred.client_credentials()
                except Exception as exc:  # a broken set must not stop the others
                    logger.warning("snmpd: credential set %s unusable: %s", cred.name, exc)
                    continue
                targets.append(Target(
                    endpoint_id=str(row[0]), address=row[1], name=row[2], credential_id=str(cred.id),
                    credential_version=str(cred.updated_at), credentials=creds, port=cred.port,
                    timeout=cred.timeout_ms / 1000, retries=cred.retries, probe_requested_at=row[3],
                ))
            return targets

    async def load_state(self, endpoint_id: str) -> Dict[str, Any]:
        from app.core.orm import text

        async with self.session_factory() as db:
            row = (await db.execute(text("""
                SELECT profile, sys_uptime, engine_boots, if_table_last_change, last_probe_at, inventory_at, counters
                FROM snmp_device_state WHERE endpoint_id = CAST(:id AS uuid)
            """), {"id": endpoint_id})).fetchone()
            ifs = (await db.execute(text("""
                SELECT if_index, if_name, if_descr, admin_status, oper_status, speed_mbps FROM snmp_interfaces
                WHERE endpoint_id = CAST(:id AS uuid)
            """), {"id": endpoint_id})).fetchall()
        keys = ("profile", "sys_uptime", "engine_boots", "if_table_last_change", "last_probe_at", "inventory_at", "counters")
        state = dict(zip(keys, row)) if row else {}
        state["interfaces"] = {int(r[0]): {"name": r[1] or r[2], "admin": r[3], "oper": r[4], "speed": r[5]} for r in ifs}
        import json
        for key in ("profile", "counters"):
            if isinstance(state.get(key), str):
                state[key] = json.loads(state[key])
        return state

    async def save_state(self, endpoint_id: str, **fields: Any) -> None:
        from app.core.orm import text
        import json

        cols = list(fields)
        if "profile" in fields and fields["profile"] is not None:
            fields["profile"] = json.dumps(fields["profile"])
        if "counters" in fields and fields["counters"] is not None:
            fields["counters"] = json.dumps(fields["counters"])
        casts = {"profile": "CAST(:profile AS jsonb)", "counters": "CAST(:counters AS jsonb)"}
        values = ", ".join(casts.get(c, f":{c}") for c in cols)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
        async with self.session_factory() as db:
            await db.execute(text(f"""
                INSERT INTO snmp_device_state (endpoint_id, {", ".join(cols)}, updated_at)
                VALUES (CAST(:endpoint_id AS uuid), {values}, NOW())
                ON CONFLICT (endpoint_id) DO UPDATE SET {updates}, updated_at = NOW()
            """), {"endpoint_id": endpoint_id, **fields})
            await db.commit()

    async def save_inventory(self, endpoint_id: str, rows: List[InterfaceRow], events: List[Tuple[Optional[int], Optional[str], str, Optional[str]]],
                             moves: List[Tuple[int, int, str]]) -> int:
        """Replaces the endpoint's interface list, records events, and moves manual interface
        entries that followed a renumbered interface. Returns how many entries moved."""
        from app.core.orm import text
        import json

        moved = 0
        async with self.session_factory() as db:
            await db.execute(text("DELETE FROM snmp_interfaces WHERE endpoint_id = CAST(:id AS uuid)"), {"id": endpoint_id})
            for r in rows:
                await db.execute(text("""
                    INSERT INTO snmp_interfaces (endpoint_id, if_index, if_name, if_descr, if_alias, if_type, speed_mbps,
                                                 mac, admin_status, oper_status, last_change, hc_counters)
                    VALUES (CAST(:id AS uuid), :if_index, :if_name, :if_descr, :if_alias, :if_type, :speed_mbps,
                            :mac, :admin_status, :oper_status, :last_change, :hc_counters)
                """), {"id": endpoint_id, **r.__dict__})
            if moves:
                cur = (await db.execute(text("SELECT flow_interface_aliases FROM endpoints WHERE id = CAST(:id AS uuid)"),
                                        {"id": endpoint_id})).scalar() or {}
                if isinstance(cur, str):
                    cur = json.loads(cur)
                new_aliases, done = remap_aliases(dict(cur), moves)
                if done:
                    await db.execute(text("UPDATE endpoints SET flow_interface_aliases = CAST(:a AS jsonb), updated_at = NOW() WHERE id = CAST(:id AS uuid)"),
                                     {"a": json.dumps(new_aliases), "id": endpoint_id})
                    moved = len(done)
            await self._insert_events(db, endpoint_id, events)
            await db.commit()
        return moved

    async def save_status(self, endpoint_id: str, changes: Dict[int, Tuple[Optional[str], Optional[str]]],
                          events: List[Tuple[Optional[int], Optional[str], str, Optional[str]]]) -> None:
        from app.core.orm import text

        async with self.session_factory() as db:
            for idx, (admin, oper) in changes.items():
                await db.execute(text("""
                    UPDATE snmp_interfaces SET admin_status = :admin, oper_status = :oper, updated_at = NOW()
                    WHERE endpoint_id = CAST(:id AS uuid) AND if_index = :idx
                """), {"id": endpoint_id, "idx": idx, "admin": admin, "oper": oper})
            await self._insert_events(db, endpoint_id, events)
            await db.commit()

    async def match_index(self, db: Any) -> MatchIndex:
        from app.core.orm import text

        index = MatchIndex()
        for ep, ip, exporter_ips, hostname in (await db.execute(text("""
            SELECT id, host(ip_address), flow_exporter_ips, hostname FROM endpoints WHERE endpoint_status != 'DELETED'
        """))).fetchall():
            for addr in [ip] + [str(a).split("/")[0] for a in (exporter_ips or [])]:
                if addr:
                    index.addresses.setdefault(str(addr), str(ep))
            if norm_name(hostname):
                index.names.setdefault(norm_name(hostname), str(ep))
        for ep, addresses, sys_name in (await db.execute(text("""
            SELECT s.endpoint_id, s.addresses, s.sys_name FROM snmp_device_state s
            JOIN endpoints e ON e.id = s.endpoint_id AND e.endpoint_status != 'DELETED'
        """))).fetchall():
            for addr in addresses or []:
                index.addresses.setdefault(str(addr), str(ep))
            if norm_name(sys_name):
                index.names.setdefault(norm_name(sys_name), str(ep))
        for ep, mac in (await db.execute(text("""
            SELECT i.endpoint_id, i.mac FROM snmp_interfaces i
            JOIN endpoints e ON e.id = i.endpoint_id AND e.endpoint_status != 'DELETED'
            WHERE i.mac IS NOT NULL
        """))).fetchall():
            m = norm_mac(mac)
            if m:
                index.macs.setdefault(m, str(ep))
        return index

    async def save_neighbours(self, endpoint_id: str, rows: List[Dict[str, Any]], addresses: Optional[List[str]],
                              sys_name: Optional[str]) -> int:
        """Stores the device's own addresses and name, then its neighbours, matched. Neighbours
        not reported this time are removed. Returns how many were matched to an endpoint."""
        from app.core.orm import text

        matched = 0
        async with self.session_factory() as db:
            await db.execute(text("""
                INSERT INTO snmp_device_state (endpoint_id, addresses, sys_name) VALUES (CAST(:id AS uuid),
                    CAST(:addresses AS text[]), :sys_name)
                ON CONFLICT (endpoint_id) DO UPDATE SET
                    addresses = CASE WHEN :keep_addresses THEN snmp_device_state.addresses ELSE EXCLUDED.addresses END,
                    sys_name = COALESCE(EXCLUDED.sys_name, snmp_device_state.sys_name), updated_at = NOW()
            """), {"id": endpoint_id, "addresses": tuple(addresses or ()), "keep_addresses": addresses is None,
                   "sys_name": sys_name})
            index = await self.match_index(db)
            before = {str(r[0]) for r in (await db.execute(text(
                "SELECT matched_endpoint_id FROM snmp_neighbours WHERE endpoint_id = CAST(:id AS uuid) AND matched_endpoint_id IS NOT NULL"
            ), {"id": endpoint_id})).fetchall()}
            after: set = set()
            seen = datetime.now(timezone.utc)
            for r in rows:
                ep, how = match_neighbour(r, index, endpoint_id)
                matched += 1 if ep else 0
                if ep:
                    after.add(ep)
                await db.execute(text("""
                    INSERT INTO snmp_neighbours (endpoint_id, nkey, protocol, local_if_index, local_port, remote_name,
                        remote_chassis, remote_port, remote_port_desc, remote_platform, remote_addresses,
                        matched_endpoint_id, matched_by, first_seen, last_seen)
                    VALUES (CAST(:id AS uuid), :nkey, :protocol, :local_if_index, :local_port, :remote_name,
                        :remote_chassis, :remote_port, :remote_port_desc, :remote_platform, CAST(:remote_addresses AS text[]),
                        CAST(:matched AS uuid), :matched_by, :seen, :seen)
                    ON CONFLICT (endpoint_id, nkey) DO UPDATE SET
                        local_if_index = EXCLUDED.local_if_index, local_port = EXCLUDED.local_port,
                        remote_name = EXCLUDED.remote_name, remote_chassis = EXCLUDED.remote_chassis,
                        remote_port = EXCLUDED.remote_port, remote_port_desc = EXCLUDED.remote_port_desc,
                        remote_platform = EXCLUDED.remote_platform, remote_addresses = EXCLUDED.remote_addresses,
                        matched_endpoint_id = EXCLUDED.matched_endpoint_id, matched_by = EXCLUDED.matched_by,
                        last_seen = EXCLUDED.last_seen
                """), {**r, "id": endpoint_id, "remote_addresses": tuple(r["remote_addresses"]),
                       "matched": ep, "matched_by": how, "seen": seen})
            await db.execute(text("DELETE FROM snmp_neighbours WHERE endpoint_id = CAST(:id AS uuid) AND last_seen < :seen"),
                             {"id": endpoint_id, "seen": seen})
            await db.commit()
        if before != after and self.announce is not None:
            # The map places switches by these links: a change must reach every API worker.
            try:
                await self.announce("snmp_neighbours", endpoint_id)
            except Exception as exc:
                logger.debug("snmpd: map update not announced: %s", exc)
        return matched

    async def add_minutes(self, endpoint_id: str, minutes: Dict[Tuple[datetime, int], List[int]]) -> None:
        """Adds amounts to snmp_interface_minute_rollups. Adding, not replacing: two readings can
        share a minute (each brings its part of it)."""
        from app.core.orm import text

        if not minutes:
            return
        cols = ", ".join(FIELDS)
        values = ", ".join(f":{f}" for f in FIELDS)
        updates = ", ".join(f"{f} = snmp_interface_minute_rollups.{f} + EXCLUDED.{f}" for f in FIELDS)
        async with self.session_factory() as db:
            for (bucket, idx), amounts in sorted(minutes.items()):
                await db.execute(text(f"""
                    INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, {cols})
                    VALUES (:bucket, CAST(:id AS uuid), :idx, {values})
                    ON CONFLICT (bucket, exporter_id, interface_idx) DO UPDATE SET {updates}
                """), {"bucket": bucket, "id": endpoint_id, "idx": idx, **dict(zip(FIELDS, amounts))})
            await db.commit()

    @staticmethod
    async def _insert_events(db: Any, endpoint_id: str, events: List[Tuple[Optional[int], Optional[str], str, Optional[str]]]) -> None:
        from app.core.orm import text

        for idx, name, kind, detail in events:
            await db.execute(text("""
                INSERT INTO snmp_interface_events (endpoint_id, if_index, if_name, kind, detail)
                VALUES (CAST(:id AS uuid), :idx, :name, :kind, :detail)
            """), {"id": endpoint_id, "idx": idx, "name": name, "kind": kind, "detail": detail})

    async def prune_events(self) -> None:
        from app.core.orm import text

        async with self.session_factory() as db:
            await db.execute(text(f"DELETE FROM snmp_interface_events WHERE at < NOW() - INTERVAL '{EVENT_RETENTION_DAYS} days'"))
            await db.commit()


@dataclass
class DevicePoller:
    target: Target
    store: Store
    client: Optional[SnmpClient] = None
    profile: Optional[DeviceProfile] = None
    sys_uptime: Optional[int] = None
    engine_boots: Optional[int] = None
    if_table_last_change: Optional[int] = None
    interfaces: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # idx -> name/admin/oper
    last_probe: float = 0.0  # monotonic
    last_inventory: float = 0.0
    failures: int = 0
    loaded: bool = False
    seen_probe_request: Optional[datetime] = None
    counters: Dict[int, Sample] = field(default_factory=dict)  # last reading per interface
    _labels: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # ifIndex -> name/descr/alias
    interval: float = 60.0  # the poll interval, set by SnmpPoller: the gap rule needs it

    async def _client(self) -> SnmpClient:
        if self.client is None:
            opts = self.profile.client_options() if self.profile else {}
            self.client = await SnmpClient(self.target.address, self.target.credentials, port=self.target.port,
                                           timeout=self.target.timeout, retries=self.target.retries, **opts).open()
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    async def _load(self) -> None:
        state = await self.store.load_state(self.target.endpoint_id)
        if state.get("profile"):
            try:
                self.profile = DeviceProfile.from_dict(state["profile"])
            except (TypeError, ValueError):
                self.profile = None
        self.sys_uptime = state.get("sys_uptime")
        self.engine_boots = state.get("engine_boots")
        self.if_table_last_change = state.get("if_table_last_change")
        self.interfaces = state.get("interfaces", {})
        # The last readings from before a restart: within MAX_GAP_INTERVALS the first poll after
        # it still makes a delta, so a restart does not cost a poll interval of traffic.
        for idx, raw in (state.get("counters") or {}).items():
            sample = Sample.from_json(raw)
            if sample is not None:
                self.counters[int(idx)] = sample
        # A restart of snmpd is not a reason to probe every device again at once: a profile and
        # inventory from the database count as fresh for their usual period.
        if self.profile and state.get("last_probe_at"):
            age = (datetime.now(timezone.utc) - state["last_probe_at"]).total_seconds()
            self.last_probe = time.monotonic() - max(0.0, age)
        if state.get("inventory_at"):
            age = (datetime.now(timezone.utc) - state["inventory_at"]).total_seconds()
            self.last_inventory = time.monotonic() - max(0.0, age)
        self.seen_probe_request = self.target.probe_requested_at
        self.loaded = True

    def _probe_due(self, now: float) -> Optional[str]:
        if self.profile is None or self.last_probe == 0.0:
            return "first contact"
        if self.target.probe_requested_at and self.target.probe_requested_at != self.seen_probe_request:
            return "requested"
        if self.failures >= FAILURES_BEFORE_PROBE:
            return "failing"
        if now - self.last_probe >= PROFILE_SECONDS:
            return "daily"
        return None

    async def run_once(self) -> str:
        """One turn for this device; returns the status it ended with."""
        if not self.loaded:
            await self._load()
        now = time.monotonic()
        reason = self._probe_due(now)
        try:
            c = await self._client()
            if reason:
                return await self._probe(c, reason)
            return await self._poll(c, now)
        except SnmpSecurityError as exc:
            return await self._failed("auth_failed", str(exc))
        except SnmpTimeout as exc:
            return await self._failed("no_reply", str(exc))
        except (SnmpError, OSError) as exc:
            return await self._failed("error", str(exc))

    async def _failed(self, status: str, error: str) -> str:
        self.failures += 1
        await self.store.save_state(self.target.endpoint_id, status=status, last_error=error[:500],
                                    last_poll_at=datetime.now(timezone.utc))
        return status

    async def _probe(self, c: SnmpClient, reason: str) -> str:
        report = await read_device(c)
        self.seen_probe_request = self.target.probe_requested_at
        if not report.get("ok"):
            # The probe's own failure: count it like a failed poll, and try again next turn
            # only if it was the first contact (else wait for the usual reasons).
            if report.get("error_kind") == "auth":
                return await self._failed("auth_failed", report.get("error") or "")
            return await self._failed("no_reply", report.get("error") or "")
        self.profile = DeviceProfile.from_dict(report["profile"])
        sysinfo = report.get("system") or {}
        now_dt = datetime.now(timezone.utc)
        self.last_probe = self.last_inventory = time.monotonic()
        self.failures = 0
        await self._store_inventory(interface_rows(report.get("interfaces") or []))
        self.sys_uptime = sysinfo.get("sysUpTime")
        self.engine_boots = sysinfo.get("snmpEngineBoots")
        status = "partial" if self.profile.unanswered else "ok"
        await self.store.save_state(
            self.target.endpoint_id, status=status, last_error=None, last_poll_at=now_dt, last_ok_at=now_dt,
            last_probe_at=now_dt, inventory_at=now_dt, profile=self.profile.to_dict(), sys_uptime=self.sys_uptime,
            engine_boots=self.engine_boots, if_table_last_change=self.if_table_last_change,
        )
        await self._store_neighbours(
            report.get("lldp"), report.get("cdp"),
            [a["address"] for a in report.get("addresses") or []] if report.get("addresses") is not None else None,
            text_of(sysinfo.get("sysName")) or None,
        )
        logger.info("snmpd: probed %s (%s, %s): %s, %d interfaces", self.target.name, self.target.address, reason,
                    status, len(report.get("interfaces") or []))
        return status

    async def _store_neighbours(self, lldp: Optional[Dict[str, Any]], cdp: Optional[Dict[str, Any]],
                                addresses: Optional[List[str]], sys_name: Optional[str]) -> None:
        rows = neighbour_rows(lldp, cdp, {i: {"name": v.get("name")} for i, v in self.interfaces.items()} | self._labels)
        matched = await self.store.save_neighbours(self.target.endpoint_id, rows, addresses, sys_name)
        if rows:
            logger.info("snmpd: %s has %d neighbours, %d of them monitored", self.target.name, len(rows), matched)

    async def _read_neighbours(self, c: SnmpClient) -> None:
        """With the inventory: neighbours and addresses, only from the MIBs this device answered
        at its probe (so a device without LLDP costs nothing here)."""
        if not self.profile:
            return
        s = Survey(c, 0, True)
        try:
            lldp = await s.lldp() if self.profile.answers.get("lldp") else None
            cdp = await s.cdp() if self.profile.answers.get("cdp") else None
            addresses = [a["address"] for a in await s.addresses()] if self.profile.answers.get("addresses") else None
        except SnmpError as exc:
            logger.info("snmpd: neighbours of %s not read: %s", self.target.name, exc)
            return
        await self._store_neighbours(lldp, cdp, addresses, None)

    async def _store_inventory(self, rows: List[InterfaceRow]) -> None:
        old = {i: v.get("name") for i, v in self.interfaces.items()}
        new = {r.if_index: r.label for r in rows}
        events: List[Tuple[Optional[int], Optional[str], str, Optional[str]]] = []
        moves: List[Tuple[int, int, str]] = []
        if self.interfaces:  # not on first contact: everything would be "added"
            moves = renumbered(old, new)
            moved_old = {o for o, _, _ in moves}
            moved_new = {n for _, n, _ in moves}
            for o, n, name in moves:
                events.append((n, name, "renumbered", f"interface number {o} became {n}"))
            for idx in sorted(set(new) - set(old) - moved_new):
                events.append((idx, new[idx], "added", None))
            for idx in sorted(set(old) - set(new) - moved_old):
                events.append((idx, old[idx], "removed", None))
        moved = await self.store.save_inventory(self.target.endpoint_id, rows, events, moves)
        if moved:
            logger.warning("snmpd: %s renumbered its interfaces; moved %d manual interface entries with them",
                           self.target.name, moved)
        self.interfaces = {r.if_index: {"name": r.label, "admin": r.admin_status, "oper": r.oper_status,
                                        "speed": r.speed_mbps} for r in rows}
        # LLDP-MIB names local ports by description: keep every label to find the ifIndex.
        self._labels = {r.if_index: {"name": r.if_name, "descr": r.if_descr, "alias": r.if_alias} for r in rows}

    async def _poll(self, c: SnmpClient, now: float) -> str:
        oids = [SYS_UPTIME, IF_TABLE_LAST_CHANGE]
        if self.profile and self.profile.answers.get("engine_boots"):
            oids.append(ENGINE_BOOTS)
        vbs = {vb.oid: vb for vb in await c.get(oids)}
        uptime = _value(vbs.get(SYS_UPTIME))
        last_change = _value(vbs.get(IF_TABLE_LAST_CHANGE))
        boots = _value(vbs.get(ENGINE_BOOTS))
        now_dt = datetime.now(timezone.utc)

        rebooted = (uptime is not None and self.sys_uptime is not None and uptime < self.sys_uptime) or \
                   (boots is not None and self.engine_boots is not None and boots > self.engine_boots)
        if rebooted:
            # sysUpTime going back can also be the SNMP agent restarting; engine boots or a
            # device that answers hrSystemUptime tell them apart, which the probe records.
            await self.store.save_state(self.target.endpoint_id, last_reboot_at=now_dt)
            await self.store.save_status(self.target.endpoint_id, {}, [(None, None, "reboot",
                                         f"uptime went from {self.sys_uptime} to {uptime} ticks")])
            self.sys_uptime, self.engine_boots = uptime, boots
            self.counters = {}  # counters restarted from zero with the device
            return await self._probe(c, "reboot")

        if (last_change is not None and self.if_table_last_change is not None and last_change != self.if_table_last_change) \
                or now - self.last_inventory >= INVENTORY_SECONDS:
            changed = last_change is not None and self.if_table_last_change is not None and last_change != self.if_table_last_change
            rows = await self._read_inventory(c)
            await self._store_inventory(rows)
            await self._read_neighbours(c)
            self.last_inventory = now
            inventory_at = now_dt
            if changed:
                # Interfaces came, went or were renumbered: a reading may now belong to
                # another interface, so start every interface afresh (one interval lost).
                self.counters = {}
        else:
            inventory_at = None
        await self._poll_counters(c, uptime)

        self.sys_uptime, self.if_table_last_change = uptime, last_change
        if boots is not None:
            self.engine_boots = boots
        self.failures = 0
        status = "partial" if self.profile and self.profile.unanswered else "ok"
        fields = dict(status=status, last_error=None, last_poll_at=now_dt, last_ok_at=now_dt, sys_uptime=uptime,
                      engine_boots=self.engine_boots, if_table_last_change=last_change,
                      profile=self._profile_with(c),
                      counters={str(i): smp.to_json() for i, smp in self.counters.items()})
        if inventory_at:
            fields["inventory_at"] = inventory_at
        await self.store.save_state(self.target.endpoint_id, **fields)
        return status

    def _profile_with(self, c: SnmpClient) -> Optional[Dict[str, Any]]:
        """The profile with what the client learned since (GETBULK off, columns to walk alone)."""
        if not self.profile:
            return None
        self.profile.use_bulk = c.use_bulk
        self.profile.max_repetitions = c.max_repetitions
        self.profile.walk_alone = sorted(ber.format_oid(o) for o in c.walk_alone)
        return self.profile.to_dict()

    async def _read_inventory(self, c: SnmpClient) -> List[InterfaceRow]:
        cols = {**mib.IF_TABLE, **mib.IF_X_TABLE}
        if self.profile and not self.profile.answers.get("if_x"):
            cols = dict(mib.IF_TABLE)
        walked = await c.walk_columns(list(cols.values()), errors={})
        rows: Dict[int, Dict[str, Any]] = {}
        for name, col in cols.items():
            for vb in walked.get(col, []):
                idx = vb.oid[len(col):]
                if len(idx) == 1:
                    rows.setdefault(idx[0], {"ifIndex": idx[0]})[name] = vb.value
        return interface_rows([rows[i] for i in sorted(rows)])

    async def _poll_counters(self, c: SnmpClient, uptime: Optional[int]) -> None:
        """Status and traffic counters in one walk: up/down events as before, and the deltas
        since the last reading as per-minute traffic (counter_delta, spread)."""
        hc = bool(self.profile and self.profile.answers.get("hc_counters"))
        cols: Dict[str, ber.Oid] = {**STATUS_COLUMNS, **(mib.HC_COUNTERS if hc else mib.LOW_COUNTERS), **mib.ERROR_COUNTERS}
        if self.profile and self.profile.answers.get("discontinuity"):
            cols["disc"] = DISCONTINUITY
        walked = await c.walk_columns(list(cols.values()), errors={})
        per: Dict[int, Dict[str, Any]] = {}
        for name, col in cols.items():
            for vb in walked.get(col, []):
                idx = vb.oid[len(col):]
                if len(idx) == 1 and not vb.is_exception:
                    per.setdefault(idx[0], {})[name] = vb.value
        await self._apply_status({
            idx: {"admin": mib.IF_ADMIN_STATUS.get(v.get("ifAdminStatus")), "oper": mib.IF_OPER_STATUS.get(v.get("ifOperStatus"))}
            for idx, v in per.items() if "ifOperStatus" in v or "ifAdminStatus" in v
        })

        now = time.time()
        minutes: Dict[Tuple[datetime, int], List[int]] = {}
        readings: Dict[int, Sample] = {}

        def total(v: Dict[str, Any], *keys: str) -> Optional[int]:
            present = [v[k] for k in keys if isinstance(v.get(k), int)]
            return sum(present) if present else None

        for idx, v in per.items():
            if not isinstance(v.get("in_octets"), int) or not isinstance(v.get("out_octets"), int):
                continue  # this interface has no counters in this mode (Null0 without 64-bit ones)
            cur = Sample(now, uptime, v.get("disc"), hc, [
                v["in_octets"], v["out_octets"],
                total(v, "in_ucast", "in_mcast", "in_bcast"), total(v, "out_ucast", "out_mcast", "out_bcast"),
                v.get("in_errors"), v.get("out_errors"), v.get("in_discards"), v.get("out_discards"),
            ])
            prev = self.counters.get(idx)
            if prev is not None:
                amounts = counter_delta(prev, cur, self.interval, (self.interfaces.get(idx) or {}).get("speed"))
                if amounts is not None and any(amounts):
                    for bucket, share in spread(prev.at, cur.at, amounts).items():
                        minutes[(bucket, idx)] = share
            readings[idx] = cur
        self.counters = readings
        await self.store.add_minutes(self.target.endpoint_id, minutes)

    async def _apply_status(self, current: Dict[int, Dict[str, Optional[str]]]) -> None:
        changes: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
        events: List[Tuple[Optional[int], Optional[str], str, Optional[str]]] = []
        for idx, st in current.items():
            before = self.interfaces.get(idx)
            if before is None:
                continue  # a new interface: the next inventory (ifTableLastChange) adds it
            admin, oper = st.get("admin") or before.get("admin"), st.get("oper") or before.get("oper")
            if (admin, oper) != (before.get("admin"), before.get("oper")):
                changes[idx] = (admin, oper)
                if oper != before.get("oper") and oper in ("up", "down"):
                    detail = "shut down by an admin" if oper == "down" and admin == "down" else None
                    events.append((idx, before.get("name"), oper, detail))
                before.update(admin=admin, oper=oper)
        if changes or events:
            await self.store.save_status(self.target.endpoint_id, changes, events)


def _value(vb: Optional[ber.Varbind]) -> Optional[int]:
    if vb is None or vb.is_exception or not isinstance(vb.value, int):
        return None
    return vb.value


def offset_for(endpoint_id: str, interval: float) -> float:
    """A stable place in the interval for each device, so polls spread over it."""
    h = int(hashlib.sha1(endpoint_id.encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000 * interval


class SnmpPoller:
    """Keeps one DevicePoller per target and runs each at its turn in every interval."""

    def __init__(self, store: Store, enabled: Callable[[], bool], interval: Callable[[], float],
                 max_concurrent: int = MAX_CONCURRENT):
        self.store = store
        self.enabled = enabled
        self.interval = interval
        self.devices: Dict[str, DevicePoller] = {}
        self.next_run: Dict[str, float] = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.running: Dict[str, asyncio.Task] = {}
        self._last_reload = 0.0
        self._last_prune = 0.0

    async def reload(self) -> None:
        targets = {t.endpoint_id: t for t in await self.store.load_targets()}
        for ep_id in list(self.devices):
            t = targets.get(ep_id)
            d = self.devices[ep_id]
            if t is None or (t.address, t.credential_id, t.credential_version) != \
                    (d.target.address, d.target.credential_id, d.target.credential_version):
                d.close()
                del self.devices[ep_id]
                self.next_run.pop(ep_id, None)
        now = time.monotonic()
        for ep_id, t in targets.items():
            if ep_id in self.devices:
                self.devices[ep_id].target.probe_requested_at = t.probe_requested_at
                if t.probe_requested_at and t.probe_requested_at != self.devices[ep_id].seen_probe_request:
                    self.next_run[ep_id] = now  # Probe now: do not wait for its turn
            else:
                self.devices[ep_id] = DevicePoller(t, self.store)
                # A new device starts within its offset, not at once with every other new one.
                self.next_run[ep_id] = now + offset_for(ep_id, min(self.interval(), 10.0))

    async def _run_device(self, ep_id: str) -> None:
        async with self.semaphore:
            d = self.devices.get(ep_id)
            if d is None:
                return
            d.interval = self.interval()
            try:
                await d.run_once()
            except Exception:  # one device's bug must not stop the loop
                logger.exception("snmpd: polling %s failed", d.target.name)

    async def tick(self) -> None:
        """Starts every device whose turn has come; called about once a second."""
        now = time.monotonic()
        if now - self._last_reload >= TARGET_RELOAD_SECONDS:
            self._last_reload = now
            await self.reload()
        if now - self._last_prune >= 3600:
            self._last_prune = now
            await self.store.prune_events()
        interval = self.interval()
        for ep_id in list(self.devices):
            task = self.running.get(ep_id)
            if task is not None and not task.done():
                continue  # still busy with the last turn: skip, never stack
            if self.next_run.get(ep_id, 0) <= now:
                self.running[ep_id] = asyncio.create_task(self._run_device(ep_id), name=f"snmp:{ep_id}")
                base = self.next_run.get(ep_id, now)
                self.next_run[ep_id] = max(base + interval, now + 1)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.enabled():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("snmpd: scheduler error")
                    self._last_reload = 0.0
            elif self.devices:
                self.close_all()
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        self.close_all()

    def close_all(self) -> None:
        for task in self.running.values():
            task.cancel()
        self.running.clear()
        for d in self.devices.values():
            d.close()
        self.devices.clear()
        self.next_run.clear()
        self._last_reload = 0.0
