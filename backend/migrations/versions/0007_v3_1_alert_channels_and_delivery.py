"""v3_1_alert_channels_and_delivery

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_channels",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("name", sa.VARCHAR(100), nullable=False),
        sa.Column("channel_type", sa.VARCHAR(50), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column(
            "endpoint_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "subnet_filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "severity_filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"DOWN\", \"RECOVERED\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "alert_delivery_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alert_channels.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("channel_name", sa.VARCHAR(100), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("endpoint_name", sa.VARCHAR(100), nullable=False),
        sa.Column("event_type", sa.VARCHAR(50), nullable=False),
        sa.Column("status", sa.VARCHAR(20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            index=True,
        ),
    )

    op.create_index(
        "idx_alert_channels_is_enabled",
        "alert_channels",
        ["is_enabled"],
    )
    op.create_index(
        "idx_endpoint_events_start_time_id",
        "endpoint_events",
        ["start_time", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_endpoint_events_start_time_id", table_name="endpoint_events")
    op.drop_index("idx_alert_channels_is_enabled", table_name="alert_channels")
    op.drop_table("alert_delivery_logs")
    op.drop_table("alert_channels")
