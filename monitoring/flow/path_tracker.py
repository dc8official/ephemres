"""Forwarding paths seen in flows, and changes of the dominant path (v3.4 Phase 4).

For every flow with a monitored endpoint on one side, the exporting router reports where it
forwarded the traffic: ingress and egress interface and, when the template carries it, the
next hop. PathTracker keeps two things:

- Observations: running totals per (exporter, endpoint, direction, in_if, out_if, next_hop),
  drained by the aggregator into flow_path_observations on each flush.
- A change detector: per (exporter, endpoint, direction) the dominant (out_if, next_hop) of
  each arrival minute, by bytes. A different path must stay dominant for `change_minutes`
  minutes with traffic, with the old path below `ecmp_share`, before a change is reported.
  Two or more paths that each keep `ecmp_share` of the bytes are load balancing, not a change,
  once the split has lasted `change_minutes` minutes. A shorter split that brings in a new path
  is the minute a route switched (a switch rarely falls on a minute boundary): it counts towards
  that new path, and the change is reported when the new path then carries the traffic alone.

Minutes are arrival minutes (wall clock), not flow start times: exporters report long flows
late, so a flow-time minute is never known to be complete.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple
from uuid import UUID

DIRECTION_TO = "to"
DIRECTION_FROM = "from"

# (out_if, next_hop)
Path = Tuple[int, Optional[str]]
DetectorKey = Tuple[UUID, UUID, str]
ObservationKey = Tuple[UUID, UUID, str, int, int, Optional[str]]


@dataclass(frozen=True)
class RouteChange:
    exporter_id: UUID
    endpoint_id: UUID
    direction: str
    old_out_if: int
    old_next_hop: Optional[str]
    new_out_if: int
    new_next_hop: Optional[str]
    detected_at: datetime.datetime

    def to_event(self) -> Dict[str, object]:
        return {
            "event_type": "ROUTE_HINT",
            "exporter_id": str(self.exporter_id),
            "endpoint_id": str(self.endpoint_id),
            "direction": self.direction,
            "old_out_if": self.old_out_if,
            "old_next_hop": self.old_next_hop,
            "new_out_if": self.new_out_if,
            "new_next_hop": self.new_next_hop,
            "detected_at": self.detected_at.isoformat(),
        }


@dataclass
class _DetectorState:
    # The known path(s): one path, or the set of load-balanced paths.
    current: FrozenSet[Path]
    candidate: Optional[Path] = None
    streak: int = 0
    last_minute: int = 0
    # A split between known and new paths, and for how many minutes in a row it was seen.
    split: Optional[FrozenSet[Path]] = None
    split_streak: int = 0


class PathTracker:
    def __init__(
        self,
        change_minutes: int = 3,
        ecmp_share: float = 0.2,
        idle_minutes: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.change_minutes = max(1, int(change_minutes))
        self.ecmp_share = float(ecmp_share)
        self.idle_minutes = max(1, int(idle_minutes))
        self._clock = clock

        # key -> [first_seen_unix, last_seen_unix, bytes, flow_count]
        self._observations: Dict[ObservationKey, List[float]] = {}
        self._minute: Optional[int] = None
        self._minute_bytes: Dict[DetectorKey, Dict[Path, int]] = {}
        self._state: Dict[DetectorKey, _DetectorState] = {}
        self._changes: List[RouteChange] = []

    def observe(
        self,
        exporter_id: UUID,
        endpoint_id: UUID,
        direction: str,
        in_if: int,
        out_if: int,
        next_hop: Optional[str],
        flow_bytes: int,
        start_unix: float,
        end_unix: float,
    ) -> None:
        self._roll(self._clock())

        key = (exporter_id, endpoint_id, direction, in_if, out_if, next_hop)
        first, last = min(start_unix, end_unix), max(start_unix, end_unix)
        entry = self._observations.get(key)
        if entry is None:
            self._observations[key] = [first, last, flow_bytes, 1]
        else:
            entry[0] = min(entry[0], first)
            entry[1] = max(entry[1], last)
            entry[2] += flow_bytes
            entry[3] += 1

        paths = self._minute_bytes.setdefault((exporter_id, endpoint_id, direction), {})
        path = (out_if, next_hop)
        paths[path] = paths.get(path, 0) + flow_bytes

    def tick(self) -> None:
        """Closes the arrival minute if it has ended, even when no flow arrived since."""
        self._roll(self._clock())

    def drain_observations(self) -> Dict[ObservationKey, List[float]]:
        drained, self._observations = self._observations, {}
        return drained

    def restore_observations(self, drained: Dict[ObservationKey, List[float]]) -> None:
        """Puts back observations whose write failed, merging with any that arrived since."""
        for key, (first, last, nbytes, flows) in drained.items():
            entry = self._observations.get(key)
            if entry is None:
                self._observations[key] = [first, last, nbytes, flows]
            else:
                entry[0] = min(entry[0], first)
                entry[1] = max(entry[1], last)
                entry[2] += nbytes
                entry[3] += flows

    def drain_changes(self) -> List[RouteChange]:
        drained, self._changes = self._changes, []
        return drained

    def _roll(self, now: float) -> None:
        minute = int(now // 60)
        if self._minute is None:
            self._minute = minute
            return
        if minute == self._minute:
            return
        closed, self._minute_bytes = self._minute_bytes, {}
        closed_minute, self._minute = self._minute, minute
        detected_at = datetime.datetime.fromtimestamp((closed_minute + 1) * 60, tz=datetime.timezone.utc)
        # Forget keys idle too long before evaluating, so traffic that returns is learned afresh.
        stale = [k for k, st in self._state.items() if closed_minute - st.last_minute > self.idle_minutes]
        for k in stale:
            del self._state[k]
        for key, paths in closed.items():
            self._evaluate(key, paths, closed_minute, detected_at)

    def _evaluate(
        self, key: DetectorKey, paths: Dict[Path, int], minute: int, detected_at: datetime.datetime
    ) -> None:
        total = sum(paths.values())
        if total <= 0:
            return
        shares = {p: b / total for p, b in paths.items()}
        significant = frozenset(p for p, s in shares.items() if s >= self.ecmp_share)
        dominant = max(paths, key=lambda p: (paths[p], p[0], p[1] or ""))

        state = self._state.get(key)
        if state is None:
            self._state[key] = _DetectorState(current=significant if len(significant) > 1 else frozenset([dominant]), last_minute=minute)
            return
        state.last_minute = minute

        if len(significant) > 1:
            new_paths = significant - state.current
            if not new_paths:
                # The known load-balanced paths (or some of them) share the traffic.
                state.current = significant
                state.candidate, state.streak = None, 0
                state.split, state.split_streak = None, 0
                return
            if state.split == significant:
                state.split_streak += 1
            else:
                state.split, state.split_streak = significant, 1
            if state.split_streak >= self.change_minutes:
                # A lasting split is load balancing: learn the set, never report it as a change.
                state.current = significant
                state.candidate, state.streak = None, 0
                state.split, state.split_streak = None, 0
                return
            # Possibly the minute a route switched: count it towards the new path, report nothing yet.
            newest = max(new_paths, key=lambda p: (paths[p], p[0], p[1] or ""))
            if state.candidate == newest:
                state.streak += 1
            else:
                state.candidate, state.streak = newest, 1
            return

        state.split, state.split_streak = None, 0
        if dominant in state.current:
            if len(state.current) > 1:
                state.current = frozenset([dominant])  # one leg of a load-balanced set remains
            state.candidate, state.streak = None, 0
            return

        if state.candidate == dominant:
            state.streak += 1
        else:
            state.candidate, state.streak = dominant, 1
        if state.streak < self.change_minutes:
            return

        old = max(state.current, key=lambda p: (p[0], p[1] or ""))
        exporter_id, endpoint_id, direction = key
        self._changes.append(
            RouteChange(
                exporter_id=exporter_id,
                endpoint_id=endpoint_id,
                direction=direction,
                old_out_if=old[0],
                old_next_hop=old[1],
                new_out_if=dominant[0],
                new_next_hop=dominant[1],
                detected_at=detected_at,
            )
        )
        state.current = frozenset([dominant])
        state.candidate, state.streak = None, 0
