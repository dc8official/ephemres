"""Traffic alert state per WAN link

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-12 18:00:00.000000

Traffic alerts (v3.3.6 build plan 7.4, designed with Kenneth): a WAN link with a speed alerts
when its in or out utilisation stays above a threshold for N minutes, and again when it is back
to normal; a separate alert when it goes silent (no flow data for 15 minutes) and when traffic
returns. One row per link and kind ("high_in", "high_out", "silent") remembers whether an alert
is open, since when, and the peak so far, so a change of alert-dispatcher leader neither repeats
nor loses an alert in progress. Rows go with their exporter. Not backed up: after a restore the
first minutes re-establish it.

The settings (on/off, default threshold and minutes) are app_settings keys and need no schema;
a link's own threshold or opt-out lives in its flow_interface_aliases entry ("alert").
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_alert_state (
            exporter_id    UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            interface_idx  INTEGER NOT NULL,
            kind           VARCHAR(12) NOT NULL,
            active         BOOLEAN NOT NULL DEFAULT FALSE,
            since          TIMESTAMPTZ,
            peak_pct       REAL,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (exporter_id, interface_idx, kind),
            CONSTRAINT ck_traffic_alert_kind CHECK (kind IN ('high_in', 'high_out', 'silent'))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS traffic_alert_state")
