"""The runtime dependencies are exactly the ones Ephemres uses, pinned and hashed (scale and core
review M5).

requirements.txt names the packages; requirements.lock, which the servers install with
`pip install --require-hashes`, pins every package (dependencies included) with its file
hashes. A package appearing in the lock, a version drifting between the two files, or a missing
hash fails here before it reaches a server.
"""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"

# What Ephemres runs on. Adding to these sets is a decision: see the header of requirements.txt.
DIRECT = {"uvicorn", "httptools", "uvloop", "asyncpg"}
LOCKED = DIRECT | {"click", "h11"}  # uvicorn's own dependencies


def _direct_pins() -> dict:
    pins = {}
    for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "[" not in line, f"no extras: name each package instead ({line})"
        name, sep, version = line.partition("==")
        assert sep and version.strip(), f"requirement not pinned with ==: {line}"
        pins[name.strip().lower()] = version.strip()
    return pins


def _locked_pins() -> dict:
    """{name: (version, number of hashes)} from requirements.lock."""
    text = (BACKEND / "requirements.lock").read_text(encoding="utf-8")
    pins = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        head = block.splitlines()[0].strip()
        if head.startswith("#") or "==" not in head:
            continue
        name, _, version = head.rstrip(" \\").partition("==")
        pins[name.strip().lower()] = (version.strip(), block.count("--hash=sha256:"))
    return pins


def test_requirements_txt_names_exactly_the_runtime_packages():
    assert set(_direct_pins()) == DIRECT


def test_the_lock_holds_exactly_these_packages_each_hashed():
    locked = _locked_pins()
    assert set(locked) == LOCKED
    for name, (version, hashes) in locked.items():
        assert version, name
        assert hashes >= 1, f"{name} has no --hash line"


def test_the_lock_matches_the_versions_in_requirements_txt():
    locked = _locked_pins()
    for name, version in _direct_pins().items():
        assert locked[name][0] == version, f"{name}: requirements.txt {version}, lock {locked[name][0]}"


def test_the_tests_run_on_the_locked_versions():
    # The servers install the lock; a development venv built from requirements-dev.txt alone
    # resolved uvicorn's dependencies afresh and ran the suite on click 8.5.0 while the lock
    # said 8.5.1 (branch review, 2026-11-10). Passing here means the suite ran on what ships.
    from importlib.metadata import PackageNotFoundError, version

    differing = []
    for name, (locked, _hashes) in sorted(_locked_pins().items()):
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = "not installed"
        if installed != locked:
            differing.append(f"{name}: {installed} here, {locked} in the lock")
    assert not differing, (
        "this environment differs from backend/requirements.lock ("
        + "; ".join(differing)
        + "). Run: pip install --require-hashes -r backend/requirements.lock"
    )
