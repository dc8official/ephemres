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


def test_frontend_is_built_once_when_the_pull_leaves_it_unchanged(tmp_path):
    content = (Path(__file__).resolve().parents[2] / "deploy" / "upgrade.sh").read_text(encoding="utf-8")
    start = content.index("frontend_source_hash() {")
    function = content[start:content.index("\n}\n", start) + 3]

    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "src" / "App.vue").write_text("<template/>", encoding="utf-8")

    def source_hash():
        script = function + f'frontend_source_hash "{frontend}"\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()

    before = source_hash()
    # npm install and npm run build only write node_modules and dist ...
    for built in ("node_modules/vue/index.js", "dist/index.html"):
        (frontend / built).parent.mkdir(parents=True, exist_ok=True)
        (frontend / built).write_text("x", encoding="utf-8")
    assert source_hash() == before
    # ... while a pulled source change must trigger the step 4 rebuild.
    (frontend / "src" / "App.vue").write_text("<template><main/></template>", encoding="utf-8")
    assert source_hash() != before

    # Step 4 reuses the bundle only for the tree the pre-flight built, with matching sources.
    assert 'PREFLIGHT_FRONTEND_HASH=$(frontend_source_hash "${FRONTEND_DIR}")' in content
    assert '"$(frontend_source_hash "${FRONTEND_DIR}")" == "${PREFLIGHT_FRONTEND_HASH}" ]]; then' in content
    assert '[[ "${FRONTEND_DIR}" == "${PROJECT_ROOT}/frontend" && -n "${PREFLIGHT_FRONTEND_HASH}"' in content
