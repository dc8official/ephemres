"""Real-time aggregation for the hourly and daily flow aggregates

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-26 12:00:00.000000

TimescaleDB 2.13+ creates continuous aggregates as materialized-only, so the 24h/7d/30d flow
windows missed everything newer than the last refresh (up to ~2 hours for hourly, ~2 days for
daily). With real-time aggregation the views union the materialized buckets with the
not-yet-materialized minute rows at query time.

Only continuous aggregates are touched; the plain-view fallbacks (no TimescaleDB) already read
the minute table directly.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _set_materialized_only(value: str) -> str:
    return f"""
        DO $$
        DECLARE
            agg_name TEXT;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                RETURN;
            END IF;
            FOR agg_name IN
                SELECT view_name
                FROM timescaledb_information.continuous_aggregates
                WHERE view_schema = current_schema()
                  AND view_name IN ('flow_hourly_rollups', 'flow_daily_rollups')
            LOOP
                EXECUTE format(
                    'ALTER MATERIALIZED VIEW %I SET (timescaledb.materialized_only = {value})',
                    agg_name
                );
            END LOOP;
        END $$;
    """


def upgrade() -> None:
    op.execute(_set_materialized_only("false"))


def downgrade() -> None:
    op.execute(_set_materialized_only("true"))
