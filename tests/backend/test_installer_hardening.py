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

    # 2. Verify quoting fix in SECRET_KEY parsing
    content = restore_sh.read_text(encoding="utf-8")
    assert 'tr -d \'"\' | tr -d "\'"' in content or 'tr -d \'"\' | tr -d "\'"' in content
    assert "tr -d '\"'\\''" not in content


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
