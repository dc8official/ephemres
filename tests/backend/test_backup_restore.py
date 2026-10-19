from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List
import re
import uuid
import asyncpg
import pytest

from app.config import settings
from app.services.auth_service import verify_password
from app.core.crypto import (
    _SALT,
    _INFO,
    _LEGACY_SALT,
    _LEGACY_INFO,
    _derive_key,
    _aes256_gcm_encrypt,
    decrypt_secret,
    GCM_IV_LEN,
)
from app.core.restore import (
    get_target_table_columns,
    scan_backup_metadata_and_configs,
    reconcile_and_restore_database,
)
from app.seed_admin import seed_admin


class MockDbConnection:
    """Mock asyncpg connection recording all executed queries and parameter bindings."""

    def __init__(self, target_schema: Dict[str, Dict[str, str]]) -> None:
        self.target_schema = target_schema
        self.executed_queries: List[str] = []
        self.executed_params: List[Any] = []
        self.batch_executions: List[tuple[str, List[List[Any]]]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {t: [] for t in target_schema}

        # Seed pre-existing roles in target DB
        self.existing_roles = [
            {"id": "00000000-0000-0000-0000-000000000001", "role_name": "ADMIN"},
            {"id": "00000000-0000-0000-0000-000000000002", "role_name": "OPERATOR"},
        ]
        # Seed pre-existing users
        self.existing_users = [
            {"id": "00000000-0000-0000-0000-000000000010", "username": "admin"},
        ]
        self.existing_endpoints: List[Dict[str, Any]] = []

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        self.executed_queries.append(query)
        if "information_schema.columns" in query:
            rows: List[Dict[str, Any]] = []
            for t_name, cols in self.target_schema.items():
                for c_name, udt in cols.items():
                    rows.append({"table_name": t_name, "column_name": c_name, "data_type": udt, "udt_name": udt})
            return rows
        elif "FROM roles" in query:
            return self.existing_roles
        elif "FROM users" in query:
            return self.existing_users
        elif "FROM endpoints" in query:
            return self.existing_endpoints
        return []

    def _validate_query_syntax(self, query: str) -> None:
        # Parse DO UPDATE SET clauses to detect invalid duplicate column assignments
        for match in re.finditer(r"\bDO\s+UPDATE\s+SET\s+(.*?)(?:\s+WHERE\b|;|\Z)", query, re.IGNORECASE | re.DOTALL):
            set_str = match.group(1).strip()
            parts: List[str] = []
            curr: List[str] = []
            depth = 0
            in_single = False
            in_double = False
            for char in set_str:
                if char == "'" and not in_double:
                    in_single = not in_single
                    curr.append(char)
                elif char == '"' and not in_single:
                    in_double = not in_double
                    curr.append(char)
                elif char == "(" and not in_single and not in_double:
                    depth += 1
                    curr.append(char)
                elif char == ")" and not in_single and not in_double:
                    depth -= 1
                    curr.append(char)
                elif char == "," and depth == 0 and not in_single and not in_double:
                    parts.append("".join(curr).strip())
                    curr = []
                else:
                    curr.append(char)
            if curr:
                parts.append("".join(curr).strip())

            seen_cols: set[str] = set()
            for part in parts:
                if "=" in part:
                    left = part.split("=", 1)[0].strip()
                    if left.startswith("(") and left.endswith(")"):
                        raw_cols = [c.strip().strip('"').lower() for c in left[1:-1].split(",") if c.strip()]
                    else:
                        raw_cols = [left.strip('"').lower()]
                    for col in raw_cols:
                        if col in seen_cols:
                            raise asyncpg.exceptions.PostgresSyntaxError(
                                f'multiple assignments to same column "{col}"'
                            )
                        seen_cols.add(col)

    async def execute(self, query: str, *args: Any) -> Any:
        self.executed_queries.append(query)
        self.executed_params.append(args)

        self._validate_query_syntax(query)

        # Parse table name from INSERT
        if "INSERT INTO" in query:
            for t in self.target_schema:
                if f'"{t}"' in query or f" {t} " in query or f" {t}(" in query:
                    # Extract column names from query
                    m_cols = query.split("(")[1].split(")")[0]
                    cols = [c.strip().strip('"') for c in m_cols.split(",")]
                    row = {c: val for c, val in zip(cols, args)}
                    self.tables[t].append(row)
                    break
        return "EXECUTE 1"

    async def executemany(self, query: str, args_list: List[List[Any]]) -> None:
        self.executed_queries.append(query)
        self.batch_executions.append((query, args_list))
        self._validate_query_syntax(query)
        if "INSERT INTO" in query:
            for t in self.target_schema:
                if f'"{t}"' in query or f" {t} " in query or f" {t}(" in query:
                    m_cols = query.split("(")[1].split(")")[0]
                    cols = [c.strip().strip('"') for c in m_cols.split(",")]
                    for args in args_list:
                        row = {c: val for c, val in zip(cols, args)}
                        self.tables[t].append(row)
                    break

    async def close(self) -> None:
        pass


def _create_legacy_encrypted_secret(plaintext: str, key_str: str) -> str:
    """Encrypts plaintext using legacy salt and info."""
    nonce = os.urandom(GCM_IV_LEN)
    key = _derive_key(key_str, salt=_LEGACY_SALT, info=_LEGACY_INFO)
    ct = _aes256_gcm_encrypt(key, nonce, plaintext.encode("utf-8"), None)
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ct_b64 = base64.b64encode(ct).decode("ascii")
    return f"ENC:v1:{nonce_b64}:{ct_b64}"


# ---------------------------------------------------------------------------
# Test 1: Synthetic v3.1.35s Dump Parsing & Restore
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_v3_1_35s_dump_restore_and_hash_upgrade(tmp_path: Path):
    dump_content = """
-- Ephemres v3.1.35s Synthetic Dump
CREATE TABLE _timescaledb_internal._hyper_1_1_chunk (
    id uuid NOT NULL,
    endpoint_id uuid NOT NULL,
    operational_state character varying(20) NOT NULL,
    detailed_state character varying(30) NOT NULL,
    success_count smallint NOT NULL,
    failed_count smallint NOT NULL,
    health_score numeric(5,2) NOT NULL,
    avg_rtt_ms numeric(10,2),
    is_split_event boolean DEFAULT false NOT NULL,
    start_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)
INHERITS (public.endpoint_events);

COPY public.roles (id, role_name, description, created_at) FROM stdin;
a0000000-0000-0000-0000-000000000001	ADMIN	System Administrator	2026-01-01 00:00:00+00
a0000000-0000-0000-0000-000000000002	OPERATOR	Operator User	2026-01-01 00:00:00+00
\\.

COPY public.users (id, username, password_hash, role_id, is_active, must_change_password, last_login, created_at, updated_at) FROM stdin;
b0000000-0000-0000-0000-000000000001	admin	$argon2id$v=19$m=65536,t=3,p=4$somelegacyadminhash	a0000000-0000-0000-0000-000000000001	t	f	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
b0000000-0000-0000-0000-000000000002	operator1	$argon2id$v=19$m=65536,t=3,p=4$somelegacyophash	a0000000-0000-0000-0000-000000000002	t	f	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

-- v3.1.35s endpoints table lacks flow_exporter_ips, flow_interface_aliases, device_role
COPY public.endpoints (id, ip_address, hostname, device_type, location, description, monitoring_enabled, endpoint_status, created_by, created_at, updated_at) FROM stdin;
e0000000-0000-0000-0000-000000000001	192.168.1.1	core-switch-01	SWITCH	DC1	Core switch	t	ACTIVE	b0000000-0000-0000-0000-000000000001	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

COPY _timescaledb_internal._hyper_1_1_chunk (id, endpoint_id, operational_state, detailed_state, success_count, failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time, created_at) FROM stdin;
ev000000-0000-0000-0000-000000000001	e0000000-0000-0000-0000-000000000001	UP	HEALTHY	5	0	100.00	12.50	f	2026-01-01 01:00:00+00	2026-01-01 01:01:00+00	2026-01-01 01:01:00+00
\\.
"""
    dump_file = tmp_path / "backup_v3.1.35s.sql"
    dump_file.write_text(dump_content, encoding="utf-8")

    # Target v3.3.2v+ schema includes the new flow and role columns
    mock_conn = MockDbConnection(get_target_table_columns.__wrapped__(None) if hasattr(get_target_table_columns, "__wrapped__") else {
        "roles": {"id": "uuid", "role_name": "varchar", "description": "text", "created_at": "timestamptz"},
        "users": {"id": "uuid", "username": "varchar", "password_hash": "text", "role_id": "uuid", "is_active": "bool", "must_change_password": "bool", "created_at": "timestamptz", "updated_at": "timestamptz"},
        "endpoints": {"id": "uuid", "ip_address": "inet", "hostname": "varchar", "device_type": "varchar", "location": "varchar", "description": "text", "monitoring_enabled": "bool", "endpoint_status": "varchar", "created_by": "uuid", "created_at": "timestamptz", "updated_at": "timestamptz", "flow_exporter_ips": "_inet", "flow_interface_aliases": "jsonb", "device_role": "varchar"},
        "endpoint_events": {"id": "uuid", "endpoint_id": "uuid", "operational_state": "varchar", "detailed_state": "varchar", "success_count": "int2", "failed_count": "int2", "health_score": "numeric", "avg_rtt_ms": "numeric", "is_split_event": "bool", "start_time": "timestamptz", "end_time": "timestamptz", "created_at": "timestamptz"},
    })

    summary = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=dump_file,
        admin_password="temporary_admin_pass",
    )

    assert summary["user_count"] == 2
    assert summary["endpoint_count"] == 1
    assert summary["telemetry_rows_restored"] == 1
    assert summary["legacy_hashes_upgraded"] == 2

    # 1. Verify admin password upgraded to $scrypt$ and matches "temporary_admin_pass"
    user_rows = mock_conn.tables["users"]
    admin_row = next(u for u in user_rows if u["username"] == "admin")
    assert admin_row["password_hash"].startswith("$scrypt$")
    assert verify_password("temporary_admin_pass", admin_row["password_hash"])
    assert admin_row["must_change_password"] is True
    # Verify role remapped to existing target role
    assert str(admin_row["role_id"]) == "00000000-0000-0000-0000-000000000001"

    # 2. Verify non-admin user received a random $scrypt$ hash and must_change_password=True
    op_row = next(u for u in user_rows if u["username"] == "operator1")
    assert op_row["password_hash"].startswith("$scrypt$")
    assert op_row["must_change_password"] is True
    assert str(op_row["role_id"]) == "00000000-0000-0000-0000-000000000002"

    # 3. Verify endpoints row omitted missing v3.3.2v columns (so PostgreSQL defaults apply)
    ep_row = mock_conn.tables["endpoints"][0]
    assert ep_row["hostname"] == "core-switch-01"
    assert "flow_exporter_ips" not in ep_row
    assert "device_role" not in ep_row

    # 4. Verify hypertable chunk row routed to public.endpoint_events
    ee_row = mock_conn.tables["endpoint_events"][0]
    assert ee_row["operational_state"] == "UP"
    assert ee_row["health_score"] == 100.00


