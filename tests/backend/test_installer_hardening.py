from pathlib import Path


def test_install_and_upgrade_traceroute_readlink():
    root = Path(__file__).resolve().parents[2]
    install_sh = (root / "deploy" / "install.sh").read_text(encoding="utf-8")
    upgrade_sh = (root / "deploy" / "upgrade.sh").read_text(encoding="utf-8")

    # Verify install.sh resolves symlinks with readlink -f before setcap
    assert 'readlink -f "$TRACEROUTE_PATH"' in install_sh
    assert 'setcap cap_net_raw+ep "$TRACEROUTE_BIN"' in install_sh

    # Verify upgrade.sh resolves symlinks with readlink -f before setcap
    assert 'readlink -f "${TRACEROUTE_PATH}"' in upgrade_sh
    assert 'setcap cap_net_raw+ep "${TRACEROUTE_BIN}"' in upgrade_sh


def test_install_script_conditional_flowd_start():
    root = Path(__file__).resolve().parents[2]
    install_sh = (root / "deploy" / "install.sh").read_text(encoding="utf-8")

    # Ensure ephemres-flowd is NOT unconditionally enabled or started
    assert 'run "Enabling ephemres-flowd" systemctl enable ephemres-flowd\n\n# Grant' not in install_sh
    assert 'if [ "$FLOW_ENABLED" = true ]; then\n    run "Enabling ephemres-flowd" systemctl enable ephemres-flowd\nfi' in install_sh
    assert 'if [ "$FLOW_ENABLED" = true ]; then\n    run "Starting ephemres-flowd" systemctl start ephemres-flowd' in install_sh


def test_flowd_service_capabilities_bounding_set():
    root = Path(__file__).resolve().parents[2]
    flowd_service = (root / "deploy" / "ephemres-flowd.service").read_text(encoding="utf-8")

    # Must contain both CAP_NET_BIND_SERVICE and CAP_NET_RAW to avoid conflict with python's file capabilities
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_RAW" in flowd_service
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_RAW" in flowd_service


def test_restore_script_syntax_and_key_parsing():
    import subprocess
    root = Path(__file__).resolve().parents[2]
    restore_sh = root / "deploy" / "restore.sh"

    # 1. Must pass bash syntax check without syntax errors
    proc = subprocess.run(["bash", "-n", str(restore_sh)], capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n deploy/restore.sh failed:\n{proc.stderr}"

    # 2. The previous SECRET_KEY is discovered by app.core.restore (<backup>.key first, quotes
    # stripped there); restore.sh must not pass the current install's key off as the old one.
    content = restore_sh.read_text(encoding="utf-8")
    assert "SECRET_KEY=" not in content.replace('OLD_SECRET_KEY="', "")
    assert "/etc/ephemres/ephemres.env;" not in content
    assert "${ALLOW_INCOMPLETE}" in content
    # 3. A failed restore still restarts the services before exiting non-zero
    assert content.index("|| RESTORE_STATUS=$?") < content.index("systemctl restart ephemres-api ephemres-engine")
    assert content.index("systemctl restart ephemres-api ephemres-engine") < content.index('exit "${RESTORE_STATUS}"')


def test_uninstall_script_purge_flag():
    import subprocess
    root = Path(__file__).resolve().parents[2]
    uninstall_sh = root / "deploy" / "uninstall.sh"

    # 1. Must pass bash syntax check
    proc = subprocess.run(["bash", "-n", str(uninstall_sh)], capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n deploy/uninstall.sh failed:\n{proc.stderr}"

    # 2. Check --purge-data flag implementation
    content = uninstall_sh.read_text(encoding="utf-8")
    assert "--purge-data" in content
    assert "DROP DATABASE IF EXISTS" in content
    assert "DROP USER IF EXISTS" in content


def test_python_interpreter_gets_no_host_wide_file_capability():
    """The venv python3 resolves to the system interpreter: setcap on it would affect every Python process."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    install_sh = (root / "deploy" / "install.sh").read_text(encoding="utf-8")
    upgrade_sh = (root / "deploy" / "upgrade.sh").read_text(encoding="utf-8")

    for name, script in (("install.sh", install_sh), ("upgrade.sh", upgrade_sh)):
        grants = re.findall(r"\bsetcap\s+cap_net_raw\+ep\s+(\S+)", script)
        assert grants, f"{name}: expected setcap on traceroute"
        assert all("TRACEROUTE_BIN" in target for target in grants), f"{name}: setcap targets {grants}"

    # upgrade.sh only ever removes the legacy interpreter capability
    assert 'setcap -r "${PY3_REAL}"' in upgrade_sh


def test_daemon_units_grant_net_raw_via_ambient_capabilities():
    from pathlib import Path

    deploy = Path(__file__).resolve().parents[2] / "deploy"
    engine = (deploy / "ephemres-engine.service").read_text(encoding="utf-8")
    api = (deploy / "ephemres-api.service").read_text(encoding="utf-8")

    assert "AmbientCapabilities=CAP_NET_RAW" in engine
    assert "CapabilityBoundingSet=CAP_NET_RAW" in engine
    assert "AmbientCapabilities=CAP_NET_RAW" in api
    # The API runs `sudo -n systemctl ... ephemres-flowd`; a bounding set without CAP_SETUID breaks sudo.
    import re

    assert not re.search(r"^CapabilityBoundingSet=", api, re.MULTILINE)
