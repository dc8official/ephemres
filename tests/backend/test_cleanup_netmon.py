"""deploy/cleanup-netmon.sh removes what a netmon-layout upgrade leaves behind, and only that.

upgrade.sh keeps the whole v3.2.0 / v3.3.0 install for a rollback: Redis still running, the
disabled netmon-* units, a sudo rule, /opt/netmon, and /etc/netmon with a second copy of the
database password and secret key. The script is optional and destructive, so these tests pin
what matters most: without --apply nothing changes; it refuses when the Ephemres install does
not stand on its own (no secrets of its own, services down, old units back on, something still
pointing into /opt/netmon); it keeps the live nginx site, a Redis someone else uses, the old
backups unless asked, and any package apt would remove something else with.

The script runs against a fake filesystem (EPHEMRES_CLEANUP_ROOT) with stub systemctl, ss,
getent, pgrep, userdel, dpkg-query and apt-get that read their answers from a state directory
and log every call.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "cleanup-netmon.sh"

STUBS = {
    # active/<svc>, enabled/<svc>, units/<svc> decide the answers; every call is logged.
    "systemctl": r'''
echo "systemctl $*" >> "$STATE/calls"
case "$1" in
  is-active) [ "$2" = "--quiet" ] && shift; [ -e "$STATE/active/$2" ] ;;
  is-enabled) [ "$2" = "--quiet" ] && shift; [ -e "$STATE/enabled/$2" ] ;;
  cat) [ -e "$STATE/units/${2%.service}" ] ;;
  *) exit 0 ;;
esac
''',
    "ss": 'cat "$STATE/ss" 2>/dev/null || true',
    "getent": '[ -e "$STATE/user_$2" ]',
    "pgrep": '[ -e "$STATE/procs_$2" ] && cat "$STATE/procs_$2"',
    "userdel": 'echo "userdel $*" >> "$STATE/calls"',
    "chown": 'echo "chown $*" >> "$STATE/calls"',
    # pkg/<name> marks it installed; apt_extra/<name> lists what else a purge would take.
    "dpkg-query": 'pkg="${@: -1}"; [ -e "$STATE/pkg/$pkg" ] && echo "install ok installed"',
    # apt_extra/<name> lists the packages that depend on <name> (purged with it).
    "apt-get": r'''
if [ "$1" = "-s" ]; then
  shift 2
  for pkg in "$@"; do
    echo "Purg $pkg"
    [ -e "$STATE/apt_extra/$pkg" ] && sed 's/^/Purg /' "$STATE/apt_extra/$pkg"
  done
  exit 0
fi
echo "apt-get $*" >> "$STATE/calls"
''',
}


class Server:
    """A migrated server: Ephemres running, netmon leftovers in place."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "root"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        for d in (self.root, self.state, self.bin):
            d.mkdir()
        for name in ("active", "enabled", "units", "pkg", "apt_extra"):
            (self.state / name).mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text("#!/usr/bin/env bash\n" + body)
            stub.chmod(0o755)

        # As upgrade.sh writes them: the env file names its netmon source in a comment.
        self.write("etc/ephemres/ephemres.env", "# Migrated from /etc/netmon/netmon.env by upgrade.sh\n"
                   "EPHEMRES_SECRET_KEY=abc\nEPHEMRES_DB_PASSWORD=pw\n")
        self.write("etc/ephemres/config.toml", 'log_dir = "/var/log/ephemres"\n')
        self.write("etc/systemd/system/ephemres-api.service", "ExecStart=/opt/ephemres/venv/bin/uvicorn\n")
        for svc in ("ephemres-api", "ephemres-engine"):
            self.flag("active", svc)
        self.flag("units", "redis-server")
        self.flag("enabled", "redis-server")
        self.flag("active", "redis-server")
        for svc in ("netmon-api", "netmon-engine", "netmon-flowd"):
            self.write(f"etc/systemd/system/{svc}.service", "ExecStart=/opt/netmon/venv/bin/uvicorn\n")
        self.write("etc/sudoers.d/netmon", "netmon ALL=(ALL) NOPASSWD: /usr/bin/systemctl start netmon-flowd\n")
        self.write("etc/logrotate.d/netmon", "/var/log/netmon/*.log {}\n")
        self.write("opt/netmon/noop/backend/app/main.py", "")
        self.write("opt/netmon/venv/bin/python3", "")
        self.write("etc/netmon/netmon.env", "NETMON_SECRET_KEY=abc\n")
        self.write("var/log/netmon/api.log", "")
        self.write("var/backups/netmon/netmon_pre.sql.gz", "")
        # The upgrade replaced the old template: netmon is kept but unlinked.
        self.write("etc/nginx/sites-available/netmon", "root /opt/netmon/noop/frontend/dist;\n")
        self.write("etc/nginx/sites-available/ephemres", "root /opt/ephemres/frontend/dist;\n")
        (self.root / "etc/nginx/sites-enabled").mkdir(parents=True)
        (self.root / "etc/nginx/sites-enabled/ephemres").symlink_to(self.root / "etc/nginx/sites-available/ephemres")
        (self.state / "user_netmon").touch()

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def flag(self, kind: str, name: str, content: str = "") -> None:
        (self.state / kind / name).write_text(content)

    def unflag(self, kind: str, name: str) -> None:
        (self.state / kind / name).unlink()

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def calls(self) -> list[str]:
        log = self.state / "calls"
        return log.read_text().splitlines() if log.exists() else []

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, EPHEMRES_CLEANUP_ROOT=str(self.root), STATE=str(self.state),
                   PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)