# ---------------------------------------------------------------------------
# Test 2: Synthetic v3.3.0 / v3.3.1n Dump Restore with Re-encrypted Secrets
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_v3_3_0_dump_restore_and_secret_reencryption(tmp_path: Path):
    old_key = "my-legacy-v3-secret-key-32chars"
    legacy_webhook_cfg = json.dumps({"webhook_url": "https://discord.com/api/webhooks/999/secret"})
    enc_legacy_secret = _create_legacy_encrypted_secret(legacy_webhook_cfg, old_key)

    dump_content = f"""
-- Ephemres v3.3.0 Synthetic Dump with Flow Telemetry and Legacy Alert Channel Secret
CREATE TABLE _timescaledb_internal._hyper_2_1_chunk (
    bucket timestamp with time zone NOT NULL,
    exporter_id uuid NOT NULL,
    src_endpoint_id uuid,
    dst_endpoint_id uuid,
    src_ip inet NOT NULL,
    dst_ip inet NOT NULL,
    protocol smallint NOT NULL,
    dst_port integer NOT NULL,
    bytes bigint DEFAULT 0 NOT NULL,
    packets bigint DEFAULT 0 NOT NULL,
    flow_count integer DEFAULT 1 NOT NULL
)
INHERITS (public.flow_minute_rollups);

CREATE TABLE _timescaledb_internal._hyper_3_1_chunk (
    bucket timestamp with time zone NOT NULL,
    exporter_id uuid NOT NULL,
    interface_idx integer NOT NULL,
    in_bytes bigint DEFAULT 0 NOT NULL,
    out_bytes bigint DEFAULT 0 NOT NULL,
    in_packets bigint DEFAULT 0 NOT NULL,
    out_packets bigint DEFAULT 0 NOT NULL,
    flow_count integer DEFAULT 0 NOT NULL
)
INHERITS (public.flow_interface_minute_rollups);

COPY public.roles (id, role_name, description, created_at) FROM stdin;
a0000000-0000-0000-0000-000000000001	ADMIN	System Administrator	2026-01-01 00:00:00+00
\\.

COPY public.users (id, username, password_hash, role_id, is_active, must_change_password, last_login, created_at, updated_at) FROM stdin;
b0000000-0000-0000-0000-000000000001	admin	$argon2id$v=19$m=65536,t=3,p=4$v33hash	a0000000-0000-0000-0000-000000000001	t	f	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

-- Child endpoint appears BEFORE parent endpoint to verify two-pass manual_parent_id FK safety
COPY public.endpoints (id, ip_address, hostname, device_type, location, description, monitoring_enabled, endpoint_status, manual_parent_id, created_by, created_at, updated_at, flow_exporter_ips, flow_interface_aliases, device_role) FROM stdin;
e0000000-0000-0000-0000-000000000003	10.0.0.10	access-sw	SWITCH	HQ	Access Switch	t	ACTIVE	e0000000-0000-0000-0000-000000000002	b0000000-0000-0000-0000-000000000001	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00	{{}}	{{}}	CORE_SWITCH
e0000000-0000-0000-0000-000000000002	10.0.0.1	border-gw	ROUTER	HQ	Border Router	t	ACTIVE	\\N	b0000000-0000-0000-0000-000000000001	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00	{{10.0.0.1,10.0.0.2}}	{{"1": "Gig0/0"}}	gateway
\\.

COPY public.alert_channels (id, name, channel_type, is_enabled, config, endpoint_ids, subnet_filters, severity_filters, created_at, updated_at) FROM stdin;
ch000000-0000-0000-0000-000000000001	Discord Alerts	DISCORD	t	{enc_legacy_secret}	[]	[]	["DOWN"]	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

COPY _timescaledb_internal._hyper_2_1_chunk (bucket, exporter_id, src_endpoint_id, dst_endpoint_id, src_ip, dst_ip, protocol, dst_port, bytes, packets, flow_count) FROM stdin;
2026-01-01 02:00:00+00	e0000000-0000-0000-0000-000000000002	e0000000-0000-0000-0000-000000000003	\\N	10.0.0.5	10.0.0.6	6	443	1048576	1000	10
\\.

COPY _timescaledb_internal._hyper_3_1_chunk (bucket, exporter_id, interface_idx, in_bytes, out_bytes, in_packets, out_packets, flow_count) FROM stdin;
2026-01-01 02:00:00+00	e0000000-0000-0000-0000-000000000002	1	524288	1048576	500	1000	10
\\.
"""
    dump_file = tmp_path / "backup_v3.3.0.sql"
    dump_file.write_text(dump_content, encoding="utf-8")

    from app.core.restore import _get_default_v332_schema
    mock_conn = MockDbConnection(_get_default_v332_schema())

    summary = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=dump_file,
        admin_password="new_admin_password",
        old_secret_key=old_key,
    )

    assert summary["channels_re_encrypted"] == 1
    assert summary["channels_disabled"] == 0
    assert summary["telemetry_rows_restored"] == 2
    assert summary["endpoint_count"] == 2

    # 1. Verify channel secret re-encrypted and readable by decrypt_secret()
    ch_row = mock_conn.tables["alert_channels"][0]
    assert ch_row["is_enabled"] is True
    re_encrypted = ch_row["config"]
    assert re_encrypted.startswith("ENC:v1:")
    assert re_encrypted != enc_legacy_secret
    decrypted_json = decrypt_secret(re_encrypted)
    assert json.loads(decrypted_json) == json.loads(legacy_webhook_cfg)

    # 2. Verify endpoints array, jsonb, normalized device_role, and deferred manual_parent_id
    sw_row = next(e for e in mock_conn.tables["endpoints"] if e["hostname"] == "access-sw")
    gw_row = next(e for e in mock_conn.tables["endpoints"] if e["hostname"] == "border-gw")
    assert sw_row["device_role"] == "L2_SWITCH"
    assert sw_row["manual_parent_id"] is None  # Deferred in pass 1 before parent existed
    assert any('UPDATE "endpoints" SET "manual_parent_id"' in q for q in mock_conn.executed_queries)
    assert gw_row["flow_exporter_ips"] == ["10.0.0.1", "10.0.0.2"]
    assert gw_row["device_role"] == "HYBRID_GATEWAY"

    # 3. Verify hypertable chunks routed to real flow tables
    flow_row = mock_conn.tables["flow_minute_rollups"][0]
    assert flow_row["bytes"] == 1048576
    assert flow_row["packets"] == 1000
    assert flow_row["flow_count"] == 10

    if_row = mock_conn.tables["flow_interface_minute_rollups"][0]
    assert if_row["interface_idx"] == 1
    assert if_row["in_bytes"] == 524288
    assert if_row["out_bytes"] == 1048576


