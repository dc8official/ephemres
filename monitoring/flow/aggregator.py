from __future__ import annotations

import asyncio
import collections
import datetime
import ipaddress
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

try:
    from app.core.orm import text
except ModuleNotFoundError:  # pragma: no cover
    text = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

STREAM_KEY = "stream:netflow:raw"
GROUP_NAME = "flow_agg_group"
CONSUMER_NAME = "aggregator_worker_1"
SENTINEL_UUID = UUID("00000000-0000-0000-0000-000000000000")


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
    Redis Stream consumer and TimescaleDB 1-minute hypertable micro-batch aggregator.
    """

    def __init__(
        self,
        store_client: Any = None,
        correlator: Any = None,
        db_session_factory: Any = None,
        flush_interval_seconds: float = 10.0,
        *,
        redis_client: Any = None,
    ):
        self.store = store_client if store_client is not None else redis_client
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

        self._active_interfaces_set: Set[Tuple[UUID, int]] = set()
        self._pending_msg_ids: List[str] = []
        self._flush_lock = asyncio.Lock()

        self._running = False
        self._consume_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._trim_task: Optional[asyncio.Task] = None

    @property
    def redis(self) -> Any:
        return self.store

    @redis.setter
    def redis(self, val: Any) -> None:
        self.store = val

    async def _setup_consumer_group(self) -> None:
        if not self.store:
            return
        try:
            await self.store.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
            logger.info("Created consumer group '%s' on '%s'.", GROUP_NAME, STREAM_KEY)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning("Error creating consumer group: %s", e)

    async def _check_circuit_breaker(self) -> bool:
        """Returns True if memory usage is within safe bounds (<85%), False if tripped."""
        if not self.store:
            return True
        try:
            info = await self.store.info("memory")
            used = int(info.get("used_memory", 0))
            maxmem = int(info.get("maxmemory", 0))
            if maxmem > 0 and (used / maxmem) > 0.85:
                logger.error(
                    "Circuit Breaker TRIPPED: FastStore memory utilization at %.1f%% (>85%%). Dropping buffer.",
                    (used / maxmem) * 100.0,
                )
                return False
        except Exception:
            pass
        return True

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

    async def flush_to_timescaledb(self) -> None:
        async with self._flush_lock:
            if not self._accumulator and not self._interface_accumulator and not self._pending_msg_ids:
                return

            # Snapshot current accumulator entries and pending stream IDs
            snapshot = {k: list(v) for k, v in self._accumulator.items()}
            if_snapshot = {k: list(v) for k, v in self._interface_accumulator.items()}
            active_if_snapshot = set(self._active_interfaces_set)
            pending_ack = list(self._pending_msg_ids)

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

            insert_sql = text("""
                INSERT INTO flow_minute_rollups (
                    bucket, exporter_id, src_endpoint_id, dst_endpoint_id,
                    src_ip, dst_ip, protocol, dst_port,
                    bytes, packets, flow_count, tcp_flags, tos
                ) VALUES (
                    :bucket, :exporter_id, :src_endpoint_id, :dst_endpoint_id,
                    CAST(:src_ip AS inet), CAST(:dst_ip AS inet), :protocol, :dst_port,
                    :bytes, :packets, :flow_count, :tcp_flags, :tos
                )
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
                            batch_size = 1000
                            for i in range(0, len(rows), batch_size):
                                chunk = rows[i : i + batch_size]
                                await session.execute(insert_sql, chunk)
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
                    # Keep accumulator intact and do not ack messages on failure
                    return

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

            # Acknowledge stream messages in FastStore only after successful DB persistence
            if pending_ack and self.store:
                try:
                    batch_ack = 500
                    for i in range(0, len(pending_ack), batch_ack):
                        ack_chunk = pending_ack[i : i + batch_ack]
                        await self.store.xack(STREAM_KEY, GROUP_NAME, *ack_chunk)
                    self._pending_msg_ids = self._pending_msg_ids[len(pending_ack):]
                except Exception as ack_err:
                    logger.warning("Failed to xack stream messages in FastStore: %s", ack_err)

    async def _consume_stream(self) -> None:
        await self._setup_consumer_group()

        while self._running:
            if not self.store:
                await asyncio.sleep(1.0)
                continue

            # Circuit breaker check
            if not await self._check_circuit_breaker():
                await asyncio.sleep(5.0)
                continue

            try:
                # Read up to 2000 messages with 1s timeout
                entries = await self.store.xreadgroup(
                    GROUP_NAME,
                    CONSUMER_NAME,
                    {STREAM_KEY: ">"},
                    count=2000,
                    block=1000,
                )

                if not entries:
                    continue

                for _, message_list in entries:
                    for msg_id, fields in message_list:
                        raw_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                        self._pending_msg_ids.append(raw_id)
                        try:
                            exp_ip = fields.get("exp", "")
                            src_ip = fields.get("src", "")
                            dst_ip = fields.get("dst", "")
                            if not src_ip or not dst_ip:
                                continue
                            try:
                                ipaddress.ip_address(src_ip.strip())
                                ipaddress.ip_address(dst_ip.strip())
                            except ValueError:
                                continue
                            sport = int(fields.get("sport", 0))
                            dport = int(fields.get("dport", 0))
                            proto = int(fields.get("proto", 0))
                            f_bytes = int(fields.get("b", 0))
                            f_pkts = int(fields.get("p", 0))
                            tcp_flags = int(fields.get("flags", 0) or 0)
                            tos = int(fields.get("tos", 0) or 0)
                            ts_start = float(fields.get("ts_start", time.time()))

                            # Extract interface indexes with IPFIX 32-bit bound checks
                            raw_in_if = int(fields.get("in_if", 0))
                            raw_out_if = int(fields.get("out_if", 0))
                            valid_in_if = raw_in_if if (0 < raw_in_if <= 2147483647) else 0
                            valid_out_if = raw_out_if if (0 < raw_out_if <= 2147483647) else 0

                            self.accumulate_flow(
                                exporter_ip=exp_ip,
                                src_ip=src_ip,
                                dst_ip=dst_ip,
                                src_port=sport,
                                dst_port=dport,
                                protocol=proto,
                                flow_bytes=f_bytes,
                                flow_packets=f_pkts,
                                start_time_unix=ts_start,
                                in_if=valid_in_if,
                                out_if=valid_out_if,
                                tcp_flags=tcp_flags,
                                tos=tos,
                            )
                        except Exception as parse_err:
                            logger.debug("Corrupted stream message %s: %s", msg_id, parse_err)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Error consuming from Redis Stream: %s", exc)
                await asyncio.sleep(1.0)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush_to_timescaledb()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Aggregator flush loop error: %s", e)

    async def _stream_trim_loop(self) -> None:
        """Prunes FastStore Stream entries older than 2 hours every 5 minutes."""
        while self._running:
            try:
                await asyncio.sleep(300.0)
                if self.store:
                    # 2 hours ago in ms
                    cutoff_ms = int((time.time() - 7200.0) * 1000)
                    await self.store.xtrim(STREAM_KEY, minid=str(cutoff_ms), approximate=True)
                    logger.debug("Trimmed stream '%s' older than 2 hours (minid=%d).", STREAM_KEY, cutoff_ms)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Stream trim error: %s", e)

    async def start(self) -> None:
        self._running = True
        self._consume_task = asyncio.create_task(self._consume_stream())
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._trim_task = asyncio.create_task(self._stream_trim_loop())
        logger.info("FlowAggregator started.")

    async def stop(self) -> None:
        self._running = False
        tasks = []
        if self._consume_task:
            self._consume_task.cancel()
            tasks.append(self._consume_task)
        if self._flush_task:
            self._flush_task.cancel()
            tasks.append(self._flush_task)
        if self._trim_task:
            self._trim_task.cancel()
            tasks.append(self._trim_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush_to_timescaledb()
        logger.info("FlowAggregator stopped.")
