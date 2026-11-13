"""SNMP poller, step 2: interface traffic from counters

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-13 10:00:00.000000

v3.3.6 build plan 8.3 (decided: where a device has SNMP counters, its interface figures use them;
flows stay the source for conversations and for devices without SNMP).

- snmp_interface_minute_rollups: one row per minute, device and interface, built by ephemres-snmpd
  from counter deltas. The columns match flow_interface_minute_rollups (exporter_id is the
  polled endpoint, interface_idx its ifIndex) so one query can read either
  (services/interface_traffic.py), plus errors and discards that flows cannot know. Kept 7 days,
  like the flow minute table.
- snmp_interface_hourly_rollups: the hourly continuous aggregate over it, kept 365 days, with
  each hour's busiest minute, shaped like flow_interface_hourly_rollups. Without TimescaleDB, a
  plain view.
- snmp_device_state.counters: each interface's last counter reading, so a restart of snmpd
  carries on with deltas instead of losing a poll interval per device.

The minute table is a hypertable without foreign keys, as the flow tables are; rows of a deleted
endpoint age out with retention.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
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
        SUM(in_errors)::BIGINT AS in_errors,
        SUM(out_errors)::BIGINT AS out_errors,
        SUM(in_discards)::BIGINT AS in_discards,
        SUM(out_discards)::BIGINT AS out_discards,
        MAX(in_bytes)::BIGINT AS peak_in_minute_bytes,
        MAX(out_bytes)::BIGINT AS peak_out_minute_bytes,
        COUNT(*)::INTEGER AS minutes_with_data
    FROM snmp_interface_minute_rollups
    GROUP BY {bucket}, exporter_id, interface_idx
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_interface_minute_rollups (
            bucket         TIMESTAMPTZ NOT NULL,
            exporter_id    UUID NOT NULL,
            interface_idx  INTEGER NOT NULL,
            in_bytes       BIGINT NOT NULL DEFAULT 0,
            out_bytes      BIGINT NOT NULL DEFAULT 0,
            in_packets     BIGINT NOT NULL DEFAULT 0,
            out_packets    BIGINT NOT NULL DEFAULT 0,
            in_errors      BIGINT NOT NULL DEFAULT 0,
            out_errors     BIGINT NOT NULL DEFAULT 0,
            in_discards    BIGINT NOT NULL DEFAULT 0,
            out_discards   BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT pk_snmp_interface_minute_rollups PRIMARY KEY (bucket, exporter_id, interface_idx),
            CONSTRAINT ck_snmp_if_bytes CHECK (in_bytes >= 0 AND out_bytes >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_snmp_if_rollups_exp_if_bucket "
        "ON snmp_interface_minute_rollups (exporter_id, interface_idx, bucket DESC)"
    )
    cagg_select = _HOURLY_SELECT.format(bucket="time_bucket('1 hour', bucket)")
    view_select = _HOURLY_SELECT.format(bucket="date_trunc('hour', bucket)")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('snmp_interface_minute_rollups', 'bucket',
                                          chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
                PERFORM add_retention_policy('snmp_interface_minute_rollups', INTERVAL '7 days', if_not_exists => TRUE);

                CREATE MATERIALIZED VIEW IF NOT EXISTS snmp_interface_hourly_rollups
                WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                {cagg_select}
                WITH NO DATA;
                PERFORM add_continuous_aggregate_policy(
                    'snmp_interface_hourly_rollups',
                    start_offset => INTERVAL '6 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                );
                PERFORM add_retention_policy('snmp_interface_hourly_rollups', INTERVAL '365 days', if_not_exists => TRUE);
            ELSE
                CREATE OR REPLACE VIEW snmp_interface_hourly_rollups AS
                {view_select};
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE snmp_device_state ADD COLUMN IF NOT EXISTS counters JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE snmp_device_state DROP COLUMN IF EXISTS counters")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')
               AND EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates
                           WHERE view_name = 'snmp_interface_hourly_rollups') THEN
                DROP MATERIALIZED VIEW IF EXISTS snmp_interface_hourly_rollups CASCADE;
            ELSE
                DROP VIEW IF EXISTS snmp_interface_hourly_rollups CASCADE;
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE IF EXISTS snmp_interface_minute_rollups")
