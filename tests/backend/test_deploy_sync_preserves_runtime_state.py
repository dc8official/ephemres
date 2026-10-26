"""install.sh/upgrade.sh sync the repo into /opt/ephemres with rsync --delete; the venv and other
runtime state living only there must survive (an upgrade once deleted /opt/ephemres/venv/bin/pip)."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _excludes(script: str) -> list[str]:
    return re.findall(r"--exclude='([^']+)'", (ROOT / "deploy" / script).read_text(encoding="utf-8"))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_sync_excludes_are_anchored_and_cover_runtime_state(script: str):
    excludes = _excludes(script)
    # Unanchored names also match inside the venv (e.g. site-packages/*/tests) and split deletions
    assert excludes and all(e.startswith("/") for e in excludes), excludes
    for path in ("/venv", "/.env", "/backend/.env", "/legacy_password_resets.txt"):
        assert path in excludes


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_sync_keeps_venv_and_removes_stale_code(tmp_path: Path, script: str):
    src, dst = tmp_path / "src", tmp_path / "opt"
    (src / "backend" / "app").mkdir(parents=True)
    (src / "backend" / "app" / "main.py").write_text("new")
    (src / "tests").mkdir()
    (src / "tests" / "test_x.py").write_text("")
    vendored = dst / "venv" / "lib" / "python3.12" / "site-packages"
    (vendored / "pip" / "_vendor" / "certifi").mkdir(parents=True)
    (vendored / "pip" / "_vendor" / "certifi" / "cacert.pem").write_text("pem")
    (vendored / "somepkg" / "tests").mkdir(parents=True)
    (vendored / "somepkg" / "tests" / "t.py").write_text("")
    (dst / "venv" / "bin").mkdir(parents=True)
    (dst / "venv" / "bin" / "pip").write_text("#!/bin/sh\n")
    (dst / "legacy_password_resets.txt").write_text("admin\ttemp\n")
    (dst / "backend" / "app").mkdir(parents=True)
    (dst / "backend" / "app" / "stale.py").write_text("old")

    args = [f"--exclude={e}" for e in _excludes(script)]
    subprocess.run(["rsync", "-a", "--delete", *args, f"{src}/", f"{dst}/"], check=True, capture_output=True)

    assert (dst / "venv" / "bin" / "pip").is_file()
    assert (vendored / "pip" / "_vendor" / "certifi" / "cacert.pem").is_file()
    assert (dst / "legacy_password_resets.txt").is_file()
    assert (dst / "backend" / "app" / "main.py").read_text() == "new"
    assert not (dst / "backend" / "app" / "stale.py").exists()
    assert not (dst / "tests").exists()


def test_broken_venv_is_rebuilt_not_reused():
    upgrade_sh = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
    install_sh = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert '[[ ! -x "${VENV_PATH}/bin/python3" || ! -x "${VENV_PATH}/bin/pip" ]]' in upgrade_sh
    assert '"${VENV_PYTHON}" -m venv --clear "${VENV_PATH}"' in upgrade_sh
    # The rebuild happens before the first pip call
    assert upgrade_sh.index("-m venv --clear") < upgrade_sh.index('"${VENV_PATH}/bin/pip" install')
    assert 'if [ ! -x "$VENV_DIR/bin/python3" ] || [ ! -x "$VENV_DIR/bin/pip" ]; then' in install_sh
