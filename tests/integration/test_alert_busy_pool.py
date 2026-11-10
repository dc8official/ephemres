"""Alerts and the connection budget against a real PostgreSQL (branch review, 2026-11-10).

The unit tests (tests/backend/test_alert_busy_database.py, test_connection_limits.py) fake the
database. These two check the facts they rest on: that a real full pool delays an alert without
losing it, and that a worker really opens one LISTEN connection per channel, which is the number
the startup connection budget counts.
"""
import asyncio
import time
import urllib.parse

import asyncpg

import app.services.alert_dispatcher as dispatcher_module
from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services.alert_dispatcher import AlertDispatcher


def test_an_alert_outlasts_a_pool_held_for_longer_than_two_timeouts(timescale_dsn, monkeypatch):
    # pool_timeout 0.3 s and the only connection held for 1 s: the alerting switch and the
    # channel list each used to give up after 0.3 s, so by 0.6 s the alert was gone.
    engine = create_async_engine(
        timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        pool_size=1, max_overflow=0, pool_timeout=0.3,
    )
    sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(dispatcher_module, "DB_RETRY_DELAYS", (0.1,))

    async def scenario():
        admin = await asyncpg.connect(timescale_dsn, timeout=30.0)
        dispatcher = AlertDispatcher()
        sent = []

        async def record(**kw):
            sent.append((kw["channel"].name, kw["severity"]))

        dispatcher._evaluate_and_send_channel = record
        try:
            await admin.execute(
                "INSERT INTO alert_channels (name, channel_type, config) "
                "VALUES ('it-busy-pool', 'GENERIC_WEBHOOK', '{}')"
            )
            held = await engine.acquire_connection()

            async def release_later():
                await asyncio.sleep(1.0)
                await engine.release_connection(held)

            releasing = asyncio.create_task(release_later())
            started = time.monotonic()
            await dispatcher._dispatch_event("STATE_TRANSITION", {
                "endpoint_id": "00000000-0000-0000-0000-000000000001", "hostname": "core-sw1",
                "ip_address": "10.0.0.1", "operational_state": "DOWN",
            })
            waited = time.monotonic() - started
            await releasing
            return sent, waited
        finally:
            await admin.execute("DELETE FROM alert_channels WHERE name = 'it-busy-pool'")
            await admin.close()
            await engine.dispose()

    sent, waited = asyncio.run(scenario())
    assert sent == [("it-busy-pool", "DOWN")]
    assert 0.9 < waited < 5.0  # it went out once the connection came back, not before


def test_a_worker_opens_one_listen_connection_per_channel(timescale_dsn, monkeypatch):
    # Without Performance Mode: the settings-sync listener, the relay's channels and, on the
    # worker that sends alerts, the dispatcher's. main.py passes these counts to the budget.
    import app.main as main
    from app.config import settings
    from app.services import telemetry_relay
    from app.services.driver_manager import driver_manager
    from app.services.event_broker import PostgresEventBroker
    from app.services.settings_sync import start_settings_sync_listener

    parts = urllib.parse.urlsplit(timescale_dsn)
    for field, value in (("host", parts.hostname), ("port", parts.port), ("user", parts.username),
                         ("password", parts.password or ""), ("name", parts.path.lstrip("/"))):
        monkeypatch.setattr(settings.database, field, value)
    monkeypatch.setattr(driver_manager, "_event_broker", PostgresEventBroker(None))

    count_sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
    )

    async def scenario():
        admin = await asyncpg.connect(timescale_dsn, timeout=30.0)
        relay = telemetry_relay.TelemetryRelay()
        dispatcher = AlertDispatcher()
        settings_sync = None
        try:
            before = await admin.fetchval(count_sql)
            await relay.start()
            settings_sync = asyncio.create_task(start_settings_sync_listener(None))
            worker_only = await _settles(admin, count_sql, before + main.LISTENERS_PER_WORKER)
            dispatcher._running = True
            dispatcher._start_leading()
            with_leader = await _settles(
                admin, count_sql, before + main.LISTENERS_PER_WORKER + main.LISTENERS_ON_ALERT_LEADER
            )
            return worker_only - before, with_leader - before
        finally:
            await relay.stop()
            await dispatcher.stop()
            if settings_sync is not None:
                settings_sync.cancel()
                await asyncio.gather(settings_sync, return_exceptions=True)
            await admin.close()

    worker_only, with_leader = asyncio.run(scenario())
    assert worker_only == main.LISTENERS_PER_WORKER == 5
    assert with_leader == main.LISTENERS_PER_WORKER + main.LISTENERS_ON_ALERT_LEADER == 7


async def _settles(admin, count_sql: str, expected: int) -> int:
    """The connection count once it has reached `expected` and stayed there for half a second
    (so one connection too many is seen), or whatever it is after 10 s."""
    deadline = time.monotonic() + 10.0
    count = await admin.fetchval(count_sql)
    while count != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        count = await admin.fetchval(count_sql)
    await asyncio.sleep(0.5)
    return await admin.fetchval(count_sql)
