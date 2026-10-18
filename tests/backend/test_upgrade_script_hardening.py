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
    assert 'if ! PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.seed_admin; then' in content

    # 4. Decompression block redirects stderr to upgrade log instead of /dev/null
    decompression_start = content.find("Applying TimescaleDB hypertable decompression safety configuration")
    decompression_end = content.find("Would upgrade pip dependencies")
    decomp_block = content[decompression_start:decompression_end]
    assert '2>>"${UPGRADE_LOG}" || true' in decomp_block
    assert 'psql' in decomp_block
    assert 'psql' in decomp_block and '2>/dev/null' not in decomp_block
