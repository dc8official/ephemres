"""When the current state began, carried across event splits

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-29 09:00:00.000000

endpoint_events keeps one row per state period, but a period is also split every 24 hours
(so updates never reach compressed chunks) and on every engine restart (the startup closes the
open events). "In state since" read the start of the latest row, so an endpoint up for weeks
showed "UP for 3 hours".

state_since holds the start of the unbroken state on a row that continues it: the engine copies
it from the previous row on a 24-hour split, and on a restart when the endpoint is still in the
same state. It stays NULL on a row that starts a new state, and on rows written before this
migration, so readers use COALESCE(state_since, start_time). Adding a nullable column without a
default only changes the catalog, also on compressed chunks.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE endpoint_events ADD COLUMN IF NOT EXISTS state_since TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE endpoint_events DROP COLUMN IF EXISTS state_since")
