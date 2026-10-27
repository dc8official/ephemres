"""Wider continuous aggregate refresh windows

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-27 22:00:00.000000

A refresh policy materializes only the buckets inside its window (start_offset back from now).
Each run also moves the aggregate's watermark to the end of the window, and below the watermark
a real-time aggregate reads materialized data only. So when the job scheduler stops for longer
than the window (TimescaleDB workers disabled, database down over a long weekend), the hours it
missed are never materialized later and disappear from the views, although the raw rows are still
there. A wider window lets the next run fill them in.

Refreshes only recompute buckets that are missing or invalidated, so a normal hourly run still
processes about one hour of data; the window only bounds how far back a run will repair.

Each window stays inside the retention of its source and of the aggregate itself:

| Aggregate               | Source kept | Aggregate kept | Window       |
|-------------------------|-------------|----------------|--------------|
| endpoint_probe_hourly   | 30 days     | 365 days       | 3 -> 29 days |
| flow_hourly_rollups     | 90 days     | 30 days        | 3 -> 29 days |
| flow_daily_rollups      | 90 days     | 365 days       | 7 -> 60 days |

flow_interface_hourly_rollups keeps 6 days (its source is kept 7) and
node_historical_baselines_cagg keeps 28 days. Without TimescaleDB nothing changes.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (aggregate, new start_offset, previous start_offset, end_offset, schedule_interval)
_WINDOWS = (
    ("endpoint_probe_hourly", "29 days", "3 days", "1 hour", "1 hour"),
    ("flow_hourly_rollups", "29 days", "3 days", "1 hour", "1 hour"),
    ("flow_daily_rollups", "60 days", "7 days", "1 day", "1 day"),
)


def _set_windows(use_new: bool) -> str:
    statements = []
    for view, new_start, old_start, end, every in _WINDOWS:
        start = new_start if use_new else old_start
        statements.append(f"""
                IF EXISTS (
                    SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = '{view}'
                ) THEN
                    PERFORM remove_continuous_aggregate_policy('{view}', if_exists => TRUE);
                    PERFORM add_continuous_aggregate_policy(
                        '{view}',
                        start_offset => INTERVAL '{start}',
                        end_offset => INTERVAL '{end}',
                        schedule_interval => INTERVAL '{every}',
                        if_not_exists => TRUE
                    );
                END IF;""")
    body = "".join(statements)
    return f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN{body}
            END IF;
        END $$;
    """


def upgrade() -> None:
    op.execute(_set_windows(use_new=True))


def downgrade() -> None:
    op.execute(_set_windows(use_new=False))
