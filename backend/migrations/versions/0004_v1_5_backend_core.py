"""v1_5_backend_core

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add columns to endpoints table
    op.add_column(
        "endpoints",
        sa.Column(
            "enable_rca",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.add_column(
        "endpoints",
        sa.Column(
            "enable_scheduled_discovery",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.add_column(
        "endpoints",
        sa.Column(
            "is_l2_segment",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )

    # 2. Create endpoint_baseline_routes table
    op.create_table(
        "endpoint_baseline_routes",
        sa.Column(
            "endpoint_id",
            postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "total_hops",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "hops",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("endpoint_id"),
    )

    # 3. Create endpoint_rca_incidents table
    op.create_table(
        "endpoint_rca_incidents",
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
            "incident_timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "status_at_execution",
            sa.VARCHAR(16),
            nullable=False,
        ),
        sa.Column(
            "failed_hop_number",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "failed_hop_ip",
            sa.VARCHAR(45),
            nullable=True,
        ),
        sa.Column(
            "last_known_good_hop_ip",
            sa.VARCHAR(45),
            nullable=True,
        ),
        sa.Column(
            "rca_summary",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "baseline_snapshot",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "failure_trace_snapshot",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "is_resolved",
            sa.Boolean(),
            server_default="false",
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
        "idx_endpoint_rca_incidents_endpoint_id",
        "endpoint_rca_incidents",
        ["endpoint_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_endpoint_rca_incidents_endpoint_id",
        table_name="endpoint_rca_incidents",
    )
    op.drop_table("endpoint_rca_incidents")
    op.drop_table("endpoint_baseline_routes")
    op.drop_column("endpoints", "is_l2_segment")
    op.drop_column("endpoints", "enable_scheduled_discovery")
    op.drop_column("endpoints", "enable_rca")
