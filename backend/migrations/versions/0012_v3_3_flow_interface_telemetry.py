"""v3.3.0 Flow Interface Telemetry and Device Role Model

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25 15:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add device_role to endpoints table
    op.add_column(
        "endpoints",
        sa.Column(
            "device_role",
            sa.String(length=30),
            server_default="'ACTIVE_HOST'",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_endpoints_device_role",
        "endpoints",
        "device_role IN ('ACTIVE_HOST', 'FLOW_EXPORTER', 'HYBRID_GATEWAY', 'L2_SWITCH')",
    )

    # 2. Create flow_interface_minute_rollups table
    op.create_table(
        "flow_interface_minute_rollups",
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exporter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interface_idx", sa.Integer(), nullable=False),
        sa.Column("in_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("out_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("in_packets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("out_packets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("flow_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint(
            "bucket", "exporter_id", "interface_idx", name="pk_flow_interface_minute_rollups"
        ),
        sa.CheckConstraint("interface_idx > 0", name="ck_flow_if_idx_positive"),
        sa.CheckConstraint("in_bytes >= 0", name="ck_flow_if_in_bytes_pos"),
        sa.CheckConstraint("out_bytes >= 0", name="ck_flow_if_out_bytes_pos"),
    )

    # 3. Add composite indexes for high-speed seeking
    op.create_index(
        "idx_flow_if_rollups_exp_bucket",
        "flow_interface_minute_rollups",
        ["exporter_id", sa.text("bucket DESC")],
    )
    op.create_index(
        "idx_flow_if_rollups_exp_if_bucket",
        "flow_interface_minute_rollups",
        ["exporter_id", "interface_idx", sa.text("bucket DESC")],
    )

    # 4. TimescaleDB hypertable and retention policy configuration with extension guards
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable(
                    'flow_interface_minute_rollups',
                    'bucket',
                    chunk_time_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'flow_interface_minute_rollups',
                    INTERVAL '7 days',
                    if_not_exists => TRUE
                );
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # 1. Clean up TimescaleDB policies safely
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM remove_retention_policy('flow_interface_minute_rollups', if_exists => TRUE);
            END IF;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
    """)

    # 2. Drop table and indexes
    op.drop_table("flow_interface_minute_rollups")

    # 3. Drop constraint and column from endpoints
    op.drop_constraint("ck_endpoints_device_role", "endpoints", type_="check")
    op.drop_column("endpoints", "device_role")
