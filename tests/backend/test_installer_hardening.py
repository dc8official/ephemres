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
