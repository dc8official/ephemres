"""Reply-TTL watch: a cheap early sign that an endpoint's route changed (v3.3.6 build plan 6.2).

Every router on the way back lowers a reply's TTL by one, so when the TTL an endpoint's replies
arrive with changes and stays changed, the return path got longer or shorter, e.g. after a link
failed and traffic moved to a backup. The engine reads it from every cycle (PingResult.reply_ttl:
from the IPv4 header on a raw socket; as ancillary data for raw IPv6 and the unprivileged ping
socket, since v3.3.6) and, once a new value has held for CONFIRM_CYCLES cycles in a row,
publishes a ROUTE_HINT with reason "ttl"; the API's leader worker then re-traces that endpoint
(services/telemetry_relay.py), and route_paths decides whether the path really changed.

A cycle without a TTL (no reply, or a socket that doesn't show it) changes nothing. The first
TTL seen is taken as the usual one without a hint. In-memory only: an engine restart starts
over, which costs at most one missed hint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# A new TTL must hold this many cycles in a row (the plan said 2 to 3; 3 matches the default
# state confirmation, so a single re-routed packet or a brief ECMP flip doesn't count).
CONFIRM_CYCLES = 3


@dataclass
class _Watch:
    stable: int
    candidate: Optional[int] = None
    seen: int = 0


class TtlWatch:
    def __init__(self, confirm_cycles: int = CONFIRM_CYCLES) -> None:
        self.confirm_cycles = confirm_cycles
        self._by_endpoint: Dict[str, _Watch] = {}

    def observe(self, endpoint_id: str, ttl: Optional[int]) -> Optional[Tuple[int, int]]:
        """Feeds one cycle's TTL. Returns (old, new) when a change has just been confirmed."""
        if ttl is None:
            return None
        key = str(endpoint_id)
        w = self._by_endpoint.get(key)
        if w is None:
            self._by_endpoint[key] = _Watch(stable=ttl)
            return None
        if ttl == w.stable:
            w.candidate, w.seen = None, 0
            return None
        if ttl == w.candidate:
            w.seen += 1
        else:
            w.candidate, w.seen = ttl, 1
        if w.seen >= self.confirm_cycles:
            old = w.stable
            w.stable, w.candidate, w.seen = ttl, None, 0
            return old, ttl
        return None

    def forget(self, endpoint_id: str) -> None:
        self._by_endpoint.pop(str(endpoint_id), None)


ttl_watch = TtlWatch()
