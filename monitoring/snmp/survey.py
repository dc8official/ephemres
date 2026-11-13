"""SNMP survey of one device: what it answers within Ephemres's fixed SNMP scope.

Build plan 8.1 says the scope is fixed only after real Meraki, Sophos and lab devices have
been checked. This command is that check, and it uses the same client the poller will use,
so a device that surveys cleanly will poll cleanly. It only reads; it changes nothing on the
device and needs no database or Ephemres config.

Run it on the Ephemres server itself (the poller will sit at the same place in the network):

    cd /opt/ephemres
    sudo -u ephemres env PYTHONPATH=/opt/ephemres/backend:/opt/ephemres \\
        /opt/ephemres/venv/bin/python -m monitoring.snmp.survey 10.0.0.1 --json /tmp/abj-rtr.json

    ... --version 3 --user ephemres --auth sha --priv aes

Secrets are never taken as arguments (shell history and `ps` would keep them): the community
or v3 passwords come from EPHEMRES_SNMP_COMMUNITY, EPHEMRES_SNMP_AUTH_PASSWORD and
EPHEMRES_SNMP_PRIV_PASSWORD, or are asked for. The report never contains them.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from monitoring.snmp import ber, mib, usm
from monitoring.snmp.profile import profile_from_survey
from monitoring.snmp.client import (CommunityAuth, Credentials, SnmpClient, SnmpError, SnmpSecurityError,
                                    SnmpTimeout, UserAuth)

COUNTER_GET_CHUNK = 10  # interfaces per GET when reading counters (2 OIDs each)


# --- value formatting --------------------------------------------------------------------------

def text_of(value: Any) -> str:
    """Octet strings are text on most devices but MACs and binary IDs on some: show text when
    it is printable, hex otherwise."""
    if isinstance(value, bytes):
        try:
            s = value.decode("utf-8").rstrip("\x00")
            # Line breaks count as text: Cisco's sysDescr spans several lines.
            if all(ch.isprintable() or ch in "\r\n\t" for ch in s):
                return " ".join(s.split())
        except UnicodeDecodeError:
            pass
        return ":".join(f"{b:02x}" for b in value)
    if isinstance(value, tuple):
        return ber.format_oid(value)
    return "" if value is None else str(value)


def address_of(raw: Any) -> str:
    if isinstance(raw, bytes) and len(raw) == 4:
        return ".".join(str(b) for b in raw)
    return text_of(raw)


def duration_of(ticks: Optional[int]) -> str:
    if ticks is None:
        return "-"
    seconds = ticks // 100
    days, rest = divmod(seconds, 86400)
    return f"{days}d {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"


def rows_by_index(columns: Dict[str, ber.Oid], walked: Dict[ber.Oid, List[ber.Varbind]]) -> Dict[Tuple[int, ...], Dict[str, Any]]:
    rows: Dict[Tuple[int, ...], Dict[str, Any]] = {}
    for name, col in columns.items():
        for vb in walked.get(col, []):
            rows.setdefault(vb.oid[len(col):], {})[name] = vb.value
    return rows


# --- the survey --------------------------------------------------------------------------------

class Survey:
    def __init__(self, client: SnmpClient, interval: float, use_bulk: bool):
        self.c = client
        self.interval = interval
        self.use_bulk = use_bulk
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.unanswered: Dict[str, str] = {}  # column name -> why (the device never answered it)

    async def section(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Runs one part and records its cost: requests, seconds and timeouts per part are what
        sizing the poller needs, and a failed part must not hide the others."""
        before = (self.c.stats.requests, self.c.stats.timeouts)
        started = time.monotonic()
        entry: Dict[str, Any] = {}
        try:
            result = await fn()
            entry["ok"] = True
        except SnmpSecurityError:
            raise
        except SnmpTimeout:
            # The first part proves the device answers at all; after that a silent part is a
            # finding (some agents drop requests for one MIB), and the other parts still run.
            if name == "system":
                raise
            result = None
            entry.update(ok=False, error="no reply")
        except SnmpError as exc:
            result = None
            entry.update(ok=False, error=str(exc))
        entry["seconds"] = round(time.monotonic() - started, 3)
        entry["requests"] = self.c.stats.requests - before[0]
        entry["timeouts"] = self.c.stats.timeouts - before[1]
        self.sections[name] = entry
        return result

    async def walk(self, columns: Dict[str, ber.Oid]) -> Dict[ber.Oid, List[ber.Varbind]]:
        errors: Dict[ber.Oid, str] = {}
        # None lets the client use what it has learned (GETBULK found broken mid-survey).
        walked = await self.c.walk_columns(list(columns.values()), None if self.use_bulk else False, errors)
        names = {oid: name for name, oid in columns.items()}
        for oid, why in errors.items():
            self.unanswered[names[oid]] = why
        return walked

    async def system(self) -> Dict[str, Any]:
        vbs = await self.c.get(list(mib.SYSTEM.values()))
        by_oid = {vb.oid: vb for vb in vbs}
        out = {}
        for name, oid in mib.SYSTEM.items():
            vb = by_oid.get(oid)
            out[name] = None if vb is None or vb.is_exception else vb.value
        return out

    async def interfaces(self) -> List[Dict[str, Any]]:
        cols = {**mib.IF_TABLE, **mib.IF_X_TABLE}
        walked = await self.walk(cols)
        rows = []
        for idx, r in sorted(rows_by_index(cols, walked).items()):
            if len(idx) != 1:
                continue
            rows.append({"ifIndex": idx[0], **r})
        return rows

    async def counters(self, interfaces: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Reads the octet counters twice, `interval` seconds apart, to show they move and how
        fast. 64-bit counters where the device has them, else the 32-bit ones."""
        hc = any("ifHCInOctets" in r for r in interfaces)
        cin, cout = (mib.IF_X_TABLE["ifHCInOctets"], mib.IF_X_TABLE["ifHCOutOctets"]) if hc else \
            (mib.IF_TABLE["ifInOctets"], mib.IF_TABLE["ifOutOctets"])
        wrap = 2 ** 64 if hc else 2 ** 32
        indexes = [r["ifIndex"] for r in interfaces]

        async def read() -> Tuple[float, Dict[int, Tuple[int, int]]]:
            got: Dict[int, Tuple[int, int]] = {}
            for i in range(0, len(indexes), COUNTER_GET_CHUNK):
                chunk = indexes[i:i + COUNTER_GET_CHUNK]
                vbs = await self.c.get([o for x in chunk for o in (cin + (x,), cout + (x,))])
                for x, (a, b) in zip(chunk, zip(vbs[0::2], vbs[1::2])):
                    if not a.is_exception and not b.is_exception:
                        got[x] = (a.value, b.value)
            return time.monotonic(), got

        t0, first = await read()
        await asyncio.sleep(self.interval)
        t1, second = await read()
        rates = []
        for x in indexes:
            if x in first and x in second:
                d_in = (second[x][0] - first[x][0]) % wrap
                d_out = (second[x][1] - first[x][1]) % wrap
                rates.append({"ifIndex": x, "in_bps": round(d_in * 8 / (t1 - t0)), "out_bps": round(d_out * 8 / (t1 - t0))})
        return {"counter": "64-bit (ifHC*)" if hc else "32-bit (ifIn/OutOctets)", "seconds": round(t1 - t0, 2), "rates": rates}

    async def addresses(self) -> List[Dict[str, Any]]:
        walked = await self.walk(mib.IP_ADDR)
        out = []
        for idx, r in sorted(rows_by_index(mib.IP_ADDR, walked).items()):
            if len(idx) == 4:
                out.append({"address": ".".join(map(str, idx)), "ifIndex": r.get("ipAdEntIfIndex"),
                            "netmask": r.get("ipAdEntNetMask")})
        return out

    async def lldp(self) -> Dict[str, Any]:
        """LLDP-MIB first; LLDP-V2-MIB (802.1AB-2009) when the first has nothing, since some
        devices answer only the newer one (vendor research, section 2)."""
        result = await self._lldp(v2=False)
        if not result["answered"]:
            v2 = await self._lldp(v2=True)
            if v2["answered"]:
                return v2
        return result

    async def _lldp(self, v2: bool) -> Dict[str, Any]:
        local_cols = mib.LLDP_V2_LOCAL if v2 else mib.LLDP_LOCAL
        port_cols = mib.LLDP_V2_LOC_PORT if v2 else mib.LLDP_LOC_PORT
        rem_cols = mib.LLDP_V2_REMOTE if v2 else mib.LLDP_REMOTE
        man_col = mib.LLDP_V2_REM_MAN_ADDR_IF_SUBTYPE if v2 else mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE
        # The remote index: timeMark.localPortNum.remIndex (v1) or
        # timeMark.localIfIndex.destAddressIndex.remIndex (v2).
        key_len = 4 if v2 else 3
        p = "lldpV2" if v2 else "lldp"

        local_vbs = await self.c.get(list(local_cols.values()))
        local = {name: (None if vb.is_exception else vb.value) for name, vb in zip(local_cols, local_vbs)}
        ports_walk = await self.walk(port_cols)
        ports = {idx[0]: r for idx, r in rows_by_index(port_cols, ports_walk).items() if len(idx) == 1}
        rem_walk = await self.walk({**rem_cols, f"{p}RemManAddrIfSubtype": man_col})
        remote = rows_by_index(rem_cols, rem_walk)
        addresses: Dict[Tuple[int, ...], List[str]] = {}
        for vb in rem_walk.get(man_col, []):
            idx = vb.oid[len(man_col):]
            if len(idx) >= key_len + 2:
                key, subtype, length, addr = idx[:key_len], idx[key_len], idx[key_len + 1], idx[key_len + 2:]
                # IANA address family numbers: 1 IPv4, 2 IPv6; anything else is shown as hex.
                if subtype == 1 and length == 4 and len(addr) == 4:
                    addresses.setdefault(key, []).append(".".join(map(str, addr)))
                elif subtype == 2 and length == 16 and len(addr) == 16 and all(a < 256 for a in addr):
                    addresses.setdefault(key, []).append(str(ipaddress.IPv6Address(bytes(addr))))
                else:
                    addresses.setdefault(key, []).append(":".join(f"{a:02x}" for a in addr))
        neighbours = []
        for idx, r in sorted(remote.items()):
            if len(idx) != key_len:
                continue
            port = ports.get(idx[1], {})
            neighbours.append({
                # v1's local port number is not an ifIndex; v2's is (lldpV2RemLocalIfIndex).
                "local_port_num": None if v2 else idx[1],
                "local_ifIndex": idx[1] if v2 else None,
                "local_port": text_of(port.get(f"{p}LocPortDesc") or port.get(f"{p}LocPortId")),
                "remote_name": text_of(r.get(f"{p}RemSysName")),
                "remote_chassis": text_of(r.get(f"{p}RemChassisId")),
                "remote_chassis_subtype": r.get(f"{p}RemChassisIdSubtype"),
                "remote_port": text_of(r.get(f"{p}RemPortId")),
                "remote_port_subtype": r.get(f"{p}RemPortIdSubtype"),
                "remote_port_desc": text_of(r.get(f"{p}RemPortDesc")),
                "remote_description": text_of(r.get(f"{p}RemSysDesc"))[:120],
                "remote_addresses": addresses.get(idx, []),
            })
        answered = any(v is not None for v in local.values()) or bool(ports) or bool(neighbours)
        return {"answered": answered, "mib": "LLDP-V2-MIB" if v2 else "LLDP-MIB",
                "local": {k: text_of(v) for k, v in local.items()},
                "local_ports": len(ports), "neighbours": neighbours}

    async def cdp(self) -> Dict[str, Any]:
        walked = await self.walk(mib.CDP_CACHE)
        neighbours = []
        for idx, r in sorted(rows_by_index(mib.CDP_CACHE, walked).items()):
            if len(idx) != 2:
                continue
            neighbours.append({
                "local_ifIndex": idx[0],
                "remote_name": text_of(r.get("cdpCacheDeviceId")),
                "remote_port": text_of(r.get("cdpCacheDevicePort")),
                "remote_platform": text_of(r.get("cdpCachePlatform")),
                "remote_address": address_of(r.get("cdpCacheAddress")) if r.get("cdpCacheAddressType") == 1 else text_of(r.get("cdpCacheAddress")),
            })
        return {"answered": bool(neighbours), "neighbours": neighbours}


def findings(report: Dict[str, Any]) -> List[str]:
    """Plain answers to the questions the scope decision needs."""
    out: List[str] = []
    ifs = report.get("interfaces") or []
    n = len(ifs)
    if n:
        hc = sum(1 for r in ifs if "ifHCInOctets" in r)
        out.append(f"64-bit counters on {hc} of {n} interfaces" + ("" if hc else " (32-bit only: they wrap in under 6 minutes at 100 Mb/s)"))
        out.append(f"ifHighSpeed on {sum(1 for r in ifs if 'ifHighSpeed' in r)} of {n}; ifName on {sum(1 for r in ifs if r.get('ifName'))} of {n}; "
                   f"ifAlias set on {sum(1 for r in ifs if r.get('ifAlias'))}")
        errs = sum(1 for r in ifs if "ifInErrors" in r and "ifOutDiscards" in r)
        out.append(f"error and discard counters on {errs} of {n} interfaces")
        disc = sum(1 for r in ifs if "ifCounterDiscontinuityTime" in r)
        out.append(f"counter reset marker (ifCounterDiscontinuityTime) on {disc} of {n} interfaces")
    elif "interfaces" in report:
        out.append("no interfaces answered (IF-MIB not in the device's SNMP view?)")
    sysinfo = report.get("system") or {}
    clocks = [k for k in ("sysUpTime", "hrSystemUptime", "snmpEngineBoots") if sysinfo.get(k) is not None]
    out.append("reboot clocks answered: " + (", ".join(clocks) if clocks else "none"))
    if report.get("addresses") is not None:
        out.append(f"IPv4 addresses listed: {len(report['addresses'])}")
    lldp = report.get("lldp")
    if lldp is not None:
        out.append(f"LLDP ({lldp['mib']}): {len(lldp['neighbours'])} neighbours on {lldp['local_ports']} local ports"
                   if lldp["answered"] else "LLDP: not answered (neither LLDP-MIB nor LLDP-V2-MIB)")
    cdp = report.get("cdp")
    if cdp is not None:
        out.append(f"CDP: {len(cdp['neighbours'])} neighbours" if cdp["answered"] else "CDP: no neighbours or not answered")
    if report.get("unanswered_columns"):
        out.append("columns the device never answered (walked alone after a multi-column request got no reply): "
                   + ", ".join(sorted(report["unanswered_columns"])))
    t = report["totals"]
    out.append(f"survey cost: {t['requests']} requests, {t['seconds']} s, {t['timeouts']} timeouts, "
               f"max-repetitions ended at {t['max_repetitions']}, {t['bytes_in']} bytes in")
    return out


async def read_device(c: SnmpClient, *, use_bulk: Optional[bool] = None, interval: float = 0.0) -> Dict[str, Any]:
    """The survey's reads on a client the caller owns: the survey command, the Test button and
    ephemres-snmpd's probe all use this, so they see a device the same way. Returns the report's
    device parts, the unanswered columns and the learned profile (profile.py)."""
    report: Dict[str, Any] = {}
    s = Survey(c, interval, c.use_bulk if use_bulk is None else use_bulk)
    try:
        report["system"] = await s.section("system", s.system)
        if c.engine_id is not None:
            report["engine"] = {"id": c.engine_id.hex(), "boots": c.engine_boots}
        report["interfaces"] = await s.section("interfaces", s.interfaces)
        if report["interfaces"] and interval > 0:
            report["counters"] = await s.section("counters", lambda: s.counters(report["interfaces"]))
        report["addresses"] = await s.section("addresses", s.addresses)
        report["lldp"] = await s.section("lldp", s.lldp)
        report["cdp"] = await s.section("cdp", s.cdp)
        report["ok"] = True
    except (SnmpTimeout, SnmpSecurityError) as exc:
        # The device stopped answering or refused us: the remaining parts would only time out.
        report["ok"] = False
        report["error"] = str(exc)
        report["error_kind"] = "auth" if isinstance(exc, SnmpSecurityError) else "timeout"
    report["sections"] = s.sections
    report["unanswered_columns"] = s.unanswered
    # What the poller remembers about this device (profile.py); the survey is the probe.
    report["profile"] = profile_from_survey(report, c).to_dict()
    return report


async def run_survey(host: str, creds: Credentials, *, port: int = 161, timeout: float = 2.0, retries: int = 1,
                     max_repetitions: int = 20, use_bulk: bool = True, interval: float = 10.0) -> Dict[str, Any]:
    started = time.monotonic()
    report: Dict[str, Any] = {
        "target": f"{host}:{port}",
        "surveyed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snmp": "v2c" if isinstance(creds, CommunityAuth) else f"v3 {creds.level} user={creds.user.decode(errors='replace')}"
                + (f" context={creds.context.decode(errors='replace')}" if isinstance(creds, UserAuth) and creds.context else "")
                + (f" auth={creds.auth}" if isinstance(creds, UserAuth) and creds.auth else "")
                + (f" priv={creds.priv}" if isinstance(creds, UserAuth) and creds.priv else ""),
    }
    async with SnmpClient(host, creds, port=port, timeout=timeout, retries=retries, max_repetitions=max_repetitions) as c:
        report.update(await read_device(c, use_bulk=use_bulk, interval=interval))
        report["totals"] = {"requests": c.stats.requests, "sends": c.stats.sends, "timeouts": c.stats.timeouts,
                            "dropped": c.stats.dropped, "bytes_in": c.stats.bytes_in, "bytes_out": c.stats.bytes_out,
                            "max_repetitions": c.max_repetitions, "seconds": round(time.monotonic() - started, 2)}
    report["findings"] = findings(report)
    return report


# --- output ------------------------------------------------------------------------------------

def render_text(r: Dict[str, Any]) -> str:
    lines = [f"SNMP survey of {r['target']} ({r['snmp']}) at {r['surveyed_at']}"]
    if not r.get("ok"):
        lines.append(f"  FAILED: {r.get('error')}")
    sysinfo = r.get("system") or {}
    if sysinfo:
        lines.append("")
        lines.append(f"  Name:      {text_of(sysinfo.get('sysName'))}")
        lines.append(f"  Describes: {text_of(sysinfo.get('sysDescr'))[:150]}")
        lines.append(f"  Object ID: {text_of(sysinfo.get('sysObjectID'))}")
        lines.append(f"  Location:  {text_of(sysinfo.get('sysLocation'))}")
        lines.append(f"  Uptime:    agent {duration_of(sysinfo.get('sysUpTime'))}, host {duration_of(sysinfo.get('hrSystemUptime'))}, "
                     f"engine boots {sysinfo.get('snmpEngineBoots', '-')}")
    if r.get("interfaces"):
        lines.append("")
        lines.append(f"  {'idx':>5} {'name':<18} {'alias / description':<28} {'Mb/s':>7} {'admin':<5} {'oper':<14} {'64b':<3} {'in err':>7} {'out err':>7}")
        for x in r["interfaces"]:
            speed = x.get("ifHighSpeed")
            if speed is None and x.get("ifSpeed") is not None:
                speed = x["ifSpeed"] // 1_000_000
            lines.append(
                f"  {x['ifIndex']:>5} {text_of(x.get('ifName'))[:18]:<18} {text_of(x.get('ifAlias') or x.get('ifDescr'))[:28]:<28} "
                f"{'-' if speed is None else speed:>7} {mib.IF_ADMIN_STATUS.get(x.get('ifAdminStatus'), '-'):<5} "
                f"{mib.IF_OPER_STATUS.get(x.get('ifOperStatus'), '-'):<14} {'yes' if 'ifHCInOctets' in x else 'no':<3} "
                f"{x.get('ifInErrors', '-'):>7} {x.get('ifOutErrors', '-'):>7}")
    counters = r.get("counters")
    if counters:
        busy = [x for x in counters["rates"] if x["in_bps"] or x["out_bps"]]
        lines.append("")
        lines.append(f"  Traffic over {counters['seconds']} s ({counters['counter']}): {len(busy)} interfaces moving")
        for x in busy[:20]:
            lines.append(f"    if{x['ifIndex']}: in {x['in_bps']:,} b/s, out {x['out_bps']:,} b/s")
    if r.get("addresses"):
        lines.append("")
        lines.append("  Addresses: " + ", ".join(f"{a['address']} (if{a['ifIndex']})" for a in r["addresses"]))
    if_names = {x["ifIndex"]: text_of(x.get("ifName") or x.get("ifDescr")) for x in r.get("interfaces") or []}
    for proto in ("lldp", "cdp"):
        block = r.get(proto)
        if block and block["neighbours"]:
            lines.append("")
            lines.append(f"  {proto.upper()} neighbours:")
            for nb in block["neighbours"]:
                local = nb.get("local_port") or if_names.get(nb.get("local_ifIndex")) \
                    or f"if{nb.get('local_ifIndex') or nb.get('local_port_num')}"
                addr = ", ".join(nb.get("remote_addresses") or []) or nb.get("remote_address") or ""
                lines.append(f"    {local} -> {nb['remote_name'] or nb.get('remote_chassis', '')} port {nb['remote_port']} {addr}".rstrip())
    lines.append("")
    lines.append("  Findings:")
    lines.extend(f"    - {f}" for f in r["findings"])
    if r.get("sections"):
        lines.append("")
        lines.append("  Cost per part: " + "; ".join(
            f"{name} {s['requests']} req {s['seconds']} s" + ("" if s.get("ok") else f" ({s.get('error')})")
            for name, s in r["sections"].items()))
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) and not (isinstance(value, tuple) and all(isinstance(i, int) for i in value) and len(value) > 1):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bytes, tuple)):
        return text_of(value)
    return value


def _secret(env: str, prompt: str) -> bytes:
    value = os.environ.get(env)
    if value is None:
        value = getpass.getpass(prompt)
    return value.encode()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m monitoring.snmp.survey", description=__doc__.split("\n\n")[0])
    p.add_argument("host")
    p.add_argument("--port", type=int, default=161)
    p.add_argument("--version", choices=("2c", "3"), default="2c")
    p.add_argument("--user", help="SNMPv3 user name")
    p.add_argument("--auth", choices=sorted(usm.AUTH_PROTOCOLS), help="SNMPv3 authentication protocol")
    p.add_argument("--priv", choices=usm.PRIV_PROTOCOLS,
                   help="SNMPv3 privacy protocol: aes (= aes128), aes192/aes256 (net-snmp style keys), "
                        "aes192c/aes256c (Cisco style keys: Cisco, HPE Aruba, FortiGate aes256cisco), des, 3des")
    p.add_argument("--priv-same", action="store_true",
                   help="use the authentication password for privacy too (Meraki and UniFi require this)")
    p.add_argument("--context", default="", help="SNMPv3 context name (default: none)")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds per try (default 2)")
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--max-repetitions", type=int, default=20)
    p.add_argument("--no-bulk", action="store_true", help="use GETNEXT instead of GETBULK")
    p.add_argument("--interval", type=float, default=10.0, help="seconds between the two counter reads (0 skips)")
    p.add_argument("--json", metavar="FILE", help="also write the full report as JSON")
    a = p.parse_args(argv)

    if a.version == "2c":
        creds: Credentials = CommunityAuth(_secret("EPHEMRES_SNMP_COMMUNITY", "Community: "))
    else:
        if not a.user:
            p.error("--user is needed for SNMPv3")
        if a.priv and not a.auth:
            p.error("--priv needs --auth as well")
        auth_pw = _secret("EPHEMRES_SNMP_AUTH_PASSWORD", "Authentication password: ") if a.auth else None
        priv_pw = None
        if a.priv and not a.priv_same:
            # Pressing Enter at the prompt means "same as authentication" (None to the client).
            priv_pw = _secret("EPHEMRES_SNMP_PRIV_PASSWORD", "Privacy password (Enter = same as authentication): ") or None
        creds = UserAuth(a.user.encode(), a.auth, auth_pw, a.priv, priv_pw, context=a.context.encode())

    report = asyncio.run(run_survey(a.host, creds, port=a.port, timeout=a.timeout, retries=a.retries,
                                    max_repetitions=a.max_repetitions, use_bulk=not a.no_bulk, interval=a.interval))
    print(render_text(report))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(_jsonable(report), fh, indent=2)
        print(f"\nFull report: {a.json}")
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
