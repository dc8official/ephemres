"""restore --admin-password must do what the restore summary claims, for every backup era."""

from pathlib import Path
from typing import Optional

import pytest

from app.core.restore import reconcile_and_restore_database
from app.services.auth_service import hash_password, verify_password
from tests.backend.test_backup_restore import MockDbConnection

USERS_SCHEMA = {
    "users": {
        "id": "uuid",
        "username": "varchar",
        "password_hash": "text",
        "role_id": "uuid",
        "is_active": "bool",
        "must_change_password": "bool",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    }
}


def _dump(tmp_path: Path, users: list[tuple[str, str]]) -> Path:
    lines = [
        "COPY public.users (id, username, password_hash, role_id, is_active, must_change_password, created_at, updated_at) FROM stdin;"
    ]
    for i, (username, pwd_hash) in enumerate(users, start=1):
        lines.append(
            f"b0000000-0000-0000-0000-00000000000{i}\t{username}\t{pwd_hash}\t\\N\tt\tf\t"
            "2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00"
        )
    lines.append("\\.")
    path = tmp_path / "backup.sql"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _restore(tmp_path: Path, users, admin_password: Optional[str]):
    conn = MockDbConnection(USERS_SCHEMA)
    conn.calls = []
    original_execute = conn.execute

    async def recording_execute(query, *args):
        conn.calls.append((query, args))
        return await original_execute(query, *args)

    conn.execute = recording_execute
    summary = await reconcile_and_restore_database(
        conn=conn, backup_path=_dump(tmp_path, users), admin_password=admin_password
    )
    rows = {r["username"]: r for r in conn.tables["users"]}
    return summary, rows, conn


@pytest.mark.anyio
async def test_supplied_password_applies_to_scrypt_era_admin(tmp_path):
    summary, rows, _ = await _restore(tmp_path, [("admin", hash_password("from-backup"))], "operator-chosen")
    assert summary["admin_password_applied"] is True
    assert verify_password("operator-chosen", rows["admin"]["password_hash"])
    assert rows["admin"]["must_change_password"] is True


@pytest.mark.anyio
async def test_scrypt_era_admin_kept_when_no_password_supplied(tmp_path):
    summary, rows, _ = await _restore(tmp_path, [("admin", hash_password("from-backup"))], None)
    assert summary["admin_password_applied"] is False
    assert summary["admin_temporary_password"] is False
    assert verify_password("from-backup", rows["admin"]["password_hash"])
    assert summary["password_reset_report"] is None


@pytest.mark.anyio
async def test_legacy_admin_without_password_gets_reported_temporary_password(tmp_path):
    summary, rows, _ = await _restore(tmp_path, [("admin", "$argon2id$v=19$m=65536,t=3,p=4$legacy")], None)
    assert summary["admin_temporary_password"] is True
    username, temp = Path(summary["password_reset_report"]).read_text(encoding="utf-8").strip().split("\t")
    assert username == "admin"
    assert verify_password(temp, rows["admin"]["password_hash"])
    assert not verify_password("admin", rows["admin"]["password_hash"])


@pytest.mark.anyio
async def test_supplied_password_applies_to_existing_admin_when_backup_has_none(tmp_path):
    summary, rows, conn = await _restore(tmp_path, [("noc1", hash_password("x"))], "operator-chosen")
    assert "admin" not in rows
    assert summary["admin_password_applied"] is True
    updates = [(q, p) for q, p in conn.calls if q.startswith('UPDATE "users"')]
    assert len(updates) == 1
    assert "\"username\" = 'admin'" in updates[0][0]
    assert verify_password("operator-chosen", updates[0][1][0])


@pytest.mark.anyio
async def test_supplied_password_not_reported_applied_when_no_admin_exists(tmp_path):
    conn = MockDbConnection(USERS_SCHEMA)
    original_execute = conn.execute

    async def execute_with_status(query, *args):
        result = await original_execute(query, *args)
        # asyncpg reports the affected row count in the command status.
        return "UPDATE 0" if query.startswith('UPDATE "users"') else result

    conn.execute = execute_with_status
    summary = await reconcile_and_restore_database(
        conn=conn, backup_path=_dump(tmp_path, [("noc1", hash_password("x"))]), admin_password="operator-chosen"
    )
    assert summary["admin_password_applied"] is False
