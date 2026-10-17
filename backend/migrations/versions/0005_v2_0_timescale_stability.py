"""v2_0_timescale_stability

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. TimescaleDB 7-Day Chunk Compression Policy on endpoint_events
    op.execute("""
    DO $$
    BEGIN
        -- Check if TimescaleDB is installed and endpoint_events is a hypertable
        IF EXISTS (
            SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
        ) THEN
            -- Enable hypertable compression on endpoint_events
            BEGIN
                ALTER TABLE endpoint_events SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'endpoint_id',
                    timescaledb.compress_orderby = 'start_time DESC'
                );
            EXCEPTION WHEN OTHERS THEN
                NULL; -- Already set or compression already enabled
            END;

            -- Add 7-Day chunk compression policy
            BEGIN
                PERFORM add_compression_policy('endpoint_events', INTERVAL '7 days', if_not_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            -- Add Continuous Aggregate Refresh Policy on node_historical_baselines
            BEGIN
                PERFORM add_continuous_aggregate_policy(
                    'node_historical_baselines',
                    start_offset => INTERVAL '30 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            BEGIN
                PERFORM remove_continuous_aggregate_policy('node_historical_baselines', if_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            BEGIN
                PERFORM remove_compression_policy('endpoint_events', if_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END IF;
    END $$;
    """)