# ---------------------------------------------------------------------------
# Test 3: Gzipped Dump, Dry Run, and Skip Telemetry
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_gzipped_dump_and_dry_run(tmp_path: Path):
    raw_sql = """
COPY public.endpoints (id, ip_address, hostname, device_type, location, description, monitoring_enabled, endpoint_status, created_by, created_at, updated_at) FROM stdin;
e0000000-0000-0000-0000-000000000003	172.16.0.1	fw-core	FIREWALL	DC2	Firewall	t	ACTIVE	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.
"""
    gz_file = tmp_path / "backup.sql.gz"
    import gzip
    with gzip.open(gz_file, "wt", encoding="utf-8") as gf:
        gf.write(raw_sql)

    mock_conn = MockDbConnection({
        "endpoints": {"id": "uuid", "ip_address": "inet", "hostname": "varchar", "device_type": "varchar", "location": "varchar", "description": "text", "monitoring_enabled": "bool", "endpoint_status": "varchar", "created_by": "uuid", "created_at": "timestamptz", "updated_at": "timestamptz"},
    })

    # Dry run test
    summary_dry = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=gz_file,
        dry_run=True,
    )
    assert summary_dry["dry_run"] is True
    assert summary_dry["endpoint_count"] == 1
    assert len(mock_conn.tables["endpoints"]) == 0  # No tables modified in dry run

    # Skip telemetry test
    summary_live = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=gz_file,
        skip_telemetry=True,
        dry_run=False,
    )
    assert summary_live["dry_run"] is False
    assert len(mock_conn.tables["endpoints"]) == 1


