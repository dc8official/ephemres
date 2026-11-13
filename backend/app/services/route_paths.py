"""Normal and current route per endpoint, and the route-change log (v3.3.6 build plan, 6.4).

Every traceroute to an endpoint goes through record_trace(), which updates only the CURRENT
path, the one root-cause analysis compares a failure with. The NORMAL path is what the endpoint
uses when nothing is wrong; it follows the current path after 24 hours, unless an admin pinned
it. Decided with Kenneth on 2026-11-12 after walking one branch failover on paper:

- A different path becomes current only when two traces at least 2 minutes apart agree
  (CONFIRM_AFTER). The first one is kept as "pending". A trace an admin asked for (Refresh
  route) and the first trace of a new endpoint count at once.
- After a change, at most one log entry per endpoint per 15 minutes (LOG_EVERY). 4 or more
  changes within an hour (FLAP_CHANGES, FLAP_WINDOW) mark the endpoint flapping: one entry,
  then quiet until it settles for an hour.
- A current path held 24 hours (PROMOTE_AFTER) becomes normal (promote_normal_paths, run by
  the discovery scheduler); a pinned normal path is never replaced automatically.

Two traces are the same path when, after dropping unanswered hops at the end, they have as many
hops and every hop answered from the same address or did not answer in one of them: routers
that rate-limit their replies leave "* * *" holes, and a hole is not a route change.

Hops are lists of {"hop": n, "ip": "a.b.c.d" | None, "rtt_ms": x}, as stored since v3.0.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from app.core.orm import AsyncSession, text

logger = logging.getLogger(__name__)

CONFIRM_AFTER = timedelta(minutes=2)
LOG_EVERY = timedelta(minutes=15)
FLAP_WINDOW = timedelta(hours=1)
FLAP_CHANGES = 4
PROMOTE_AFTER = timedelta(hours=24)
LOG_RETENTION = timedelta(days=90)

# What started a trace, as stored in endpoint_route_changes.trigger.
TRIGGERS = ("added", "manual", "scheduled", "recovery", "route_hint", "transit", "ttl", "confirm")


# --- comparing paths ----------------------------------------------------------------------

def as_hops(value: Any) -> List[Dict[str, Any]]:
    """Hops from a JSONB value, which the driver may hand back as text."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [h for h in value if isinstance(h, dict)] if isinstance(value, list) else []


def _ips(hops: Sequence[Dict[str, Any]]) -> List[Optional[str]]:
    ips = [h.get("ip") for h in hops]
    while ips and ips[-1] is None:
        ips.pop()
    return ips


def has_answers(hops: Sequence[Dict[str, Any]]) -> bool:
    return any(h.get("ip") for h in hops)


