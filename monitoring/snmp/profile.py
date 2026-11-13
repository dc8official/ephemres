"""What one device answers, learned once and remembered (vendor research, design rule 3).

Every device family answers a different part of the fixed scope and has its own habits: some
silently drop a request that touches a table they cannot serve (Meraki MS/MR, RouterOS), some
have a broken GETBULK, some lack 64-bit counters or LLDP. Polling blindly would pay a timeout
for each of those on every poll. The profile records what the first full read (the survey)
found, so regular polls ask only for what works, and ask for it in a way that works.

It is plain data with to_dict()/from_dict() so the poller can keep it in the database as JSON.
A profile goes stale when the device reboots or changes software, so the poller refreshes it
then (sysUpTime or engine boots going back, or sysDescr changing), when polls start failing,
and once a day.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from monitoring.snmp import ber, mib

PROFILE_VERSION = 1


@dataclass
class DeviceProfile:
    probed_at: str
    vendor: Optional[str] = None  # from sysObjectID, for people and setup hints only
    enterprise: Optional[int] = None
    sys_descr: Optional[str] = None  # a change means new software: re-probe
    engine_id: Optional[str] = None  # hex, SNMPv3 only
    engine_boots: Optional[int] = None
    sys_uptime: Optional[int] = None  # TimeTicks at the probe
    # What answered. Keys: system, interfaces, if_x, hc_counters, discontinuity, addresses,
    # lldp ("LLDP-MIB" / "LLDP-V2-MIB" / None), cdp, hr_uptime, engine_boots.
    answers: Dict[str, Any] = field(default_factory=dict)
    use_bulk: bool = True
    max_repetitions: int = 20
    walk_alone: List[str] = field(default_factory=list)  # dotted column OIDs
    unanswered: List[str] = field(default_factory=list)  # column names the device never answered
    average_reply_ms: Optional[float] = None
    version: int = PROFILE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceProfile":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def walk_alone_oids(self) -> List[ber.Oid]:
        return [ber.parse_oid(o) for o in self.walk_alone]

    def client_options(self) -> Dict[str, Any]:
        """Keyword arguments for SnmpClient so it starts from what this device needs."""
        return {"use_bulk": self.use_bulk, "max_repetitions": self.max_repetitions,
                "walk_alone": self.walk_alone_oids}


def enterprise_of(sys_object_id: Any) -> Optional[int]:
    if isinstance(sys_object_id, tuple) and len(sys_object_id) > 6 and sys_object_id[:6] == (1, 3, 6, 1, 4, 1):
        return sys_object_id[6]
    return None


def profile_from_survey(report: Dict[str, Any], client: Any) -> DeviceProfile:
    """Builds the profile from a survey report and the client that ran it (whose use_bulk,
    max_repetitions and walk_alone were learned during the survey)."""
    sysinfo = report.get("system") or {}
    ifs = report.get("interfaces") or []
    enterprise = enterprise_of(sysinfo.get("sysObjectID"))
    descr = sysinfo.get("sysDescr")
    lldp = report.get("lldp") or {}
    cdp = report.get("cdp") or {}
    answers = {
        "system": bool(sysinfo),
        "interfaces": bool(ifs),
        "if_x": any("ifName" in r or "ifHCInOctets" in r for r in ifs),
        "hc_counters": any("ifHCInOctets" in r for r in ifs),
        "discontinuity": any("ifCounterDiscontinuityTime" in r for r in ifs),
        "addresses": bool(report.get("addresses")),
        "lldp": lldp.get("mib") if lldp.get("answered") else None,
        "cdp": bool(cdp.get("answered")),
        "hr_uptime": sysinfo.get("hrSystemUptime") is not None,
        "engine_boots": sysinfo.get("snmpEngineBoots") is not None,
    }
    return DeviceProfile(
        probed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        vendor=mib.ENTERPRISES.get(enterprise) if enterprise is not None else None,
        enterprise=enterprise,
        sys_descr=" ".join(descr.decode("utf-8", "replace").split())[:255] if isinstance(descr, bytes) else None,
        engine_id=client.engine_id.hex() if getattr(client, "engine_id", None) else None,
        engine_boots=sysinfo.get("snmpEngineBoots") if sysinfo.get("snmpEngineBoots") is not None else (
            getattr(client, "engine_boots", None) if getattr(client, "engine_id", None) else None),
        sys_uptime=sysinfo.get("sysUpTime"),
        answers=answers,
        use_bulk=client.use_bulk,
        max_repetitions=client.max_repetitions,
        walk_alone=sorted(ber.format_oid(o) for o in client.walk_alone),
        unanswered=sorted(report.get("unanswered_columns") or {}),
        average_reply_ms=client.stats.average_reply_ms,
    )