# ---------------------------------------------------------------------------
# Test 4: seed_admin auto-resets legacy $argon2id$ hash
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_seed_admin_upgrades_legacy_argon2id(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    mock_db = MagicMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=None)
    mock_db.commit = AsyncMock()

    # Return ADMIN role
    role_res = MagicMock()
    role_res.fetchone.return_value = MagicMock(id=uuid.uuid4())

    # Return existing admin with legacy $argon2id$ hash
    user_res = MagicMock()
    user_res.fetchone.return_value = {
        "id": uuid.uuid4(),
        "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$legacyhash",
        "must_change_password": False,
    }

    executed_queries: List[tuple[str, Any]] = []

    async def mock_execute(stmt, params=None):
        sql_str = getattr(stmt, "text_str", str(stmt))
        executed_queries.append((sql_str, params))
        if "FROM roles" in sql_str:
            return role_res
        elif "FROM users" in sql_str:
            return user_res
        return MagicMock()

    mock_db.execute = mock_execute
    monkeypatch.setattr("app.seed_admin.AsyncSessionLocal", lambda: mock_db)

    result = await seed_admin("new_admin_password")
    assert result is True
    assert mock_db.commit.called

    # Verify UPDATE users was executed with new $scrypt$ password hash
    update_executed = False
    for sql, p in executed_queries:
        if "UPDATE users" in sql and p and "p" in p:
            update_executed = True
            assert p["p"].startswith("$scrypt$")
            assert verify_password("new_admin_password", p["p"])
            break
    assert update_executed is True


