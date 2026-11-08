import asyncio
import json
import logging
import secrets
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, Optional

from app.core.orm import text
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# How often an idle LISTEN connection is checked. A termination callback covers a closed socket;
# this covers a connection that silently stopped working (network cut, no FIN).
LISTENER_CHECK_SECONDS = 5.0
LISTENER_RECONNECT_SECONDS = 2.0
_CONNECTION_LOST = object()


class EventBroker(ABC):
    """Abstract interface for real-time pub/sub messaging and state broadcasts."""

    @abstractmethod
    async def publish(self, channel: str, event_data: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    async def subscribe(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
        pass


class PostgresEventBroker(EventBroker):
    """
    PostgreSQL-native pub/sub broker using native LISTEN / NOTIFY.
    """

    def __init__(self, session_factory=AsyncSessionLocal) -> None:
        self.session_factory = session_factory

    async def publish(self, channel: str, event_data: Dict[str, Any]) -> None:
        payload = json.dumps(event_data)
        if len(payload.encode("utf-8")) > 7500:
            data_copy = dict(event_data)
            if "symptom_endpoint_ids" in data_copy and isinstance(data_copy["symptom_endpoint_ids"], list):
                symptoms = data_copy["symptom_endpoint_ids"]
                data_copy["symptom_count"] = len(symptoms)
                data_copy["truncated"] = True
                compacted: list[Any] = []
                for s in symptoms:
                    compacted.append(s)
                    data_copy["symptom_endpoint_ids"] = compacted
                    if len(json.dumps(data_copy).encode("utf-8")) > 7200:
                        compacted.pop()
                        break
                data_copy["symptom_endpoint_ids"] = compacted
            payload = json.dumps(data_copy)
            if len(payload.encode("utf-8")) > 7500:
                for k, v in list(data_copy.items()):
                    if isinstance(v, str) and len(v) > 200:
                        data_copy[k] = v[:200] + "... [truncated]"
                payload = json.dumps(data_copy)
        async with self.session_factory() as db:
            try:
                query = text("SELECT pg_notify(:channel, :payload);")
                await db.execute(query, {"channel": channel, "payload": payload})
                await db.commit()
            except Exception as e:
                logger.error("PostgresEventBroker.publish error: %s", e)
                raise

    async def subscribe(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
        """
        True real-time LISTEN subscription using a dedicated unpooled asyncpg connection
        with automated reconnection loop.

        Waiting on the local queue alone never noticed a dead connection: after a PostgreSQL
        restart every subscriber kept waiting forever, and live events and alerts silently
        stopped until the API restarted. NOTIFY is not durable, so an event published while
        reconnecting is not delivered.
        """
        import asyncio
        from app.config import settings

        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        def _on_notification(connection, pid, ch, payload):
            try:
                data = json.loads(payload)
            except Exception:
                data = {"raw": payload}
            queue.put_nowait(data)

        def _on_termination(connection):
            queue.put_nowait(_CONNECTION_LOST)

        failing = False
        while True:
            conn = None
            try:
                import asyncpg

                conn = await asyncpg.connect(
                    host=settings.database.host,
                    port=settings.database.port,
                    user=settings.database.user,
                    password=settings.database.password,
                    database=settings.database.name,
                )
                conn.add_termination_listener(_on_termination)
                await conn.add_listener(channel, _on_notification)
                logger.info("PostgresEventBroker: connected & listening to '%s'", channel)
                failing = False
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=LISTENER_CHECK_SECONDS)
                    except asyncio.TimeoutError:
                        await conn.fetchval("SELECT 1", timeout=LISTENER_CHECK_SECONDS)
                        continue
                    if event is _CONNECTION_LOST:
                        # Left over from the previous connection when this one is open.
                        if conn.is_closed():
                            raise ConnectionError("LISTEN connection lost")
                        continue
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as exc:
                # Once per outage: this retries every couple of seconds per channel.
                if not failing:
                    logger.warning(
                        "PostgresEventBroker: listener error on channel '%s': %s. Reconnecting...",
                        channel,
                        exc,
                    )
                failing = True
                await asyncio.sleep(LISTENER_RECONNECT_SECONDS)
            finally:
                if conn and not conn.is_closed():
                    try:
                        await conn.remove_listener(channel, _on_notification)
                        await conn.close()
                    except Exception:
                        pass


class FastStoreEventBroker(EventBroker):
    """
    FastStore Pub/Sub broker using FastStoreClient channels and streams.
    """

    def __init__(self, store_client: Any = None, redis_client: Any = None) -> None:
        self.store = store_client if store_client is not None else redis_client

    @property
    def redis(self) -> Any:
        return self.store

    async def publish(self, channel: str, event_data: Dict[str, Any]) -> None:
        payload = json.dumps(event_data)
        try:
            # 1. Pub/Sub for active SSE listeners
            await self.store.publish(channel, payload)
            # 2. FastStore Streams for persistent replay / worker fanout (STAB-06 / ARCH-01)
            stream_key = f"stream:{channel}"
            await self.store.xadd(
                stream_key, {"payload": payload}, maxlen=10000, approximate=True
            )
        except Exception as e:
            logger.error("FastStoreEventBroker.publish error: %s", e)
            raise

    async def subscribe(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
        pubsub = self.store.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, (bytes, bytearray)):
                        data = data.decode("utf-8")
                    yield json.loads(data)
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

    async def subscribe_stream(
        self, channel: str, group: str = "ephemres-workers", consumer: Optional[str] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Subscribes to durable FastStore Streams with consumer group checkpointing and ACK semantics.
        """
        stream_key = f"stream:{channel}"
        consumer_name = consumer or f"consumer-{secrets.token_hex(4)}"

        # Ensure group exists
        try:
            await self.store.xgroup_create(stream_key, group, id="0", mkstream=True)
        except Exception:
            # Group likely already exists
            pass

        try:
            while True:
                entries = await self.store.xreadgroup(
                    groupname=group,
                    consumername=consumer_name,
                    streams={stream_key: ">"},
                    count=10,
                    block=2000,
                )
                if not entries:
                    await asyncio.sleep(0.1)
                    continue

                for _, messages in entries:
                    for msg_id, data in messages:
                        try:
                            raw_payload = data.get(b"payload") or data.get("payload")
                            if isinstance(raw_payload, (bytes, bytearray)):
                                raw_payload = raw_payload.decode("utf-8")
                            parsed = json.loads(raw_payload) if raw_payload else {}
                            yield parsed
                        finally:
                            try:
                                await self.store.xack(stream_key, group, msg_id)
                            except Exception:
                                pass
        except (asyncio.CancelledError, GeneratorExit):
            pass


RedisEventBroker = FastStoreEventBroker
