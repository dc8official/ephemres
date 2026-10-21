"""Admin UI settings and config.toml knobs must actually change runtime behaviour."""

import asyncio
import socket
import struct
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import runtime_settings
from app.services.runtime_settings import config_defaults, get_runtime_settings, resolve, set_runtime_settings


@pytest.fixture(autouse=True)
def _reset_runtime_settings():
    runtime_settings._current = None
    yield
    runtime_settings._current = None


def _with(**overrides):
    set_runtime_settings(replace(config_defaults(), **overrides))


# --- resolve(): app_settings rows override config.toml, invalid rows fall back ---------------


def test_resolve_prefers_db_values_over_config():
    rt = resolve(
        {
            "session_timeout": "45",
            "lockout_threshold": "3",
            "l2_auto_bypass": "false",
            "flow_sampling_multiplier": "100",
            "flow_netflow_port": "9995",
            "flow_ipfix_port": "4740",
        }
    )
    assert rt.session_timeout_minutes == 45
    assert rt.lockout_threshold == 3
    assert rt.l2_auto_bypass is False
    assert rt.flow_sampling_multiplier == 100
    assert rt.flow_netflow_port == 9995
    assert rt.flow_ipfix_port == 4740


def test_resolve_falls_back_to_config_for_missing_or_invalid_rows():
    defaults = config_defaults()
    rt = resolve({"session_timeout": "abc", "lockout_threshold": "0", "flow_ipfix_port": "70000"})
    assert rt == defaults


def test_refresh_keeps_previous_snapshot_on_db_error():
    _with(lockout_threshold=7)

    def broken_factory():
        raise RuntimeError("db down")

    result = asyncio.run(runtime_settings.refresh_runtime_settings(broken_factory))
    assert result.lockout_threshold == 7
    assert get_runtime_settings().lockout_threshold == 7


# --- session_timeout --------------------------------------------------------------------------


def test_access_token_expiry_follows_session_timeout():
    from app.services.auth_service import create_access_token, decode_access_token

    _with(session_timeout_minutes=15)
    token = create_access_token(user_id="u1", username="alice", role_name="ADMIN")
    claims = decode_access_token(token)
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == 15 * 60


# --- lockout_threshold ------------------------------------------------------------------------


def test_in_memory_lockout_uses_threshold():
    from app.services import auth_service

    _with(lockout_threshold=2)
    auth_service._failed_attempts.clear()
    auth_service.record_failed_attempt("203.0.113.9", "bob")
    assert not auth_service.is_account_locked("203.0.113.9", "bob")
    auth_service.record_failed_attempt("203.0.113.9", "bob")
    assert auth_service.is_account_locked("203.0.113.9", "bob")
    auth_service._failed_attempts.clear()


def test_faststore_lockout_uses_threshold():
    from app.services.session_store import FastStoreSessionStore

    store = MagicMock()
    store.incr = AsyncMock(return_value=3)
    store.expire = AsyncMock()
    store.set = AsyncMock()
    session_store = FastStoreSessionStore.__new__(FastStoreSessionStore)
    session_store.store = store

    _with(lockout_threshold=4)
    asyncio.run(session_store.record_failed_attempt("203.0.113.9", "bob"))
    store.set.assert_not_called()

    _with(lockout_threshold=3)
    asyncio.run(session_store.record_failed_attempt("203.0.113.9", "bob"))
    store.set.assert_called_once()


# --- l2_auto_bypass ---------------------------------------------------------------------------


