"""Continuous aggregate refresh windows after migration 0018, against real TimescaleDB.

Each window must stay inside the retention of the aggregate's source and of the aggregate itself:
a window past the source's retention would refresh over dropped chunks, and one past the
aggregate's own retention would rebuild hours that retention deletes again.
"""
import asyncio

import asyncpg

SOURCE = {
    "endpoint_probe_hourly": "endpoint_probe_samples",
    "flow_hourly_rollups": "flow_minute_rollups",
    "flow_daily_rollups": "flow_minute_rollups",
    "flow_interface_hourly_rollups": "flow_interface_minute_rollups",
    "node_historical_baselines_cagg": "endpoint_events",
}
EXPECTED_START = {
    "endpoint_probe_hourly": "29 days",
    "flow_hourly_rollups": "29 days",
    "flow_daily_rollups": "60 days",
    "flow_interface_hourly_rollups": "6 days",
    "node_historical_baselines_cagg": "28 days",
}


def test_refresh_windows_are_wide_and_inside_retention(timescale_dsn):
    async def read():
        conn = await asyncpg.connect(timescale_dsn, timeout=30.0)
        try:
            return await conn.fetch("""
                SELECT proc_name, hypertable_name, config->>'start_offset' AS start_text,
                       (config->>'start_offset')::interval AS start_offset,
                       (config->>'drop_after')::interval AS drop_after
                FROM timescaledb_information.jobs
                WHERE proc_name IN ('policy_refresh_continuous_aggregate', 'policy_retention')
            """)
        finally:
            await conn.close()

    jobs = asyncio.run(read())
    refresh = {r["hypertable_name"]: r for r in jobs if r["proc_name"] == "policy_refresh_continuous_aggregate"}
    retention = {r["hypertable_name"]: r["drop_after"] for r in jobs if r["proc_name"] == "policy_retention"}

    assert set(refresh) == set(EXPECTED_START)
    for view, expected in EXPECTED_START.items():
        assert refresh[view]["start_text"] == expected, view
        window = refresh[view]["start_offset"]
        source_drop = retention.get(SOURCE[view])
        if source_drop is not None:
            assert window < source_drop, f"{view} window reaches past {SOURCE[view]} retention"
        own_drop = retention.get(view)
        if own_drop is not None:
            assert window < own_drop, f"{view} window reaches past its own retention"
