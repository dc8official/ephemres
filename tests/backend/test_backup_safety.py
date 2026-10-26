"""Backup → purge → restore safety: a backup only counts once pg_dump finished it, uninstall.sh
never purges without one, the encryption key travels with it, and restore refuses cut-off dumps."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Dict, List

import pytest

from app.core.restore import (
    IncompleteBackupError,
    backup_dump_is_complete,
    discover_old_secret_key,
    reconcile_and_restore_database,
)
from tests.backend.test_backup_restore import MockDbConnection, PG_DUMP_TRAILER

ROOT = Path(__file__).resolve().parents[2]
BACKUP_LIB = ROOT / "deploy" / "backup_lib.sh"
UNINSTALL_SH = ROOT / "deploy" / "uninstall.sh"

# As root, backup_create retries through `sudo -u postgres pg_dump`, which would bypass the fake
running_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="superuser fallback would run the real pg_dump")


def _fake_pg_dump(bin_dir: Path, body: str) -> None:
    """A pg_dump stand-in that writes `body` to its -f file (or stdout) and runs the given shell."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "pg_dump"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -f ]] && out="$2"; shift; done\n'
        f"{body}\n"
    )
    script.chmod(0o755)


def _run_bash(script: str, bin_dir: Path, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(bin_dir)}
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env)


COMPLETE_DUMP = 'printf "COPY public.users (id) FROM stdin;\\n\\\\.\\n\\n--\\n-- PostgreSQL database dump complete\\n--\\n" > "$out"'


@running_as_root
def test_backup_create_keeps_only_a_complete_dump(tmp_path: Path):
    _fake_pg_dump(tmp_path / "bin", COMPLETE_DUMP)
    backup = tmp_path / "b.sql"

    proc = _run_bash(f'source "{BACKUP_LIB}"; backup_create "{backup}" netmon netmon_user pw 127.0.0.1 5432', tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert backup.is_file() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "bin") == ["b.sql"]


@running_as_root
@pytest.mark.parametrize(
    "body, expected_error",
    [
        # pg_dump fails outright: its own error must reach the operator
        ('echo "pg_dump: error: FATAL:  password authentication failed for user \\"netmon_user\\"" >&2; exit 1',
         "password authentication failed"),
        # pg_dump "succeeds" but the dump stops early: no completion trailer
        ('printf "COPY _timescaledb_internal._hyper_1_1_chunk (id) FROM stdin;\\n" > "$out"', "completion trailer"),
    ],
)
def test_backup_create_rejects_failed_or_cut_off_dumps(tmp_path: Path, body: str, expected_error: str):
    _fake_pg_dump(tmp_path / "bin", body)
    backup = tmp_path / "b.sql"

    proc = _run_bash(f'source "{BACKUP_LIB}"; backup_create "{backup}" netmon netmon_user pw 127.0.0.1 5432', tmp_path / "bin")

    assert proc.returncode == 1
    assert expected_error in proc.stderr
    # Nothing is left behind for restore.sh to offer
    assert [p.name for p in tmp_path.iterdir() if p.name != "bin"] == []


def test_backup_save_key_writes_a_private_key_file(tmp_path: Path):
    backup = tmp_path / "b.sql"
    backup.write_text("")

    proc = _run_bash(
        f'source "{BACKUP_LIB}"; backup_save_key "{backup}" /nonexistent ""',
        tmp_path,
        {"EPHEMRES_SECRET_KEY": "old-install-key"},
    )

    key_file = tmp_path / "b.sql.key"
    assert proc.returncode == 0, proc.stderr
    assert key_file.read_text() == "EPHEMRES_SECRET_KEY=old-install-key\n"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    # And without any key it reports failure instead of writing an empty file
    proc = _run_bash(f'source "{BACKUP_LIB}"; backup_save_key "{tmp_path}/c.sql" /nonexistent ""', tmp_path)
    assert proc.returncode == 1 and not (tmp_path / "c.sql.key").exists()


