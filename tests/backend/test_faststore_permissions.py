import logging
import os
import stat
import pytest
from app.core.faststore import (
    FastStoreClient,
    FastStoreError,
    _SharedSQLiteStore,
    _get_faststore_dir,
    _get_db_path,
)


def test_faststore_dir_priority(tmp_path, monkeypatch):
    custom_dir = str(tmp_path / "custom_faststore")
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", custom_dir)
    assert _get_faststore_dir() == custom_dir
    assert os.path.isdir(custom_dir)


def test_insecure_mode_rejected(tmp_path, monkeypatch):
    custom_dir = str(tmp_path / "insecure_store")
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", custom_dir)
    db_path = _get_db_path(88)

    # Pre-create file with mode 0666 (world/group writable)
    with open(db_path, "w") as f:
        f.write("")
    os.chmod(db_path, 0o666)

    # Clear cached instances
    _SharedSQLiteStore._instances.pop(88, None)

    with pytest.raises(FastStoreError, match="insecure permissions"):
        FastStoreClient(db=88)


def test_new_file_created_with_0600(tmp_path, monkeypatch):
    custom_dir = str(tmp_path / "secure_store")
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", custom_dir)
    db_path = _get_db_path(89)

    _SharedSQLiteStore._instances.pop(89, None)

    client = FastStoreClient(db=89)
    assert os.path.exists(db_path)
    file_stat = os.stat(db_path)
    # Mode should be 0600 (only user read/write)
    file_mode = stat.S_IMODE(file_stat.st_mode)
    assert file_mode == 0o600
    assert file_mode & 0o077 == 0  # No group or other access


def test_ignored_params_warning(caplog):
    import app.core.faststore as faststore_mod
    faststore_mod._WARNED_PARAMS = False

    with caplog.at_level(logging.WARNING):
        FastStoreClient(db=90, password="secret_ignored")
    assert any("ignored" in r.message for r in caplog.records)

    # Second call should not warn again (one-time warning)
    caplog.clear()
    FastStoreClient(db=91, host="10.0.0.1")
    assert not any("ignored" in r.message for r in caplog.records)
