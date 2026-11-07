import pytest
from app.routers.reports import resolve_export_columns, BatchExportRequest


def test_resolve_export_columns_default():
    cols = resolve_export_columns(None)
    assert len(cols) == 9
    assert cols[0] == "Endpoint_ID"
    assert "Hostname" in cols
    assert "IP_Address" in cols


def test_resolve_export_columns_mandatory_hostname_and_ip():
    # User only selected Timestamp and Operational State, leaving out Hostname and IP
    requested = ["Timestamp", "Operational_State"]
    cols = resolve_export_columns(requested)

    # Hostname and IP_Address must be guaranteed!
    assert "Hostname" in cols
    assert "IP_Address" in cols
    assert "Timestamp" in cols
    assert "Operational_State" in cols
    assert "Endpoint_ID" not in cols  # unselected, not mandatory


def test_resolve_export_columns_aliases():
    requested = ["hostname", "ip address", "health score / packet loss %", "avg latency (rtt ms)"]
    cols = resolve_export_columns(requested)
    assert "Hostname" in cols
    assert "IP_Address" in cols
    assert "Packet_Success_Rate" in cols
    assert "Avg_RTT_ms" in cols


def test_batch_export_request_schema():
    req = BatchExportRequest(
        endpoint_ids=[],
        start_time="2026-09-09T00:00:00Z",
        end_time="2026-09-09T01:00:00Z",
        columns=["Hostname", "IP_Address", "Detailed_State"],
    )
    assert req.columns == ["Hostname", "IP_Address", "Detailed_State"]


@pytest.mark.anyio
async def test_csv_generator_batch_buffering():
    from datetime import datetime, timezone, timedelta
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4
    from app.models.endpoint_event import EndpointEvent
    from app.routers.reports import csv_generator

    ep_id = uuid4()
    start_dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end_dt = datetime(2026, 9, 2, tzinfo=timezone.utc)

    # Generate 1,200 mock rows (1,000 in first query, 200 in second query)
    rows_batch_1 = []
    for i in range(1000):
        ev = EndpointEvent(
            id=uuid4(),
            endpoint_id=ep_id,
            operational_state="UP",
            detailed_state="UP",
            health_score=100.0,
            avg_rtt_ms=10.0,
            start_time=start_dt + timedelta(seconds=i),
        )
        rows_batch_1.append((ev, f"host-{i}", "192.168.1.1", "SWITCH"))

    rows_batch_2 = []
    for i in range(1000, 1200):
        ev = EndpointEvent(
            id=uuid4(),
            endpoint_id=ep_id,
            operational_state="UP",
            detailed_state="UP",
            health_score=100.0,
            avg_rtt_ms=10.0,
            start_time=start_dt + timedelta(seconds=i),
        )
        rows_batch_2.append((ev, f"host-{i}", "192.168.1.1", "SWITCH"))

    mock_session = AsyncMock()
    mock_res_1 = MagicMock()
    mock_res_1.all.return_value = rows_batch_1
    mock_res_2 = MagicMock()
    mock_res_2.all.return_value = rows_batch_2
    mock_session.execute.side_effect = [mock_res_1, mock_res_2]

    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None

    with patch("app.routers.reports.AsyncSessionLocal", return_value=mock_ctx):
        chunks = []
        async for chunk in csv_generator([ep_id], start_dt, end_dt):
            chunks.append(chunk)

    # chunks[0] is the CSV header
    # chunks[1] is batch 1 (500 rows)
    # chunks[2] is batch 2 (500 rows)
    # chunks[3] is batch 3 (remaining 200 rows flushed at end)
    assert len(chunks) == 4
    assert "Endpoint_ID,Hostname,IP_Address" in chunks[0]

    # Verify each chunk contains multiple rows (batch buffered, not row-by-row)
    data_chunk_1_rows = [line for line in chunks[1].strip().split("\n") if line]
    data_chunk_2_rows = [line for line in chunks[2].strip().split("\n") if line]
    data_chunk_3_rows = [line for line in chunks[3].strip().split("\n") if line]

    assert len(data_chunk_1_rows) == 500
    assert len(data_chunk_2_rows) == 500
    assert len(data_chunk_3_rows) == 200


@pytest.mark.anyio
async def test_csv_generator_empty_endpoints():
    from datetime import datetime, timezone
    from app.routers.reports import csv_generator

    start_dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end_dt = datetime(2026, 9, 2, tzinfo=timezone.utc)

    chunks = []
    async for chunk in csv_generator([], start_dt, end_dt):
        chunks.append(chunk)

    # Should only emit header and exit immediately
    assert len(chunks) == 1
    assert "Endpoint_ID,Hostname,IP_Address" in chunks[0]


def test_telemetry_router_prefix():
    from app.routers.reports import telemetry_router

    assert telemetry_router.prefix == "/api/v1/telemetry"
