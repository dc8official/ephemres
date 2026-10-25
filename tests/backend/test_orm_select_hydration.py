"""Mixed model + scalar SELECTs must map NULL scalars to None, never to another column's value."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from app.core.orm import AsyncSession, func, select
from app.models.endpoint import Endpoint
from app.repositories.endpoint_repo import EndpointRepository


def _endpoint_record(**overrides):
    rec = {c: None for c in Endpoint.__table_columns__}
    rec.update(
        id=uuid.uuid4(),
        hostname="edge-01",
        ip_address="10.10.10.5",
        # The 5th/6th endpoint columns; a NULL scalar slot at index 4 used to read these.
        location="Lagos DC",
        description="",
        monitoring_enabled=True,
    )
    rec.update(overrides)
    return rec


def _hydrate(stmt, records):
    return asyncio.run(AsyncSession(MagicMock())._hydrate_select_results(stmt, records))


def test_null_scalars_after_model_slot_stay_none():
    """Regression: GET /endpoints 500'd with float('') when avg_rtt_ms was NULL and location was ''."""
    db = AsyncMock()
    res = MagicMock()
    res.all.return_value = []
    db.execute.return_value = res
    now = datetime.now(timezone.utc)
    asyncio.run(EndpointRepository(db).list_with_stats(since_utc=now - timedelta(days=1), now_utc=now))
    stmt = db.execute.call_args[0][0]

    rec = _endpoint_record(
        current_operational_state="DOWN",
        current_detailed_state="DOWN",
        current_health_score=0.0,
        avg_rtt_ms=None,
        last_seen=None,
        up_events_count=0,
        uptime_seconds=0,
    )
    row = _hydrate(stmt, [rec])[0]

    assert row.avg_rtt_ms is None
    assert row.last_seen is None
    assert row.current_operational_state == "DOWN"
    assert row[0].location == "Lagos DC"


def test_unnamed_scalar_falls_back_to_its_own_record_position():
    """A scalar whose name is not in the record is read from its column, past the model's columns."""
    stmt = select(Endpoint, func.count())
    rec = _endpoint_record()
    rec["unexpected_driver_name"] = 7  # last column: the count, under a name the ORM cannot predict
    row = _hydrate(stmt, [rec])[0]

    assert row[1] == 7