LEFTOVERS = [
    "etc/systemd/system/netmon-api.service", "etc/sudoers.d/netmon", "etc/logrotate.d/netmon",
    "opt/netmon", "etc/netmon", "var/log/netmon", "etc/nginx/sites-available/netmon",
]


@pytest.fixture
def server(tmp_path):
    return Server(tmp_path)


def test_the_script_is_valid_bash_and_executable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_without_apply_it_only_reports(server):
    result = server.run()

    assert result.returncode == 0, result.stderr
    assert "Report only" in result.stdout
    for rel in LEFTOVERS:
        assert server.exists(rel), rel
        assert f"[WOULD REMOVE] {server.root / rel}" in result.stdout
    assert "redis-server: stop it and disable it" in result.stdout
    # Only questions were asked: no disable, no daemon-reload, no userdel.
    assert not [c for c in server.calls() if not c.startswith(("systemctl is-", "systemctl cat"))]


def test_apply_removes_the_leftovers_and_keeps_the_rest(server):
    result = server.run("--apply")

    assert result.returncode == 0, result.stderr
    for rel in LEFTOVERS:
        assert not server.exists(rel), rel
    calls = server.calls()
    assert "systemctl disable --now redis-server" in calls
    assert "systemctl daemon-reload" in calls
    assert "userdel netmon" in calls
    # Kept: the Ephemres install, the old backups (not asked for), the database.
    assert server.exists("etc/ephemres/ephemres.env")
    assert server.exists("etc/nginx/sites-available/ephemres")
    assert server.exists("var/backups/netmon/netmon_pre.sql.gz")
    assert "netmon_user" in result.stdout
    assert not [c for c in calls if c.startswith("apt-get")]


def test_a_second_run_finds_nothing(server):
    server.run("--apply")
    server.unflag("enabled", "redis-server")
    server.unflag("active", "redis-server")
    (server.state / "user_netmon").unlink()

    result = server.run()
    assert result.returncode == 0, result.stderr
    assert "Nothing left to remove." in result.stdout


def test_remove_backups_deletes_the_old_dumps(server):
    assert server.run("--apply", "--remove-backups").returncode == 0
    assert not server.exists("var/backups/netmon")


