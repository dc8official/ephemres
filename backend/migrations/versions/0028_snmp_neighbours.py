"""SNMP poller, step 3: neighbours, device addresses, link events handled once

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-13 14:00:00.000000

v3.3.6 build plan 8.3 (decided: LLDP/CDP places switches traceroute cannot place, and RCA uses
it; a WAN link going down or up alerts; a link going down re-traces the paths through it).

- snmp_neighbours: what each polled device reports over LLDP, LLDP-V2 or CDP, one row per
  neighbour per protocol (`nkey` identifies it across reads, so first_seen survives). The
  neighbour is matched to a monitored endpoint by management address, then chassis MAC, then
  system name (matched_endpoint_id, matched_by). Transient: re-read within 15 minutes.
- snmp_device_state.addresses / sys_name: the device's own IPv4 addresses (ipAddrTable) and
  sysName, so a neighbour reporting another of its addresses, or only its name, still matches,
  and route re-traces recognise a router that answers traceroute from another interface.
- snmp_interface_events.handled_at: set by the alert leader when it has acted on an up/down
  event (alert, re-trace). Claimed with SKIP LOCKED, so each event is acted on once even across
  a change of leader.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_neighbours (
            id                   BIGSERIAL PRIMARY KEY,
            endpoint_id          UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            nkey                 VARCHAR(600) NOT NULL,
            protocol             VARCHAR(8) NOT NULL,
            local_if_index       INTEGER,
            local_port           VARCHAR(255),
            remote_name          VARCHAR(255),
            remote_chassis       VARCHAR(255),
            remote_port          VARCHAR(255),
            remote_port_desc     VARCHAR(255),
            remote_platform      VARCHAR(255),
            remote_addresses     TEXT[] NOT NULL DEFAULT '{}',
            matched_endpoint_id  UUID REFERENCES endpoints(id) ON DELETE SET NULL,
            matched_by           VARCHAR(16),
            first_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_snmp_neighbours_key UNIQUE (endpoint_id, nkey),
            CONSTRAINT ck_snmp_neighbours_protocol CHECK (protocol IN ('lldp', 'lldp-v2', 'cdp'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_snmp_neighbours_matched ON snmp_neighbours (matched_endpoint_id)")
    op.execute("ALTER TABLE snmp_device_state ADD COLUMN IF NOT EXISTS addresses TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE snmp_device_state ADD COLUMN IF NOT EXISTS sys_name VARCHAR(255)")
    op.execute("ALTER TABLE snmp_interface_events ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_snmp_if_events_unhandled ON snmp_interface_events (at) "
        "WHERE handled_at IS NULL AND kind IN ('up', 'down')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_snmp_if_events_unhandled")
    op.execute("ALTER TABLE snmp_interface_events DROP COLUMN IF EXISTS handled_at")
    op.execute("ALTER TABLE snmp_device_state DROP COLUMN IF EXISTS sys_name")
    op.execute("ALTER TABLE snmp_device_state DROP COLUMN IF EXISTS addresses")
    op.execute("DROP TABLE IF EXISTS snmp_neighbours")
