"""v3_2_network_flow_ingestion

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Extend endpoints table with multi-IP aliases and interface aliases
    op.add_column(
        "endpoints",
        sa.Column(
            "flow_exporter_ips",
            postgresql.ARRAY(postgresql.INET()),
            server_default=sa.text("'{}'::inet[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "endpoints",
        sa.Column(
            "flow_interface_aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # GIN index for fast IP membership queries in flow_exporter_ips
    op.create_index(
        "idx_endpoints_flow_exporter_ips",
        "endpoints",
        ["flow_exporter_ips"],
        postgresql_using="gin",
    )

    # 2. Create flow_minute_rollups table
    op.create_table(
        "flow_minute_rollups",
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "exporter_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("'00000000-0000-0000-0000-000000000000'::uuid"),
            nullable=False,
        ),
        sa.Column("src_endpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dst_endpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("src_ip", postgresql.INET(), nullable=False),
        sa.Column("dst_ip", postgresql.INET(), nullable=False),
        sa.Column("protocol", sa.SmallInteger(), nullable=False),
        sa.Column("dst_port", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("packets", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("flow_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint(
            "bucket", "exporter_id", "src_ip", "dst_ip", "protocol", "dst_port"
        ),
    )

    # Indexes on flow_minute_rollups
    op.create_index(
        "idx_flow_minute_src_dst_endpoint",
        "flow_minute_rollups",
        ["bucket", "src_endpoint_id", "dst_endpoint_id"],
    )
    op.create_index(
        "idx_flow_minute_exporter_bucket",
        "flow_minute_rollups",
        ["exporter_id", "bucket"],
    )

    # 3. Configure TimescaleDB Hypertable & Policies
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            -- Convert to hypertable with 1-day chunk interval
            BEGIN
                PERFORM create_hypertable(
                    'flow_minute_rollups',
                    'bucket',
                    chunk_time_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            -- Compression policy: older than 1 day
            BEGIN
                ALTER TABLE flow_minute_rollups SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'exporter_id, protocol, dst_port',
                    timescaledb.compress_orderby = 'bucket DESC'
                );
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            BEGIN
                PERFORM add_compression_policy('flow_minute_rollups', INTERVAL '1 day', if_not_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            -- Retention policy: 7 days
            BEGIN
                PERFORM add_retention_policy('flow_minute_rollups', INTERVAL '7 days', if_not_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            -- Continuous Aggregate: flow_hourly_rollups
            BEGIN
                CREATE MATERIALIZED VIEW flow_hourly_rollups
                WITH (timescaledb.continuous) AS
                SELECT
                    time_bucket('1 hour', bucket) AS bucket,
                    exporter_id,
                    src_endpoint_id,
                    dst_endpoint_id,
                    protocol,
                    dst_port,
                    SUM(bytes)::BIGINT AS bytes,
                    SUM(packets)::BIGINT AS packets,
                    SUM(flow_count)::INTEGER AS flow_count
                FROM flow_minute_rollups
                GROUP BY time_bucket('1 hour', bucket), exporter_id, src_endpoint_id, dst_endpoint_id, protocol, dst_port
                WITH NO DATA;

                PERFORM add_continuous_aggregate_policy(
                    'flow_hourly_rollups',
                    start_offset => INTERVAL '3 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );

                PERFORM add_retention_policy('flow_hourly_rollups', INTERVAL '30 days', if_not_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                CREATE VIEW flow_hourly_rollups AS
                SELECT
                    date_trunc('hour', bucket) AS bucket,
                    exporter_id,
                    src_endpoint_id,
                    dst_endpoint_id,
                    protocol,
                    dst_port,
                    SUM(bytes)::BIGINT AS bytes,
                    SUM(packets)::BIGINT AS packets,
                    SUM(flow_count)::INTEGER AS flow_count
                FROM flow_minute_rollups
                GROUP BY date_trunc('hour', bucket), exporter_id, src_endpoint_id, dst_endpoint_id, protocol, dst_port;
            END;

            -- Continuous Aggregate: flow_daily_rollups
            BEGIN
                CREATE MATERIALIZED VIEW flow_daily_rollups
                WITH (timescaledb.continuous) AS
                SELECT
                    time_bucket('1 day', bucket) AS bucket,
                    exporter_id,
                    protocol,
                    dst_port,
                    SUM(bytes)::BIGINT AS bytes,
                    SUM(packets)::BIGINT AS packets,
                    SUM(flow_count)::INTEGER AS flow_count
                FROM flow_minute_rollups
                GROUP BY time_bucket('1 day', bucket), exporter_id, protocol, dst_port
                WITH NO DATA;

                PERFORM add_continuous_aggregate_policy(
                    'flow_daily_rollups',
                    start_offset => INTERVAL '7 days',
                    end_offset => INTERVAL '1 day',
                    schedule_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );

                PERFORM add_retention_policy('flow_daily_rollups', INTERVAL '365 days', if_not_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                CREATE VIEW flow_daily_rollups AS
                SELECT
                    date_trunc('day', bucket) AS bucket,
                    exporter_id,
                    protocol,
                    dst_port,
                    SUM(bytes)::BIGINT AS bytes,
                    SUM(packets)::BIGINT AS packets,
                    SUM(flow_count)::INTEGER AS flow_count
                FROM flow_minute_rollups
                GROUP BY date_trunc('day', bucket), exporter_id, protocol, dst_port;
            END;

        ELSE
            -- Plain PostgreSQL fallbacks (when TimescaleDB extension is not active)
            CREATE VIEW flow_hourly_rollups AS
            SELECT
                date_trunc('hour', bucket) AS bucket,
                exporter_id,
                src_endpoint_id,
                dst_endpoint_id,
                protocol,
                dst_port,
                SUM(bytes)::BIGINT AS bytes,
                SUM(packets)::BIGINT AS packets,
                SUM(flow_count)::INTEGER AS flow_count
            FROM flow_minute_rollups
            GROUP BY date_trunc('hour', bucket), exporter_id, src_endpoint_id, dst_endpoint_id, protocol, dst_port;

            CREATE VIEW flow_daily_rollups AS
            SELECT
                date_trunc('day', bucket) AS bucket,
                exporter_id,
                protocol,
                dst_port,
                SUM(bytes)::BIGINT AS bytes,
                SUM(packets)::BIGINT AS packets,
                SUM(flow_count)::INTEGER AS flow_count
            FROM flow_minute_rollups
            GROUP BY date_trunc('day', bucket), exporter_id, protocol, dst_port;
        END IF;
    END $$;
    """)

    # 4. Inherited Baseline Repair from 0002
    op.execute("""
    DO $$
    BEGIN
        -- Ensure node_historical_baselines exists as a valid view or continuous aggregate
        IF NOT EXISTS (
            SELECT 1 FROM pg_views WHERE viewname = 'node_historical_baselines'
        ) AND NOT EXISTS (
            SELECT 1 FROM pg_matviews WHERE matviewname = 'node_historical_baselines'
        ) THEN
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
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            BEGIN
                PERFORM remove_retention_policy('flow_daily_rollups', if_exists => TRUE);
                PERFORM remove_continuous_aggregate_policy('flow_daily_rollups', if_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            BEGIN
                PERFORM remove_retention_policy('flow_hourly_rollups', if_exists => TRUE);
                PERFORM remove_continuous_aggregate_policy('flow_hourly_rollups', if_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;

            BEGIN
                PERFORM remove_retention_policy('flow_minute_rollups', if_exists => TRUE);
                PERFORM remove_compression_policy('flow_minute_rollups', if_exists => TRUE);
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END IF;

        BEGIN
            EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS flow_daily_rollups CASCADE';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        BEGIN
            EXECUTE 'DROP VIEW IF EXISTS flow_daily_rollups CASCADE';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;

        BEGIN
            EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS flow_hourly_rollups CASCADE';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        BEGIN
            EXECUTE 'DROP VIEW IF EXISTS flow_hourly_rollups CASCADE';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END $$;
    """)

    op.drop_index("idx_flow_minute_exporter_bucket", table_name="flow_minute_rollups")
    op.drop_index("idx_flow_minute_src_dst_endpoint", table_name="flow_minute_rollups")
    op.drop_table("flow_minute_rollups")

    op.drop_index("idx_endpoints_flow_exporter_ips", table_name="endpoints")
    op.drop_column("endpoints", "flow_interface_aliases")
    op.drop_column("endpoints", "flow_exporter_ips")
