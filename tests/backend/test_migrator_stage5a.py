from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure backend root is on sys.path
backend_path = Path(__file__).resolve().parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.core.migrator import (
    OperationsProxy,
    find_versions_dir,
    load_revisions,
)


def test_requirements_no_alembic_psycopg2():
    """Ensure alembic and psycopg2 are completely removed from requirements.txt."""
    req_file = backend_path / "requirements.txt"
    content = req_file.read_text(encoding="utf-8")
    assert "alembic" not in content.lower()
    assert "psycopg2" not in content.lower()


def test_database_sync_engine_none():
    """Ensure app.database.sync_engine is None and does not eagerly load psycopg2."""
    import app.database as db
    assert db.sync_engine is None


def test_migrator_loads_all_13_revisions():
    """Ensure all 13 migration files are discovered, loaded, and sorted topologically."""
    versions_dir = find_versions_dir()
    ordered = load_revisions(versions_dir)

    assert len(ordered) == 13
    expected_chain = [f"{i:04d}" for i in range(1, 14)]
    actual_chain = [r.revision for r in ordered]
    assert actual_chain == expected_chain

    # Check root and head
    assert ordered[0].down_revision is None
    assert ordered[-1].revision == "0013"

    # Check continuous singly-linked list integrity
    for i in range(1, 13):
        assert ordered[i].down_revision == ordered[i - 1].revision


def test_all_13_revisions_upgrade_ddl_compilation():
    """Execute dry-run DDL compilation for all 13 revisions and verify generated SQL."""
    versions_dir = find_versions_dir()
    ordered = load_revisions(versions_dir)

    total_statements = 0
    all_statements: list[str] = []

    for rev in ordered:
        stmts = rev.get_upgrade_statements()
        assert len(stmts) > 0, f"Revision {rev.revision} produced 0 statements"
        total_statements += len(stmts)
        all_statements.extend(stmts)

    # 56 total statements across all 13 migrations
    assert total_statements == 56

    full_sql = "\n".join(all_statements)

    # Verify critical DDL constructs across migrations:
    # 0001 Initial schema
    assert 'CREATE TABLE roles (' in full_sql
    assert 'CREATE TABLE users (' in full_sql
    assert 'CREATE TABLE endpoints (' in full_sql
    assert 'CREATE TABLE endpoint_events (' in full_sql
    assert 'CREATE INDEX IF NOT EXISTS "idx_endpoint_events_incident_query"' in full_sql
    assert 'FOREIGN KEY ("role_id") REFERENCES "roles" ("id")' in full_sql
    assert 'FOREIGN KEY ("created_by") REFERENCES "users" ("id")' in full_sql
    assert 'FOREIGN KEY ("endpoint_id") REFERENCES "endpoints" ("id")' in full_sql
    assert 'REFERENCES roles.id' not in full_sql
    assert 'REFERENCES users.id' not in full_sql

    # 0003 Diagnostic and topology
    assert 'ALTER TABLE "endpoints" ADD COLUMN allow_incident_trace' in full_sql
    assert 'ALTER TABLE "endpoints" ADD COLUMN allow_topology_discovery' in full_sql
    assert 'ALTER TABLE "endpoints" ADD COLUMN manual_parent_id' in full_sql
    assert 'CREATE TABLE endpoint_diagnostic_traces (' in full_sql
    assert 'ALTER TABLE "endpoints" ADD CONSTRAINT "fk_endpoints_manual_parent_id"' in full_sql

    # 0004 v1.5 backend core
    assert 'CREATE TABLE endpoint_baseline_routes (' in full_sql
    assert 'CREATE TABLE endpoint_rca_incidents (' in full_sql

    # 0006 v3.0 session storage
    assert 'CREATE TABLE user_sessions (' in full_sql
    assert 'CREATE INDEX IF NOT EXISTS "idx_user_sessions_user_id"' in full_sql

    # 0007 v3.1 alert channels
    assert 'CREATE TABLE alert_channels (' in full_sql
    assert 'CREATE TABLE alert_delivery_logs (' in full_sql

    # 0008 v3.2 flow ingestion
    assert 'CREATE TABLE flow_minute_rollups (' in full_sql
    assert 'CREATE INDEX IF NOT EXISTS "idx_endpoints_flow_exporter_ips" ON "endpoints" USING gin ("flow_exporter_ips")' in full_sql

    # 0012 v3.3 flow interface telemetry
    assert 'ALTER TABLE "endpoints" ADD COLUMN device_role VARCHAR(30) DEFAULT \'ACTIVE_HOST\' NOT NULL;' in full_sql
    assert 'DEFAULT ACTIVE_HOST' not in full_sql
    assert 'ALTER TABLE "endpoints" ADD CONSTRAINT "ck_endpoints_device_role"' in full_sql
    assert 'CREATE TABLE flow_interface_minute_rollups (' in full_sql
    assert 'CREATE INDEX IF NOT EXISTS "idx_flow_if_rollups_exp_bucket"' in full_sql

    # 0013 add flow tcp_flags and tos
    assert 'ALTER TABLE flow_minute_rollups' in full_sql
    assert 'ADD COLUMN IF NOT EXISTS tcp_flags SMALLINT NOT NULL DEFAULT 0' in full_sql
    assert 'ADD COLUMN IF NOT EXISTS tos SMALLINT NOT NULL DEFAULT 0' in full_sql


