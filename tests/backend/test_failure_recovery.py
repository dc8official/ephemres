"""Recovery from a lost database: found by breaking a real install under systemd.

- Event listeners never noticed a dead LISTEN connection: after one PostgreSQL restart live
  events stopped, and after two no alert was sent, with nothing logged.
- The alert leader never checked its lock, so after a restart it and the standby both sent.
- `Requires=postgresql.service` left the API and engine down after a stop and start.
- Probe results were thrown away during an outage; see tests/integration/test_outage_recovery.py.
- An endpoint already down at its first probe never alerted.
- A worker whose event loop was stuck was never replaced.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from app.core.watchdog import LOOP_STALL_EXIT_CODE, start_loop_stall_guard
from app.database import is_database_unavailable
from app.services import alert_dispatcher as dispatcher_mod
from app.services import event_broker
from app.services.alert_dispatcher import AlertDispatcher
from monitoring.cycle_store import CycleStore, ProbeCycle
from monitoring.ping import PingResult
from monitoring.state_machine import EndpointState, StateMachine

ROOT = Path(__file__).resolve().parents[2]
GOOD = PingResult(success_count=5, failed_count=0, avg_rtt_ms=1.0)
BAD = PingResult(success_count=0, failed_count=5, avg_rtt_ms=None)
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


# --- units -----------------------------------------------------------------------------------

@pytest.mark.parametrize("unit", ["ephemres-api", "ephemres-engine", "ephemres-flowd"])
def test_services_outlive_a_postgresql_stop_and_retry_on_their_own(unit):
    text = (ROOT / "deploy" / f"{unit}.service").read_text()
    assert "Requires=postgresql" not in text
    assert "Wants=postgresql.service" in text and "After=network.target postgresql.service" in text
    assert "Restart=always" in text


# --- what counts as "the database is away" ---------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        OSError("Multiple exceptions: [Errno 111] Connect call failed"),
        ConnectionResetError(),
        asyncio.TimeoutError(),
        asyncpg.exceptions.ConnectionDoesNotExistError("gone"),
        asyncpg.InterfaceError("connection is closed"),
        asyncpg.exceptions.CannotConnectNowError("the database system is starting up"),
        asyncpg.exceptions.AdminShutdownError("terminating connection"),
    ],
)
def test_lost_database_errors_are_recognized(exc):
    assert is_database_unavailable(exc)


@pytest.mark.parametrize("exc", [ValueError("bad"), asyncpg.exceptions.UndefinedTableError("no table")])
def test_other_errors_are_not_an_outage(exc):
    assert not is_database_unavailable(exc)


# --- the LISTEN loop -------------------------------------------------------------------------

class FakeListenConn:
    def __init__(self, ping_error=None):
        self.closed = False
        self.ping_error = ping_error
        self.on_notify = None
        self.on_terminate = None

    def add_termination_listener(self, cb):
        self.on_terminate = cb

    async def add_listener(self, channel, cb):
        self.on_notify = cb

    async def remove_listener(self, channel, cb):
        pass

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def fetchval(self, *a, **kw):
        if self.ping_error:
            raise self.ping_error
        return 1

    def die(self):
        self.closed = True
        self.on_terminate(self)

    def notify(self, payload):
        self.on_notify(self, 1, "c", payload)


async def _next(gen, timeout=2.0):
    return await asyncio.wait_for(gen.__anext__(), timeout)


def _broker_env(conns, check=0.02):
    return (
        patch("asyncpg.connect", new=AsyncMock(side_effect=conns)),
        patch.object(event_broker, "LISTENER_CHECK_SECONDS", check),
        patch.object(event_broker, "LISTENER_RECONNECT_SECONDS", 0),
    )


def test_listener_reconnects_when_its_connection_dies():
    async def scenario():
        first, second = FakeListenConn(), FakeListenConn()
        a, b, c = _broker_env([first, second])
        with a, b, c:
            gen = event_broker.PostgresEventBroker(session_factory=None).subscribe("c")
            pending = asyncio.ensure_future(_next(gen))
            await asyncio.sleep(0.01)
            first.die()
            await asyncio.sleep(0.05)
            assert second.on_notify is not None, "no reconnect after the connection died"
            second.notify('{"n": 1}')
            assert await pending == {"n": 1}
            await gen.aclose()

    asyncio.run(scenario())


def test_listener_reconnects_when_a_silent_connection_stops_answering():
    """No termination callback: a network cut only shows when the connection is used."""
    async def scenario():
        first, second = FakeListenConn(ping_error=OSError("timed out")), FakeListenConn()
        a, b, c = _broker_env([first, second])
        with a, b, c:
            gen = event_broker.PostgresEventBroker(session_factory=None).subscribe("c")
            pending = asyncio.ensure_future(_next(gen))
            await asyncio.sleep(0.1)
            assert first.closed and second.on_notify is not None
            second.notify('{"n": 2}')
            assert await pending == {"n": 2}
            await gen.aclose()

    asyncio.run(scenario())


# --- the alert leader ------------------------------------------------------------------------

def _lock_conn(acquired=True):
    conn = AsyncMock()
    res = MagicMock()
    res.scalar.return_value = acquired
    conn.execute = AsyncMock(return_value=res)
    return conn


def test_leader_steps_down_and_stops_listening_when_its_lock_connection_dies():
    async def scenario():
        dispatcher = AlertDispatcher()
        leader_conn, next_conn = _lock_conn(), _lock_conn()

        async def fake_listen(channel):
            await asyncio.sleep(3600)

        with patch.object(dispatcher_mod, "async_engine") as engine, \
             patch.object(dispatcher_mod, "LOCK_CHECK_SECONDS", 0.01), \
             patch.object(dispatcher, "_listen_broker", side_effect=fake_listen):
            engine.dialect.name = "postgresql"
            engine.connect = AsyncMock(side_effect=[leader_conn, next_conn])
            await dispatcher.start()
            assert dispatcher._is_leader is True
            first_listeners = list(dispatcher._listener_tasks)
            assert len(first_listeners) == 2

            leader_conn.execute.side_effect = asyncpg.InterfaceError("connection is closed")
            await asyncio.sleep(0.1)

            assert all(t.cancelled() for t in first_listeners), "old listeners would keep sending"
            leader_conn.close.assert_awaited()
            # It competes for the lock again and leads with new listeners.
            assert dispatcher._is_leader is True and dispatcher._lock_conn is next_conn
            assert len(dispatcher._listener_tasks) == 2
            assert not set(first_listeners) & set(dispatcher._listener_tasks)
            await dispatcher.stop()
            assert dispatcher._is_leader is False

    asyncio.run(scenario())


def test_worker_that_loses_the_election_after_stepping_down_does_not_send():
    async def scenario():
        dispatcher = AlertDispatcher()
        leader_conn = _lock_conn()
        taken = _lock_conn(acquired=False)  # the other worker holds it now

        async def fake_listen(channel):
            await asyncio.sleep(3600)

        with patch.object(dispatcher_mod, "async_engine") as engine, \
             patch.object(dispatcher_mod, "LOCK_CHECK_SECONDS", 0.01), \
             patch.object(dispatcher, "_listen_broker", side_effect=fake_listen):
            engine.dialect.name = "postgresql"
            engine.connect = AsyncMock(side_effect=[leader_conn] + [taken] * 200)
            await dispatcher.start()
            leader_conn.execute.side_effect = OSError("connection reset")
            await asyncio.sleep(0.1)
            assert dispatcher._is_leader is False
            assert dispatcher._listener_tasks == []
            await dispatcher.enqueue_event("STATE_TRANSITION", {"x": 1})
            assert dispatcher._queue.empty()
            await dispatcher.stop()

    asyncio.run(scenario())


# --- the probe backlog -----------------------------------------------------------------------

def _state(detailed="UP"):
    op = "DOWN" if detailed == "DOWN" else "UP"
    return EndpointState(
        endpoint_id=uuid4(), active_event_id=uuid4(), confirmed_operational_state=op,
        confirmed_detailed_state=detailed, pending_detailed_state=None, pending_cycle_count=0,
    )


class FakeSession:
    def __init__(self, error=None):
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **kw):
        return MagicMock()

    async def commit(self):
        pass


def _store(machine, session_error=None):
    return CycleStore(machine, asyncio.Semaphore(5), session_factory=lambda: FakeSession(session_error))


def _cycles(*results):
    return deque(ProbeCycle(T0 + timedelta(minutes=i), T0 + timedelta(minutes=i, seconds=32), r) for i, r in enumerate(results))


def _run_write(store, state, backlog):
    with patch("monitoring.cycle_store.record_probe_sample", new=AsyncMock(return_value=True)):
        return asyncio.run(store.write(state.endpoint_id, state, backlog))


def test_unavailable_database_keeps_the_backlog_and_announces_nothing():
    machine = MagicMock()
    machine.process_cycle = AsyncMock()
    machine.announce_transition = AsyncMock()
    backlog = _cycles(GOOD, BAD)
    assert _run_write(_store(machine, OSError("refused")), _state(), backlog) is None
    assert len(backlog) == 2
    machine.process_cycle.assert_not_called()
    machine.announce_transition.assert_not_called()


def test_a_cycle_that_cannot_be_written_does_not_block_every_later_one():
    machine = MagicMock()
    machine.process_cycle = AsyncMock(side_effect=ValueError("bad data"))
    backlog = _cycles(GOOD)
    with pytest.raises(ValueError):
        _run_write(_store(machine), _state(), backlog)
    assert len(backlog) == 0


def test_cycles_are_written_with_the_time_they_ran_and_without_announcing_each():
    machine = MagicMock()
    start = _state("UP")
    machine.process_cycle = AsyncMock(side_effect=lambda st, *a, **kw: st)
    machine.announce_transition = AsyncMock()
    backlog = _cycles(GOOD, GOOD)
    times = [c.finished_at for c in backlog]
    _run_write(_store(machine), start, backlog)
    assert [c.kwargs["at"] for c in machine.process_cycle.call_args_list] == times
    assert all(c.kwargs["announce"] is False for c in machine.process_cycle.call_args_list)
    assert len(backlog) == 0


def test_only_a_change_still_in_effect_is_announced():
    start, down = _state("UP"), _state("DOWN")

    def machine_ending_in(final):
        machine = MagicMock()
        machine.process_cycle = AsyncMock(return_value=final)
        machine.announce_transition = AsyncMock()
        return machine

    over = machine_ending_in(start)   # went down and came back during the outage
    _run_write(_store(over), start, _cycles(BAD, GOOD))
    over.announce_transition.assert_not_called()

    ongoing = machine_ending_in(down)  # still down when the database returned
    _run_write(_store(ongoing), start, _cycles(GOOD, BAD))
    ongoing.announce_transition.assert_awaited_once()


def test_failed_announcement_does_not_rewrite_the_saved_cycles():
    machine = MagicMock()
    down = _state("DOWN")
    machine.process_cycle = AsyncMock(return_value=down)
    machine.announce_transition = AsyncMock(side_effect=RuntimeError("broker down"))
    backlog = _cycles(BAD)
    assert _run_write(_store(machine), _state("UP"), backlog) is down
    assert len(backlog) == 0


def test_a_full_backlog_notes_the_lost_period_and_records_it_as_a_gap():
    machine = MagicMock()
    machine.process_cycle = AsyncMock(side_effect=lambda st, *a, **kw: st)
    machine.announce_transition = AsyncMock()
    store = _store(machine)
    backlog = deque(maxlen=3)
    for i in range(5):
        store.add(backlog, ProbeCycle(T0 + timedelta(minutes=i), T0 + timedelta(minutes=i), GOOD))
    assert len(backlog) == 3
    assert store._lost_from == T0 and store._lost_to == T0 + timedelta(minutes=2)
    with patch("monitoring.cycle_store.record_lost_cycles_gap", new=AsyncMock()) as gap:
        _run_write(store, _state(), backlog)
    assert gap.await_args.args[1:] == (T0, T0 + timedelta(minutes=2))
    assert store._lost_from is None


def test_a_startup_gap_still_closes_when_the_database_is_away_at_that_moment():
    from monitoring import engine

    close = AsyncMock(side_effect=[OSError("refused"), None])
    with patch.object(engine, "STARTUP_SETTLE_SECONDS", 0), patch.object(engine, "CYCLE_SECONDS", 0), \
            patch.object(engine, "AsyncSessionLocal", lambda: FakeSession()), patch.object(engine, "close_startup_gap", close):
        asyncio.run(engine._close_startup_gap(uuid4(), T0))
    assert close.await_count == 2


# --- an endpoint already down at its first probe ----------------------------------------------

def _first_probe(*results):
    """Creates an endpoint from the first result, then writes the rest one cycle at a time."""
    machine = StateMachine(confirmation_threshold=3)
    machine.announce_transition = AsyncMock()
    store = _store(machine)
    endpoint_id = uuid4()

    async def run():
        state = await machine.create_initial_event(endpoint_id, results[0], FakeSession())
        for cycle in results[1:]:
            state = await store.write(endpoint_id, state, _cycles(cycle))
        return state

    with patch("monitoring.cycle_store.record_probe_sample", new=AsyncMock(return_value=True)):
        state = asyncio.run(run())
    return machine.announce_transition, state


def _announced_states(announce):
    return [(c.args[0].confirmed_operational_state, c.args[1].confirmed_operational_state) for c in announce.await_args_list]


def test_down_at_first_probe_alerts_once_confirmed_and_only_once():
    announce, _ = _first_probe(BAD, BAD)
    announce.assert_not_awaited()
    announce, _ = _first_probe(BAD, BAD, BAD, BAD, BAD)
    assert _announced_states(announce) == [("UNKNOWN", "DOWN")]


def test_one_lost_ping_on_a_new_device_sends_no_down_alert():
    announce, state = _first_probe(BAD, GOOD, GOOD, GOOD)
    assert ("UNKNOWN", "DOWN") not in _announced_states(announce)
    assert state.first_down_cycles is None and state.confirmed_operational_state == "UP"


def test_a_flapping_first_down_needs_the_cycles_in_a_row():
    announce, _ = _first_probe(BAD, BAD, GOOD, BAD, BAD)
    announce.assert_not_awaited()
    announce, _ = _first_probe(BAD, BAD, GOOD, BAD, BAD, BAD)
    assert _announced_states(announce) == [("UNKNOWN", "DOWN")]


def test_up_at_first_probe_owes_nothing():
    announce, state = _first_probe(GOOD, GOOD, GOOD, GOOD)
    announce.assert_not_awaited()
    assert state.first_down_cycles is None


def test_first_down_kept_through_a_database_outage_follows_the_net_change():
    def replay(*results):
        machine = StateMachine(confirmation_threshold=3)
        machine.announce_transition = AsyncMock()
        endpoint_id = uuid4()
        with patch("monitoring.cycle_store.record_probe_sample", new=AsyncMock(return_value=True)):
            asyncio.run(_store(machine).write(endpoint_id, None, _cycles(*results)))
        return machine.announce_transition

    assert _announced_states(replay(BAD, BAD, BAD, BAD)) == [("UNKNOWN", "DOWN")]
    replay(BAD, BAD, BAD, GOOD, GOOD, GOOD).assert_not_awaited()  # over before the database returned


# --- a stuck event loop -----------------------------------------------------------------------

def _run_guard(body, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent")
    stalls = []

    async def run():
        task = start_loop_stall_guard(limit_seconds=0.3, tick_seconds=0.05, on_stall=stalls.append)
        await asyncio.sleep(0.01)
        await body()
        task.cancel()

    asyncio.run(run())
    return stalls


def test_stall_guard_fires_when_the_loop_stops_running(monkeypatch):
    async def stuck():
        time.sleep(0.7)

    assert len(_run_guard(stuck, monkeypatch)) == 1


def test_stall_guard_stays_quiet_while_the_loop_runs(monkeypatch):
    async def busy():
        await asyncio.sleep(0.7)

    assert _run_guard(busy, monkeypatch) == []


def test_stall_guard_is_off_outside_systemd(monkeypatch):
    # A debugger stopped at a breakpoint must not kill the process.
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    async def run():
        return start_loop_stall_guard()

    assert asyncio.run(run()) is None


GUARDED_PROCESS = """
import asyncio, sys, time
from app.core.watchdog import start_loop_stall_guard

