"""One API worker runs the cluster-wide schedules; the rest stand by and take over on failure."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import scheduler_leader as leader_mod
from app.services.scheduler_leader import SchedulerLeader


def _engine(dialect: str, lock_results=()):
    """Fake async engine whose connections answer pg_try_advisory_lock from `lock_results`."""
    engine = MagicMock()
    engine.dialect.name = dialect
    results = iter(lock_results)
    conns = []

    async def connect():
        conn = MagicMock()
        conn.close = AsyncMock()

        async def execute(stmt):
            res = MagicMock()
            if "pg_try_advisory_lock" in str(stmt):
                res.scalar.return_value = next(results)
            return res

        conn.execute = AsyncMock(side_effect=execute)
        conns.append(conn)
        return conn

    engine.connect = connect
    return engine, conns


@pytest.mark.anyio
async def test_non_postgres_always_leads():
    engine, _ = _engine("sqlite")
    with patch.object(leader_mod, "async_engine", engine):
        leader = SchedulerLeader()
        await leader.start()
        assert leader.is_leader
        await leader.stop()
        assert not leader.is_leader


@pytest.mark.anyio
async def test_lock_winner_leads_and_releases_on_stop():
    engine, conns = _engine("postgresql", [True])
    with patch.object(leader_mod, "async_engine", engine):
        leader = SchedulerLeader(retry_seconds=60)
        await leader.start()
        assert leader.is_leader
        await leader.stop()
    assert not leader.is_leader
    assert any("pg_advisory_unlock" in str(c.args[0]) for c in conns[0].execute.call_args_list)
    conns[0].close.assert_awaited()


@pytest.mark.anyio
async def test_standby_is_promoted_when_lock_frees():
    engine, conns = _engine("postgresql", [False, True])
    with patch.object(leader_mod, "async_engine", engine):
        leader = SchedulerLeader(retry_seconds=0.01)
        await leader.start()
        assert not leader.is_leader
        conns[0].close.assert_awaited()  # the losing connection is not kept open
        for _ in range(100):
            if leader.is_leader:
                break
            await asyncio.sleep(0.01)
        assert leader.is_leader
        await leader.stop()


@pytest.mark.anyio
async def test_leader_steps_down_when_lock_connection_fails():
    engine, conns = _engine("postgresql", [True, False, False, False])
    with patch.object(leader_mod, "async_engine", engine):
        leader = SchedulerLeader(retry_seconds=0.01)
        await leader.start()
        assert leader.is_leader
        conns[0].execute.side_effect = ConnectionError("server closed the connection")
        for _ in range(100):
            if not leader.is_leader:
                break
            await asyncio.sleep(0.01)
        assert not leader.is_leader
        await leader.stop()


@pytest.mark.anyio
async def test_diagnostic_cleanup_skipped_on_standby_worker():
    from app.services import diagnostics

    session_factory = MagicMock()
    with patch.object(leader_mod.scheduler_leader, "_is_leader", False):
        task = await diagnostics.start_diagnostic_cleanup_task(session_factory, interval_seconds=3600)
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    session_factory.assert_not_called()

    with patch.object(leader_mod.scheduler_leader, "_is_leader", True), \
         patch.object(diagnostics, "cleanup_old_diagnostic_traces", AsyncMock()) as cleanup:
        db = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=db)
        ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=ctx)
        task = await diagnostics.start_diagnostic_cleanup_task(session_factory, interval_seconds=3600)
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    cleanup.assert_awaited_once()
