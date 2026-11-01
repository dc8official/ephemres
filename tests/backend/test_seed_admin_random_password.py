import pytest
from unittest.mock import AsyncMock, MagicMock
from app.seed_admin import seed_admin
import app.seed_admin as seed_mod


@pytest.mark.anyio
async def test_seed_admin_generates_readable_password_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("DEFAULT_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    fake_db = AsyncMock()
    # Mock role query returning an ADMIN role row
    role_row = MagicMock()
    role_row.id = "00000000-0000-0000-0000-000000000001"
    role_result = MagicMock()
    role_result.fetchone.return_value = role_row

    # Mock user query returning None (new user)
    user_result = MagicMock()
    user_result.fetchone.return_value = None

    async def fake_execute(stmt, params=None):
        sql = str(stmt)
        if "FROM roles" in sql:
            return role_result
        if "FROM users" in sql:
            return user_result
        return MagicMock()

    fake_db.execute = fake_execute

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(seed_mod, "AsyncSessionLocal", lambda: FakeSessionContext())

    success = await seed_admin()
    assert success is True

    captured = capsys.readouterr().out
    assert "[seed] Initial admin password generated: " in captured
    gen_pwd = captured.split("[seed] Initial admin password generated: ")[1].strip()
    assert len(gen_pwd) > 0
    assert gen_pwd != "admin"


@pytest.mark.anyio
async def test_seed_admin_does_not_generate_password_when_admin_exists_and_unforced(monkeypatch, capsys):
    monkeypatch.delenv("DEFAULT_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("FORCE_RESET_ADMIN", raising=False)

    fake_db = AsyncMock()
    role_row = MagicMock()
    role_row.id = "00000000-0000-0000-0000-000000000001"
    role_result = MagicMock()
    role_result.fetchone.return_value = role_row

    existing_user_row = MagicMock()
    existing_user_row.id = "00000000-0000-0000-0000-000000000002"
    existing_user_row.password_hash = "$scrypt$ln=16,r=8,p=1$..."
    existing_user_row.must_change_password = False
    user_result = MagicMock()
    user_result.fetchone.return_value = existing_user_row

    async def fake_execute(stmt, params=None):
        sql = str(stmt)
        if "FROM roles" in sql:
            return role_result
        if "FROM users" in sql:
            return user_result
        return MagicMock()

    fake_db.execute = fake_execute

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(seed_mod, "AsyncSessionLocal", lambda: FakeSessionContext())

    success = await seed_admin()
    assert success is True

    captured = capsys.readouterr().out
    assert "[seed] Initial admin password generated: " not in captured
    assert "[seed] Admin user 'admin' already exists — skipping." in captured



@pytest.mark.anyio
async def test_seed_admin_if_missing_leaves_an_existing_legacy_admin_alone(monkeypatch, capsys):
    # Upgrades seed with if_missing: the install-time default ("admin" on v3.2/v3.3.0) must not
    # replace the admin password; app.reset_legacy_passwords gives it a random one instead.
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "admin")

    role_row = MagicMock()
    role_row.id = "00000000-0000-0000-0000-000000000001"
    role_result = MagicMock()
    role_result.fetchone.return_value = role_row
    existing_user_row = MagicMock()
    existing_user_row.password_hash = "$argon2id$v=19$m=65536,t=3,p=4$..."
    existing_user_row.must_change_password = False
    user_result = MagicMock()
    user_result.fetchone.return_value = existing_user_row
    statements = []

    async def fake_execute(stmt, params=None):
        sql = str(stmt)
        statements.append(sql)
        if "FROM roles" in sql:
            return role_result
        if "FROM users" in sql:
            return user_result
        return MagicMock()

    fake_db = AsyncMock()
    fake_db.execute = fake_execute

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(seed_mod, "AsyncSessionLocal", lambda: FakeSessionContext())

    assert await seed_admin("admin", if_missing=True) is True
    assert not any("UPDATE users" in sql or "INSERT INTO users" in sql for sql in statements)
    assert "[seed] Admin user 'admin' already exists — skipping." in capsys.readouterr().out
