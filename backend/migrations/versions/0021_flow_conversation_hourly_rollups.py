"""Hourly conversation rollups with addresses

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-07 12:00:00.000000

Top talkers, conversations, the anomaly summary and the QoS classes need addresses or ToS, which
no tier above flow_minute_rollups kept, so they scanned the minute table for every window. With
about 1,400 conversations a minute, 24 hours took 26 s and 7 days hit the 10 s query timeout.

flow_conversation_hourly_rollups keeps everything those views group by, per exporter, so they can
still count a conversation seen by two routers once (conversations.counted_once_cte).

Windows:
- The refresh policy looks back 31 days: the longest window the UI offers, and all the history
  that gets materialized when this migration runs. Older minute rows are not backfilled.
- Kept 90 days, like the minute table it is built from.
- No group indexes (timescaledb.create_group_indexes): 7 extra indexes for lookups that scan at
  most a few hundred thousand hourly rows.

The first refresh is scheduled for right after this migration, in the background. Until it
finishes, reads go through the real-time part of the aggregate, so long windows stay slow. Later
refreshes only recompute missing or invalidated buckets.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HOURLY_SELECT = """
    SELECT
        {bucket} AS bucket,
        exporter_id,
        src_endpoint_id,
        dst_endpoint_id,
        src_ip,
        dst_ip,
        protocol,
        dst_port,
        SUM(bytes)::BIGINT AS bytes,
        SUM(packets)::BIGINT AS packets,
        SUM(flow_count)::BIGINT AS flow_count,
        BIT_OR(tcp_flags)::SMALLINT AS tcp_flags,
        MAX(tos)::SMALLINT AS tos
    FROM flow_minute_rollups
    GROUP BY {bucket}, exporter_id, src_endpoint_id, dst_endpoint_id, src_ip, dst_ip, protocol, dst_port
"""


def upgrade() -> None:
    cagg_select = _HOURLY_SELECT.format(bucket="time_bucket('1 hour', bucket)")
    view_select = _HOURLY_SELECT.format(bucket="date_trunc('hour', bucket)")
    op.execute(f"""
        DO $$
        DECLARE
            refresh_job INTEGER;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                CREATE MATERIALIZED VIEW IF NOT EXISTS flow_conversation_hourly_rollups
                WITH (
                    timescaledb.continuous,
                    timescaledb.materialized_only = false,
                    timescaledb.create_group_indexes = false
                ) AS
                {cagg_select}
                WITH NO DATA;

                refresh_job := add_continuous_aggregate_policy(
                    'flow_conversation_hourly_rollups',
                    start_offset => INTERVAL '31 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );
                -- Materialize the history now instead of one schedule interval from now.
                IF refresh_job IS NOT NULL AND refresh_job > 0 THEN
                    PERFORM alter_job(refresh_job, next_start => NOW());
                END IF;
                PERFORM add_retention_policy('flow_conversation_hourly_rollups', INTERVAL '90 days', if_not_exists => TRUE);
            ELSE
                CREATE OR REPLACE VIEW flow_conversation_hourly_rollups AS
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
                WHERE view_name = 'flow_conversation_hourly_rollups'
            ) THEN
                DROP MATERIALIZED VIEW IF EXISTS flow_conversation_hourly_rollups CASCADE;
            ELSE
                DROP VIEW IF EXISTS flow_conversation_hourly_rollups CASCADE;
            END IF;
        END $$;
    """)
