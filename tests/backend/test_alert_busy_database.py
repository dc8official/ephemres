"""An alert waits out a busy database instead of being lost (branch review, 2026-11-10).

Since scale review S6 a full pool raises PoolTimeoutError after 5 s where it used to wait. The
dispatcher took that as final at each step: the alerting switch read as "on", the channel list
as empty. A pool busy for 10 s lost the alert with one line in the log, and a busy pool at the
wrong moment sent alerts that were switched off. Now each read is tried again for up to
DB_PATIENCE_SECONDS, one alert at a time so waiting alerts keep their order.
tests/integration/test_alert_busy_pool.py runs the same case on a real pool.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.alert_dispatcher as mod
from app.core.orm import PoolTimeoutError
from app.services.alert_dispatcher import AlertDispatcher


class _Database:
    """Stands in for AsyncSessionLocal. Opening a session raises each of `failures` in turn,
    then works: a read returns the alerting switch and the channels, a write is recorded."""

    def __init__(self, failures=(), switch=None, channels=()):
        self.failures = list(failures)
        self.switch = switch
        self.channels = list(channels)
        self.opened = 0
        self.written = []

    def __call__(self):
        return self

    async def __aenter__(self):
        self.opened += 1
        if self.failures:
            raise self.failures.pop(0)
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        setting = None if self.switch is None else SimpleNamespace(setting_value=self.switch)
        scalars = SimpleNamespace(all=lambda: list(self.channels))
        return SimpleNamespace(scalar_one_or_none=lambda: setting, scalars=lambda: scalars)

    def add(self, row):
        self._pending = row

    async def commit(self):
        self.written.append(self._pending)


def _busy() -> PoolTimeoutError:
    return PoolTimeoutError("no database connection became free within 5 s (pool of 10)")


def _channel(name="ops"):
    return SimpleNamespace(id=uuid4(), name=name)


def _down(hostname="core-sw1"):
    return {"endpoint_id": str(uuid4()), "hostname": hostname, "ip_address": "10.0.0.1",
            "operational_state": "DOWN"}


@pytest.fixture
def dispatcher(monkeypatch):
    """A dispatcher that records which alerts it would send, with no pause between tries."""
    monkeypatch.setattr(mod, "DB_RETRY_DELAYS", (0.0,))
    d = AlertDispatcher()
    d.sent = []

    async def record(**kw):
        d.sent.append(kw["endpoint_name"])

    d._evaluate_and_send_channel = record
    return d


def _use(monkeypatch, database: _Database) -> _Database:
    monkeypatch.setattr(mod, "AsyncSessionLocal", database)
    return database


# --- the alert is sent once the database is back ------------------------------------------------

@pytest.mark.anyio
async def test_an_alert_is_sent_after_the_pool_was_busy_for_several_tries(dispatcher, monkeypatch):
    # Three failed tries is more than the two (switch, then channels) that used to lose it.
    db = _use(monkeypatch, _Database(failures=[_busy(), _busy(), _busy()], channels=[_channel()]))
    await dispatcher._dispatch_event("STATE_TRANSITION", _down())
    assert dispatcher.sent == ["core-sw1"]
    assert db.opened == 5  # 3 failed, then the switch and the channels


@pytest.mark.anyio
async def test_a_restarting_database_is_waited_out_too(dispatcher, monkeypatch):
    _use(monkeypatch, _Database(failures=[ConnectionRefusedError("refused")], channels=[_channel()]))
    await dispatcher._dispatch_event("STATE_TRANSITION", _down())
    assert dispatcher.sent == ["core-sw1"]


@pytest.mark.anyio
async def test_alerting_switched_off_stays_off_through_a_busy_pool(dispatcher, monkeypatch):
    # One failed read used to mean "on", so a busy pool sent alerts an admin had switched off.
    _use(monkeypatch, _Database(failures=[_busy()], switch="false", channels=[_channel()]))
    await dispatcher._dispatch_event("STATE_TRANSITION", _down())
    assert dispatcher.sent == []


# --- when waiting can't help ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_an_error_that_waiting_cannot_cure_is_not_retried(dispatcher, monkeypatch, caplog):
    dispatcher._is_alerting_enabled = lambda: asyncio.sleep(0, result=True)
    db = _use(monkeypatch, _Database(failures=[ValueError("relation alert_channels does not exist")]))
    with caplog.at_level("ERROR", logger="app.services.alert_dispatcher"):
        await dispatcher._dispatch_event("STATE_TRANSITION", _down())
    assert db.opened == 1
    assert dispatcher.sent == []
    assert "the DOWN alert for core-sw1 was not sent" in caplog.text


@pytest.mark.anyio
async def test_the_alert_is_given_up_after_the_patience_runs_out(dispatcher, monkeypatch, caplog):
    monkeypatch.setattr(mod, "DB_PATIENCE_SECONDS", 0.0)
    _use(monkeypatch, _Database(failures=[_busy()] * 4, channels=[_channel()]))
    with caplog.at_level("ERROR", logger="app.services.alert_dispatcher"):
        await dispatcher._dispatch_event("STATE_TRANSITION", _down())
    assert dispatcher.sent == []
    assert "the DOWN alert for core-sw1 was not sent" in caplog.text


# --- order ---------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_alerts_that_waited_are_sent_in_the_order_their_events_arrived(dispatcher, monkeypatch):
    # The first alert meets the busy pool and pauses; the second arrives while it does. Without
    # the gate the second read the database first and RECOVERED went out before DOWN.
    monkeypatch.setattr(mod, "DB_RETRY_DELAYS", (0.05,))
    _use(monkeypatch, _Database(failures=[_busy()], channels=[_channel()]))
    first = asyncio.create_task(dispatcher._dispatch_event("STATE_TRANSITION", _down("down-first")))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(dispatcher._dispatch_event("STATE_TRANSITION", _down("recovered-second")))
    await asyncio.gather(first, second)
    assert dispatcher.sent == ["down-first", "recovered-second"]


# --- the delivery log ----------------------------------------------------------------------------

def _log_args():
    return dict(db=None, channel_id=uuid4(), channel_name="ops", endpoint_id=None,
                endpoint_name="core-sw1", event_type="STATE_TRANSITION", status="DELIVERED",
                status_code=200, response_message="OK")


@pytest.mark.anyio
async def test_a_delivery_log_is_written_after_a_busy_pool(dispatcher, monkeypatch):
    db = _use(monkeypatch, _Database(failures=[_busy(), _busy()]))
    await dispatcher._record_delivery_log(**_log_args())
    assert len(db.written) == 1 and db.written[0].status == "DELIVERED"


@pytest.mark.anyio
async def test_a_delivery_log_write_that_may_have_reached_the_database_is_not_repeated(
    dispatcher, monkeypatch, caplog
):
    # A reset mid-write may or may not have stored the row; trying again could store it twice.
    db = _use(monkeypatch, _Database(failures=[ConnectionResetError("reset")]))
    with caplog.at_level("ERROR", logger="app.services.alert_dispatcher"):
        await dispatcher._record_delivery_log(**_log_args())
    assert db.opened == 1 and db.written == []
    assert "Failed to write delivery log" in caplog.text
