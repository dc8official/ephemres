"""SNMP poller, step 1: credential sets, device state, interface inventory

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-12 20:00:00.000000

v3.3.6 build plan 8.3, decided with Kenneth 2026-11-12. ephemres-snmpd polls endpoints that have
a credential set; everything it learns lands here.

- snmp_credentials: shared, named sets ("Branch v3", "Meraki"). Non-secret settings are plain
  columns; the community or v3 passwords are one encrypted JSON value in `secrets` (the
  ENC:v1: envelope alert channels use), so restore re-encrypts them the same way. Backed up.
- endpoints.snmp_credential_id: the set an endpoint is polled with; NULL means no SNMP. Deleting a
  set turns SNMP off for its endpoints rather than failing.
- snmp_device_state: one row per polled endpoint: status, last error, clocks used to spot a
  reboot, the learned device profile (monitoring/snmp/profile.py) and a probe request flag the
  API sets for the daemon. Transient: re-learned after a restore.
- snmp_interfaces: the device's interfaces as SNMP reports them (names, speed, status). Manual
  names and speeds in endpoints.flow_interface_aliases still win. Transient.
- snmp_interface_events: interface up/down, added/removed and renumbered (ifIndex moved after a
  reboot), for the endpoint page. Kept 90 days. Transient.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_credentials (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name           VARCHAR(64) NOT NULL UNIQUE,
            version        VARCHAR(2) NOT NULL,
            username       VARCHAR(64),
            auth_protocol  VARCHAR(8),
            priv_protocol  VARCHAR(8),
            priv_same      BOOLEAN NOT NULL DEFAULT FALSE,
            context        VARCHAR(64) NOT NULL DEFAULT '',
            port           INTEGER NOT NULL DEFAULT 161,
            timeout_ms     INTEGER NOT NULL DEFAULT 2000,
            retries        INTEGER NOT NULL DEFAULT 1,
            secrets        TEXT NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_snmp_cred_version CHECK (version IN ('2c', '3')),
            CONSTRAINT ck_snmp_cred_port CHECK (port BETWEEN 1 AND 65535),
            CONSTRAINT ck_snmp_cred_timeout CHECK (timeout_ms BETWEEN 500 AND 10000),
            CONSTRAINT ck_snmp_cred_retries CHECK (retries BETWEEN 0 AND 3)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS snmp_credential_id UUID
            REFERENCES snmp_credentials(id) ON DELETE SET NULL
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_endpoints_snmp_credential_id ON endpoints (snmp_credential_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_device_state (
            endpoint_id           UUID PRIMARY KEY REFERENCES endpoints(id) ON DELETE CASCADE,
            status                VARCHAR(16) NOT NULL DEFAULT 'pending',
            last_error            TEXT,
            last_poll_at          TIMESTAMPTZ,
            last_ok_at            TIMESTAMPTZ,
            last_probe_at         TIMESTAMPTZ,
            probe_requested_at    TIMESTAMPTZ,
            last_reboot_at        TIMESTAMPTZ,
            sys_uptime            BIGINT,
            engine_boots          INTEGER,
            if_table_last_change  BIGINT,
            inventory_at          TIMESTAMPTZ,
            profile               JSONB,
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_snmp_state_status CHECK (status IN ('pending', 'ok', 'partial', 'no_reply', 'auth_failed', 'error'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_interfaces (
            endpoint_id   UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            if_index      INTEGER NOT NULL,
            if_name       VARCHAR(255),
            if_descr      VARCHAR(255),
            if_alias      VARCHAR(255),
            if_type       INTEGER,
            speed_mbps    BIGINT,
            mac           VARCHAR(64),
            admin_status  VARCHAR(16),
            oper_status   VARCHAR(16),
            last_change   BIGINT,
            hc_counters   BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (endpoint_id, if_index)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS snmp_interface_events (
            id           BIGSERIAL PRIMARY KEY,
            endpoint_id  UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            if_index     INTEGER,
            if_name      VARCHAR(255),
            at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            kind         VARCHAR(16) NOT NULL,
            detail       TEXT,
            CONSTRAINT ck_snmp_if_event_kind CHECK (kind IN ('up', 'down', 'added', 'removed', 'renumbered', 'reboot'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_snmp_if_events_endpoint_at ON snmp_interface_events (endpoint_id, at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS snmp_interface_events")
    op.execute("DROP TABLE IF EXISTS snmp_interfaces")
    op.execute("DROP TABLE IF EXISTS snmp_device_state")
    op.execute("DROP INDEX IF EXISTS ix_endpoints_snmp_credential_id")
    op.execute("ALTER TABLE endpoints DROP COLUMN IF EXISTS snmp_credential_id")
    op.execute("DROP TABLE IF EXISTS snmp_credentials")