# ---------------------------------------------------------------------------
# Test 5: Undecryptable Secret Disables Alert Channel
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_undecryptable_secret_disables_channel(tmp_path: Path):
    corrupt_secret = "ENC:v1:YWJjZGVmZ2hpams=:dGhpc2lzbm90YXZhbGlkY2lwaGVydGV4dA=="

    dump_content = f"""
COPY public.alert_channels (id, name, channel_type, is_enabled, config, endpoint_ids, subnet_filters, severity_filters, created_at, updated_at) FROM stdin;
ch000000-0000-0000-0000-000000000099	Broken Secret Channel	WEBHOOK	t	{corrupt_secret}	[]	[]	["DOWN"]	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.
"""
    dump_file = tmp_path / "broken_channel.sql"
    dump_file.write_text(dump_content, encoding="utf-8")

    mock_conn = MockDbConnection({
        "alert_channels": {"id": "uuid", "name": "varchar", "channel_type": "varchar", "is_enabled": "bool", "config": "text", "endpoint_ids": "jsonb", "subnet_filters": "jsonb", "severity_filters": "jsonb", "created_at": "timestamptz", "updated_at": "timestamptz"},
    })

    summary = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=dump_file,
    )
    assert summary["channels_disabled"] == 1
    ch_row = mock_conn.tables["alert_channels"][0]
    assert ch_row["is_enabled"] is False


