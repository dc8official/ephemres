"""Names for addresses that are no endpoint (decided 2026-11-15, build plan 11b; review O5).

The Traffic page showed outside addresses as numbers: "52.97.146.2" is Microsoft 365, but nothing
said so. Two sources, in order:

1. The providers' published ranges, shipped with each release in app/data/provider_ranges.json
   (deploy/update-provider-ranges.sh): Microsoft 365 and Teams, Google, AWS and CloudFront,
   Cloudflare, Zoom. No lookup at run time, so it works on a server without Internet access.
   Re-read within TABLE_CHECK_SECONDS when the script replaces the file: no restart needed.
2. Reverse DNS (PTR) for the rest, through the server's own resolver: an internal DNS often names
   users' PCs too. Never on the request path: names_for() answers from the cache and queues the
   unknown addresses, which a background task resolves, 8 at a time on threads of its own. A
   page shows them from its next update, a minute later. Cached per API worker: a name for a
   day, "no name" for an hour.

Endpoints keep their own hostname; callers ask only for the other addresses.
"""
from __future__ import annotations

import asyncio
import bisect
import concurrent.futures
import ipaddress
import json
import logging
import socket
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = Path(__file__).resolve().parents[1] / "data" / "provider_ranges.json"
# An address with no answer after this long is shown without a name for now ("no name" in the
# cache); the lookup itself runs on until the resolver gives up, and a late name is still kept.
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
# The lookups' own threads (first-phase review M2, 2026-11-16). They ran in the event loop's
# default pool, which FastStore commands, PDF rendering and asyncio.to_thread share: with a
# resolver that does not answer (a server without Internet access), blocked lookups filled it
# and a to_thread call waited 8 s. gethostbyaddr cannot be interrupted, so a slow resolver
# still holds these threads; now it holds nothing else.
_dns_pool: "concurrent.futures.ThreadPoolExecutor | None" = None


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


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _dns_pool
    if _dns_pool is None:
        _dns_pool = concurrent.futures.ThreadPoolExecutor(max_workers=DNS_PARALLEL, thread_name_prefix="ephemres-dns")
    return _dns_pool


def _remember(address: str, name: Optional[str]) -> None:
    ttl = NAME_TTL_SECONDS if name else NO_NAME_TTL_SECONDS
    _dns_cache[address] = (name, time.monotonic() + ttl)


async def _resolve_forever(queue: "asyncio.Queue[str]") -> None:
    gate = asyncio.Semaphore(DNS_PARALLEL)
    loop = asyncio.get_running_loop()

    async def one(address: str) -> None:
        # The slot is held until the thread is back, not until the 2 s are up: a lookup handed
        # to the pool while all its threads are blocked would only wait in the pool's queue,
        # and be written off as "no name" without ever having been asked.
        async with gate:
            lookup = loop.run_in_executor(_pool(), _reverse, address)
            try:
                name = await asyncio.wait_for(asyncio.shield(lookup), DNS_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # No name for now, so pages stop asking; the resolver's late answer replaces it.
                _remember(address, None)
                _queued.discard(address)
                try:
                    name = await lookup
                except Exception:  # noqa: BLE001 - _reverse returns None for every lookup failure
                    name = None
                if not name:
                    return
            _remember(address, name)
            _queued.discard(address)

    while True:
        address = await queue.get()
        loop.create_task(one(address))


def reset_for_tests() -> None:
    global _table, _queue, _worker, _table_mtime, _table_checked_at, _dns_pool
    _table = None
    _table_mtime = None
    _table_checked_at = 0.0
    _dns_cache.clear()
    _queued.clear()
    _queue = None
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None
    if _dns_pool is not None:
        _dns_pool.shutdown(wait=False, cancel_futures=True)
    _dns_pool = None
