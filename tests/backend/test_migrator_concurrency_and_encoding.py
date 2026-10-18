import urllib.parse
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from app.core.migrator import get_db_url, upgrade_database, downgrade_database, MigrationRevision


def test_get_db_url_password_encoding_roundtrips(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("EPHEMRES_DATABASE_URL", raising=False)
    special_pw = "p@ss:w/rd#"
    special_user = "user:with@special/chars"

    # Test via settings.database
    try:
        from app.config import settings
        monkeypatch.setattr(settings.database, "user", special_user)
        monkeypatch.setattr(settings.database, "password", special_pw)
        monkeypatch.setattr(settings.database, "host", "db.example.com")
        monkeypatch.setattr(settings.database, "port", 5432)
        monkeypatch.setattr(settings.database, "name", "ephemres_db")

        url = get_db_url()
        parsed = urllib.parse.urlparse(url)

        assert parsed.scheme == "postgresql"
        assert parsed.hostname == "db.example.com"
        assert parsed.port == 5432
        assert parsed.path == "/ephemres_db"
        assert urllib.parse.unquote(parsed.username) == special_user
        assert urllib.parse.unquote(parsed.password) == special_pw
    except ImportError:
        pass

    # Test via environment variables fallback
    with patch.dict("sys.modules", {"app.config": None}):
        monkeypatch.setenv("EPHEMRES_DB_USER", special_user)
        monkeypatch.setenv("EPHEMRES_DB_PASSWORD", special_pw)
        monkeypatch.setenv("EPHEMRES_DB_HOST", "db.example.com")
        monkeypatch.setenv("EPHEMRES_DB_PORT", "5432")
        monkeypatch.setenv("EPHEMRES_DB_NAME", "ephemres_db")

        url = get_db_url()
        parsed = urllib.parse.urlparse(url)

        assert parsed.scheme == "postgresql"
        assert parsed.hostname == "db.example.com"
        assert parsed.port == 5432
        assert parsed.path == "/ephemres_db"
        assert urllib.parse.unquote(parsed.username) == special_user
        assert urllib.parse.unquote(parsed.password) == special_pw


@pytest.mark.anyio
async def test_upgrade_database_advisory_lock():
    fake_conn = AsyncMock()
    fake_tx = AsyncMock()
    fake_conn.transaction = MagicMock()
    fake_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=fake_tx)
    fake_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

    rev = MigrationRevision(
        revision="rev1",
        down_revision=None,
        file_path="",
        module_name="test_mod",
        description="first",
        upgrade_fn=lambda: None,
        downgrade_fn=lambda: None,
    )

    with patch("app.core.migrator.get_db_connection", AsyncMock(return_value=fake_conn)), \
         patch("app.core.migrator.find_versions_dir", return_value="fake_dir"), \
         patch("app.core.migrator.load_revisions", return_value=[rev]), \
         patch("app.core.migrator.get_current_revision", AsyncMock(return_value=None)), \
         patch("app.core.migrator.set_current_revision", AsyncMock()):

        await upgrade_database(target="rev1")

        calls = [c[0][0] for c in fake_conn.execute.call_args_list]
        assert "SELECT pg_advisory_lock(727274)" in calls
        assert "SELECT pg_advisory_unlock(727274)" in calls
        assert calls.index("SELECT pg_advisory_lock(727274)") < calls.index("SELECT pg_advisory_unlock(727274)")


@pytest.mark.anyio
async def test_downgrade_database_advisory_lock_on_error():
    fake_conn = AsyncMock()

    rev = MigrationRevision(
        revision="rev1",
        down_revision=None,
        file_path="",
        module_name="test_mod",
        description="first",
        upgrade_fn=lambda: None,
        downgrade_fn=lambda: None,
    )

    with patch("app.core.migrator.get_db_connection", AsyncMock(return_value=fake_conn)), \
         patch("app.core.migrator.find_versions_dir", return_value="fake_dir"), \
         patch("app.core.migrator.load_revisions", return_value=[rev]), \
         patch("app.core.migrator.get_current_revision", AsyncMock(side_effect=RuntimeError("DB query failure"))):

        with pytest.raises(RuntimeError, match="DB query failure"):
            await downgrade_database(target="base")

        calls = [c[0][0] for c in fake_conn.execute.call_args_list]
        assert "SELECT pg_advisory_lock(727274)" in calls
        assert "SELECT pg_advisory_unlock(727274)" in calls
