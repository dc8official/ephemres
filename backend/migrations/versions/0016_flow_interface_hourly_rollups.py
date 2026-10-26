"""Hourly interface rollups for long windows

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-26 16:00:00.000000

flow_interface_minute_rollups is kept 7 days, so the 30-day interface view and exporter chart
silently showed one week of data averaged over thirty days. flow_interface_hourly_rollups is a
real-time continuous aggregate over the minute table, kept 365 days, that also stores each
hour's busiest minute so peaks survive aggregation.

The refresh policy looks back 6 days: inside the 7-day minute retention (a refresh never runs
over dropped chunks) and wide enough to materialize the history that already exists when this
migration runs.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HOURLY_SELECT = """
    SELECT
        {bucket} AS bucket,
        exporter_id,
        interface_idx,
        SUM(in_bytes)::BIGINT AS in_bytes,
        SUM(out_bytes)::BIGINT AS out_bytes,
        SUM(in_packets)::BIGINT AS in_packets,
        SUM(out_packets)::BIGINT AS out_packets,
        SUM(flow_count)::BIGINT AS flow_count,
        MAX(in_bytes)::BIGINT AS peak_in_minute_bytes,
        MAX(out_bytes)::BIGINT AS peak_out_minute_bytes,
        COUNT(*)::INTEGER AS minutes_with_data
    FROM flow_interface_minute_rollups
    GROUP BY {bucket}, exporter_id, interface_idx
"""


def upgrade() -> None:
    cagg_select = _HOURLY_SELECT.format(bucket="time_bucket('1 hour', bucket)")
    view_select = _HOURLY_SELECT.format(bucket="date_trunc('hour', bucket)")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                CREATE MATERIALIZED VIEW IF NOT EXISTS flow_interface_hourly_rollups
                WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                {cagg_select}
                WITH NO DATA;

                PERFORM add_continuous_aggregate_policy(
                    'flow_interface_hourly_rollups',
                    start_offset => INTERVAL '6 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );
                PERFORM add_retention_policy('flow_interface_hourly_rollups', INTERVAL '365 days', if_not_exists => TRUE);
            ELSE
                CREATE OR REPLACE VIEW flow_interface_hourly_rollups AS
                {view_select};
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
            ) AND EXISTS (
                SELECT 1 FROM timescaledb_information.continuous_aggregates
                WHERE view_name = 'flow_interface_hourly_rollups'
            ) THEN
                DROP MATERIALIZED VIEW IF EXISTS flow_interface_hourly_rollups CASCADE;
            ELSE
                DROP VIEW IF EXISTS flow_interface_hourly_rollups CASCADE;
            END IF;
        END $$;
    """)