def _uninstall_step1(tmp_path: Path, purge: bool, force: bool) -> subprocess.CompletedProcess[str]:
    """Runs uninstall.sh's real backup step and purge guard against a temporary backup dir."""
    content = UNINSTALL_SH.read_text(encoding="utf-8")
    step1 = content[content.index("# 1. Final Safety Database Backup"):content.index("# 2. Stop and Disable Systemd Services")]
    step1 = step1.replace('BACKUP_DIR="/var/backups/ephemres"', f'BACKUP_DIR="{tmp_path}/backups"')
    script = (
        f'set -euo pipefail; source "{BACKUP_LIB}"; GREEN=; YELLOW=; RED=; BLUE=; NC=\n'
        f"PURGE_DATA={str(purge).lower()}; FORCE_PURGE={str(force).lower()}\n"
        "DB_NAME=netmon; DB_USER=netmon_user; DB_PASS=pw; DB_HOST=127.0.0.1; DB_PORT=5432\n"
        f"{step1}\n"
        'echo "REACHED_STEP_2 BACKUP_OK=${BACKUP_OK}"\n'
    )
    return _run_bash(script, tmp_path / "bin", {"EPHEMRES_SECRET_KEY": "k"})


@running_as_root
def test_uninstall_refuses_to_purge_without_a_backup(tmp_path: Path):
    _fake_pg_dump(tmp_path / "bin", 'echo "pg_dump: error: connection refused" >&2; exit 1')

    refused = _uninstall_step1(tmp_path, purge=True, force=False)
    assert refused.returncode == 1
    assert "connection refused" in refused.stderr and "will not be purged" in refused.stderr
    assert "REACHED_STEP_2" not in refused.stdout

    # Keeping the database is still allowed, and --force-purge is an explicit override
    assert "REACHED_STEP_2 BACKUP_OK=false" in _uninstall_step1(tmp_path, purge=False, force=False).stdout
    assert "REACHED_STEP_2 BACKUP_OK=false" in _uninstall_step1(tmp_path, purge=True, force=True).stdout


@running_as_root
def test_uninstall_saves_backup_and_key_before_purging(tmp_path: Path):
    _fake_pg_dump(tmp_path / "bin", COMPLETE_DUMP)

    proc = _uninstall_step1(tmp_path, purge=True, force=False)

    assert "REACHED_STEP_2 BACKUP_OK=true" in proc.stdout, proc.stderr
    backups = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert len(backups) == 2 and backups[0].endswith(".sql") and backups[1] == backups[0] + ".key"


def test_uninstall_banner_and_prompt_respect_backup_state():
    content = UNINSTALL_SH.read_text(encoding="utf-8")
    assert "netmon_secure_password" not in content
    banner = content[content.index("[UNINSTALL COMPLETE]"):]
    assert 'if [[ "${BACKUP_OK}" == true ]]; then' in banner
    assert "No safety database backup was created." in banner
    # The interactive purge question is only asked when a backup exists
    prompt = content[content.index("# 4. Optional Application Directory"):content.index('if [[ "${PURGE_DATA}" == true ]]; then')]
    assert prompt.index('if [[ "${BACKUP_OK}" != true ]]; then') < prompt.index("Do you want to delete the PostgreSQL database")


def test_upgrade_uses_the_verified_backup_helper():
    content = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
    assert 'source "${SCRIPT_DIR}/backup_lib.sh"' in content
    assert 'backup_create "${BACKUP_FILE}"' in content
    assert 'backup_save_key "${BACKUP_FILE}"' in content
    assert not re.search(r"^\s*(PGPASSWORD=\S+ )?pg_dump ", content, re.MULTILINE)


