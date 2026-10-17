"""add_node_historical_baselines

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Define continuous aggregate view 'node_historical_baselines' on endpoint_events
    # Grouped by endpoint_id, EXTRACT(dow FROM start_time) (0-6), and EXTRACT(hour FROM start_time) (0-23).
    # Calculates historical_mean (AVG of latency) and historical_stddev (STDDEV of latency).
    op.execute("""
        DO $$
        BEGIN
            BEGIN
                CREATE MATERIALIZED VIEW node_historical_baselines
                WITH (timescaledb.continuous) AS
                SELECT
                    endpoint_id,
                    EXTRACT(dow FROM start_time)::INTEGER AS day_of_week,
                    EXTRACT(hour FROM start_time)::INTEGER AS hour_of_day,
                    AVG(avg_rtt_ms)::FLOAT AS historical_mean,
                    STDDEV(avg_rtt_ms)::FLOAT AS historical_stddev
                FROM endpoint_events
                WHERE avg_rtt_ms IS NOT NULL
                GROUP BY endpoint_id, EXTRACT(dow FROM start_time), EXTRACT(hour FROM start_time);
            EXCEPTION WHEN OTHERS THEN
                CREATE VIEW node_historical_baselines AS
                SELECT
                    endpoint_id,
                    EXTRACT(dow FROM start_time)::INTEGER AS day_of_week,
                    EXTRACT(hour FROM start_time)::INTEGER AS hour_of_day,
                    AVG(avg_rtt_ms)::FLOAT AS historical_mean,
                    STDDEV(avg_rtt_ms)::FLOAT AS historical_stddev
                FROM endpoint_events
                WHERE avg_rtt_ms IS NOT NULL
                GROUP BY endpoint_id, EXTRACT(dow FROM start_time), EXTRACT(hour FROM start_time);
            END;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS node_historical_baselines CASCADE';
        EXCEPTION WHEN OTHERS THEN
            EXECUTE 'DROP VIEW IF EXISTS node_historical_baselines CASCADE';
        END $$;
    """)