@pytest.mark.parametrize("bypass_enabled", [True, False])
def test_l2_auto_bypass_toggle(bypass_enabled):
    from app.services import diagnostics

    _with(l2_auto_bypass=bypass_enabled)
    raw_hops = [{"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.0}, {"hop": 2, "ip": "192.168.1.20", "rtt_ms": 2.0}]
    with patch.object(diagnostics, "is_local_subnet_destination", return_value=True), patch.object(
        diagnostics, "_run_raw_icmp_traceroute", AsyncMock(return_value=raw_hops)
    ) as raw:
        result = asyncio.run(diagnostics.run_traceroute("192.168.1.20"))

    if bypass_enabled:
        assert result["hops"][0].get("is_l2") is True
        raw.assert_not_called()
    else:
        assert not any(h.get("is_l2") for h in result["hops"])
        raw.assert_awaited_once()


# --- flow_sampling_multiplier -----------------------------------------------------------------


def _v5_packet(sampling: int, d_octets: int = 1500, d_pkts: int = 10) -> bytes:
    from monitoring.flow.v5_parser import HEADER_STRUCT, RECORD_STRUCT

    header = HEADER_STRUCT.pack(5, 1, 100000, 1700000000, 0, 1, 0, 0, sampling)
    src = struct.unpack("!I", socket.inet_aton("10.0.0.1"))[0]
    dst = struct.unpack("!I", socket.inet_aton("10.0.0.2"))[0]
    record = RECORD_STRUCT.pack(
        src, dst, 0, 1, 2, d_pkts, d_octets, 90000, 99000, 443, 54321, 0, 0x18, 6, 0, 0, 0, 24, 24, 0
    )
    return header + record


def _collect(packet: bytes) -> dict:
    from monitoring.flow.collector import FlowCollector

    collector = FlowCollector(store_client=None)
    collector.handle_packet(packet, ("192.0.2.10", 2055), 2055)
    assert len(collector._queue) == 1
    return collector._queue[0]


def test_sampling_multiplier_scales_unsampled_flows():
    _with(flow_sampling_multiplier=1000)
    flow = _collect(_v5_packet(sampling=0))
    assert flow["bytes"] == 1500 * 1000
    assert flow["packets"] == 10 * 1000


def test_sampling_multiplier_not_applied_twice_to_header_sampled_v5():
    _with(flow_sampling_multiplier=1000)
    flow = _collect(_v5_packet(sampling=100))
    assert flow["bytes"] == 1500 * 100
    assert flow["packets"] == 10 * 100


def test_default_sampling_multiplier_leaves_counters_untouched():
    flow = _collect(_v5_packet(sampling=0))
    assert flow["bytes"] == 1500
    assert flow["packets"] == 10


# --- monitoring.ping_count / ping_interval_seconds / state_confirmation_cycles ---------------


def test_ping_plan_accepts_configs_within_cycle_budget():
    from monitoring.engine import resolve_ping_plan, worst_case_sweep_seconds

    assert worst_case_sweep_seconds(5, 8.0) == 42.0
    assert resolve_ping_plan(5, 8) == (5, 8.0)
    assert resolve_ping_plan(3, 10) == (3, 10.0)


def test_ping_plan_rejects_sweeps_that_overrun_the_cycle():
    from monitoring.engine import DEFAULT_PING_COUNT, DEFAULT_PING_INTERVAL_SECONDS, resolve_ping_plan

    # The previous config.py defaults (10 pings @ 6s) take up to 74s.
    assert resolve_ping_plan(10, 6) == (DEFAULT_PING_COUNT, DEFAULT_PING_INTERVAL_SECONDS)


def test_config_defaults_fit_the_cycle():
    from app.config import MonitoringSettings
    from monitoring.engine import resolve_ping_plan

    m = MonitoringSettings()
    assert resolve_ping_plan(m.ping_count, m.ping_interval_seconds) == (m.ping_count, float(m.ping_interval_seconds))


def test_engine_state_machine_uses_configured_confirmation_cycles():
    import inspect

    from monitoring import engine

    assert "settings.monitoring.state_confirmation_cycles" in inspect.getsource(engine.main)


# --- monitoring.retention_days ----------------------------------------------------------------


def _retention_db(current_drop_after):
    db = MagicMock()
    db.commit = AsyncMock()
    calls = []

    async def execute(stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        calls.append((sql, params))
        res = MagicMock()
        if "pg_extension" in sql:
            res.fetchone.return_value = (1,)
        elif "timescaledb_information.jobs" in sql:
            res.fetchone.return_value = None if current_drop_after is None else (current_drop_after,)
        return res

    db.execute = execute
    return db, calls


def test_retention_sync_is_noop_when_policy_matches():
    from app.services.retention_policy import sync_event_retention_policy

    db, calls = _retention_db("730 days")
    assert asyncio.run(sync_event_retention_policy(db, 730)) is False
    assert not any("add_retention_policy" in sql for sql, _ in calls)


def test_retention_sync_replaces_differing_policy():
    from app.services.retention_policy import sync_event_retention_policy

    db, calls = _retention_db("730 days")
    assert asyncio.run(sync_event_retention_policy(db, 365)) is True
    sqls = [sql for sql, _ in calls]
    assert any("remove_retention_policy" in s for s in sqls)
    add = [(s, p) for s, p in calls if "add_retention_policy" in s]
    assert add and add[0][1] == {"drop_after": "365 days"}
    db.commit.assert_awaited_once()


# --- flow.buffer_memory_limit_mb --------------------------------------------------------------


def test_circuit_breaker_uses_configured_memory_limit():
    from monitoring.flow.aggregator import FlowAggregator

    store = MagicMock()
    store.info = AsyncMock(return_value={"used_memory": 450 * 1024 * 1024, "maxmemory": 256 * 1024 * 1024})

    assert asyncio.run(FlowAggregator(store_client=store, memory_limit_mb=512)._check_circuit_breaker()) is False
    assert asyncio.run(FlowAggregator(store_client=store, memory_limit_mb=1024)._check_circuit_breaker()) is True