def same_path(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> bool:
    ia, ib = _ips(a), _ips(b)
    if len(ia) != len(ib):
        return False
    return all(x is None or y is None or x == y for x, y in zip(ia, ib))


def first_difference(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> Optional[int]:
    """Hop number (1-based) where b first differs from a, or None when they are the same path."""
    ia, ib = _ips(a), _ips(b)
    for i in range(max(len(ia), len(ib))):
        x = ia[i] if i < len(ia) else None
        y = ib[i] if i < len(ib) else None
        if i >= len(ia) or i >= len(ib) or (x is not None and y is not None and x != y):
            return i + 1
    return None


def merge_holes(old: Sequence[Dict[str, Any]], new: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The new trace of the same path, with hops it missed filled in from the old one, so a
    rate-limited reply doesn't punch a lasting hole in the map."""
    out = []
    for i, h in enumerate(new):
        if h.get("ip") is None and i < len(old) and old[i].get("ip"):
            out.append({**old[i], "rtt_ms": None})
        else:
            out.append(dict(h))
    return out


def describe_path(hops: Sequence[Dict[str, Any]], at_hop: Optional[int] = None) -> str:
    """'via 172.16.1.1' (the hop where it differs), or the first answering hop."""
    if at_hop and 1 <= at_hop <= len(hops) and hops[at_hop - 1].get("ip"):
        return f"via {hops[at_hop - 1]['ip']}"
    for h in hops:
        if h.get("ip"):
            return f"via {h['ip']}"
    return "with no answering hops"


# --- recording a trace --------------------------------------------------------------------

async def _log(
    db: AsyncSession,
    endpoint_id: UUID,
    kind: str,
    trigger: Optional[str],
    from_hops: Optional[Sequence[Dict[str, Any]]],
    to_hops: Optional[Sequence[Dict[str, Any]]],
    now: datetime,
    detail: Optional[str] = None,
) -> None:
    diff = first_difference(from_hops or [], to_hops or []) if from_hops is not None and to_hops is not None else None
    await db.execute(
        text("""
            INSERT INTO endpoint_route_changes
                (endpoint_id, changed_at, kind, trigger, from_hops, to_hops, first_diff_hop, detail)
            VALUES (CAST(:eid AS uuid), CAST(:now AS timestamptz), :kind, :trigger,
                    CAST(:from_hops AS jsonb), CAST(:to_hops AS jsonb), CAST(:diff AS integer), :detail)
        """),
        {
            "eid": str(endpoint_id), "now": now, "kind": kind, "trigger": trigger,
            "from_hops": None if from_hops is None else list(from_hops),
            "to_hops": None if to_hops is None else list(to_hops),
            "diff": diff, "detail": detail,
        },
    )


async def record_trace(
    db: AsyncSession,
    endpoint_id: UUID,
    hops: List[Dict[str, Any]],
    trigger: str = "scheduled",
    *,
    confirm_now: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Applies one traceroute to the endpoint's paths and returns what happened:

    {"outcome": "first" | "unchanged" | "pending" | "changed" | "back_to_normal" | "no_answer",
     "hops": the current path afterwards, "confirm_after": seconds until a confirming trace may
     count (pending only)}.

    "pending" means the caller should trace again after confirm_after seconds. `confirm_now`
    (an admin's Refresh route) skips the confirmation."""
    now = now or datetime.now(timezone.utc)
    res = await db.execute(
        text("""
            SELECT hops, current_since, normal_hops, normal_pinned, pending_hops, pending_at,
                   recent_changes, flapping_since, last_logged_at
            FROM endpoint_baseline_routes WHERE endpoint_id = CAST(:eid AS uuid)
            FOR UPDATE
        """),
        {"eid": str(endpoint_id)},
    )
    row = res.fetchone()

    if row is None:
        # First path ever: current and normal at once. Nothing to log; there is no "before".
        await db.execute(
            text("""
                INSERT INTO endpoint_baseline_routes
                    (endpoint_id, updated_at, total_hops, hops, current_since, normal_hops, normal_since)
                VALUES (CAST(:eid AS uuid), CAST(:now AS timestamptz), :n, CAST(:hops AS jsonb),
                        CAST(:now AS timestamptz), CAST(:hops AS jsonb), CAST(:now AS timestamptz))
                ON CONFLICT (endpoint_id) DO NOTHING
            """),
            {"eid": str(endpoint_id), "now": now, "n": len(hops), "hops": list(hops)},
        )
        return {"outcome": "first", "hops": hops}

    current = as_hops(row[0])
    normal = as_hops(row[2]) or current
    pending = as_hops(row[4])
    pending_at = row[5]

    if not has_answers(hops):
        # Nothing answered (filtered, or the box is down): keep the last-known-good path, as
        # before v3.3.6. Not evidence of a route change either way.
        logger.warning("Trace to %s got no answers; current path kept.", endpoint_id)
        return {"outcome": "no_answer", "hops": current}

    if same_path(hops, current):
        merged = merge_holes(current, hops)
        await db.execute(
            text("""
                UPDATE endpoint_baseline_routes
                SET updated_at = CAST(:now AS timestamptz), hops = CAST(:hops AS jsonb),
                    total_hops = :n, pending_hops = NULL, pending_at = NULL
                WHERE endpoint_id = CAST(:eid AS uuid)
            """),
            {"eid": str(endpoint_id), "now": now, "hops": merged, "n": len(merged)},
        )
        return {"outcome": "unchanged", "hops": merged}

    confirmed = confirm_now or (
        pending and pending_at is not None and same_path(hops, pending) and now - pending_at >= CONFIRM_AFTER
    )
    if not confirmed:
        if pending and pending_at is not None and same_path(hops, pending):
            wait = (CONFIRM_AFTER - (now - pending_at)).total_seconds()
            return {"outcome": "pending", "hops": current, "confirm_after": max(1.0, wait)}
        await db.execute(
            text("""
                UPDATE endpoint_baseline_routes
                SET pending_hops = CAST(:hops AS jsonb), pending_at = CAST(:now AS timestamptz)
                WHERE endpoint_id = CAST(:eid AS uuid)
            """),
            {"eid": str(endpoint_id), "now": now, "hops": list(hops)},
        )
        return {"outcome": "pending", "hops": current, "confirm_after": CONFIRM_AFTER.total_seconds()}

    # A confirmed change of the current path.
    back = same_path(hops, normal)
    kind = "back_to_normal" if back else "path_changed"
    recent = [t for t in (row[6] or []) if now - t < FLAP_WINDOW] + [now]
    flapping_since = row[7]
    last_logged = row[8]

    log_kind: Optional[str] = None
    if flapping_since is not None:
        # Already flapping: silent until it has been quiet for an hour (promote_normal_paths
        # clears the flag), however few changes the last hour holds now.
        pass
    elif len(recent) >= FLAP_CHANGES:
        flapping_since = now
        log_kind = "flapping"
    elif last_logged is None or now - last_logged >= LOG_EVERY:
        log_kind = kind

    detail = None
    if log_kind == "flapping":
        detail = f"{len(recent)} route changes within an hour; further changes are not logged until it settles."
    elif log_kind == "path_changed":
        detail = f"Left the normal path ({describe_path(normal, first_difference(normal, hops))})."
    if log_kind:
        await _log(db, endpoint_id, log_kind, trigger, current, hops, now, detail)
        last_logged = now

    await db.execute(
        text("""
            UPDATE endpoint_baseline_routes
            SET hops = CAST(:hops AS jsonb), total_hops = :n, updated_at = CAST(:now AS timestamptz),
                current_since = CAST(:now AS timestamptz), pending_hops = NULL, pending_at = NULL,
                recent_changes = CAST(:recent AS timestamptz[]),
                flapping_since = CAST(:flap AS timestamptz),
                last_logged_at = CAST(:logged AS timestamptz)
            WHERE endpoint_id = CAST(:eid AS uuid)
        """),
        {
            "eid": str(endpoint_id), "now": now, "hops": list(hops), "n": len(hops),
            "recent": tuple(recent), "flap": flapping_since, "logged": last_logged,
        },
    )
    logger.info("Route of %s changed (%s, trigger %s).", endpoint_id, kind, trigger)
    return {"outcome": kind, "hops": hops}


# --- normal path ----------------------------------------------------------------------------

async def promote_normal_paths(db: AsyncSession, now: Optional[datetime] = None) -> int:
    """Makes a current path held for 24 hours the normal one (unpinned endpoints), ends
    flapping that has been quiet for an hour, and prunes the log. Returns paths promoted."""
    now = now or datetime.now(timezone.utc)
    res = await db.execute(
        text("""
            SELECT endpoint_id, hops, normal_hops FROM endpoint_baseline_routes
            WHERE NOT normal_pinned
              AND current_since <= CAST(:cutoff AS timestamptz)
              AND normal_hops IS DISTINCT FROM hops
        """),
        {"cutoff": now - PROMOTE_AFTER},
    )
    promoted = 0
    for endpoint_id, hops_raw, normal_raw in res.fetchall():
        hops, normal = as_hops(hops_raw), as_hops(normal_raw)
        if normal and same_path(hops, normal):
            continue
        await db.execute(
            text("""
                UPDATE endpoint_baseline_routes
                SET normal_hops = hops, normal_since = CAST(:now AS timestamptz)
                WHERE endpoint_id = CAST(:eid AS uuid)
            """),
            {"eid": str(endpoint_id), "now": now},
        )
        await _log(db, endpoint_id, "became_normal", None, normal or None, hops, now,
                   "Current path held for 24 hours; it is now the normal path.")
        promoted += 1
    await db.execute(
        text("""
            UPDATE endpoint_baseline_routes SET flapping_since = NULL
            WHERE flapping_since IS NOT NULL
              AND current_since <= CAST(:quiet AS timestamptz)
        """),
        {"quiet": now - FLAP_WINDOW},
    )
    await db.execute(
        text("DELETE FROM endpoint_route_changes WHERE changed_at < CAST(:before AS timestamptz)"),
        {"before": now - LOG_RETENTION},
    )
    return promoted


async def set_normal_path(
    db: AsyncSession, endpoint_id: UUID, pinned: bool, now: Optional[datetime] = None
) -> bool:
    """Admin action: the current path becomes the normal one now, pinned or not. Returns False
    when the endpoint has no path yet."""
    now = now or datetime.now(timezone.utc)
    res = await db.execute(
        text("""
            SELECT hops, normal_hops, normal_pinned FROM endpoint_baseline_routes
            WHERE endpoint_id = CAST(:eid AS uuid) FOR UPDATE
        """),
        {"eid": str(endpoint_id)},
    )
    row = res.fetchone()
    if row is None:
        return False
    hops, normal = as_hops(row[0]), as_hops(row[1])
    await db.execute(
        text("""
            UPDATE endpoint_baseline_routes
            SET normal_hops = hops, normal_since = CAST(:now AS timestamptz), normal_pinned = :pinned
            WHERE endpoint_id = CAST(:eid AS uuid)
        """),
        {"eid": str(endpoint_id), "now": now, "pinned": bool(pinned)},
    )
    kind = "pinned" if pinned else "made_normal"
    await _log(db, endpoint_id, kind, "manual", normal or None, hops, now,
               "Pinned as the normal path by an administrator." if pinned
               else "Made the normal path by an administrator.")
    return True


async def unpin_normal_path(db: AsyncSession, endpoint_id: UUID, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    res = await db.execute(
        text("""
            UPDATE endpoint_baseline_routes SET normal_pinned = FALSE
            WHERE endpoint_id = CAST(:eid AS uuid) AND normal_pinned
            RETURNING endpoint_id
        """),
        {"eid": str(endpoint_id)},
    )
    if res.fetchone() is None:
        return False
    await _log(db, endpoint_id, "unpinned", "manual", None, None, now,
               "Unpinned: the normal path follows the current one again after 24 hours.")
    return True


# --- reading --------------------------------------------------------------------------------

async def path_status(db: AsyncSession, endpoint_id: UUID, log_limit: int = 50) -> Optional[Dict[str, Any]]:
    """Current and normal path, pending and flapping state, and the latest log entries."""
    res = await db.execute(
        text("""
            SELECT hops, current_since, updated_at, normal_hops, normal_since, normal_pinned,
                   pending_hops, pending_at, flapping_since
            FROM endpoint_baseline_routes WHERE endpoint_id = CAST(:eid AS uuid)
        """),
        {"eid": str(endpoint_id)},
    )
    row = res.fetchone()
    if row is None:
        return None
    current, normal = as_hops(row[0]), as_hops(row[3]) or as_hops(row[0])
    on_normal = same_path(current, normal)
    log_res = await db.execute(
        text("""
            SELECT changed_at, kind, trigger, from_hops, to_hops, first_diff_hop, detail
            FROM endpoint_route_changes WHERE endpoint_id = CAST(:eid AS uuid)
            ORDER BY changed_at DESC LIMIT :n
        """),
        {"eid": str(endpoint_id), "n": int(log_limit)},
    )
    return {
        "current_hops": current,
        "current_since": row[1],
        "last_traced_at": row[2],
        "normal_hops": normal,
        "normal_since": row[4],
        "normal_pinned": bool(row[5]),
        "on_normal_path": on_normal,
        "first_diff_hop": None if on_normal else first_difference(normal, current),
        "pending_hops": as_hops(row[6]) or None,
        "pending_since": row[7],
        "flapping_since": row[8],
        "changes": [
            {
                "changed_at": r[0], "kind": r[1], "trigger": r[2],
                "from_hops": as_hops(r[3]) or None, "to_hops": as_hops(r[4]) or None,
                "first_diff_hop": r[5], "detail": r[6],
            }
            for r in log_res.fetchall()
        ],
    }


def alternate_path_note(current: Sequence[Dict[str, Any]], normal: Sequence[Dict[str, Any]],
                        current_since: Optional[datetime]) -> Optional[str]:
    """For RCA: 'On an alternate path since 14:02 UTC (normal path via 172.16.1.1).' or None."""
    if not normal or same_path(current, normal):
        return None
    diff = first_difference(normal, current)
    since = current_since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if current_since else "an unknown time"
    return f"On an alternate path since {since} (normal path {describe_path(normal, diff)})."