def test_the_live_nginx_site_is_kept(server):
    # The upgrade edited an admin-changed site in place: it is still the one nginx serves.
    server.write("etc/nginx/sites-available/netmon", "root /opt/ephemres/frontend/dist;\n")
    server.write("etc/nginx/sites-available/netmon.pre-ephemres", "root /opt/netmon/noop/frontend/dist;\n")
    (server.root / "etc/nginx/sites-enabled/netmon").symlink_to(server.root / "etc/nginx/sites-available/netmon")

    result = server.run("--apply")

    assert result.returncode == 0, result.stderr
    assert server.exists("etc/nginx/sites-available/netmon")
    assert "it is the live nginx site" in result.stdout
    assert not server.exists("etc/nginx/sites-available/netmon.pre-ephemres")


def test_redis_in_use_by_something_else_is_kept(server):
    # ss -tnxp output: the server end of each client connection belongs to redis-server.
    (server.state / "ss").write_text(
        'tcp ESTAB 0 0 127.0.0.1:6379 127.0.0.1:51234 users:(("redis-server",pid=812,fd=9))\n'
        'tcp ESTAB 0 0 127.0.0.1:51234 127.0.0.1:6379 users:(("worker",pid=990,fd=4))\n'
    )
    server.flag("pkg", "redis-server")

    result = server.run("--apply", "--purge-packages")

    assert result.returncode == 0, result.stderr
    assert "1 client connection(s) open" in result.stdout
    assert "package redis-server: Redis is still in use" in result.stdout
    assert "systemctl disable --now redis-server" not in server.calls()
    assert not [c for c in server.calls() if "redis" in c and c.startswith("apt-get")]


PDF_LIBS = ("libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz-subset0", "libharfbuzz0b")


def _install_pdf_libs(server) -> None:
    """The four libraries v3.3.0 installed, with their real dependencies on each other."""
    for pkg in PDF_LIBS:
        server.flag("pkg", pkg)
    server.flag("apt_extra", "libharfbuzz0b", "libpango-1.0-0\nlibpangoft2-1.0-0\n")
    server.flag("apt_extra", "libpango-1.0-0", "libpangoft2-1.0-0\n")


def test_purge_packages_purges_a_group_that_depends_only_on_itself(server):
    # Purging them one at a time in this order would refuse libpango (it takes libpangoft2) and
    # libharfbuzz0b (it takes both); as a group nothing outside goes.
    _install_pdf_libs(server)
    server.flag("pkg", "redis-server")
    server.flag("pkg", "redis-tools")
    server.flag("apt_extra", "redis-tools", "redis-server\n")

    report = server.run("--purge-packages")
    result = server.run("--apply", "--purge-packages")

    assert result.returncode == 0, result.stderr
    purged = [c for c in server.calls() if c.startswith("apt-get purge")]
    assert purged == [
        "apt-get purge -y -qq redis-server redis-tools",
        "apt-get purge -y -qq " + " ".join(PDF_LIBS),
    ]
    for pkg in PDF_LIBS:
        assert f"[WOULD REMOVE] package {pkg}" in report.stdout


def test_purge_packages_keeps_what_another_program_needs(server):
    # libpangocairo (not ours) depends on libpangoft2, which depends on libpango, which depends
    # on libharfbuzz0b: only libharfbuzz-subset0 can go.
    _install_pdf_libs(server)
    server.flag("apt_extra", "libpangoft2-1.0-0", "libpangocairo-1.0-0\n")
    server.flag("apt_extra", "libpango-1.0-0", "libpangoft2-1.0-0\nlibpangocairo-1.0-0\n")
    server.flag("apt_extra", "libharfbuzz0b", "libpango-1.0-0\nlibpangoft2-1.0-0\nlibpangocairo-1.0-0\n")

    report = server.run("--purge-packages")
    result = server.run("--apply", "--purge-packages")

    assert result.returncode == 0, result.stderr
    assert [c for c in server.calls() if c.startswith("apt-get purge")] == [
        "apt-get purge -y -qq libharfbuzz-subset0"
    ]
    for out in (report.stdout, result.stdout):
        assert "package libpangoft2-1.0-0: purging it would also remove libpangocairo-1.0-0" in out
        assert "[KEEP] package libpango-1.0-0" in out
        assert "[KEEP] package libharfbuzz0b" in out