# ---------------------------------------------------------------------------
# Test 6: Custom Role Insertion and Orphan Hypertable Row Filtering
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_custom_role_and_orphan_hypertable_filtering(tmp_path: Path):
    dump_content = """
COPY public.roles (id, role_name, description, created_at) FROM stdin;
r0000000-0000-0000-0000-000000000099	SECURITY_AUDITOR	Security Auditor	2026-01-01 00:00:00+00
\\.

COPY public.users (id, username, password_hash, role_id, is_active, must_change_password, last_login, created_at, updated_at) FROM stdin;
u0000000-0000-0000-0000-000000000099	auditor	$argon2id$v=19$m=65536,t=3,p=4$auditpass	r0000000-0000-0000-0000-000000000099	t	f	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

COPY public.endpoints (id, ip_address, hostname, device_type, location, description, monitoring_enabled, endpoint_status, created_by, created_at, updated_at) FROM stdin;
e0000000-0000-0000-0000-000000000010	192.168.10.1	edge-01	ROUTER	DC3	Edge	t	ACTIVE	\\N	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.

-- Hypertable with 1 valid row (matching edge-01) and 1 orphan row (missing endpoint_id)
COPY public.endpoint_events (id, endpoint_id, operational_state, detailed_state, success_count, failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time, created_at) FROM stdin;
ev000000-0000-0000-0000-000000000001	e0000000-0000-0000-0000-000000000010	UP	HEALTHY	5	0	100.00	12.50	f	2026-01-01 01:00:00+00	2026-01-01 01:01:00+00	2026-01-01 01:01:00+00
ev000000-0000-0000-0000-000000000002	ffffffff-ffff-ffff-ffff-ffffffffffff	DOWN	OFFLINE	0	5	0.00	\\N	f	2026-01-01 01:00:00+00	2026-01-01 01:01:00+00	2026-01-01 01:01:00+00
\\.
"""
    dump_file = tmp_path / "custom_role.sql"
    dump_file.write_text(dump_content, encoding="utf-8")

    mock_conn = MockDbConnection({
        "roles": {"id": "uuid", "role_name": "varchar", "description": "text", "created_at": "timestamptz"},
        "users": {"id": "uuid", "username": "varchar", "password_hash": "text", "role_id": "uuid", "is_active": "bool", "must_change_password": "bool", "created_at": "timestamptz", "updated_at": "timestamptz"},
        "endpoints": {"id": "uuid", "ip_address": "inet", "hostname": "varchar", "device_type": "varchar", "location": "varchar", "description": "text", "monitoring_enabled": "bool", "endpoint_status": "varchar", "created_by": "uuid", "created_at": "timestamptz", "updated_at": "timestamptz"},
        "endpoint_events": {"id": "uuid", "endpoint_id": "uuid", "operational_state": "varchar", "detailed_state": "varchar", "success_count": "int2", "failed_count": "int2", "health_score": "numeric", "avg_rtt_ms": "numeric", "is_split_event": "bool", "start_time": "timestamptz", "end_time": "timestamptz", "created_at": "timestamptz"},
    })

    summary = await reconcile_and_restore_database(
        conn=mock_conn,
        backup_path=dump_file,
    )

    # 1. Verify custom role inserted
    role_row = mock_conn.tables["roles"][0]
    assert role_row["role_name"] == "SECURITY_AUDITOR"
    assert str(role_row["id"]) == "r0000000-0000-0000-0000-000000000099"

    # 2. Verify user mapped to new role
    user_row = mock_conn.tables["users"][0]
    assert str(user_row["role_id"]) == "r0000000-0000-0000-0000-000000000099"

    # 3. Verify only 1 telemetry row restored (orphan was skipped)
    assert summary["telemetry_rows_restored"] == 1
    assert len(mock_conn.tables["endpoint_events"]) == 1
    assert str(mock_conn.tables["endpoint_events"][0]["endpoint_id"]) == "e0000000-0000-0000-0000-000000000010"


