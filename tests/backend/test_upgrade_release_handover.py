"""upgrade.sh fetches the right release before anything stops, hands over to that release's own
script, refuses a downgrade, and brings stopped services back when it fails.

The v3.3.5 -> v3.3.6 upgrade test (2026-11-14, in a systemd container) found the documented
command fetching v3.3.5 again (U1), the old script running the new release without its new steps
(U2), services left stopped after a failure (U3), and the clone failing in a noexec /tmp (U4).
These run the script's own functions in bash with git, systemctl and apt stubbed or local.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPGRADE = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")


def _function(name):
    start = UPGRADE.index(f"{name}() {{")
    return UPGRADE[start:UPGRADE.index("\n}\n", start) + 3]


def _bash(script, **env):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env})


def _tree(path, version, script_body="echo running"):
    (path / "backend" / "app").mkdir(parents=True)
    (path / "deploy").mkdir()
    (path / "backend" / "app" / "version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    (path / "deploy" / "upgrade.sh").write_text(script_body + "\n", encoding="utf-8")


PRELUDE = """
set -euo pipefail
GREEN=; YELLOW=; RED=; BLUE=; NC=
apt-get() { :; }
dpkg() { echo "ii"; }
"""


def test_one_version_everywhere():
    version = re.search(r'^VERSION = "([0-9.]+)"', (ROOT / "backend/app/version.py").read_text(), re.M).group(1)
    assert re.search(r'^RELEASE_VERSION="([0-9.]+)"', UPGRADE, re.M).group(1) == version
    assert json.loads((ROOT / "frontend/package.json").read_text())["version"] == version
    # No release is named by hand where the version should be (comments may mention one).
    for path in ("backend/app/main.py", "backend/app/core/pdf.py", "backend/app/services/alert_formatters.py",
                 "backend/app/routers/events.py", "backend/app/core/asgi.py",
                 "frontend/src/App.vue", "frontend/src/views/LoginView.vue", "frontend/index.html"):
        text = (ROOT / path).read_text()
        assert not re.search(r'Ephemres[^"\n]* v3\.\d+\.\d+|"3\.3\.\d+"|\'v3\.\d+\.\d+\'', text), path


def test_the_release_is_fetched_and_built_before_anything_stops():
    fetch = UPGRADE.index("    fetch_release\n")
    build = UPGRADE.index("npm run build")
    download = UPGRADE.index('"${PACKAGE_PIP[@]}" download --quiet --require-hashes')
    backup = UPGRADE.index("--- Step 1/7: Executing Pre-Upgrade Database Backup ---")
    first_stop = UPGRADE.index("    pause_services\n")
    assert fetch < build < download < backup < first_stop
    # The default is this script's release, never a fixed older branch (U1) ...
    assert 'UPGRADE_BRANCH="${EPHEMRES_BRANCH:-${CURRENT_GIT_BRANCH:-v${RELEASE_VERSION}}}"' in UPGRADE
    assert not re.search(r"(checkout|--branch|pull origin) \"?v3\.3\.(5|2v|1n)", UPGRADE)
    # ... the clone is not staged in /tmp (U4), and every stop goes through pause_services (U3).
    assert "/tmp/ephemres-upgrade-stage" not in UPGRADE
    assert 'stage_root="${EPHEMRES_UPGRADE_STAGE_ROOT:-/var/lib/ephemres}"' in UPGRADE
    assert not re.search(r"^\s*systemctl stop ephemres-engine", UPGRADE, re.M)
    assert "trap on_exit EXIT" in UPGRADE


def _fetch(tmp_path, installed, fetched, script_body, **env):
    """Runs fetch_release from a copy without .git against a local repository holding `fetched`."""
    repo = tmp_path / "repo"
    _tree(repo, fetched, script_body)
    subprocess.run(["git", "init", "-q", "-b", f"v{fetched}", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "r"], check=True)
    install = tmp_path / "install"
    if installed:
        _tree(install, installed)
    else:
        install.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    script = PRELUDE + f"""
