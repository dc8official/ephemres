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