# ---------------------------------------------------------------------------
# Test 7: MockDbConnection Syntax Check & Restore DO UPDATE SET Integrity
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_mock_db_detects_duplicate_assignments_and_restore_syntax(tmp_path: Path):
    mock_conn = MockDbConnection({
        "users": {"id": "uuid", "username": "varchar", "password_hash": "text", "role_id": "uuid", "is_active": "bool", "must_change_password": "bool", "created_at": "timestamptz", "updated_at": "timestamptz"},
    })

    # 1. Direct test of MockDbConnection.execute catching duplicate DO UPDATE SET column
    with pytest.raises(asyncpg.exceptions.PostgresSyntaxError) as exc_info:
        await mock_conn.execute(
            'INSERT INTO "users" ("id", "username") VALUES ($1, $2) '
            'ON CONFLICT ("username") DO UPDATE SET "updated_at" = EXCLUDED."updated_at", updated_at = NOW();',
            uuid.uuid4(), "testuser"
        )
    assert 'multiple assignments to same column "updated_at"' in str(exc_info.value)

    # 1b. Test lowercase 'do update set' syntax detection
    with pytest.raises(asyncpg.exceptions.PostgresSyntaxError) as exc_info:
        await mock_conn.execute(
            'INSERT INTO "users" ("id", "username") VALUES ($1, $2) '
            'on conflict ("username") do update set "updated_at" = EXCLUDED."updated_at", updated_at = NOW();',
            uuid.uuid4(), "testuser_lower"
        )
    assert 'multiple assignments to same column "updated_at"' in str(exc_info.value)

    # 1c. Test unquoted case-folding detection (UPDATED_AT vs updated_at)
    with pytest.raises(asyncpg.exceptions.PostgresSyntaxError) as exc_info:
        await mock_conn.execute(
            'INSERT INTO "users" ("id", "username") VALUES ($1, $2) '
            'ON CONFLICT ("username") DO UPDATE SET UPDATED_AT = NOW(), "updated_at" = NOW();',
            uuid.uuid4(), "testuser_case"
        )
    assert 'multiple assignments to same column "updated_at"' in str(exc_info.value)

    # 1d. Test MockDbConnection.executemany duplicate assignment detection
    with pytest.raises(asyncpg.exceptions.PostgresSyntaxError) as exc_info:
        await mock_conn.executemany(
            'INSERT INTO "users" ("id", "username") VALUES ($1, $2) '
            'ON CONFLICT ("username") DO UPDATE SET "updated_at" = EXCLUDED."updated_at", updated_at = NOW();',
            [[uuid.uuid4(), "batchuser"]]
        )
    assert 'multiple assignments to same column "updated_at"' in str(exc_info.value)

    # 2. Verify execute succeeds on distinct DO UPDATE SET columns
    await mock_conn.execute(
        'INSERT INTO "users" ("id", "username") VALUES ($1, $2) '
        'ON CONFLICT ("username") DO UPDATE SET "password_hash" = EXCLUDED."password_hash", "updated_at" = NOW();',
        uuid.uuid4(), "testuser2"
    )

    # 3. Test minimal user restore without updated_at column in target schema (DO NOTHING fallback)
    dump_content = """
COPY public.users (id, username, created_at) FROM stdin;
u0000000-0000-0000-0000-000000000001	user_minimal	2026-01-01 00:00:00+00
\\.
"""
    dump_file = tmp_path / "minimal_user.sql"
    dump_file.write_text(dump_content, encoding="utf-8")

    mock_conn_minimal = MockDbConnection({
        "users": {"id": "uuid", "username": "varchar", "created_at": "timestamptz"},
    })

    summary = await reconcile_and_restore_database(
        conn=mock_conn_minimal,
        backup_path=dump_file,
    )
    assert summary["user_count"] == 1
    # Verify DO NOTHING was used because there are no update columns and no updated_at
    assert any('ON CONFLICT ("username") DO NOTHING;' in q for q in mock_conn_minimal.executed_queries)

    # 4. Test user restore with update columns and updated_at in target schema:
    # Verifies that "updated_at" = NOW() is used, EXCLUDED."updated_at" is excluded,
    # and exactly one assignment to updated_at exists.
    dump_content_full = """
COPY public.users (id, username, password_hash, role_id, is_active, must_change_password, created_at, updated_at) FROM stdin;
u0000000-0000-0000-0000-000000000002	user_full	$scrypt$ln=14,r=8,p=1$already_scrypt_hash	00000000-0000-0000-0000-000000000001	t	f	2026-01-01 00:00:00+00	2026-01-01 00:00:00+00
\\.
"""
    dump_file_full = tmp_path / "full_user.sql"
    dump_file_full.write_text(dump_content_full, encoding="utf-8")

    mock_conn_full = MockDbConnection({
        "roles": {"id": "uuid", "role_name": "varchar"},
        "users": {"id": "uuid", "username": "varchar", "password_hash": "text", "role_id": "uuid", "is_active": "bool", "must_change_password": "bool", "created_at": "timestamptz", "updated_at": "timestamptz"},
    })

    summary_full = await reconcile_and_restore_database(
        conn=mock_conn_full,
        backup_path=dump_file_full,
    )
    assert summary_full["user_count"] == 1
    user_queries = [q for q in mock_conn_full.executed_queries if 'INSERT INTO "users"' in q]
    assert len(user_queries) == 1
    uq = user_queries[0]
    assert 'ON CONFLICT ("username") DO UPDATE SET' in uq
    assert '"updated_at" = NOW()' in uq
    assert 'EXCLUDED."updated_at"' not in uq
    assert uq.count('"updated_at"') == 2  # Once in INSERT column list, once in DO UPDATE SET "updated_at" = NOW()

