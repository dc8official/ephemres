"""add_diagnostic_and_topology

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27 16:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add columns to endpoints table
    op.add_column(
        "endpoints",
        sa.Column(
            "allow_incident_trace",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.add_column(
        "endpoints",
        sa.Column(
            "allow_topology_discovery",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.add_column(
        "endpoints",
        sa.Column(
            "manual_parent_id",
            postgresql.UUID(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_endpoints_manual_parent_id",
        "endpoints",
        "endpoints",
        ["manual_parent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 2. Create endpoint_diagnostic_traces table
    op.create_table(
        "endpoint_diagnostic_traces",
        sa.Column(
            "id",
            postgresql.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "trigger_reason",
            sa.VARCHAR(50),
            nullable=False,
        ),
        sa.Column(
            "trace_data",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "idx_diagnostic_traces_endpoint",
        "endpoint_diagnostic_traces",
        ["endpoint_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("idx_diagnostic_traces_endpoint", table_name="endpoint_diagnostic_traces")
    op.drop_table("endpoint_diagnostic_traces")
    op.drop_constraint("fk_endpoints_manual_parent_id", "endpoints", type_="foreignkey")
    op.drop_column("endpoints", "manual_parent_id")
    op.drop_column("endpoints", "allow_topology_discovery")
    op.drop_column("endpoints", "allow_incident_trace")