PROJECT_ROOT="{project}"; INSTALL_DIR="{install}"; REPO_URL="{repo}"; UPGRADE_BRANCH="v{fetched}"
SELF_HASH=not-the-same; ORIGINAL_ARGS=(--dry-run-flag-passed-on)
""" + _function("tree_version") + _function("fetch_release") + 'fetch_release\necho "no hand-over: $SOURCE_DIR"\n'
    return _bash(script, EPHEMRES_UPGRADE_STAGE_ROOT=str(tmp_path / "stage"), **env)


def test_an_older_script_hands_over_to_the_fetched_release(tmp_path):
    out = _fetch(tmp_path, "3.3.6", "3.3.7",
                 'echo "new script: source=$EPHEMRES_UPGRADE_SOURCE branch=$EPHEMRES_BRANCH args=$*"')
    assert out.returncode == 0, out.stderr
    assert "Upgrading v3.3.6 to v3.3.7" in out.stdout
    line = next(l for l in out.stdout.splitlines() if l.startswith("new script:"))
    assert f"source={tmp_path}/stage/upgrade-stage-" in line
    assert "branch=v3.3.7 args=--dry-run-flag-passed-on" in line
    assert "no hand-over" not in out.stdout


def test_a_server_from_before_v336_upgrades(tmp_path):
    # v3.3.5 and older have no version.py: anything with one is newer.
    out = _fetch(tmp_path, None, "3.3.6", "echo handed over")
    assert out.returncode == 0, out.stderr
    assert "Upgrading v3.3.5 or older to v3.3.6" in out.stdout


def test_a_downgrade_is_refused_before_anything_changes(tmp_path):
    out = _fetch(tmp_path, "3.3.7", "3.3.6", "echo should not run")
    assert out.returncode == 1
    assert "v3.3.6 holds v3.3.6, which is older than the installed v3.3.7. Nothing was changed." in out.stderr
    assert "should not run" not in out.stdout


def test_a_downgrade_goes_ahead_only_when_asked_for(tmp_path):
    out = _fetch(tmp_path, "3.3.7", "3.3.6", "echo handed over", EPHEMRES_ALLOW_DOWNGRADE="1")
    assert out.returncode == 0, out.stderr
    assert "handed over" in out.stdout


def test_the_handed_over_script_uses_the_fetched_tree_without_fetching_again(tmp_path):
    source = tmp_path / "fetched"
    _tree(source, "3.3.7")
    install = tmp_path / "install"
    _tree(install, "3.3.6")
    script = PRELUDE + f"""
PROJECT_ROOT="{tmp_path}/nowhere"; INSTALL_DIR="{install}"; REPO_URL=/nonexistent; UPGRADE_BRANCH=v3.3.7
SELF_HASH=x; ORIGINAL_ARGS=()
git() {{ echo "git must not run" >&2; return 1; }}
""" + _function("tree_version") + _function("fetch_release") + 'fetch_release\necho "source=$SOURCE_DIR stage=$STAGE_DIR"\n'
    out = _bash(script, EPHEMRES_UPGRADE_SOURCE=str(source), EPHEMRES_UPGRADE_STAGE_DIR=str(source))
    assert out.returncode == 0, out.stderr
    assert f"source={source} stage={source}" in out.stdout
    assert "git must not run" not in out.stderr


def _failure(stage_flags):
    """Pauses two running units, then fails with the given progress flags set."""
    return PRELUDE + """
PAUSED_UNITS=(); CODE_SYNCED=0; DB_MIGRATED=0; UPGRADE_FINISHED=0; STAGE_DIR=""; PACKAGE_DIR=""
BACKUP_FILE=/var/backups/ephemres/x.sql; INSTALL_DIR=/opt/ephemres
systemctl() {
    case "$1" in
        is-active) [[ "$3" == ephemres-api || "$3" == ephemres-engine ]] ;;
        *) echo "systemctl $*" ;;
    esac
}
""" + _function("pause_services") + _function("on_exit") + f"""
trap on_exit EXIT
pause_services
{stage_flags}
false
"""


def test_a_failure_before_the_code_changes_restarts_what_was_stopped():
    out = _bash(_failure(""))
    assert out.returncode == 1
    assert "systemctl stop ephemres-engine" in out.stdout and "systemctl stop ephemres-api" in out.stdout
    assert "systemctl start ephemres-engine ephemres-api" in out.stdout
    assert "restarting ephemres-engine ephemres-api" in out.stderr


def test_a_failure_after_the_migration_starts_the_new_services():
    out = _bash(_failure("CODE_SYNCED=1; DB_MIGRATED=1"))
    assert "systemctl start ephemres-api ephemres-engine" in out.stdout
    assert "systemctl start ephemres-snmpd" in out.stdout


def test_new_code_on_an_unmigrated_database_stays_stopped_and_says_how_to_recover():
    out = _bash(_failure("CODE_SYNCED=1"))
    assert "systemctl start" not in out.stdout
    assert "the database was not migrated, so the services stay stopped" in out.stderr
    assert "/var/backups/ephemres/x.sql" in out.stderr


def test_a_finished_upgrade_restarts_nothing():
    out = _bash(_failure("UPGRADE_FINISHED=1"))
    assert "systemctl start" not in out.stdout


def test_packages_are_downloaded_before_the_stop_on_a_netmon_server_too():
    # The v3.3.0 -> v3.3.6 test skipped the early download: /var/lib/ephemres didn't exist yet on a
    # netmon server (only the clone path made it), so mktemp failed quietly and the packages were
    # fetched after the stop. The directory is made first, and a Python without pip gets one from
    # a throwaway venv.
    block = UPGRADE[UPGRADE.index("# The release's Python packages, downloaded"):UPGRADE.index("# 3. Pre-Upgrade Database Backup")]
    assert block.index('mkdir -p "${EPHEMRES_UPGRADE_STAGE_ROOT:-/var/lib/ephemres}"') < block.index("mktemp -d")
    assert '-m venv "${PACKAGE_DIR}/.pip-venv"' in block
    assert "step 6 downloads them, after the services stop." in block