async def main():
    start_loop_stall_guard(limit_seconds=1.0, tick_seconds=0.1)
    await asyncio.sleep(0.2)
    print("ready", flush=True)
    if sys.argv[1] == "stuck":
        time.sleep(30)
    else:
        await asyncio.sleep(4)

asyncio.run(main())
"""


def _guarded_process(mode):
    env = {**os.environ, "NOTIFY_SOCKET": "/nonexistent", "PYTHONPATH": str(ROOT / "backend")}
    proc = subprocess.Popen(
        [sys.executable, "-c", GUARDED_PROCESS, mode], env=env, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    return proc


def test_stuck_process_exits_with_the_code_uvicorn_replaces_and_shows_where_it_was():
    proc = _guarded_process("stuck")
    _, stderr = proc.communicate(timeout=15)
    assert proc.returncode == LOOP_STALL_EXIT_CODE
    assert "Event loop stuck" in stderr and "in main" in stderr


def test_a_paused_process_is_not_mistaken_for_a_stuck_loop():
    proc = _guarded_process("paused")
    os.kill(proc.pid, signal.SIGSTOP)
    time.sleep(2.5)  # more than the limit
    os.kill(proc.pid, signal.SIGCONT)
    proc.communicate(timeout=15)
    assert proc.returncode == 0


# --- API answers while the database is away --------------------------------------------------

def _client_with_session_store(store):
    from app.core.asgi import TestClient
    from app.database import get_db
    from app.main import app
    from app.services.driver_manager import driver_manager

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = override_db
    return (
        TestClient(app),
        lambda: app.dependency_overrides.pop(get_db, None),
        patch.object(driver_manager, "get_session_store", return_value=store),
        patch("app.routers.auth.decode_access_token", return_value={"sub": str(uuid4()), "jti": "j", "username": "u"}),
    )


@pytest.mark.parametrize(
    "error",
    [
        OSError("Multiple exceptions: [Errno 111] Connect call failed"),
        asyncpg.InterfaceError("connection is closed"),
    ],
    ids=["refused", "connection_closed"],
)
def test_session_check_failure_answers_503_not_401(error):
    """A 401 made the browser sign the user out of a session that was still valid."""
    from app.core.orm import DatabaseUnavailableError

    if type(error) is OSError:
        error = DatabaseUnavailableError(str(error))
    store = AsyncMock()
    store.is_session_active.side_effect = error
    client, clean, a, b = _client_with_session_store(store)
    try:
        with a, b:
            res = client.get("/api/v1/endpoints/", headers={"Cookie": "ephemres_access_token=t"})
    finally:
        clean()
    assert res.status_code == 503
    assert "database" in res.json()["detail"]
    assert res.headers.get("retry-after") == "15"


def test_a_session_that_is_really_gone_still_answers_401():
    store = AsyncMock()
    store.is_session_active.return_value = False
    client, clean, a, b = _client_with_session_store(store)
    try:
        with a, b:
            res = client.get("/api/v1/endpoints/", headers={"Cookie": "ephemres_access_token=t"})
    finally:
        clean()
    assert res.status_code == 401


def test_refused_connection_becomes_a_recognizable_error():
    """Every address refused: asyncpg raises a plain OSError, here and from any other file call."""
    from app.core.orm import AsyncEngine, DatabaseUnavailableError

    engine = AsyncEngine.__new__(AsyncEngine)

    async def scenario(pool_error):
        with patch.object(AsyncEngine, "_get_pool", new=AsyncMock(side_effect=pool_error)):
            await engine.acquire_connection()

    with pytest.raises(DatabaseUnavailableError):
        asyncio.run(scenario(OSError("Multiple exceptions")))
    assert is_database_unavailable(DatabaseUnavailableError("x"))
    with pytest.raises(asyncio.TimeoutError):  # stays a timeout: the 504 path
        asyncio.run(scenario(asyncio.TimeoutError()))


# --- readiness -------------------------------------------------------------------------------

def _readiness(mode, client_obj):
    from app.core.asgi import TestClient
    from app.main import app
    from app.services.driver_manager import driver_manager

    # The database side is a working ping (readiness uses app.main.ping_database since S6).
    with patch("app.main.ping_database", new=AsyncMock()), \
         patch.object(driver_manager, "_driver_mode", mode), \
         patch.object(driver_manager, "get_redis_client", return_value=client_obj):
        return TestClient(app).get("/health/readiness")


def test_healthy_standard_mode_server_is_ready():
    """No FastStore client exists in standard mode, and that made every server answer 503."""
    res = _readiness("postgres", None)
    assert res.status_code == 200 and res.json()["ready"] is True
    assert res.json()["redis"]["enabled"] is False


def test_performance_mode_still_depends_on_faststore():
    broken = AsyncMock()
    broken.ping.side_effect = RuntimeError("gone")
    assert _readiness("redis", broken).status_code == 503
    assert _readiness("redis", None).status_code == 503
    assert _readiness("redis", AsyncMock()).status_code == 200


# --- installer -------------------------------------------------------------------------------

def test_installer_installs_what_it_uses():
    """A minimal system has none of these, and the install stopped halfway."""
    script = (ROOT / "deploy" / "install.sh").read_text()
    packages = script[script.index('PACKAGES="'):]
    packages = packages[: packages.index('"\n')]
    for tool in ("rsync", "git", "sudo"):
        assert f" {tool} " in packages.replace("\\\n", " ")


# --- sessions and lockouts survive a reboot --------------------------------------------------

def test_performance_mode_keeps_sessions_and_lockouts_in_postgresql():
    """FastStore's file is gone after a reboot: that signed everyone out and cleared lockouts."""
    from app.services import driver_manager as dm
    from app.services.session_store import PostgresSessionStore

    class FakeClient:
        def __init__(self, **kw):
            self.ping = AsyncMock()
            self.aclose = AsyncMock()

    setting = MagicMock()
    setting.setting_value = "true"
    result = MagicMock()
    result.scalar_one_or_none.return_value = setting
    session = AsyncMock()
    session.execute.return_value = result
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    factory.return_value.__aexit__.return_value = None

    async def scenario():
        manager = dm.StorageDriverManager()
        with patch.object(dm, "AsyncSessionLocal", factory), patch("app.core.faststore.FastStoreClient", FakeClient):
            await manager.initialize()
        return manager

    manager = asyncio.run(scenario())
    assert manager.driver_mode == "redis"
    assert isinstance(manager.get_session_store(), PostgresSessionStore)
