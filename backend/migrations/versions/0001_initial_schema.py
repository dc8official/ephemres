"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa
from app.core.orm import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # roles
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("role_name", sa.VARCHAR(30), nullable=False),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_name"),
    )

    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("username", sa.VARCHAR(50), nullable=False),
        sa.Column("password_hash", sa.TEXT(), nullable=False),
        sa.Column("role_id", postgresql.UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(),
                  server_default="true", nullable=False),
        sa.Column("must_change_password", sa.Boolean(),
                  server_default="true", nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    # endpoints
    op.create_table(
        "endpoints",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=False),
        sa.Column("hostname", sa.VARCHAR(255), nullable=False),
        sa.Column("device_type", sa.VARCHAR(100), nullable=False),
        sa.Column("location", sa.VARCHAR(255), nullable=True),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("monitoring_enabled", sa.Boolean(),
                  server_default="true", nullable=False),
        sa.Column("endpoint_status", sa.VARCHAR(20), nullable=False),
        sa.Column("created_by", postgresql.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.CheckConstraint(
            "endpoint_status IN ('ACTIVE', 'DISABLED', 'DELETED')",
            name="ck_endpoints_status",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_address"),
    )

    # endpoint_events
    op.create_table(
        "endpoint_events",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(), nullable=False),
        sa.Column("operational_state", sa.VARCHAR(20), nullable=False),
        sa.Column("detailed_state", sa.VARCHAR(30), nullable=False),
        sa.Column("success_count", sa.SmallInteger(), nullable=False),
        sa.Column("failed_count", sa.SmallInteger(), nullable=False),
        sa.Column("health_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("avg_rtt_ms", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_split_event", sa.Boolean(),
                  server_default="false", nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("duration_seconds", sa.BigInteger(), nullable=True),
        sa.Column("monitoring_cycle_count", sa.Integer(),
                  server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint(
            "operational_state IN ('UP', 'DOWN')",
            name="ck_events_operational_state",
        ),
        sa.CheckConstraint(
            "detailed_state IN "
            "('UP', 'UP-UNSTABLE', 'DOWN-UNSTABLE', 'DOWN')",
            name="ck_events_detailed_state",
        ),
        sa.CheckConstraint(
            "success_count >= 0 AND success_count <= 10",
            name="ck_events_success_count",
        ),
        sa.CheckConstraint(
            "failed_count >= 0 AND failed_count <= 10",
            name="ck_events_failed_count",
        ),
        sa.CheckConstraint(
            "health_score >= 0 AND health_score <= 100",
            name="ck_events_health_score",
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"]),
        sa.PrimaryKeyConstraint("id", "start_time"),
    )

    # Convert endpoint_events to TimescaleDB hypertable
    op.execute(
        "SELECT create_hypertable("
        "'endpoint_events', 'start_time', "
        "if_not_exists => TRUE);"
    )

    # Performance index
    op.create_index(
        "idx_endpoint_events_incident_query",
        "endpoint_events",
        ["endpoint_id", "operational_state", "start_time"],
    )

    # monitoring_service_events
    op.create_table(
        "monitoring_service_events",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("event_type", sa.VARCHAR(50), nullable=False),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("duration_seconds", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ("
            "'SERVICE_RESTART', 'DATABASE_DISCONNECT', "
            "'SERVER_REBOOT', 'MONITORING_UNAVAILABLE')",
            name="ck_monitoring_events_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=True),
        sa.Column("action", sa.VARCHAR(100), nullable=False),
        sa.Column("target_type", sa.VARCHAR(50), nullable=False),
        sa.Column("target_id", postgresql.UUID(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # app_settings
    op.create_table(
        "app_settings",
        sa.Column("id", postgresql.UUID(),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("setting_key", sa.VARCHAR(100), nullable=False),
        sa.Column("setting_value", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setting_key"),
    )

    # Seed roles
    op.execute("""
        INSERT INTO roles (role_name, description)
        VALUES
            ('ADMIN', 'Full administrative access'),
            ('VIEWER', 'Read-only access to reports and dashboards')
    """)

    # Seed app_settings
    op.execute("""
        INSERT INTO app_settings (setting_key, setting_value)
        VALUES
            ('state_confirmation_cycles', '3'),
            ('retention_days', '730'),
            ('session_timeout_minutes', '30'),
            ('monitoring_interval_seconds', '60'),
            ('ping_count_per_cycle', '10'),
            ('ping_interval_seconds', '6'),
            ('log_level', 'INFO')
    """)


def downgrade() -> None:
    op.execute("DELETE FROM app_settings")
    op.execute("DELETE FROM roles")
    op.drop_index("idx_endpoint_events_incident_query",
                  table_name="endpoint_events")
    op.drop_table("audit_logs")
    op.drop_table("app_settings")
    op.drop_table("monitoring_service_events")
    op.drop_table("endpoint_events")
    op.drop_table("endpoints")
    op.drop_table("users")
    op.drop_table("roles")