def test_all_13_revisions_downgrade_ddl_compilation():
    """Execute dry-run DDL compilation for all 13 downgrades and verify generated SQL."""
    versions_dir = find_versions_dir()
    ordered = load_revisions(versions_dir)

    total_statements = 0
    all_statements: list[str] = []

    for rev in reversed(ordered):
        stmts = rev.get_downgrade_statements()
        assert len(stmts) > 0, f"Revision {rev.revision} downgrade produced 0 statements"
        total_statements += len(stmts)
        all_statements.extend(stmts)

    # 48 total downgrade statements
    assert total_statements == 48

    full_sql = "\n".join(all_statements)

    assert 'ALTER TABLE flow_minute_rollups DROP COLUMN IF EXISTS tcp_flags;' in full_sql
    assert 'DROP TABLE IF EXISTS "flow_interface_minute_rollups" CASCADE;' in full_sql
    assert 'ALTER TABLE "endpoints" DROP CONSTRAINT IF EXISTS "ck_endpoints_device_role";' in full_sql
    assert 'ALTER TABLE "endpoints" DROP COLUMN IF EXISTS "device_role";' in full_sql
    assert 'DROP TABLE IF EXISTS "flow_minute_rollups" CASCADE;' in full_sql
    assert 'DROP TABLE IF EXISTS "user_sessions" CASCADE;' in full_sql
    assert 'DROP TABLE IF EXISTS "alert_channels" CASCADE;' in full_sql
    assert 'DROP TABLE IF EXISTS "roles" CASCADE;' in full_sql


def test_cli_subcommands(capsys):
    """Test CLI commands heads, history, and upgrade head --sql via cli_main."""
    from app.core.migrator import cli_main

    # 1. Test heads
    cli_main(["heads"])
    captured = capsys.readouterr()
    assert "0013 (head)" in captured.out

    # 2. Test history
    cli_main(["history"])
    captured = capsys.readouterr()
    assert "0001" in captured.out
    assert "0013 (head)" in captured.out

    # 3. Test upgrade head --sql
    cli_main(["upgrade", "head", "--sql"])
    captured = capsys.readouterr()
    assert "CREATE TABLE roles" in captured.out
    assert "CREATE TABLE users" in captured.out
    assert "flow_interface_minute_rollups" in captured.out
    assert "flow_minute_rollups" in captured.out


def test_migrator_cli_subprocess_invocation():
    """Verify that invoking `python -m app.core.migrator upgrade --sql` in a fresh

    subprocess properly resolves the active migration context without RuntimeError.
    """
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "app.core.migrator", "upgrade", "--sql"],
        cwd=str(backend_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Process failed with stderr: {result.stderr}"
    assert "CREATE TABLE roles" in result.stdout
    assert "ERROR: op.create_table() called outside an active migration context" not in result.stderr

