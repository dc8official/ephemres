"""Add flow TCP flags and ToS metadata

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add tcp_flags and tos columns to flow_minute_rollups
    op.execute("""
        ALTER TABLE flow_minute_rollups
        ADD COLUMN IF NOT EXISTS tcp_flags SMALLINT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS tos SMALLINT NOT NULL DEFAULT 0;
    """)

    # 2. Add columns or update hourly / daily continuous aggregates or tables safely if they exist
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE flow_hourly_rollups ADD COLUMN IF NOT EXISTS tcp_flags SMALLINT NOT NULL DEFAULT 0;
            ALTER TABLE flow_hourly_rollups ADD COLUMN IF NOT EXISTS tos SMALLINT NOT NULL DEFAULT 0;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE flow_daily_rollups ADD COLUMN IF NOT EXISTS tcp_flags SMALLINT NOT NULL DEFAULT 0;
            ALTER TABLE flow_daily_rollups ADD COLUMN IF NOT EXISTS tos SMALLINT NOT NULL DEFAULT 0;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE flow_daily_rollups DROP COLUMN IF EXISTS tcp_flags;
            ALTER TABLE flow_daily_rollups DROP COLUMN IF EXISTS tos;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE flow_hourly_rollups DROP COLUMN IF EXISTS tcp_flags;
            ALTER TABLE flow_hourly_rollups DROP COLUMN IF EXISTS tos;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE flow_minute_rollups DROP COLUMN IF EXISTS tcp_flags;
            ALTER TABLE flow_minute_rollups DROP COLUMN IF EXISTS tos;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
    """)
