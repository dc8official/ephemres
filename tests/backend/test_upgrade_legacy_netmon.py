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
    assert "    migrate_legacy_nginx_site\n" in body
    body = UPGRADE[UPGRADE.index("migrate_legacy_nginx_site() {"):UPGRADE.index("migrate_legacy_services() {")]
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


def test_flow_collection_carries_over_when_netmon_flowd_was_receiving():
    # v3.2/v3.3.0 always ran netmon-flowd; v3.3.5 only starts ephemres-flowd when flow ingestion
    # (which needs performance mode) is on, so the upgrade must switch both on for such servers.
    detect = UPGRADE.index("LEGACY_FLOWD_ENABLED=0")
    stop = UPGRADE.index("--- Step 1/7: Executing Pre-Upgrade Database Backup ---")
    assert detect < stop  # recorded before the netmon units are stopped or disabled
    assert "if systemctl is-enabled --quiet netmon-flowd 2>/dev/null; then\n        LEGACY_FLOWD_ENABLED=1" in UPGRADE
    body = UPGRADE[UPGRADE.index("migrate_legacy_services() {"):UPGRADE.index("# 9. Service Unit Refresh")]
    assert "('performance_mode', 'true'), ('flow_ingestion_enabled', 'true')" in body
    assert "ON CONFLICT (setting_key) DO UPDATE" in body
    # ... before the step 7 check that decides whether ephemres-flowd is started.
    flow_check = UPGRADE.index("SELECT setting_value FROM app_settings WHERE setting_key = 'flow_ingestion_enabled'")
    assert UPGRADE.index("        migrate_legacy_services\n") < flow_check


def test_upgrade_gives_a_legacy_admin_a_random_password_not_the_install_default():
    # seed_admin only creates a missing admin; the argon2 admin is reset by reset_legacy_passwords
    # (random, recorded in the root-only report), and a failure there stops the upgrade.
    seed = UPGRADE.index("-m app.seed_admin --if-missing; then")
    reset = UPGRADE.index('if ! EPHEMRES_RESET_REPORT="${RESET_REPORT}" PYTHONPATH="${INSTALL_DIR}:${INSTALL_DIR}/backend" "${PYTHON_BIN}" -m app.reset_legacy_passwords; then')
    assert seed < reset
    assert "-m app.seed_admin;" not in UPGRADE
    complete = UPGRADE.index("[UPGRADE COMPLETE]")
    notice = UPGRADE.index("sudo grep '^admin' /opt/ephemres/legacy_password_resets.txt")
    assert complete < notice


# The site v3.2.0 and v3.3.0 install.sh generated (both shipped the same template).
NETMON_SITE = """server {
    listen 80;
    server_name 10.20.0.5;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name 10.20.0.5;
    
    ssl_certificate     /etc/ssl/certs/netmon.crt;
    ssl_certificate_key /etc/ssl/private/netmon.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    location / {
        root /opt/netmon/noop/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";
    add_header Referrer-Policy "strict-origin-when-cross-origin";
}
"""


def _shell_function(name):
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UPGRADE, re.S | re.M).group(0)


def _site_body(tmp_path, text):
    site = tmp_path / "site"
    site.write_text(text, encoding="utf-8")
    script = _shell_function("nginx_site_body") + f'nginx_site_body "{site}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def test_an_unedited_netmon_site_matches_its_template(tmp_path):
    old_template = NETMON_SITE.replace("10.20.0.5", "YOUR_DOMAIN")
    assert _site_body(tmp_path, NETMON_SITE) == _site_body(tmp_path, old_template)
    # certbot --nginx swaps the certificates and adds its own lines: still the installer's site.
    certbot = NETMON_SITE.replace(
        "    ssl_certificate     /etc/ssl/certs/netmon.crt;\n    ssl_certificate_key /etc/ssl/private/netmon.key;\n",
        "    ssl_certificate /etc/letsencrypt/live/mon.example/fullchain.pem; # managed by Certbot\n"
        "    ssl_certificate_key /etc/letsencrypt/live/mon.example/privkey.pem; # managed by Certbot\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot\n",
    ).replace("10.20.0.5", "mon.example")
    assert _site_body(tmp_path, certbot) == _site_body(tmp_path, old_template)
    # A local change keeps the site as it is.
    edited = NETMON_SITE.replace("    location / {", "    client_max_body_size 50m;\n\n    location / {")
    assert _site_body(tmp_path, edited) != _site_body(tmp_path, old_template)


def test_the_new_site_keeps_server_name_and_certificates(tmp_path):
    template = ROOT / "deploy" / "nginx.conf.template"
    script = (_shell_function("render_nginx_site")
              + f'render_nginx_site "{template}" "mon.example mon" '
              + '/etc/letsencrypt/live/mon.example/fullchain.pem /etc/letsencrypt/live/mon.example/privkey.pem\n')
    site = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert site.count("server_name mon.example mon;") == 2
    assert "YOUR_DOMAIN" not in site
    assert "ssl_certificate     /etc/letsencrypt/live/mon.example/fullchain.pem;" in site
    assert "ssl_certificate_key /etc/letsencrypt/live/mon.example/privkey.pem;" in site
    assert "gzip on;" in site and "location /assets/" in site and "root /opt/ephemres/frontend/dist;" in site
