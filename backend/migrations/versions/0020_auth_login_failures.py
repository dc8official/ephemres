"""Failed sign-in counters shared by every API worker

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-06 12:00:00.000000

Lockouts used to live in each uvicorn worker's memory: every worker counted on its own, a
restart forgot them, and the per-worker table evicted its oldest entries at 2000 keys, so
sending a few thousand sign-ins with junk usernames released a locked account.

One row per "<client ip>:<username>" key, written with a single upsert so concurrent workers
never lose a count. Rows are transient: the PostgreSQL session store deletes those idle for
a day, and a restore leaves the table empty.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_login_failures (
            lock_key         TEXT PRIMARY KEY,
            fail_count       INTEGER NOT NULL DEFAULT 0,
            last_failure_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            locked_until     TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_auth_login_failures_last_failure_at "
        "ON auth_login_failures (last_failure_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_login_failures")
