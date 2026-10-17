"""Ephemres Network Monitoring Platform — Cross-Version Database Backup Restore Engine.

Restores PostgreSQL and TimescaleDB database dumps created by pg_dump across
different platform generations (v3.1.35s, v3.3.0, v3.3.1n, v3.3.2v+).

Key Capabilities:
1. TimescaleDB Chunk Remapping:
   Resolves COPY _timescaledb_internal._hyper_* chunk blocks into target public
   hypertables (endpoint_events, flow_minute_rollups, flow_interface_minute_rollups)
   via CREATE TABLE ... INHERITS introspection and column signature heuristics.
2. Dynamic Schema Reconciliation:
   Introspects target information_schema.columns to gracefully handle schema drift;
   new columns (flow_exporter_ips, flow_interface_aliases, device_role) automatically
   receive schema default values, while removed legacy columns are ignored.
3. Password Hashing Upgrade:
   Converts legacy $argon2id$ password hashes to native Scrypt ($scrypt$) hashes with
   forced password change (must_change_password = TRUE).
4. Alert Secret Re-encryption:
   Re-encrypts alert channel credentials (ENC:v1:...) using current and old secret keys
   across both current and legacy HKDF salts.
5. High-Throughput Streaming & Memory Bounding:
   Streams massive time-series hypertable blocks in batches of 1,000 rows with constant
   memory utilization.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import gzip
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Dict, Generator, Iterable, List, Optional, Set, Tuple
import uuid

from app.config import settings
from app.services.auth_service import hash_password
from app.core.crypto import (
    _SALT,
    _INFO,
    _LEGACY_SALT,
    _LEGACY_INFO,
    _derive_key,
    _aes256_gcm_decrypt,
    DecryptionError,
    encrypt_secret,
    decrypt_secret,
)

logger = logging.getLogger("ephemres.restore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

COPY_HEADER_RE = re.compile(
    r"^COPY\s+([\"\w]+)\.([\"\w]+)\s*\((.*?)\)\s+FROM\s+stdin;?",
    re.IGNORECASE,
)
CREATE_TABLE_INTERNAL_RE = re.compile(
    r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?_timescaledb_internal\.([\"\w]+)",
    re.IGNORECASE,
)
INHERITS_RE = re.compile(
    r"INHERITS\s*\(\s*(?:public\.)?([\"\w]+)\s*\)",
    re.IGNORECASE,
)

HYPERTABLE_NAMES = {
    "endpoint_events",
    "flow_minute_rollups",
    "flow_hourly_rollups",
    "flow_daily_rollups",
    "flow_interface_minute_rollups",
    "monitoring_service_events",
}

CONFIG_TABLE_ORDER = [
    "roles",
    "users",
    "app_settings",
    "system_settings",
    "endpoint_groups",
    "endpoints",
    "alert_channels",
    "alert_rules",
    "alert_delivery_logs",
    "audit_logs",
    "endpoint_baseline_routes",
    "endpoint_diagnostic_traces",
    "endpoint_rca_incidents",
    "user_sessions",
]


def _unescape_pg_copy_field(val: str) -> Optional[str]:
    """Unescapes PostgreSQL COPY text format escape sequences."""
    if val == r"\N":
        return None
    if "\\" not in val:
        return val

    res: List[str] = []
    i = 0
    n = len(val)
    while i < n:
        if val[i] == "\\" and i + 1 < n:
            c = val[i + 1]
            if c == "n":
                res.append("\n")
            elif c == "r":
                res.append("\r")
            elif c == "t":
                res.append("\t")
            elif c == "b":
                res.append("\b")
            elif c == "f":
                res.append("\f")
            elif c == "v":
                res.append("\v")
            elif c == "\\":
                res.append("\\")
            else:
                res.append(c)
            i += 2
        else:
            res.append(val[i])
            i += 1
    return "".join(res)


def _open_dump_file(sql_path: Path | str) -> Any:
    """Opens a .sql or .sql.gz dump file for reading."""
    p_str = str(sql_path)
    if p_str.endswith(".gz"):
        return gzip.open(sql_path, "rt", encoding="utf-8", errors="replace")
    return open(sql_path, "r", encoding="utf-8", errors="replace")


def _get_default_v332_schema() -> Dict[str, Dict[str, str]]:
    """Returns canonical v3.3.2v+ public table schema for introspection fallbacks."""
    return {
        "roles": {"id": "uuid", "role_name": "varchar", "description": "text", "created_at": "timestamptz"},
        "users": {
            "id": "uuid",
            "username": "varchar",
            "password_hash": "text",
            "role_id": "uuid",
            "is_active": "bool",
            "must_change_password": "bool",
            "last_login": "timestamptz",
            "created_at": "timestamptz",
            "updated_at": "timestamptz",
        },
        "app_settings": {"id": "uuid", "setting_key": "varchar", "setting_value": "text", "updated_at": "timestamptz"},
        "system_settings": {"id": "uuid", "setting_key": "varchar", "setting_value": "text", "updated_at": "timestamptz"},
        "endpoint_groups": {"id": "uuid", "name": "varchar", "description": "text", "created_at": "timestamptz"},
        "endpoints": {
            "id": "uuid",
            "ip_address": "inet",
            "hostname": "varchar",
            "device_type": "varchar",
            "location": "varchar",
            "description": "text",
            "monitoring_enabled": "bool",
            "endpoint_status": "varchar",
            "created_by": "uuid",
            "created_at": "timestamptz",
            "updated_at": "timestamptz",
            "flow_exporter_ips": "_inet",
            "flow_interface_aliases": "jsonb",
            "device_role": "varchar",
        },
        "endpoint_baseline_routes": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "route_hash": "varchar",
            "hop_count": "int4",
            "hops": "jsonb",
            "created_at": "timestamptz",
        },
        "endpoint_diagnostic_traces": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "trace_type": "varchar",
            "trace_data": "jsonb",
            "created_at": "timestamptz",
        },
        "endpoint_rca_incidents": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "incident_type": "varchar",
            "summary": "text",
            "root_cause_analysis": "jsonb",
            "status": "varchar",
            "started_at": "timestamptz",
            "resolved_at": "timestamptz",
            "created_at": "timestamptz",
        },
        "alert_channels": {
            "id": "uuid",
            "name": "varchar",
            "channel_type": "varchar",
            "is_enabled": "bool",
            "config": "text",
            "endpoint_ids": "jsonb",
            "subnet_filters": "jsonb",
            "severity_filters": "jsonb",
            "created_at": "timestamptz",
            "updated_at": "timestamptz",
        },
        "alert_rules": {
            "id": "uuid",
            "name": "varchar",
            "endpoint_id": "uuid",
            "rule_type": "varchar",
            "threshold": "float8",
            "is_enabled": "bool",
            "created_at": "timestamptz",
        },
        "alert_delivery_logs": {
            "id": "uuid",
            "channel_id": "uuid",
            "alert_id": "uuid",
            "status": "varchar",
            "attempt_count": "int4",
            "error_message": "text",
            "dispatched_at": "timestamptz",
        },
        "audit_logs": {
            "id": "uuid",
            "user_id": "uuid",
            "action": "varchar",
            "target_type": "varchar",
            "target_id": "uuid",
            "created_at": "timestamptz",
        },
        "user_sessions": {
            "id": "uuid",
            "user_id": "uuid",
            "session_token": "varchar",
            "ip_address": "inet",
            "user_agent": "text",
            "expires_at": "timestamptz",
            "created_at": "timestamptz",
        },
        "endpoint_events": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "operational_state": "varchar",
            "detailed_state": "varchar",
            "success_count": "int2",
            "failed_count": "int2",
            "health_score": "numeric",
            "avg_rtt_ms": "numeric",
            "is_split_event": "bool",
            "start_time": "timestamptz",
            "end_time": "timestamptz",
            "created_at": "timestamptz",
        },
        "flow_minute_rollups": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "bucket_start": "timestamptz",
            "src_ip": "inet",
            "dst_ip": "inet",
            "src_port": "int4",
            "dst_port": "int4",
            "protocol": "int2",
            "byte_count": "int8",
            "packet_count": "int8",
            "flow_count": "int4",
            "created_at": "timestamptz",
        },
        "flow_interface_minute_rollups": {
            "id": "uuid",
            "endpoint_id": "uuid",
            "bucket_start": "timestamptz",
            "input_snmp": "int4",
            "output_snmp": "int4",
            "in_bytes": "int8",
            "out_bytes": "int8",
            "in_packets": "int8",
            "out_packets": "int8",
            "created_at": "timestamptz",
        },
    }


async def get_target_table_columns(conn: Any) -> Dict[str, Dict[str, str]]:
    """Introspects target database schema from information_schema.columns.

    Returns: {table_name: {column_name: udt_name}}
    """
    query = """
        SELECT table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
    """
    rows: List[Any] = []
    try:
        if hasattr(conn, "fetch"):
            rows = await conn.fetch(query)
        elif hasattr(conn, "execute"):
            res = await conn.execute(query)
            if hasattr(res, "fetchall"):
                rows = res.fetchall()
            elif isinstance(res, list):
                rows = res
    except Exception as exc:
        logger.debug("Information schema introspection fallback: %s", exc)

    target_schema: Dict[str, Dict[str, str]] = {}
    for r in rows:
        if isinstance(r, dict):
            t_name = r.get("table_name")
            c_name = r.get("column_name")
            udt = r.get("udt_name") or r.get("data_type") or "varchar"
        elif hasattr(r, "__getitem__"):
            t_name = r["table_name"]
            c_name = r["column_name"]
            udt = r["udt_name"] if "udt_name" in r else r["data_type"]
        else:
            t_name = getattr(r, "table_name", None)
            c_name = getattr(r, "column_name", None)
            udt = getattr(r, "udt_name", getattr(r, "data_type", "varchar"))

        if t_name and c_name:
            if t_name not in target_schema:
                target_schema[t_name] = {}
            target_schema[t_name][c_name] = str(udt).lower()

    if not target_schema:
        target_schema = _get_default_v332_schema()

    return target_schema


def _try_decrypt_with_key(ciphertext: str, key_str: str) -> Optional[str]:
    """Attempts AES-256-GCM decryption using specified key string with both salts."""
    if not ciphertext or not ciphertext.startswith("ENC:v1:"):
        return None
    parts = ciphertext.split(":")
    if len(parts) != 4:
        return None

    try:
        nonce = base64.b64decode(parts[2], validate=True)
        raw_ct = base64.b64decode(parts[3], validate=True)
    except Exception:
        return None

    for salt, info in [(_SALT, _INFO), (_LEGACY_SALT, _LEGACY_INFO)]:
        try:
            key = _derive_key(key_str, salt=salt, info=info)
            decrypted = _aes256_gcm_decrypt(key, nonce, raw_ct, None)
            return decrypted.decode("utf-8")
        except Exception:
            continue
    return None


def re_encrypt_alert_channel_config(
    raw_config: str,
    old_secret_key: Optional[str] = None,
) -> Tuple[str, bool]:
    """Attempts to decrypt raw_config with current secret_key (and fallback keys).

    If that fails, tries old_secret_key. If successful, re-encrypts with current key.
    Returns (new_config, is_valid).
    """
    if not raw_config or not raw_config.startswith("ENC:v1:"):
        return raw_config, True

    # 1. Attempt decryption using current settings key / fallback keys
    try:
        decrypted = decrypt_secret(raw_config)
        # Already decryptable with current key
        return raw_config, True
    except DecryptionError:
        pass

    # 2. Attempt decryption using old_secret_key if provided
    if old_secret_key:
        decrypted = _try_decrypt_with_key(raw_config, old_secret_key)
        if decrypted is not None:
            new_enc = encrypt_secret(decrypted)
            return new_enc, True

    # 3. Decryption failed
    return raw_config, False


def _prepare_column_val(val: Optional[str], udt: str) -> Any:
    """Prepares and casts string values from pg_dump into native Python types for asyncpg."""
    if val is None:
        return None

    u = udt.lower()
    if u in ("bool", "boolean"):
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("t", "true", "1", "yes")

    if u in ("int2", "int4", "int8", "smallint", "integer", "bigint"):
        try:
            return int(val)
        except (ValueError, TypeError):
            return val

    if u in ("float4", "float8", "numeric"):
        try:
            return float(val)
        except (ValueError, TypeError):
            return val

    if u in ("json", "jsonb"):
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return str(val)

    if u.startswith("_"):
        # Array type
        if isinstance(val, (list, tuple)):
            return list(val)
        if isinstance(val, str):
            if val in ("{}", ""):
                return []
            if val.startswith("{") and val.endswith("}"):
                items = [item.strip('"') for item in val[1:-1].split(",") if item.strip()]
                return items
            return [val]
        return [val]

    if "timestamp" in u:
        if isinstance(val, datetime):
            return val
        try:
            dt = datetime.fromisoformat(str(val))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return val

    if u == "uuid":
        if isinstance(val, uuid.UUID):
            return val
        try:
            return uuid.UUID(str(val))
        except Exception:
            return val

    return val


async def _fetch_all(conn: Any, sql: str, *args: Any) -> List[Dict[str, Any]]:
    """Helper to execute query and return list of dictionaries."""
    rows: List[Any] = []
    if hasattr(conn, "fetch"):
        rows = await conn.fetch(sql, *args)
    elif hasattr(conn, "execute"):
        res = await conn.execute(sql, *args)
        if hasattr(res, "fetchall"):
            rows = res.fetchall()
        elif isinstance(res, list):
            rows = res

    results: List[Dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            results.append(r)
        elif hasattr(r, "__getitem__"):
            results.append(dict(r))
        else:
            results.append(r.__dict__)
    return results


async def _execute_sql(conn: Any, sql: str, *args: Any) -> None:
    """Helper to execute non-returning SQL."""
    if hasattr(conn, "execute"):
        await conn.execute(sql, *args)


async def _execute_batch(conn: Any, sql: str, args_list: List[List[Any]]) -> None:
    """Helper to execute a batch of parameter rows."""
    if not args_list:
        return
    if hasattr(conn, "executemany"):
        await conn.executemany(sql, args_list)
    elif hasattr(conn, "execute"):
        for args in args_list:
            await conn.execute(sql, *args)


def scan_backup_metadata_and_configs(
    backup_path: Path | str,
) -> Tuple[Dict[str, str], Dict[str, Tuple[List[str], List[Dict[str, Any]]]]]:
    """Scans backup file to extract chunk inheritance DDL and all configuration tables.

    Returns: (chunk_to_parent, config_data)
    """
    chunk_to_parent: Dict[str, str] = {}
    config_data: Dict[str, Tuple[List[str], List[Dict[str, Any]]]] = {}

    current_chunk: Optional[str] = None
    in_copy = False
    copy_table: Optional[str] = None
    copy_cols: List[str] = []
    copy_rows: List[Dict[str, Any]] = []

    with _open_dump_file(backup_path) as f:
        for line in f:
            line_str = line

            if not in_copy:
                # Detect CREATE TABLE _timescaledb_internal.<chunk>
                chunk_m = CREATE_TABLE_INTERNAL_RE.match(line_str)
                if chunk_m:
                    current_chunk = chunk_m.group(1).strip('"')

                if current_chunk:
                    inherits_m = INHERITS_RE.search(line_str)
                    if inherits_m:
                        parent = inherits_m.group(1).strip('"')
                        chunk_to_parent[current_chunk] = parent
                        current_chunk = None
                    elif ";" in line_str:
                        current_chunk = None

                # Detect COPY header
                copy_m = COPY_HEADER_RE.match(line_str)
                if copy_m:
                    schema = copy_m.group(1).strip('"')
                    raw_table = copy_m.group(2).strip('"')
                    cols = [c.strip().strip('"') for c in copy_m.group(3).split(",") if c.strip()]

                    if schema == "_timescaledb_internal":
                        parent = chunk_to_parent.get(raw_table)
                        if not parent:
                            if "event_timestamp" in cols or ("status_code" in cols and "endpoint_id" in cols) or ("detailed_state" in cols and "endpoint_id" in cols):
                                parent = "endpoint_events"
                            elif "input_snmp" in cols or "output_snmp" in cols:
                                parent = "flow_interface_minute_rollups"
                            elif ("src_ip" in cols and "dst_ip" in cols) or ("byte_count" in cols and "flow_count" in cols):
                                parent = "flow_minute_rollups"
                        if parent:
                            chunk_to_parent[raw_table] = parent
                        effective_table = parent
                    elif schema == "public":
                        if raw_table == "system_settings":
                            effective_table = "app_settings"
                        elif raw_table == "alert_logs":
                            effective_table = "alert_delivery_logs"
                        else:
                            effective_table = raw_table
                    else:
                        effective_table = None

                    if effective_table and effective_table not in HYPERTABLE_NAMES and effective_table != "alembic_version":
                        in_copy = True
                        copy_table = effective_table
                        copy_cols = cols
                        copy_rows = []
                    else:
                        in_copy = False
                        copy_table = None
            else:
                # Inside config COPY block
                stripped = line_str.rstrip("\r\n")
                if stripped == r"\.":
                    if copy_table and copy_cols:
                        if copy_table in config_data:
                            # Append rows to existing table buffer
                            existing_cols, existing_rows = config_data[copy_table]
                            existing_rows.extend(copy_rows)
                        else:
                            config_data[copy_table] = (copy_cols, copy_rows)
                    in_copy = False
                    copy_table = None
                    copy_cols = []
                    copy_rows = []
                else:
                    parts = stripped.split("\t")
                    if len(parts) == len(copy_cols):
                        row = {col: _unescape_pg_copy_field(p) for col, p in zip(copy_cols, parts)}
                        copy_rows.append(row)

    return chunk_to_parent, config_data


async def reconcile_and_restore_database(
    conn: Any,
    backup_path: Path | str,
    admin_password: str = "admin",
    old_secret_key: Optional[str] = None,
    skip_telemetry: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Core execution engine for cross-version database restoration."""
    logger.info("Starting Ephemres database restoration from %s...", backup_path)
    target_schema = await get_target_table_columns(conn)

    chunk_to_parent, config_data = scan_backup_metadata_and_configs(backup_path)
    logger.info("Backup parsed: %d internal chunks mapped, %d config tables extracted.", len(chunk_to_parent), len(config_data))

    summary: Dict[str, Any] = {
        "dry_run": dry_run,
        "tables_restored": [],
        "user_count": 0,
        "endpoint_count": 0,
        "telemetry_rows_restored": 0,
        "legacy_hashes_upgraded": 0,
        "channels_re_encrypted": 0,
        "channels_disabled": 0,
    }

    if dry_run:
        logger.info("[DRY RUN] Simulating restoration without committing database changes.")
        for t in CONFIG_TABLE_ORDER:
            if t in config_data:
                cols, rows = config_data[t]
                target_cols = target_schema.get(t, {})
                usable = [c for c in cols if c in target_cols]
                logger.info("[DRY RUN] Table '%s': %d dump rows, %d/%d usable columns.", t, len(rows), len(usable), len(target_cols))
                summary["tables_restored"].append(t)
                if t == "users":
                    summary["user_count"] = len(rows)
                elif t == "endpoints":
                    summary["endpoint_count"] = len(rows)
        return summary

    role_id_map: Dict[str, str] = {}
    user_id_map: Dict[str, str] = {}
    valid_endpoint_ids: Set[str] = set()

    # Query pre-existing endpoints in target database
    existing_eps = await _fetch_all(conn, "SELECT id FROM endpoints;")
    valid_endpoint_ids.update(str(r["id"]) for r in existing_eps)

    # 1. Restore roles
    if "roles" in config_data and "roles" in target_schema:
        cols, rows = config_data["roles"]
        target_cols = target_schema["roles"]
        usable = [c for c in cols if c in target_cols]

        existing_roles = await _fetch_all(conn, "SELECT id, role_name FROM roles;")
        target_role_map = {str(r["role_name"]).upper(): str(r["id"]) for r in existing_roles}

        for row in rows:
            r_name = str(row.get("role_name", "")).upper()
            dump_id = str(row.get("id"))
            if r_name in target_role_map:
                role_id_map[dump_id] = target_role_map[r_name]
            else:
                # Insert custom role
                role_id_map[dump_id] = dump_id
                col_names = ", ".join(f'"{c}"' for c in usable)
                placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))
                vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
                sql = f'INSERT INTO "roles" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO NOTHING;'
                await _execute_sql(conn, sql, *vals)
        summary["tables_restored"].append("roles")

    # 2. Restore users
    if "users" in config_data and "users" in target_schema:
        cols, rows = config_data["users"]
        target_cols = target_schema["users"]
        usable = [c for c in cols if c in target_cols]

        existing_users = await _fetch_all(conn, "SELECT id, username FROM users;")
        target_user_map = {str(r["username"]): str(r["id"]) for r in existing_users}

        for row in rows:
            uname = str(row.get("username", ""))
            dump_id = str(row.get("id"))
            pwd = str(row.get("password_hash") or "")

            if dump_id in target_user_map.values():
                pass
            if uname in target_user_map:
                user_id_map[dump_id] = target_user_map[uname]
            else:
                user_id_map[dump_id] = dump_id

            # Remap role_id
            if row.get("role_id") and str(row["role_id"]) in role_id_map:
                row["role_id"] = role_id_map[str(row["role_id"])]

            # Check password hash transition ($argon2id$ -> $scrypt$)
            if not pwd.startswith("$scrypt$"):
                if uname == "admin":
                    row["password_hash"] = hash_password(admin_password)
                    row["must_change_password"] = True
                    logger.info("[restore] Reset admin password to $scrypt$ with must_change_password=True.")
                else:
                    rand_pass = secrets.token_urlsafe(24)
                    row["password_hash"] = hash_password(rand_pass)
                    row["must_change_password"] = True
                summary["legacy_hashes_upgraded"] += 1

            col_names = ", ".join(f'"{c}"' for c in usable)
            placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))
            vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]

            update_cols = [c for c in usable if c not in ("id", "username", "created_at")]
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            if set_clause:
                sql = f'INSERT INTO "users" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("username") DO UPDATE SET {set_clause}, updated_at = NOW();'
            else:
                sql = f'INSERT INTO "users" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("username") DO NOTHING;'
            await _execute_sql(conn, sql, *vals)

        summary["user_count"] = len(rows)
        summary["tables_restored"].append("users")

    # 3. Restore app_settings / system_settings
    settings_table = "app_settings" if "app_settings" in target_schema else "system_settings"
    if ("app_settings" in config_data or "system_settings" in config_data) and settings_table in target_schema:
        source_key = "app_settings" if "app_settings" in config_data else "system_settings"
        cols, rows = config_data[source_key]
        target_cols = target_schema[settings_table]

        if "setting_key" in target_cols and "setting_key" in cols:
            # Key-value app_settings format
            usable = [c for c in cols if c in target_cols]
            col_names = ", ".join(f'"{c}"' for c in usable)
            placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))
            for row in rows:
                vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
                sql = f'INSERT INTO "{settings_table}" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("setting_key") DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = NOW();'
                await _execute_sql(conn, sql, *vals)
        summary["tables_restored"].append(settings_table)

    # 4. Restore endpoint_groups
    if "endpoint_groups" in config_data and "endpoint_groups" in target_schema:
        cols, rows = config_data["endpoint_groups"]
        target_cols = target_schema["endpoint_groups"]
        usable = [c for c in cols if c in target_cols]
        col_names = ", ".join(f'"{c}"' for c in usable)
        placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))
        for row in rows:
            vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
            sql = f'INSERT INTO "endpoint_groups" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO NOTHING;'
            await _execute_sql(conn, sql, *vals)
        summary["tables_restored"].append("endpoint_groups")

    # 5. Restore endpoints
    if "endpoints" in config_data and "endpoints" in target_schema:
        cols, rows = config_data["endpoints"]
        target_cols = target_schema["endpoints"]
        usable = [c for c in cols if c in target_cols]
        col_names = ", ".join(f'"{c}"' for c in usable)
        placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))

        update_cols = [c for c in usable if c not in ("id", "created_at")]
        set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)

        for row in rows:
            ep_id = str(row.get("id"))
            valid_endpoint_ids.add(ep_id)

            if row.get("created_by") and str(row["created_by"]) in user_id_map:
                row["created_by"] = user_id_map[str(row["created_by"])]

            vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
            if set_clause:
                sql = f'INSERT INTO "endpoints" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO UPDATE SET {set_clause};'
            else:
                sql = f'INSERT INTO "endpoints" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO NOTHING;'
            await _execute_sql(conn, sql, *vals)

        summary["endpoint_count"] = len(rows)
        summary["tables_restored"].append("endpoints")

    # 6. Restore alert_channels
    if "alert_channels" in config_data and "alert_channels" in target_schema:
        cols, rows = config_data["alert_channels"]
        target_cols = target_schema["alert_channels"]
        usable = [c for c in cols if c in target_cols]
        col_names = ", ".join(f'"{c}"' for c in usable)
        placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))

        update_cols = [c for c in usable if c not in ("id", "created_at")]
        set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)

        for row in rows:
            raw_cfg = str(row.get("config") or "")
            new_cfg, is_valid = re_encrypt_alert_channel_config(raw_cfg, old_secret_key)
            row["config"] = new_cfg
            if not is_valid:
                row["is_enabled"] = False
                summary["channels_disabled"] += 1
                logger.warning("[restore] Alert channel '%s' secret could not be decrypted. Disabled channel for operator safety.", row.get("name"))
            else:
                if new_cfg != raw_cfg:
                    summary["channels_re_encrypted"] += 1

            vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
            if set_clause:
                sql = f'INSERT INTO "alert_channels" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO UPDATE SET {set_clause};'
            else:
                sql = f'INSERT INTO "alert_channels" ({col_names}) VALUES ({placeholders}) ON CONFLICT ("id") DO NOTHING;'
            await _execute_sql(conn, sql, *vals)
        summary["tables_restored"].append("alert_channels")

    # 7. Restore remaining non-hypertable config tables
    for t in ["alert_rules", "alert_delivery_logs", "audit_logs", "endpoint_baseline_routes", "endpoint_diagnostic_traces", "endpoint_rca_incidents", "user_sessions"]:
        if t in config_data and t in target_schema:
            cols, rows = config_data[t]
            target_cols = target_schema[t]
            usable = [c for c in cols if c in target_cols]
            col_names = ", ".join(f'"{c}"' for c in usable)
            placeholders = ", ".join(f"${i+1}" for i in range(len(usable)))
            sql = f'INSERT INTO "{t}" ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING;'

            batch: List[List[Any]] = []
            for row in rows:
                if "endpoint_id" in row and row["endpoint_id"] and str(row["endpoint_id"]) not in valid_endpoint_ids:
                    continue
                if "user_id" in row and row["user_id"] and str(row["user_id"]) in user_id_map:
                    row["user_id"] = user_id_map[str(row["user_id"])]

                vals = [_prepare_column_val(row.get(c), target_cols.get(c, "varchar")) for c in usable]
                batch.append(vals)
                if len(batch) >= 1000:
                    await _execute_batch(conn, sql, batch)
                    batch.clear()
            if batch:
                await _execute_batch(conn, sql, batch)
                batch.clear()
            summary["tables_restored"].append(t)

    # 8. Stream Hypertables in Batches of 1,000 (Pass 2)
    if not skip_telemetry:
        logger.info("Streaming hypertable time-series blocks into PostgreSQL...")
        in_hyper = False
        current_target_table: Optional[str] = None
        current_cols: List[str] = []
        usable_cols: List[str] = []
        insert_sql: str = ""
        batch_rows: List[List[Any]] = []

        with _open_dump_file(backup_path) as f:
            for line in f:
                if not in_hyper:
                    copy_m = COPY_HEADER_RE.match(line)
                    if copy_m:
                        schema = copy_m.group(1).strip('"')
                        tbl = copy_m.group(2).strip('"')
                        raw_cols = [c.strip().strip('"') for c in copy_m.group(3).split(",") if c.strip()]

                        if schema == "_timescaledb_internal":
                            mapped = chunk_to_parent.get(tbl)
                        elif schema == "public" and tbl in HYPERTABLE_NAMES:
                            mapped = tbl
                        else:
                            mapped = None

                        if mapped and mapped in target_schema:
                            in_hyper = True
                            current_target_table = mapped
                            current_cols = raw_cols
                            target_cols = target_schema[mapped]
                            usable_cols = [c for c in current_cols if c in target_cols]
                            col_names = ", ".join(f'"{c}"' for c in usable_cols)
                            placeholders = ", ".join(f"${i+1}" for i in range(len(usable_cols)))
                            insert_sql = f'INSERT INTO "{mapped}" ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING;'
                            batch_rows.clear()
                else:
                    stripped = line.rstrip("\r\n")
                    if stripped == r"\.":
                        if batch_rows:
                            await _execute_batch(conn, insert_sql, batch_rows)
                            summary["telemetry_rows_restored"] += len(batch_rows)
                            batch_rows.clear()
                        in_hyper = False
                        current_target_table = None
                    else:
                        parts = stripped.split("\t")
                        if len(parts) == len(current_cols) and current_target_table:
                            row_dict = {c: _unescape_pg_copy_field(p) for c, p in zip(current_cols, parts)}
                            # Filter by endpoint_id
                            ep_id = row_dict.get("endpoint_id")
                            if ep_id and str(ep_id) not in valid_endpoint_ids:
                                continue

                            t_cols = target_schema[current_target_table]
                            vals = [_prepare_column_val(row_dict.get(c), t_cols.get(c, "varchar")) for c in usable_cols]
                            batch_rows.append(vals)

                            if len(batch_rows) >= 1000:
                                await _execute_batch(conn, insert_sql, batch_rows)
                                summary["telemetry_rows_restored"] += len(batch_rows)
                                batch_rows.clear()

        if current_target_table not in summary["tables_restored"] and summary["telemetry_rows_restored"] > 0:
            summary["tables_restored"].append(current_target_table)

    logger.info("Restoration finished successfully: %s", summary)
    return summary


