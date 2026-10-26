"""Flow path observations and route changes

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-26 18:00:00.000000

Traceroute stays the route of record. Flows add the path real traffic takes through each
exporting router (v3.4 Phase 4, NetFlow route augmentation):

- flow_path_observations: one row per forwarding choice an exporter made for traffic to or
  from a monitored endpoint (interfaces and next hop), with first/last seen and running totals.
  Small (exporters x endpoints x a few paths), so a plain table; ephemres-flowd prunes rows
  not seen for 30 days.
- flow_route_changes: one row per change of the dominant path detected by ephemres-flowd;
  pruned after 90 days.
- endpoint_rca_incidents.flow_evidence: what the flow data showed when the RCA ran.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS flow_path_observations (
            exporter_id UUID NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
            endpoint_id UUID NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
            direction VARCHAR(4) NOT NULL,
            in_if INTEGER NOT NULL DEFAULT 0,
            out_if INTEGER NOT NULL DEFAULT 0,
            next_hop INET,
            first_seen TIMESTAMPTZ NOT NULL,
            last_seen TIMESTAMPTZ NOT NULL,
            bytes BIGINT NOT NULL DEFAULT 0,
            flow_count BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT ck_flow_path_direction CHECK (direction IN ('to', 'from'))
        );
    """)
    # next_hop is nullable, so the upsert key uses an expression index (works before PG 15's
    # NULLS NOT DISTINCT).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_flow_path_observations
        ON flow_path_observations (
            exporter_id, endpoint_id, direction, in_if, out_if,
            (COALESCE(next_hop, '0.0.0.0'::inet))
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_flow_path_observations_endpoint
        ON flow_path_observations (endpoint_id, last_seen DESC);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS flow_route_changes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            exporter_id UUID NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
            endpoint_id UUID NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
            direction VARCHAR(4) NOT NULL,
            old_out_if INTEGER,
            old_next_hop INET,
            new_out_if INTEGER,
            new_next_hop INET,
            CONSTRAINT ck_flow_route_change_direction CHECK (direction IN ('to', 'from'))
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_flow_route_changes_endpoint
        ON flow_route_changes (endpoint_id, detected_at DESC);
    """)

    op.execute("ALTER TABLE endpoint_rca_incidents ADD COLUMN IF NOT EXISTS flow_evidence JSONB;")


def downgrade() -> None:
    op.execute("ALTER TABLE endpoint_rca_incidents DROP COLUMN IF EXISTS flow_evidence;")
    op.execute("DROP TABLE IF EXISTS flow_route_changes CASCADE;")
    op.execute("DROP TABLE IF EXISTS flow_path_observations CASCADE;")
