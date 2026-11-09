from __future__ import annotations

import asyncio
import collections
import datetime
import ipaddress
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from uuid import UUID

from monitoring.flow.path_tracker import DIRECTION_FROM, DIRECTION_TO, PathTracker

try:
    from app.core.orm import text
except ModuleNotFoundError:  # pragma: no cover
    text = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Memory budget per accumulator entry (about 370 bytes measured, plus the flush snapshot's copy),
# used to turn flow.buffer_memory_limit_mb into an entry cap.
ACCUMULATOR_BYTES_PER_ENTRY = 1024

ROLLUP_COLUMNS = (
    "bucket", "exporter_id", "src_endpoint_id", "dst_endpoint_id", "src_ip", "dst_ip",
    "protocol", "dst_port", "bytes", "packets", "flow_count", "tcp_flags", "tos",
)
ROLLUP_ROWS_PER_STATEMENT = 5000

SENTINEL_UUID = UUID("00000000-0000-0000-0000-000000000000")

# flow_path_observations rows not seen for this long, and flow_route_changes older than this,
# are pruned hourly.
PATH_OBSERVATION_RETENTION_DAYS = 30
ROUTE_CHANGE_RETENTION_DAYS = 90


KNOWN_SERVICE_PORTS = frozenset({
    1194, 1433, 1521, 1812, 1813, 2049, 2055, 2181, 2379, 3000, 3306, 3389,
    4500, 4739, 5000, 5060, 5061, 5432, 5601, 5672, 5900, 5985, 5986, 6379,
    6443, 8000, 8080, 8443, 8888, 9000, 9090, 9092, 9100, 9200, 11211, 27017,
    51820,
})


def normalize_service_port(src_port: int, dst_port: int, protocol: int = 0) -> int:
    """
    Normalizes service ports to protect index cardinality from ephemeral client ports (1024-65535).
    """
    if protocol in (1, 58):
        return 0
    if 0 < dst_port < 1024 or dst_port in KNOWN_SERVICE_PORTS:
        return dst_port
    if 0 < src_port < 1024 or src_port in KNOWN_SERVICE_PORTS:
        return src_port
    if 0 < src_port < 10000 and dst_port >= 10000:
        return src_port
    return dst_port


class FlowAggregator:
    """
    Sums flows into 1-minute rollups in memory and writes them to TimescaleDB every few seconds.

    The collector hands parsed flows straight to ingest() in the same process. They used to go
    through a FastStore stream, which wrote, read back and trimmed every flow in SQLite; above
    about 1,000 flows/s the trim starved the reader and deleted flows it had not read yet.
    """

    def __init__(
        self,
        store_client: Any = None,
        correlator: Any = None,
        db_session_factory: Any = None,
        flush_interval_seconds: float = 10.0,
        *,
        redis_client: Any = None,
        memory_limit_mb: Optional[int] = None,
        path_tracker: Optional[PathTracker] = None,
        event_publisher: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ):
        # FastStore: active interface sets for the API (flows themselves never go there).
        self.store = store_client if store_client is not None else redis_client
        # flow.buffer_memory_limit_mb bounds the flows held here while the database is
        # unreachable: past it, new conversations are dropped (and counted) until a flush works.
        if memory_limit_mb is None:
            from app.config import settings

            memory_limit_mb = getattr(getattr(settings, "flow", None), "buffer_memory_limit_mb", 512)
        self.memory_limit_bytes = int(memory_limit_mb) * 1024 * 1024
        self.max_accumulator_entries = max(1000, self.memory_limit_bytes // ACCUMULATOR_BYTES_PER_ENTRY)
        # Counters since start, logged by flowd every minute.
        self.flows_ingested = 0
        self.flows_dropped_memory = 0
        self.rows_stored = 0
        self.correlator = correlator
        self.db_factory = db_session_factory
        self.flush_interval = flush_interval_seconds

        # (bucket, exporter_id, src_endpoint_id, dst_endpoint_id, src_ip, dst_ip, protocol, dst_port) -> [bytes, packets, flow_count, tcp_flags_or, max_tos]
        self._accumulator: Dict[
            Tuple[datetime.datetime, UUID, Optional[UUID], Optional[UUID], str, str, int, int],
            List[int],
        ] = collections.defaultdict(lambda: [0, 0, 0, 0, 0])

        # (bucket, exporter_id, interface_idx) -> [in_bytes, out_bytes, in_packets, out_packets, flow_count]
        self._interface_accumulator: Dict[
            Tuple[datetime.datetime, UUID, int],
            List[int],
        ] = collections.defaultdict(lambda: [0, 0, 0, 0, 0])

        # Forwarding paths to and from monitored endpoints; route changes go to event_publisher
        # (the event broker) as ROUTE_HINT.
        self.path_tracker = path_tracker if path_tracker is not None else PathTracker()
        self.event_publisher = event_publisher

        self._active_interfaces_set: Set[Tuple[UUID, int]] = set()
        self._flush_lock = asyncio.Lock()
        self._memory_cap_logged = False

        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None

    @property
    def redis(self) -> Any:
        return self.store

    @redis.setter
    def redis(self, val: Any) -> None:
        self.store = val

    def ingest(self, flows: List[Dict[str, Any]]) -> None:
        """Adds one packet's parsed flows (the collector calls this for every datagram)."""
        for f in flows:
            src_ip = f.get("src_ip")
            dst_ip = f.get("dst_ip")
            if not src_ip or not dst_ip:
                continue
            try:
                start = float(f.get("start_time") or time.time())
                end = float(f.get("end_time") or start)
                next_hop = f.get("next_hop") or None
                if next_hop is not None:
                    try:
                        ipaddress.ip_address(next_hop)
                    except ValueError:
                        next_hop = None
                # IPFIX interface indexes are 32-bit; anything outside the column's range is unknown.
                in_if = int(f.get("input_if") or 0)
                out_if = int(f.get("output_if") or 0)
                self.accumulate_flow(
                    exporter_ip=f.get("exporter_ip", ""),
                    src_ip=src_ip,
                    dst_ip=dst_ip,
                    src_port=int(f.get("src_port") or 0),
                    dst_port=int(f.get("dst_port") or 0),
                    protocol=int(f.get("protocol") or 0),
                    flow_bytes=int(f.get("bytes") or 0),
                    flow_packets=int(f.get("packets") or 0),
                    start_time_unix=start,
                    in_if=in_if if 0 < in_if <= 2147483647 else 0,
                    out_if=out_if if 0 < out_if <= 2147483647 else 0,
                    tcp_flags=int(f.get("tcp_flags") or 0),
                    tos=int(f.get("tos") or 0),
                    next_hop=next_hop,
                    end_time_unix=end,
                )
            except (TypeError, ValueError) as err:
                logger.debug("Skipping malformed flow record from %s: %s", f.get("exporter_ip"), err)

    def accumulate_interface_flow(
        self,
        bucket: datetime.datetime,
        exporter_id: UUID,
        in_if: int,
        out_if: int,
        flow_bytes: int,
        flow_packets: int,
    ) -> None:
        """
        Accumulates per-interface telemetry with single-direction attribution:
        - in_bytes and in_packets credited strictly to in_if (when in_if > 0)
        - out_bytes and out_packets credited strictly to out_if (when out_if > 0)
        - flow_count incremented on in_if, and also on out_if when out_if > 0 and out_if != in_if
        - Strictly excludes SENTINEL_UUID
        """
        if exporter_id is None or exporter_id == SENTINEL_UUID:
            return

        # Ingress attribution
        if 0 < in_if <= 2147483647:
            key_in = (bucket, exporter_id, in_if)
            if key_in not in self._interface_accumulator:
                self._interface_accumulator[key_in] = [flow_bytes, 0, flow_packets, 0, 1]
            else:
                entry = self._interface_accumulator[key_in]
                entry[0] += flow_bytes
                entry[2] += flow_packets
                entry[4] += 1
            self._active_interfaces_set.add((exporter_id, in_if))

        # Egress attribution
        if 0 < out_if <= 2147483647:
            key_out = (bucket, exporter_id, out_if)
            is_diff = (out_if != in_if)
            if key_out not in self._interface_accumulator:
                self._interface_accumulator[key_out] = [
                    0,
                    flow_bytes,
                    0,
                    flow_packets,
                    1 if is_diff else 0,
                ]
            else:
                entry = self._interface_accumulator[key_out]
                entry[1] += flow_bytes
                entry[3] += flow_packets
                if is_diff:
                    entry[4] += 1
            self._active_interfaces_set.add((exporter_id, out_if))

    def accumulate_flow(
        self,
        exporter_ip: str,
        src_ip: str,
        dst_ip: str,
        src_port: int,
        dst_port: int,
        protocol: int,
        flow_bytes: int,
        flow_packets: int,
        start_time_unix: float,
        in_if: int = 0,
        out_if: int = 0,
        tcp_flags: int = 0,
        tos: int = 0,
        next_hop: Optional[str] = None,
        end_time_unix: Optional[float] = None,
    ) -> None:
        if not src_ip or not dst_ip:
            return
        try:
            ipaddress.ip_address(src_ip.strip() if isinstance(src_ip, str) else src_ip)
            ipaddress.ip_address(dst_ip.strip() if isinstance(dst_ip, str) else dst_ip)
        except (ValueError, AttributeError):
            return

        tcp_flags = int(tcp_flags) & 0xFF
        tos = int(tos) & 0xFF
        self.flows_ingested += 1

        exporter_id, src_ep_id, dst_ep_id = self.correlator.correlate(
            exporter_ip, src_ip, dst_ip
        )
        resolved_exporter = exporter_id if exporter_id is not None else SENTINEL_UUID

        # Truncate start_time to 1-minute bucket (UTC)
        dt = datetime.datetime.fromtimestamp(start_time_unix, tz=datetime.timezone.utc)
        bucket = dt.replace(second=0, microsecond=0)

        normalized_port = normalize_service_port(src_port, dst_port, protocol)
        key = (
            bucket,
            resolved_exporter,
            src_ep_id,
            dst_ep_id,
            src_ip,
            dst_ip,
            protocol,
            normalized_port,
        )

        if key not in self._accumulator:
            if len(self._accumulator) >= self.max_accumulator_entries:
                # Only reachable while flushes fail (database down): keep what is held, drop new
                # conversations, and say so once until a flush succeeds.
                self.flows_dropped_memory += 1
                if not self._memory_cap_logged:
                    self._memory_cap_logged = True
                    logger.error(
                        "Flow buffer full (%d conversations, flow.buffer_memory_limit_mb=%d): "
                        "dropping new flows until the database accepts the backlog.",
                        len(self._accumulator), self.memory_limit_bytes // (1024 * 1024),
                    )
                return
            self._accumulator[key] = [flow_bytes, flow_packets, 1, tcp_flags, tos]
        else:
            entry = self._accumulator[key]
            entry[0] += flow_bytes
            entry[1] += flow_packets
            entry[2] += 1
            entry[3] |= tcp_flags
            entry[4] = max(entry[4], tos)

        # Accumulate interface metrics if exporter is identified and not sentinel
        if exporter_id is not None and exporter_id != SENTINEL_UUID:
            self.accumulate_interface_flow(
                bucket=bucket,
                exporter_id=exporter_id,
                in_if=in_if,
                out_if=out_if,
                flow_bytes=flow_bytes,
                flow_packets=flow_packets,
            )
            end_unix = end_time_unix if end_time_unix is not None else start_time_unix
            for endpoint_id, direction in ((dst_ep_id, DIRECTION_TO), (src_ep_id, DIRECTION_FROM)):
                if endpoint_id is not None and endpoint_id != exporter_id:
                    self.path_tracker.observe(
                        exporter_id, endpoint_id, direction, in_if, out_if, next_hop,
                        flow_bytes, start_time_unix, end_unix,
                    )

    async def flush_to_timescaledb(self) -> None:
        await self._flush_rollups()
        await self._flush_paths()

    async def _flush_rollups(self) -> None:
        async with self._flush_lock:
            if not self._accumulator and not self._interface_accumulator:
                return

            # Snapshot current accumulator entries
            snapshot = {k: list(v) for k, v in self._accumulator.items()}
            if_snapshot = {k: list(v) for k, v in self._interface_accumulator.items()}
            active_if_snapshot = set(self._active_interfaces_set)

            rows: List[Dict[str, Any]] = []
            if_rows: List[Dict[str, Any]] = []

            if snapshot:
                logger.debug("Flushing %d aggregated rollup entries to TimescaleDB...", len(snapshot))

                # Deduplicate by primary key (bucket, exporter_id, src_ip, dst_ip, protocol, dst_port)
                # to prevent ON CONFLICT DO UPDATE from affecting a row twice within a micro-batch
                grouped_rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
                for key, vals in snapshot.items():
                    bucket, exp_id, src_ep_id, dst_ep_id, src_ip, dst_ip, proto, d_port = key
                    bytes_val = vals[0]
                    pkts_val = vals[1]
                    count_val = vals[2]
                    tcp_flags_val = vals[3] if len(vals) > 3 else 0
                    tos_val = vals[4] if len(vals) > 4 else 0
                    pk = (bucket, exp_id, src_ip, dst_ip, proto, d_port)
                    if pk not in grouped_rows:
                        grouped_rows[pk] = {
                            "bucket": bucket,
                            "exporter_id": exp_id,
                            "src_endpoint_id": src_ep_id,
                            "dst_endpoint_id": dst_ep_id,
                            "src_ip": src_ip,
                            "dst_ip": dst_ip,
                            "protocol": proto,
                            "dst_port": d_port,
                            "bytes": bytes_val,
                            "packets": pkts_val,
                            "flow_count": count_val,
                            "tcp_flags": tcp_flags_val,
                            "tos": tos_val,
                        }
                    else:
                        existing = grouped_rows[pk]
                        existing["bytes"] += bytes_val
                        existing["packets"] += pkts_val
                        existing["flow_count"] += count_val
                        existing["tcp_flags"] |= tcp_flags_val
                        existing["tos"] = max(existing["tos"], tos_val)
                        if not existing["src_endpoint_id"] and src_ep_id:
                            existing["src_endpoint_id"] = src_ep_id
                        if not existing["dst_endpoint_id"] and dst_ep_id:
                            existing["dst_endpoint_id"] = dst_ep_id

                rows = list(grouped_rows.values())

            if if_snapshot:
                logger.debug("Flushing %d interface rollup entries to TimescaleDB...", len(if_snapshot))
                # Pre-aggregate interface rollups by (bucket, exporter_id, interface_idx)
                grouped_if: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
                for key, vals in if_snapshot.items():
                    bucket, exp_id, if_idx = key
                    in_b, out_b, in_p, out_p, fc = vals
                    pk = (bucket, exp_id, if_idx)
                    if pk not in grouped_if:
                        grouped_if[pk] = {
                            "bucket": bucket,
                            "exporter_id": exp_id,
                            "interface_idx": if_idx,
                            "in_bytes": in_b,
                            "out_bytes": out_b,
                            "in_packets": in_p,
                            "out_packets": out_p,
                            "flow_count": fc,
                        }
                    else:
                        ex = grouped_if[pk]
                        ex["in_bytes"] += in_b
                        ex["out_bytes"] += out_b
                        ex["in_packets"] += in_p
                        ex["out_packets"] += out_p
                        ex["flow_count"] += fc

                if_rows = list(grouped_if.values())

            # One statement per chunk, each parameter an array of the chunk's column: an
            # executemany ran the upsert once per row and capped flowd at about 1,900 new
            # conversations a second.
            insert_sql = text("""
                INSERT INTO flow_minute_rollups (
                    bucket, exporter_id, src_endpoint_id, dst_endpoint_id,
                    src_ip, dst_ip, protocol, dst_port,
                    bytes, packets, flow_count, tcp_flags, tos
                )
                SELECT u.bk, u.ex, u.sep, u.dep, CAST(u.sip AS inet), CAST(u.dip AS inet),
                       u.pr, u.dpt, u.byt, u.pkt, u.fcn, u.tfl, u.tsv
                FROM unnest(
                    CAST(:bucket AS timestamptz[]), CAST(:exporter_id AS uuid[]),
                    CAST(:src_endpoint_id AS uuid[]), CAST(:dst_endpoint_id AS uuid[]),
                    CAST(:src_ip AS text[]), CAST(:dst_ip AS text[]),
                    CAST(:protocol AS smallint[]), CAST(:dst_port AS integer[]),
                    CAST(:bytes AS bigint[]), CAST(:packets AS bigint[]), CAST(:flow_count AS integer[]),
                    CAST(:tcp_flags AS smallint[]), CAST(:tos AS smallint[])
                ) AS u(bk, ex, sep, dep, sip, dip, pr, dpt, byt, pkt, fcn, tfl, tsv)
                ON CONFLICT (bucket, exporter_id, src_ip, dst_ip, protocol, dst_port)
                DO UPDATE SET
                    bytes = flow_minute_rollups.bytes + EXCLUDED.bytes,
                    packets = flow_minute_rollups.packets + EXCLUDED.packets,
                    flow_count = flow_minute_rollups.flow_count + EXCLUDED.flow_count,
                    tcp_flags = (COALESCE(flow_minute_rollups.tcp_flags, 0) | EXCLUDED.tcp_flags),
                    tos = GREATEST(COALESCE(flow_minute_rollups.tos, 0), EXCLUDED.tos),
                    src_endpoint_id = COALESCE(flow_minute_rollups.src_endpoint_id, EXCLUDED.src_endpoint_id),
                    dst_endpoint_id = COALESCE(flow_minute_rollups.dst_endpoint_id, EXCLUDED.dst_endpoint_id);
            """)

            insert_if_sql = text("""
                INSERT INTO flow_interface_minute_rollups (
                    bucket, exporter_id, interface_idx,
                    in_bytes, out_bytes, in_packets, out_packets, flow_count
                ) VALUES (
                    :bucket, :exporter_id, :interface_idx,
                    :in_bytes, :out_bytes, :in_packets, :out_packets, :flow_count
                )
                ON CONFLICT (bucket, exporter_id, interface_idx)
                DO UPDATE SET
                    in_bytes = flow_interface_minute_rollups.in_bytes + EXCLUDED.in_bytes,
                    out_bytes = flow_interface_minute_rollups.out_bytes + EXCLUDED.out_bytes,
                    in_packets = flow_interface_minute_rollups.in_packets + EXCLUDED.in_packets,
                    out_packets = flow_interface_minute_rollups.out_packets + EXCLUDED.out_packets,
                    flow_count = flow_interface_minute_rollups.flow_count + EXCLUDED.flow_count;
            """)

            if rows or if_rows:
                try:
                    async with self.db_factory() as session:
                        if rows:
                            batch_size = ROLLUP_ROWS_PER_STATEMENT
                            for i in range(0, len(rows), batch_size):
                                chunk = rows[i : i + batch_size]
                                # Tuples, not lists: the ORM sends a list parameter as JSON, a
                                # tuple goes to asyncpg as a PostgreSQL array.
                                await session.execute(
                                    insert_sql, {name: tuple(r[name] for r in chunk) for name in ROLLUP_COLUMNS}
                                )
                        if if_rows:
                            batch_size = 1000
                            for i in range(0, len(if_rows), batch_size):
                                chunk = if_rows[i : i + batch_size]
                                await session.execute(insert_if_sql, chunk)
                        await session.commit()
                    if rows:
                        logger.info("Successfully persisted %d flow rollup records to TimescaleDB.", len(rows))
                    if if_rows:
                        logger.info("Successfully persisted %d interface rollup records to TimescaleDB.", len(if_rows))
                except Exception as e:
                    logger.error("Failed to commit flow rollup micro-batch to DB: %s", e)
                    # Keep the accumulator intact; the next flush retries.
                    return
                self.rows_stored += len(rows)
                self._memory_cap_logged = False

                # Only deduct committed quantities from accumulator on successful DB commit
                if rows:
                    for k, flushed_vals in snapshot.items():
                        if k in self._accumulator:
                            self._accumulator[k][0] -= flushed_vals[0]
                            self._accumulator[k][1] -= flushed_vals[1]
                            self._accumulator[k][2] -= flushed_vals[2]
                            if self._accumulator[k][2] <= 0:
                                del self._accumulator[k]

                if if_rows:
                    for k, flushed_vals in if_snapshot.items():
                        if k in self._interface_accumulator:
                            self._interface_accumulator[k][0] -= flushed_vals[0]
                            self._interface_accumulator[k][1] -= flushed_vals[1]
                            self._interface_accumulator[k][2] -= flushed_vals[2]
                            self._interface_accumulator[k][3] -= flushed_vals[3]
                            self._interface_accumulator[k][4] -= flushed_vals[4]
                            if self._interface_accumulator[k][4] <= 0:
                                del self._interface_accumulator[k]

                # Flush active interfaces to FastStore with TTL
                if self.store and active_if_snapshot:
                    try:
                        pipe = self.store.pipeline()
                        for exp_id, if_idx in active_if_snapshot:
                            pipe.sadd(f"flow:exporter:{exp_id}:interfaces", if_idx)
                            pipe.expire(f"flow:exporter:{exp_id}:interfaces", 7200)
                        await pipe.execute()
                        self._active_interfaces_set.difference_update(active_if_snapshot)
                    except Exception as store_err:
                        logger.warning("Failed to record active interfaces in FastStore: %s", store_err)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush_to_timescaledb()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Aggregator flush loop error: %s", e)

    async def _flush_paths(self) -> None:
        """Writes path observations and route changes, then publishes the changes.

        Runs in its own transaction after the rollups: an endpoint deleted mid-batch (a foreign
        key violation here) must never hold back the rollups. Observations whose write fails are
        kept for the next flush; route changes are dropped with a warning.
        """
        async with self._flush_lock:
            self.path_tracker.tick()
            observations = self.path_tracker.drain_observations()
            changes = self.path_tracker.drain_changes()
            if not observations and not changes:
                return

            def _ts(unix: float) -> datetime.datetime:
                return datetime.datetime.fromtimestamp(unix, tz=datetime.timezone.utc)

            obs_rows = [
                {
                    "exporter_id": exp_id,
                    "endpoint_id": ep_id,
                    "direction": direction,
                    "in_if": in_if,
                    "out_if": out_if,
                    "next_hop": next_hop,
                    "first_seen": _ts(first),
                    "last_seen": _ts(last),
                    "bytes": int(nbytes),
                    "flow_count": int(flows),
                }
                for (exp_id, ep_id, direction, in_if, out_if, next_hop), (first, last, nbytes, flows) in observations.items()
            ]
            change_rows = [
                {
                    "detected_at": c.detected_at,
                    "exporter_id": c.exporter_id,
                    "endpoint_id": c.endpoint_id,
                    "direction": c.direction,
                    "old_out_if": c.old_out_if,
                    "old_next_hop": c.old_next_hop,
                    "new_out_if": c.new_out_if,
                    "new_next_hop": c.new_next_hop,
                }
                for c in changes
            ]

            observation_sql = text("""
                INSERT INTO flow_path_observations (
                    exporter_id, endpoint_id, direction, in_if, out_if, next_hop,
                    first_seen, last_seen, bytes, flow_count
                )
                SELECT
                    :exporter_id, :endpoint_id, :direction, :in_if, :out_if, CAST(:next_hop AS inet),
                    :first_seen, :last_seen, :bytes, :flow_count
                WHERE EXISTS (SELECT 1 FROM endpoints WHERE id = :exporter_id)
                  AND EXISTS (SELECT 1 FROM endpoints WHERE id = :endpoint_id)
                ON CONFLICT (exporter_id, endpoint_id, direction, in_if, out_if, (COALESCE(next_hop, '0.0.0.0'::inet)))
                DO UPDATE SET
                    first_seen = LEAST(flow_path_observations.first_seen, EXCLUDED.first_seen),
                    last_seen = GREATEST(flow_path_observations.last_seen, EXCLUDED.last_seen),
                    bytes = flow_path_observations.bytes + EXCLUDED.bytes,
                    flow_count = flow_path_observations.flow_count + EXCLUDED.flow_count;
            """)
            change_sql = text("""
                INSERT INTO flow_route_changes (
                    detected_at, exporter_id, endpoint_id, direction,
                    old_out_if, old_next_hop, new_out_if, new_next_hop
                )
                SELECT
                    :detected_at, :exporter_id, :endpoint_id, :direction,
                    :old_out_if, CAST(:old_next_hop AS inet), :new_out_if, CAST(:new_next_hop AS inet)
                WHERE EXISTS (SELECT 1 FROM endpoints WHERE id = :exporter_id)
                  AND EXISTS (SELECT 1 FROM endpoints WHERE id = :endpoint_id);
            """)
            try:
                async with self.db_factory() as session:
                    for i in range(0, len(obs_rows), 1000):
                        await session.execute(observation_sql, obs_rows[i : i + 1000])
                    if change_rows:
                        await session.execute(change_sql, change_rows)
                    await session.commit()
            except Exception as e:
                logger.warning("Failed to persist %d path observations / %d route changes: %s", len(obs_rows), len(change_rows), e)
                self.path_tracker.restore_observations(observations)
                return

        for change in changes:
            logger.info(
                "Route change seen in flows: exporter %s now sends traffic %s endpoint %s out if%s via %s (was if%s via %s).",
                change.exporter_id, change.direction, change.endpoint_id, change.new_out_if,
                change.new_next_hop or "no next hop", change.old_out_if, change.old_next_hop or "no next hop",
            )
            if self.event_publisher is not None:
                try:
                    await self.event_publisher("ROUTE_HINT", change.to_event())
                except Exception as pub_err:
                    logger.warning("Failed to publish ROUTE_HINT: %s", pub_err)

    async def _prune_loop(self) -> None:
        """Drops stale path observations and old route changes once an hour."""
        while self._running:
            try:
                await asyncio.sleep(3600.0)
                async with self.db_factory() as session:
                    await session.execute(
                        text(f"DELETE FROM flow_path_observations WHERE last_seen < now() - INTERVAL '{PATH_OBSERVATION_RETENTION_DAYS} days'")
                    )
                    await session.execute(
                        text(f"DELETE FROM flow_route_changes WHERE detected_at < now() - INTERVAL '{ROUTE_CHANGE_RETENTION_DAYS} days'")
                    )
                    await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Path observation prune error: %s", e)

    async def start(self) -> None:
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info("FlowAggregator started.")

    async def stop(self) -> None:
        self._running = False
        tasks = []
        if self._flush_task:
            self._flush_task.cancel()
            tasks.append(self._flush_task)
        if self._prune_task:
            self._prune_task.cancel()
            tasks.append(self._prune_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush_to_timescaledb()
        logger.info("FlowAggregator stopped.")
