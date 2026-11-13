"""Normal and current route per endpoint, and a route-change log

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-12 12:00:00.000000

Until now each endpoint had one baseline route, overwritten by every traceroute (nightly, on
recovery, by a flow route hint, by hand). A failover path then became the "baseline" silently,
and root-cause analysis could compare a later failure with the wrong path. Decided with Kenneth
on 2026-11-12 (v3.3.6 build plan, section 6.4):

endpoint_baseline_routes keeps its columns and their meaning of "the path in use now":
- hops / total_hops: the CURRENT path, what root-cause analysis compares a failure with.
- current_since: when the current path was confirmed.
New columns:
- normal_hops / normal_since / normal_pinned: the NORMAL path. It becomes the current path
  after 24 hours as current, unless an admin pinned it. Backfilled from the current path, so
  every endpoint starts "on its normal path".
- pending_hops / pending_at: a different path seen once, waiting for a second trace at least
  2 minutes later to confirm it (a flapping link does not rewrite anything on one trace).
- recent_changes: when the current path changed in the last hour; 4 or more marks it flapping.
- flapping_since / last_logged_at: flapping state, and the 15-minute limit on log entries.

endpoint_route_changes: one row per logged event (path changed, back on the normal path, became
normal, pinned, flapping), with the paths before and after and the first hop that differs.
Pruned after 90 days. Backed up and restored with the other route tables.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE endpoint_baseline_routes
            ADD COLUMN IF NOT EXISTS current_since TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD COLUMN IF NOT EXISTS normal_hops JSONB,
            ADD COLUMN IF NOT EXISTS normal_since TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS normal_pinned BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS pending_hops JSONB,
            ADD COLUMN IF NOT EXISTS pending_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS recent_changes TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS flapping_since TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_logged_at TIMESTAMPTZ
        """
    )
    # Every existing path is both current and normal on the day of the upgrade.
    op.execute(
        """
        UPDATE endpoint_baseline_routes
        SET current_since = updated_at, normal_hops = hops, normal_since = updated_at
        WHERE normal_hops IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS endpoint_route_changes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            endpoint_id     UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            kind            VARCHAR(24) NOT NULL,
            trigger         VARCHAR(24),
            from_hops       JSONB,
            to_hops         JSONB,
            first_diff_hop  INTEGER,
            detail          TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_endpoint_route_changes_endpoint_time "
        "ON endpoint_route_changes (endpoint_id, changed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS endpoint_route_changes")
    op.execute(
        """
        ALTER TABLE endpoint_baseline_routes
            DROP COLUMN IF EXISTS last_logged_at,
            DROP COLUMN IF EXISTS flapping_since,
            DROP COLUMN IF EXISTS recent_changes,
            DROP COLUMN IF EXISTS pending_at,
            DROP COLUMN IF EXISTS pending_hops,
            DROP COLUMN IF EXISTS normal_pinned,
            DROP COLUMN IF EXISTS normal_since,
            DROP COLUMN IF EXISTS normal_hops,
            DROP COLUMN IF EXISTS current_since
        """
    )
