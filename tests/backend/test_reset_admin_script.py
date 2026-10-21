import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "reset-admin-password.sh"


def test_reset_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_blank_password_is_not_replaced_with_static_default():
    """Blank input must fall through to app.seed_admin's random generator, never a fixed password."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r'NEW_PASS\s*=\s*["\']?admin["\']?\s*$', content, re.MULTILINE)
    assert 'DEFAULT_ADMIN_PASSWORD="$NEW_PASS"' in content
