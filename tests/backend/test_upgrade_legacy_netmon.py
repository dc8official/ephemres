"""upgrade.sh moves a v3.2.0 / v3.3.0 netmon install to the ephemres layout and fails loudly.

The end-to-end proof is a container run (a v3.3.0 server layout upgraded by the real script);
these checks pin the contracts that run depends on so a later edit cannot silently drop them.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPGRADE = (ROOT / "deploy" / "upgrade.sh").read_text(encoding="utf-8")


def test_legacy_install_is_detected_from_the_netmon_env_file():
    assert 'if [[ ! -f /etc/ephemres/ephemres.env && -f /etc/netmon/netmon.env ]]; then' in UPGRADE


def test_config_migration_runs_after_the_backup_and_before_the_config_defaults():
    backup_ok = UPGRADE.index("Pre-upgrade backup successfully saved")
    call = UPGRADE.index("        migrate_legacy_config\n")
    defaults = UPGRADE.index("--- Step 2/7: Migrating System Configuration Defaults ---")
    assert backup_ok < call < defaults


def test_legacy_install_gets_the_venv_the_ephemres_units_start():
    # Every ephemres unit starts from /opt/ephemres/venv ...
    for unit in ("ephemres-api", "ephemres-engine", "ephemres-flowd"):
        text = (ROOT / "deploy" / f"{unit}.service").read_text(encoding="utf-8")
        exec_start = re.search(r"^ExecStart=(\S+)", text, re.M).group(1)
        assert exec_start.startswith("/opt/ephemres/venv/bin/"), unit
    # ... so a legacy install must build that venv, not keep using /opt/netmon/venv.
    legacy = UPGRADE.index('if [[ ${LEGACY_NETMON} -eq 1 ]]; then\n        # The ephemres units start /opt/ephemres/venv')
    netmon_venv = UPGRADE.index('elif [[ -d "/opt/netmon/venv" ]]; then')
    assert legacy < netmon_venv
    assert 'VENV_PATH="/opt/ephemres/venv"' in UPGRADE[legacy:netmon_venv]


def test_legacy_env_file_keeps_its_owner_for_a_rollback():
    assert 'if [[ "${ENV_FILE}" != "/etc/netmon/netmon.env" ]]; then\n            chown ephemres:ephemres "${ENV_FILE}"' in UPGRADE


def test_netmon_keys_are_carried_over_as_ephemres_keys():
    program = re.search(r"awk '(.*?)' /etc/netmon/netmon\.env", UPGRADE, re.S).group(1)
    sample = ("NETMON_DB_PASSWORD=p=w'd\nNETMON_SECRET_KEY=abc123\nDOMAIN_NAME=monitor.example\n# comment\n"
              "NETMON_CONFIG_PATH=/etc/netmon/config.toml\n")
    out = subprocess.run(["awk", program], input=sample, capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()
    assert "EPHEMRES_DB_PASSWORD=p=w'd" in lines and "NETMON_DB_PASSWORD=p=w'd" in lines
    assert "EPHEMRES_SECRET_KEY=abc123" in lines and "NETMON_SECRET_KEY=abc123" in lines
    assert "DOMAIN_NAME=monitor.example" in lines
    assert not any(line.startswith("EPHEMRES_DOMAIN") for line in lines)
    # The ephemres units must not be pointed back at /etc/netmon, which user ephemres cannot read.
    assert not any("CONFIG_PATH" in line for line in lines)


def test_netmon_units_are_disabled_and_nginx_repointed_with_a_backup():
    body = UPGRADE[UPGRADE.index("migrate_legacy_services() {"):]
    assert "systemctl disable netmon-api netmon-engine netmon-flowd" in body
    assert 'cp -p "${site}" "${site}.pre-ephemres"' in body
    assert "sed -i 's#/opt/netmon/noop#/opt/ephemres#g'" in body
    assert 'cp -p "${site}.pre-ephemres" "${site}"' in body  # restored when nginx -t fails


def test_success_is_only_reported_after_the_services_are_verified():
    assert 'if ! systemctl restart ephemres-api ephemres-engine; then' in UPGRADE
    verify = UPGRADE.index("        verify_services_running\n")
    complete = UPGRADE.index("[UPGRADE COMPLETE]")
    assert verify < complete
    failed = UPGRADE[UPGRADE.index("upgrade_failed() {"):UPGRADE.index("verify_services_running() {")]
    assert "UPGRADE INCOMPLETE" in failed and "exit 1" in failed
    assert "curl -fsS --max-time 3 http://127.0.0.1:8000/api/v1/health" in UPGRADE


def test_upgrade_script_parses():
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "upgrade.sh")], check=True)


def test_migrated_install_gets_backend_env_like_install_sh():
    body = UPGRADE[UPGRADE.index("migrate_legacy_services() {"):]
    assert 'cp /etc/ephemres/ephemres.env "${INSTALL_DIR}/backend/.env"' in body


def test_restore_sh_loads_the_install_env_before_running_the_engine():
    # restore.sh runs app.core.restore as root; without the env file the engine has no database
    # password on installs that lack /opt/ephemres/backend/.env (e.g. migrated from netmon).
    restore = (ROOT / "deploy" / "restore.sh").read_text(encoding="utf-8")
    load = restore.index('for candidate in "/etc/ephemres/ephemres.env"')
    engine = restore.index("-m app.core.restore")
    assert load < engine
    assert 'source "${ENV_FILE}"' in restore[load:engine]
