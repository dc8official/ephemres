import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent.parent
backend_dir = root_dir / "backend"

if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))


import pytest


@pytest.fixture(autouse=True)
def _isolated_password_reset_report(tmp_path, monkeypatch):
    """Keep restore / legacy-reset credential reports inside the test's tmp dir, never /opt or /tmp."""
    monkeypatch.setenv("EPHEMRES_RESET_REPORT", str(tmp_path / "legacy_password_resets.txt"))
