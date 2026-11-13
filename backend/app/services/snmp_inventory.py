"""Interface names and speeds from SNMP, under the manual ones (v3.3.6 build plan 8.3).

ephemres-snmpd stores each polled device's interfaces in snmp_interfaces. Everything that shows
or computes with an interface's name or speed reads the endpoint's manual map
(flow_interface_aliases) through merge() here, so a device with SNMP needs no typing:

- name: the manual name, else the device's interface description (ifAlias), else its
  interface name (ifName), else its long name (ifDescr);
- speed: the manual speed, else ifHighSpeed. A manual value still wins: the contracted link
  may be 50 Mb on a 1 Gb port;
- role (WAN/LAN) and the traffic alert rule stay manual only; SNMP cannot know them.

The merged map is for reading only. It is never stored and never given to the interface
editors as manual values (they get the SNMP values separately, as defaults to show), so an
SNMP name does not become a manual one by being saved.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

from app.core.orm import AsyncSession, text

SnmpMap = Dict[int, Dict[str, Any]]


async def load(db: AsyncSession, endpoint_ids: Iterable[Any]) -> Dict[str, SnmpMap]:
    """endpoint id -> ifIndex -> {name, alias, descr, speed_mbps, oper_status, admin_status}."""
    ids = tuple(str(i) for i in endpoint_ids if i)
    if not ids:
        return {}
    try:
        res = await db.execute(text("""
            SELECT endpoint_id, if_index, if_name, if_alias, if_descr, speed_mbps, oper_status, admin_status
            FROM snmp_interfaces WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids})
    except Exception:
        # Before migration 0026 (or a test database without it): no SNMP, manual names only.
        return {}
    out: Dict[str, SnmpMap] = {}
    for ep, idx, name, alias, descr, speed, oper, admin in res.fetchall():
        out.setdefault(str(ep), {})[int(idx)] = {
            "name": name, "alias": alias, "descr": descr,
            "speed_mbps": int(speed) if speed else None, "oper_status": oper,
            # Shut down by an admin ("down") vs failed (admin "up", oper "down"): the Traffic
            # page's WAN links say which (build plan 7.5 step 4).
            "admin_status": admin,
        }
    return out


def snmp_name(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    return row.get("alias") or row.get("name") or row.get("descr") or None


def _manual(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if isinstance(entry, str):
        return {"name": entry}  # legacy name-only alias
    return {}


def _decode(aliases: Any) -> Dict[str, Any]:
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            return {}
    return aliases if isinstance(aliases, dict) else {}


def merge(aliases: Any, snmp: Optional[SnmpMap]) -> Dict[str, Any]:
    """The manual map with SNMP filling what is not set. Only interfaces that already have a
    manual entry or are known to SNMP appear; manual-only entries are kept as they are."""
    manual = _decode(aliases)
    if not snmp:
        return manual
    merged: Dict[str, Any] = {k: v for k, v in manual.items()}
    for idx, row in snmp.items():
        key = str(idx)
        entry = _manual(manual.get(key))
        if not entry.get("name"):
            name = snmp_name(row)
            if name:
                entry["name"] = name
        try:
            manual_speed = int(entry.get("speed_mbps") or 0)
        except (TypeError, ValueError):
            manual_speed = 0
        if manual_speed <= 0 and row.get("speed_mbps"):
            entry["speed_mbps"] = int(row["speed_mbps"])
        if entry:
            merged[key] = entry
    return merged


def sources(aliases: Any, snmp_row: Optional[Dict[str, Any]], idx: int) -> Dict[str, Any]:
    """Where an interface's shown name and speed come from, and SNMP's own values, for the
    interface editors (they show SNMP's as greyed defaults under the manual fields)."""
    entry = _manual(_decode(aliases).get(str(idx)))
    try:
        manual_speed = int(entry.get("speed_mbps") or 0)
    except (TypeError, ValueError):
        manual_speed = 0
    s_name = snmp_name(snmp_row)
    s_speed = (snmp_row or {}).get("speed_mbps") or None
    return {
        "name_source": "manual" if entry.get("name") else "snmp" if s_name else "default",
        "speed_source": "manual" if manual_speed > 0 else "snmp" if s_speed else "default",
        "snmp_name": s_name,
        "snmp_speed_mbps": s_speed,
        "oper_status": (snmp_row or {}).get("oper_status"),
    }


async def merged_for(db: AsyncSession, endpoint_aliases: Dict[Any, Any]) -> Dict[str, Dict[str, Any]]:
    """{endpoint id: raw aliases} -> {endpoint id: merged aliases}, one query for all."""
    snmp = await load(db, endpoint_aliases.keys())
    return {str(ep): merge(aliases, snmp.get(str(ep))) for ep, aliases in endpoint_aliases.items()}
