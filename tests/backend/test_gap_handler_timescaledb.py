from __future__ import annotations
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from monitoring.gap_handler import resolve_startup_state


def _fake_db(executed, newest=None, fail_override=False):
    """AsyncMock session: no unresolved gap, and `newest` as the newest open period's last cycle."""
    mock_db = AsyncMock()

    async def fake_execute(statement, params=None):
        sql = str(statement)
        if fail_override and "timescaledb.max_tuples_decompressed_per_dml_transaction" in sql:
            raise Exception("permission denied or extension not available")
        executed.append((sql, params or {}))
        result = MagicMock()
        result.fetchone.return_value = None
        if "MAX(" in sql and "open_count" in sql:
            result.fetchone.return_value = MagicMock(newest=newest, open_count=1 if newest else 0)
        elif "INSERT INTO monitoring_service_events" in sql:
            result.fetchone.return_value = MagicMock(id=uuid4())
        result.scalar.return_value = newest
        result.rowcount = 42
        return result

    mock_db.execute.side_effect = fake_execute
    return mock_db


@pytest.mark.anyio
async def test_resolve_startup_state_applies_chunk_pruning():
    """The startup lifts the decompression cap and bounds the cleanup to the 7-day window."""
    executed = []
    newest = datetime.now(timezone.utc) - timedelta(minutes=3)
    gap_id = await resolve_startup_state(_fake_db(executed, newest=newest))

    assert gap_id is not None
    assert "timescaledb.max_tuples_decompressed_per_dml_transaction" in executed[0][0]
    update_sql, update_params = next(e for e in executed if e[0].lstrip().startswith("UPDATE endpoint_events"))
    assert "WHERE end_time IS NULL" in update_sql
    assert "AND start_time >= :window_start" in update_sql
    delta = datetime.now(timezone.utc) - update_params["window_start"]
    assert delta.total_seconds() == pytest.approx(7 * 86400, abs=5)
    insert_sql, insert_params = next(e for e in executed if "INSERT INTO monitoring_service_events" in e[0])
    assert insert_params["event_type"] == "SERVICE_RESTART"
    assert insert_params["start_time"] == newest


@pytest.mark.anyio
async def test_resolve_startup_state_without_open_periods_records_nothing():
    executed = []
    assert await resolve_startup_state(_fake_db(executed)) is None
    assert not any("INSERT INTO monitoring_service_events" in sql for sql, _ in executed)
    assert not any(sql.lstrip().startswith("UPDATE endpoint_events") for sql, _ in executed)


@pytest.mark.anyio
async def test_resolve_startup_state_timescaledb_override_graceful_fallback():
    """Verify that if TimescaleDB override fails (e.g. extension not present), startup continues cleanly."""
    executed = []
    # Must NOT raise exception despite the DO block throwing
    await resolve_startup_state(_fake_db(executed, newest=datetime.now(timezone.utc), fail_override=True))
    assert "FROM monitoring_service_events" in executed[0][0]
    assert any(sql.lstrip().startswith("UPDATE endpoint_events") for sql, _ in executed)
