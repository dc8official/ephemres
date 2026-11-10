"""Waiting for a database connection, readiness under load, and the connection budget (scale and
core review S6).

Under a 25 s lock with the pool full, requests used to wait for a free connection with no limit
and then for their 10 s query timeout (the slowest took 24 s), and readiness waited 10 s and
then said "ready". Now a request gets 503 after database.pool_timeout (5 s), readiness says
"busy" within 2 s, and the API warns at startup when the pools can't all fit in PostgreSQL's
max_connections. tests/integration/test_pool_timeout.py checks the timeout on a real pool.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app.database as database
import app.main as main
from app.core.asgi import TestClient
from app.core.orm import DatabaseUnavailableError, PoolTimeoutError, create_async_engine
from app.database import connection_budget

ROOT = Path(__file__).resolve().parents[2]


# --- acquire timeout ---------------------------------------------------------------------------

class _FullPool:
    """Stands in for an asyncpg pool whose connections are all in use."""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self.waited = None

    async def acquire(self, timeout=None):
        self.waited = timeout
        raise TimeoutError


@pytest.mark.anyio
async def test_a_full_pool_raises_pool_timeout_after_pool_timeout():
    engine = create_async_engine("postgresql://u:p@localhost/db", pool_size=2, max_overflow=1, pool_timeout=5)
    engine._pool = pool = _FullPool()
    with pytest.raises(PoolTimeoutError, match="within 5 s .pool of 3."):
        await engine.acquire_connection()
    assert pool.waited == 5.0
    with pytest.raises(PoolTimeoutError):
        await engine.acquire_connection(timeout=0.5)
    assert pool.waited == 0.5


def test_pool_timeout_is_a_database_unavailable_error():
    # So the API's existing handler answers 503 with Retry-After, and the engine's
    # is_database_unavailable() treats it as "try again later".
    assert issubclass(PoolTimeoutError, DatabaseUnavailableError)
    assert database.is_database_unavailable(PoolTimeoutError("busy"))


def test_a_pool_timeout_in_a_route_is_a_503():
    @main.app.get("/test-only/pool-timeout", include_in_schema=False)  # keep out of openapi.json
    async def _raise():
        raise PoolTimeoutError("no database connection became free within 5 s")

    resp = TestClient(main.app).get("/test-only/pool-timeout")
    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "15"


def test_the_app_engine_uses_the_configured_pool_timeout():
    assert database.async_engine.pool_timeout == float(database.settings.database.pool_timeout) == 5.0


# --- readiness ---------------------------------------------------------------------------------

@pytest.mark.parametrize("raised, status, db_status", [
    (None, 200, "ok"),
    (PoolTimeoutError("busy"), 503, "busy"),
    (OSError("connection refused"), 503, "error"),
])
def test_readiness_reports_a_busy_pool_as_busy(monkeypatch, raised, status, db_status):
    waits = []

    async def fake_ping(timeout=2.0):
        waits.append(timeout)
        if raised is not None:
            raise raised

    monkeypatch.setattr(main, "ping_database", fake_ping)
    resp = TestClient(main.app).get("/health/readiness")
    assert resp.status_code == status
    assert resp.json()["database"]["status"] == db_status
    assert waits == [2.0]  # never waits longer than a health checker's usual 5 s


# --- pool sizes and the budget -----------------------------------------------------------------

@pytest.mark.parametrize("argv0, env, expected", [
    ("/opt/ephemres/venv/bin/uvicorn", "10", (10, 0)),   # an API worker: its own pool
    ("/opt/ephemres/venv/bin/uvicorn", None, (10, 10)),  # no setting: the config's pool
    ("/opt/ephemres/monitoring/engine.py", "10", (10, 10)),  # the engine ignores the API's
])
def test_only_api_workers_take_the_api_pool_size(monkeypatch, argv0, env, expected):
    monkeypatch.setattr(database.sys, "argv", [argv0])
    if env is None:
        monkeypatch.delenv(database.API_DB_POOL_ENV, raising=False)
    else:
        monkeypatch.setenv(database.API_DB_POOL_ENV, env)
    assert database._pool_sizes() == expected


# What main.py passes: 5 LISTEN connections per worker and 2 more on the alert leader.
LISTENERS = dict(api_listeners=5, leader_listeners=2)


def test_the_default_layout_fits_postgresql_defaults():
    # 2 workers x (10 + 5 LISTEN) + 2 on the alert leader + engine and flowd x (20 + 1), against
    # max_connections 100 with 3 reserved for superusers.
    assert connection_budget(100, 3, api_workers=2, api_pool=10, other_pool=20, **LISTENERS) == (74, 97)


def test_a_fourth_worker_does_not_fit_once_every_listener_is_counted():
    # Counting one LISTEN connection per worker called this 86 of 97 and said nothing; the
    # workers really open 104 (branch review, 2026-11-10).
    assert connection_budget(100, 3, api_workers=4, api_pool=10, other_pool=20)[0] == 86
    need, available = connection_budget(100, 3, api_workers=4, api_pool=10, other_pool=20, **LISTENERS)
    assert need == 104 and need > available
    assert connection_budget(100, 3, api_workers=3, api_pool=10, other_pool=20, **LISTENERS)[0] == 89


def test_the_api_counts_one_listener_per_channel_it_subscribes_to():
    # The budget is only right while these follow the code that opens the connections:
    # settings sync, the relay's channels, and the alert leader's.
    from app.services import alert_dispatcher, telemetry_relay

    assert main.LISTENERS_PER_WORKER == 1 + len(telemetry_relay.CHANNELS) == 5
    assert main.LISTENERS_ON_ALERT_LEADER == len(alert_dispatcher.BROKER_CHANNELS) == 2


@pytest.mark.anyio
async def test_the_relay_and_the_alert_leader_subscribe_to_exactly_their_channel_lists(monkeypatch):
    from app.services import alert_dispatcher, telemetry_relay

    subscribed = []

    async def subscribe(channel):
        subscribed.append(channel)
        await asyncio.Event().wait()  # a subscription that never delivers
        yield {}

    monkeypatch.setattr(telemetry_relay.driver_manager, "subscribe", subscribe)
    relay = telemetry_relay.TelemetryRelay()
    await relay.start()
    await asyncio.sleep(0)
    await relay.stop()
    assert subscribed == list(telemetry_relay.CHANNELS)

    subscribed.clear()
    dispatcher = alert_dispatcher.AlertDispatcher()
    dispatcher._running = True
    dispatcher._start_leading()
    await asyncio.sleep(0)
    await dispatcher.stop()
    assert subscribed == list(alert_dispatcher.BROKER_CHANNELS)


def test_a_third_worker_with_the_old_pool_did_not_fit():
    # The case the review found: 3 workers of 20 each.
    need, available = connection_budget(100, 3, api_workers=3, api_pool=20, other_pool=20)
    assert need == 105 and need > available


@pytest.mark.anyio
async def test_the_budget_check_warns_when_the_pools_do_not_fit(monkeypatch, caplog):
    class _Conn:
        async def execute(self, _q):
            return type("R", (), {"fetchone": lambda self: (60, 3, 0)})()

    class _Connect:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database.async_engine, "connect", lambda: _Connect())
    monkeypatch.setenv(database.API_WORKERS_ENV, "2")
    with caplog.at_level("WARNING", logger="app.database"):
        await database.check_connection_budget(**LISTENERS)
    assert "connection budget exceeded" in caplog.text
    assert "2 API worker(s) x (20 + 5 for live events), 2 more on the worker that sends alerts" in caplog.text
    assert "can open 94 connections, but PostgreSQL allows 57" in caplog.text


def test_the_api_unit_sets_workers_and_pool_in_one_place():
    unit = (ROOT / "deploy" / "ephemres-api.service").read_text(encoding="utf-8")
    assert "Environment=EPHEMRES_API_WORKERS=2" in unit
    assert "Environment=EPHEMRES_API_DB_POOL=10" in unit
    assert "--workers ${EPHEMRES_API_WORKERS}" in unit