def test_kept_backups_are_handed_to_root_before_the_user_goes(server):
    # Left owned by a deleted UID, the dumps would belong to whoever is given that UID next.
    result = server.run("--apply")

    assert result.returncode == 0, result.stderr
    calls = server.calls()
    chown = f"chown -R root:root {server.root}/var/backups/netmon"
    assert calls.index(chown) < calls.index("userdel netmon")
    assert "[CHANGE]" in result.stdout


def test_backups_being_removed_are_not_chowned(server):
    assert server.run("--apply", "--remove-backups").returncode == 0
    assert not [c for c in server.calls() if c.startswith("chown")]


def test_packages_are_only_listed_without_the_flag(server):
    server.flag("pkg", "libpango-1.0-0")
    result = server.run("--apply")
    assert "add --purge-packages" in result.stdout
    assert not [c for c in server.calls() if c.startswith("apt-get")]


# --- refusals: each leaves everything in place --------------------------------------------------

def _refused(server, result, reason: str) -> None:
    assert result.returncode == 1
    assert reason in result.stderr
    assert "Nothing was changed." in result.stderr
    for rel in LEFTOVERS:
        assert server.exists(rel), rel
    assert not [c for c in server.calls() if not c.startswith(("systemctl is-", "systemctl cat"))]


def test_refuses_when_the_ephemres_env_file_lacks_the_secret_key(server):
    # /etc/netmon/netmon.env would then be the only copy of it.
    server.write("etc/ephemres/ephemres.env", "EPHEMRES_DB_PASSWORD=pw\n")
    _refused(server, server.run("--apply"), "no EPHEMRES_SECRET_KEY")


def test_refuses_without_its_own_config_file(server):
    # The app would read /etc/netmon/config.toml, which is about to go.
    (server.root / "etc/ephemres/config.toml").unlink()
    _refused(server, server.run("--apply"), "config.toml is missing")


def test_refuses_when_the_config_still_logs_to_var_log_netmon(server):
    server.write("etc/ephemres/config.toml", 'log_dir = "/var/log/netmon"\n')
    _refused(server, server.run("--apply"), "still refers to /var/log/netmon")


def test_refuses_when_a_unit_drop_in_points_at_etc_netmon(server):
    server.write("etc/systemd/system/ephemres-api.service.d/override.conf",
                 "[Service]\nEnvironment=EPHEMRES_CONFIG_PATH=/etc/netmon/config.toml\n")
    _refused(server, server.run("--apply"), "still refers to /etc/netmon")


def test_refuses_on_a_server_that_was_never_migrated(server):
    (server.root / "etc/ephemres/ephemres.env").unlink()
    _refused(server, server.run("--apply"), "has not been migrated")


def test_refuses_while_ephemres_is_down(server):
    server.unflag("active", "ephemres-engine")
    _refused(server, server.run("--apply"), "ephemres-engine is not running")


def test_refuses_when_the_old_units_are_back_on(server):
    server.flag("enabled", "netmon-api")
    _refused(server, server.run("--apply"), "may have been rolled back")


def test_refuses_when_an_enabled_site_still_points_into_opt_netmon(server):
    (server.root / "etc/nginx/sites-enabled/netmon").symlink_to(server.root / "etc/nginx/sites-available/netmon")
    _refused(server, server.run("--apply"), "still refers to /opt/netmon")


def test_an_unknown_option_changes_nothing(server):
    result = server.run("--aply")
    assert result.returncode == 2
    assert server.exists("opt/netmon")