def main() -> None:
    """CLI Entrypoint for python3 -m app.core.restore."""
    parser = argparse.ArgumentParser(
        description="Ephemres Cross-Version Database Backup Restore Engine",
    )
    parser.add_argument(
        "--backup-file",
        required=True,
        help="Path to .sql or .sql.gz backup dump file",
    )
    parser.add_argument(
        "--admin-password",
        default="admin",
        help="Temporary password for admin account (defaults to 'admin')",
    )
    parser.add_argument(
        "--old-secret-key",
        default=None,
        help="Previous SECRET_KEY for re-encrypting alert channel configurations",
    )
    parser.add_argument(
        "--skip-telemetry",
        action="store_true",
        help="Skip restoring hypertable time-series chunks for fast config-only restore",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate backup and target schema compatibility without committing",
    )

    args = parser.parse_args()
    backup_path = Path(args.backup_file).resolve()
    if not backup_path.is_file():
        logger.error("Backup file not found: %s", backup_path)
        sys.exit(1)

    # Discover old secret key if not passed explicitly
    old_secret = args.old_secret_key or os.environ.get("OLD_SECRET_KEY")
    if not old_secret:
        for env_path in [
            Path("/etc/netmon/netmon.env.bak"),
            Path("/etc/netmon/netmon.env"),
            Path("/etc/ephemres/ephemres.env.bak"),
        ]:
            if env_path.is_file():
                try:
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("SECRET_KEY="):
                            old_secret = line.split("=", 1)[1].strip("'\"")
                            logger.info("Discovered previous SECRET_KEY from %s", env_path)
                            break
                    if old_secret:
                        break
                except Exception:
                    pass

    async def _run() -> None:
        from app.core.migrator import get_db_connection
        conn = await get_db_connection()
        try:
            summary = await reconcile_and_restore_database(
                conn=conn,
                backup_path=backup_path,
                admin_password=args.admin_password,
                old_secret_key=old_secret,
                skip_telemetry=args.skip_telemetry,
                dry_run=args.dry_run,
            )
            print("\n" + "=" * 60)
            print(" Ephemres Database Restoration Summary Report")
            print("=" * 60)
            print(f" Mode:               {'DRY RUN (no changes committed)' if summary['dry_run'] else 'LIVE EXECUTION'}")
            print(f" Tables Restored:    {len(summary['tables_restored'])} tables")
            print(f" Users Restored:     {summary['user_count']}")
            print(f" Endpoints Restored: {summary['endpoint_count']}")
            print(f" Telemetry Rows:     {summary['telemetry_rows_restored']}")
            print(f" Hashes Upgraded:    {summary['legacy_hashes_upgraded']} legacy hashes -> $scrypt$")
            print(f" Secrets Re-enc:     {summary['channels_re_encrypted']} channel secrets")
            if summary['channels_disabled'] > 0:
                print(f" Channels Disabled:  {summary['channels_disabled']} (undecryptable secrets)")
            print("=" * 60)
            if not summary["dry_run"]:
                print(f" [SUCCESS] Database restored. Admin login password set to '{args.admin_password}'.")
                print("           First login requires forced password change.")
            print("=" * 60 + "\n")
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("Restore failed: %s", exc, exc_info=exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
