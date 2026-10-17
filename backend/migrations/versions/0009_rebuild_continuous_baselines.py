"""rebuild_continuous_baselines

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop previous view/materialized view
    op.execute("""
        DO $$
        BEGIN
            EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS node_historical_baselines CASCADE';
        EXCEPTION WHEN OTHERS THEN
            EXECUTE 'DROP VIEW IF EXISTS node_historical_baselines CASCADE';
        END $$;
    """)

    # Rebuild with proper TimescaleDB continuous aggregate or plain Postgres view
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                BEGIN
                    -- Create continuous aggregate with valid time_bucket
                    CREATE MATERIALIZED VIEW node_historical_baselines_cagg
                    WITH (timescaledb.continuous) AS
                    SELECT
                        endpoint_id,
                        time_bucket('1 hour', start_time) AS bucket,
                        AVG(avg_rtt_ms)::FLOAT AS hourly_mean,
                        STDDEV(avg_rtt_ms)::FLOAT AS hourly_stddev
                    FROM endpoint_events
                    WHERE avg_rtt_ms IS NOT NULL
                    GROUP BY endpoint_id, time_bucket('1 hour', start_time)
                    WITH NO DATA;

                    PERFORM add_continuous_aggregate_policy(
                        'node_historical_baselines_cagg',
                        start_offset => INTERVAL '28 days',
                        end_offset => INTERVAL '1 hour',
                        schedule_interval => INTERVAL '1 hour',
                        if_not_exists => TRUE
                    );

                    -- Provide outer view matching the exact 168-hour schema expected by baseline_service.py
                    CREATE OR REPLACE VIEW node_historical_baselines AS
                    SELECT
                        endpoint_id,
                        EXTRACT(dow FROM bucket)::INTEGER AS day_of_week,
                        EXTRACT(hour FROM bucket)::INTEGER AS hour_of_day,
                        AVG(hourly_mean)::FLOAT AS historical_mean,
                        AVG(hourly_stddev)::FLOAT AS historical_stddev
                    FROM node_historical_baselines_cagg
                    GROUP BY endpoint_id, EXTRACT(dow FROM bucket), EXTRACT(hour FROM bucket);
                EXCEPTION WHEN OTHERS THEN
                    CREATE OR REPLACE VIEW node_historical_baselines AS
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
            ELSE
                CREATE OR REPLACE VIEW node_historical_baselines AS
                SELECT
                    endpoint_id,
                    EXTRACT(dow FROM start_time)::INTEGER AS day_of_week,
                    EXTRACT(hour FROM start_time)::INTEGER AS hour_of_day,
                    AVG(avg_rtt_ms)::FLOAT AS historical_mean,
                    STDDEV(avg_rtt_ms)::FLOAT AS historical_stddev
                FROM endpoint_events
                WHERE avg_rtt_ms IS NOT NULL
                GROUP BY endpoint_id, EXTRACT(dow FROM start_time), EXTRACT(hour FROM start_time);
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                BEGIN
                    PERFORM remove_continuous_aggregate_policy('node_historical_baselines_cagg', if_exists => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
                BEGIN
                    EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS node_historical_baselines_cagg CASCADE';
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
            END IF;
            BEGIN
                EXECUTE 'DROP VIEW IF EXISTS node_historical_baselines CASCADE';
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
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
        END $$;
    """)
