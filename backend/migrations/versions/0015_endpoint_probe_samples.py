"""Per-cycle ICMP probe history

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-26 14:00:00.000000

endpoint_events keeps one row per state period, so RTT, loss and jitter over time cannot be
reconstructed from it. The monitoring engine now writes one row per endpoint per cycle (60 s)
to endpoint_probe_samples:

- endpoint_probe_samples: hypertable, compressed after 2 days, kept 30 days.
- endpoint_probe_hourly: real-time continuous aggregate over the samples, kept 365 days.
  Its refresh policy looks back 3 days, well inside the 30-day sample retention, so a refresh
  never runs over chunks that have already been dropped.

Without TimescaleDB the samples are a plain table and the hourly aggregate a plain view.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HOURLY_SELECT = """
    SELECT
        {bucket} AS bucket,
        endpoint_id,
        COUNT(*)::INTEGER AS sample_count,
        SUM(sent)::INTEGER AS sent,
        SUM(received)::INTEGER AS received,
        MIN(rtt_min_ms) AS rtt_min_ms,
        -- Mean RTT over replies, not over samples: weight each cycle by its replies.
        (SUM(rtt_avg_ms * received) / NULLIF(SUM(received) FILTER (WHERE rtt_avg_ms IS NOT NULL), 0))::REAL AS rtt_avg_ms,
        MAX(rtt_max_ms) AS rtt_max_ms,
        AVG(jitter_ms)::REAL AS jitter_ms
    FROM endpoint_probe_samples
    GROUP BY {bucket}, endpoint_id
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS endpoint_probe_samples (
            endpoint_id UUID NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
            sampled_at TIMESTAMPTZ NOT NULL,
            sent SMALLINT NOT NULL,
            received SMALLINT NOT NULL,
            rtt_min_ms REAL,
            rtt_avg_ms REAL,
            rtt_max_ms REAL,
            jitter_ms REAL,
            CONSTRAINT pk_endpoint_probe_samples PRIMARY KEY (endpoint_id, sampled_at),
            CONSTRAINT ck_probe_samples_counts CHECK (sent >= 0 AND received >= 0 AND received <= sent)
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_probe_samples_sampled_at
        ON endpoint_probe_samples (sampled_at DESC);
    """)

    hourly_cagg = _HOURLY_SELECT.format(bucket="time_bucket('1 hour', sampled_at)")
    hourly_view = _HOURLY_SELECT.format(bucket="date_trunc('hour', sampled_at)")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable(
                    'endpoint_probe_samples', 'sampled_at',
                    chunk_time_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );
                ALTER TABLE endpoint_probe_samples SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'endpoint_id',
                    timescaledb.compress_orderby = 'sampled_at DESC'
                );
                PERFORM add_compression_policy('endpoint_probe_samples', INTERVAL '2 days', if_not_exists => TRUE);
                PERFORM add_retention_policy('endpoint_probe_samples', INTERVAL '30 days', if_not_exists => TRUE);

                CREATE MATERIALIZED VIEW IF NOT EXISTS endpoint_probe_hourly
                WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                {hourly_cagg}
                WITH NO DATA;

                PERFORM add_continuous_aggregate_policy(
                    'endpoint_probe_hourly',
                    start_offset => INTERVAL '3 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );
                PERFORM add_retention_policy('endpoint_probe_hourly', INTERVAL '365 days', if_not_exists => TRUE);
            ELSE
                CREATE OR REPLACE VIEW endpoint_probe_hourly AS
                {hourly_view};
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
                WHERE view_name = 'endpoint_probe_hourly'
            ) THEN
                DROP MATERIALIZED VIEW IF EXISTS endpoint_probe_hourly CASCADE;
            ELSE
                DROP VIEW IF EXISTS endpoint_probe_hourly CASCADE;
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE IF EXISTS endpoint_probe_samples CASCADE;")
