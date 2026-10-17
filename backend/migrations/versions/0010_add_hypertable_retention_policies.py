"""add_hypertable_retention_policies

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                -- Configure retention policy on endpoint_events (730 days / 2 years)
                BEGIN
                    PERFORM add_retention_policy('endpoint_events', INTERVAL '730 days', if_not_exists => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;

                -- Ensure retention policy on flow_minute_rollups (90 days)
                BEGIN
                    PERFORM remove_retention_policy('flow_minute_rollups', if_exists => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
                BEGIN
                    PERFORM add_retention_policy('flow_minute_rollups', INTERVAL '90 days', if_not_exists => TRUE);
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
                    PERFORM remove_retention_policy('endpoint_events', if_exists => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
                BEGIN
                    PERFORM remove_retention_policy('flow_minute_rollups', if_exists => TRUE);
                    PERFORM add_retention_policy('flow_minute_rollups', INTERVAL '7 days', if_not_exists => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
            END IF;
        END $$;
    """)
