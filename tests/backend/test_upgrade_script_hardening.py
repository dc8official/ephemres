from pathlib import Path
import subprocess


def test_upgrade_script_hardening():
    upgrade_sh = Path(__file__).resolve().parents[2] / "deploy" / "upgrade.sh"
    content = upgrade_sh.read_text(encoding="utf-8")

    # 1. No default netmon_secure_password fallback
    assert "netmon_secure_password" not in content

    # 2. Database password check exists and aborts if empty
    assert 'if [[ -z "${DB_PASS}" && ${DRY_RUN} -eq 0 ]]; then' in content

    # 3. seed_admin is checked and not suppressed with || true
    assert "app.seed_admin || true" not in content
    assert 'if ! PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.seed_admin --if-missing; then' in content

    # 4. Decompression block redirects stderr to upgrade log instead of /dev/null
    decompression_start = content.find("Applying TimescaleDB hypertable decompression safety configuration")
    decompression_end = content.find("Would upgrade pip dependencies")
    decomp_block = content[decompression_start:decompression_end]
    assert '2>>"${UPGRADE_LOG}" || true' in decomp_block
    assert 'psql' in decomp_block
    assert 'psql' in decomp_block and '2>/dev/null' not in decomp_block


def test_frontend_is_built_once_in_the_fetched_release_before_services_stop():
    # Since v3.3.6 the release is fetched first (fetch_release), so the pre-flight builds the
    # fetched tree once and step 4 no longer rebuilds after a pull. Until then the pre-flight
    # built the old checkout and step 4 built the clone again, in /tmp, with services stopped.
    content = (Path(__file__).resolve().parents[2] / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
    assert content.count("npm run build") == 1
    assert 'FRONTEND_DIR="${SOURCE_DIR}/frontend"' in content
    assert content.index("    fetch_release\n") < content.index("npm run build") < content.index("    pause_services\n")
    assert "Aborting upgrade before stopping services." in content
