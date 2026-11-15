"""Names for addresses that are no endpoint (decided 2026-11-15, build plan 11b; review O5).

The Traffic page showed outside addresses as numbers: "52.97.146.2" is Microsoft 365, but nothing
said so. Two sources, in order:

1. The providers' published ranges, shipped with each release in app/data/provider_ranges.json
   (deploy/update-provider-ranges.sh): Microsoft 365 and Teams, Google, AWS and CloudFront,
   Cloudflare, Zoom. No lookup at run time, so it works on a server without Internet access.
   Re-read within TABLE_CHECK_SECONDS when the script replaces the file: no restart needed.
2. Reverse DNS (PTR) for the rest, through the server's own resolver: an internal DNS often names
   users' PCs too. Never on the request path: names_for() answers from the cache and queues the
   unknown addresses, which a background task resolves (2 s each, 8 at a time). A page shows them
   from its next update, a minute later. Cached per API worker: a name for a day, "no name" for
   an hour.

Endpoints keep their own hostname; callers ask only for the other addresses.
"""
from __future__ import annotations

import asyncio
import bisect
import ipaddress
import json
import logging
import socket
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = Path(__file__).resolve().parents[1] / "data" / "provider_ranges.json"
DNS_TIMEOUT_SECONDS = 2.0
DNS_PARALLEL = 8
NAME_TTL_SECONDS = 24 * 3600
NO_NAME_TTL_SECONDS = 3600
# Queued lookups beyond this are dropped and asked for again on a later request.
QUEUE_LIMIT = 500
# How often the table file is checked for a newer copy (update-provider-ranges.sh on a server).
TABLE_CHECK_SECONDS = 300

_table: Optional[Tuple[List[str], Dict[int, Tuple[List[int], List[int], List[int]]]]] = None
_table_mtime: Optional[float] = None
_table_checked_at = 0.0
_dns_cache: Dict[str, Tuple[Optional[str], float]] = {}
_queue: "asyncio.Queue[str] | None" = None
_queued: set = set()
_worker: "asyncio.Task | None" = None


def _table_changed() -> bool:
    """True when the file's modification time differs from the loaded table's; checked at most
    every TABLE_CHECK_SECONDS, so a lookup costs no stat call."""
    global _table_checked_at
    now = time.monotonic()
    if now - _table_checked_at < TABLE_CHECK_SECONDS:
        return False
    _table_checked_at = now
    try:
        return TABLE.stat().st_mtime != _table_mtime
    except OSError:
        return _table_mtime is not None


def _load() -> Tuple[List[str], Dict[int, Tuple[List[int], List[int], List[int]]]]:
    """(names, {4|6: (firsts, lasts, name indexes)}) sorted by first address; empty if the table is
    missing or unreadable (reverse DNS still works). Reloaded when the file changes."""
    global _table, _table_mtime, _table_checked_at
    if _table is not None and _table_changed():
        _table = None
    if _table is None:
        _table_checked_at = time.monotonic()
        try:
            _table_mtime = TABLE.stat().st_mtime
        except OSError:
            _table_mtime = None
        names: List[str] = []
        families: Dict[int, Tuple[List[int], List[int], List[int]]] = {4: ([], [], []), 6: ([], [], [])}
        try:
            data = json.loads(TABLE.read_text(encoding="utf-8"))
            names = list(data.get("names") or [])
            for key, version in (("v4", 4), ("v6", 6)):
                firsts, lasts, idx = families[version]
                for first, last, i in data.get(key) or []:
                    firsts.append(int(first))
                    lasts.append(int(last))
                    idx.append(int(i))
        except (OSError, ValueError) as exc:
            logger.warning("Provider range table %s not loaded (%s); outside addresses get DNS names only.", TABLE, exc)
        _table = (names, families)
    return _table


def provider_name(address: str) -> Optional[str]:
    """"Microsoft 365", "Google", ... for an address in a published provider range, else None."""
    try:
        ip = ipaddress.ip_address(str(address).split("/")[0].strip())
    except ValueError:
        return None
    names, families = _load()
    firsts, lasts, idx = families[ip.version]
    value = int(ip)
    pos = bisect.bisect_right(firsts, value) - 1
    if pos >= 0 and value <= lasts[pos]:
        return names[idx[pos]]
    return None


def _cached_dns(address: str, now: float) -> Tuple[bool, Optional[str]]:
    hit = _dns_cache.get(address)
    if hit and hit[1] > now:
        return True, hit[0]
    return False, None


def names_for(addresses: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """{address: {"name", "source": "provider" | "dns"}} for the addresses with a name known now.
    Addresses with neither a provider range nor a cached reverse-DNS answer are queued for a lookup
    in the background (when an event loop is running) and named on a later call."""
    now = time.monotonic()
    out: Dict[str, Dict[str, str]] = {}
    unknown: List[str] = []
    for raw in addresses:
        address = str(raw).split("/")[0].strip()
        if not address or address in out:
            continue
        name = provider_name(address)
        if name:
            out[address] = {"name": name, "source": "provider"}
            continue
        known, dns_name = _cached_dns(address, now)
        if known:
            if dns_name:
                out[address] = {"name": dns_name, "source": "dns"}
        else:
            unknown.append(address)
    if unknown:
        _enqueue(unknown)
    return out


def _enqueue(addresses: List[str]) -> None:
    global _queue, _worker
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _queue is None or _worker is None or _worker.done() or _worker.get_loop() is not loop:
        _queue = asyncio.Queue()
        _queued.clear()
        _worker = loop.create_task(_resolve_forever(_queue))
    for address in addresses:
        if address in _queued or len(_queued) >= QUEUE_LIMIT:
            continue
        _queued.add(address)
        _queue.put_nowait(address)


def _reverse(address: str) -> Optional[str]:
    try:
        name = socket.gethostbyaddr(address)[0]
    except (OSError, UnicodeError):
        return None
    # A resolver that echoes the address back has no name for it.
    return None if not name or name == address else name.rstrip(".")


async def _resolve_forever(queue: "asyncio.Queue[str]") -> None:
    gate = asyncio.Semaphore(DNS_PARALLEL)
    loop = asyncio.get_running_loop()

    async def one(address: str) -> None:
        async with gate:
            try:
                name = await asyncio.wait_for(loop.run_in_executor(None, _reverse, address), DNS_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                name = None
            ttl = NAME_TTL_SECONDS if name else NO_NAME_TTL_SECONDS
            _dns_cache[address] = (name, time.monotonic() + ttl)
            _queued.discard(address)

    while True:
        address = await queue.get()
        loop.create_task(one(address))


def reset_for_tests() -> None:
    global _table, _queue, _worker, _table_mtime, _table_checked_at
    _table = None
    _table_mtime = None
    _table_checked_at = 0.0
    _dns_cache.clear()
    _queued.clear()
    _queue = None
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None