# ---------------------------------------------------------------------------
# Restore side
# ---------------------------------------------------------------------------
def test_backup_dump_is_complete_plain_and_gzip(tmp_path: Path):
    body = "COPY public.users (id) FROM stdin;\n\\.\n"
    complete, cut = tmp_path / "ok.sql", tmp_path / "cut.sql"
    complete.write_text(body + PG_DUMP_TRAILER)
    cut.write_text(body)
    assert backup_dump_is_complete(complete) and not backup_dump_is_complete(cut)

    gz_ok, gz_truncated = tmp_path / "ok.sql.gz", tmp_path / "truncated.sql.gz"
    with gzip.open(gz_ok, "wt") as f:
        f.write(body * 5000 + PG_DUMP_TRAILER)
    gz_truncated.write_bytes(gz_ok.read_bytes()[: gz_ok.stat().st_size // 2])
    assert backup_dump_is_complete(gz_ok) and not backup_dump_is_complete(gz_truncated)


@pytest.mark.anyio
async def test_restore_refuses_a_cut_off_dump_before_writing(tmp_path: Path):
    dump = tmp_path / "cut.sql"
    dump.write_text("COPY public.endpoints (id) FROM stdin;\ne0000000-0000-0000-0000-000000000010\n\\.\n")
    conn = MockDbConnection({"endpoints": {"id": "uuid"}})

    with pytest.raises(IncompleteBackupError):
        await reconcile_and_restore_database(conn=conn, backup_path=dump)
    assert conn.executed_queries == []

    summary = await reconcile_and_restore_database(conn=conn, backup_path=dump, allow_incomplete=True)
    assert summary["endpoint_count"] == 1


def test_discover_old_secret_key_prefers_the_backup_key_file(tmp_path: Path):
    backup = tmp_path / "ephemres_decommission_backup_1.sql"
    legacy = tmp_path / "netmon.env"
    legacy.write_text("NETMON_DB_PASSWORD=x\nNETMON_SECRET_KEY='legacy-key'\n")

    # Old installs only: the prefixed, quoted variable is read
    assert discover_old_secret_key(backup, [legacy]) == ("legacy-key", str(legacy))

    Path(f"{backup}.key").write_text("EPHEMRES_SECRET_KEY=backup-key\n")
    assert discover_old_secret_key(backup, [legacy]) == ("backup-key", f"{backup}.key")

    assert discover_old_secret_key(tmp_path / "other.sql", []) == (None, None)


class AggregateMockDbConnection(MockDbConnection):
    """Mock connection whose TimescaleDB catalog lists continuous aggregates."""

    AGGREGATES = [
        # Listed before its source on purpose: stacked aggregates must wait for theirs
        {"view_name": "flow_weekly", "hypertable_name": "_materialized_hypertable_5", "materialization_hypertable_name": "_materialized_hypertable_9"},
        {"view_name": "flow_hourly_rollups", "hypertable_name": "flow_minute_rollups", "materialization_hypertable_name": "_materialized_hypertable_5"},
        {"view_name": "node_historical_baselines_cagg", "hypertable_name": "endpoint_events", "materialization_hypertable_name": "_materialized_hypertable_7"},
    ]

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        if "timescaledb_information.continuous_aggregates" in query:
            self.executed_queries.append(query)
            return [
                {
                    "view_schema": "public",
                    "hypertable_schema": "public" if not a["hypertable_name"].startswith("_") else "_timescaledb_internal",
                    "materialization_hypertable_schema": "_timescaledb_internal",
                    **a,
                }
                for a in self.AGGREGATES
            ]
        return await super().fetch(query, *args)


@pytest.mark.anyio
async def test_restore_rebuilds_aggregates_of_restored_hypertables(tmp_path: Path):
    dump = tmp_path / "flows.sql"
    dump.write_text(
        "COPY public.flow_minute_rollups (bucket, exporter_id, src_ip, dst_ip, protocol, dst_port, bytes) FROM stdin;\n"
        "2026-09-01 00:00:00+00\te0000000-0000-0000-0000-000000000010\t10.0.0.1\t10.0.0.2\t6\t443\t100\n"
        "\\.\n" + PG_DUMP_TRAILER
    )
    conn = AggregateMockDbConnection({
        "flow_minute_rollups": {"bucket": "timestamptz", "exporter_id": "uuid", "src_ip": "inet", "dst_ip": "inet", "protocol": "int2", "dst_port": "int4", "bytes": "int8"},
    })

    summary = await reconcile_and_restore_database(conn=conn, backup_path=dump)

    calls = [q for q in conn.executed_queries if q.startswith("CALL refresh_continuous_aggregate")]
    assert calls == [
        """CALL refresh_continuous_aggregate('"public"."flow_hourly_rollups"', NULL, NULL);""",
        """CALL refresh_continuous_aggregate('"public"."flow_weekly"', NULL, NULL);""",
    ]
    # endpoint_events was not restored, so its baselines are left to their policy
    assert summary["aggregates_refreshed"] == ["flow_hourly_rollups", "flow_weekly"]
