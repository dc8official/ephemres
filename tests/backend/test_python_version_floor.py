import io
import re
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = [PROJECT_ROOT / "backend", PROJECT_ROOT / "monitoring"]


def _iter_sources():
    for base in SOURCE_DIRS:
        for path in base.rglob("*.py"):
            if any(part in ("venv", ".venv", "__pycache__") for part in path.parts):
                continue
            yield path


def test_installer_rejects_python_below_3_11():
    """app.config imports stdlib tomllib (3.11+), so the installer must not accept 3.10."""
    install_sh = (PROJECT_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    loop = re.search(r"for ver in ([0-9. ]+); do", install_sh)
    assert loop, "Python version detection loop not found in install.sh"
    versions = [tuple(int(p) for p in v.split(".")) for v in loop.group(1).split()]
    assert versions, "install.sh lists no Python versions"
    assert min(versions) >= (3, 11), f"install.sh accepts Python below 3.11: {loop.group(1)}"


def test_no_backslash_inside_fstring_expressions():
    """Backslashes inside f-string replacement fields are a SyntaxError before Python 3.12 (PEP 701)."""
    violations = []
    for path in _iter_sources():
        src = path.read_text(encoding="utf-8")
        fstring_depth = 0
        brace_depth = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.FSTRING_START:
                fstring_depth += 1
                brace_depth.append(0)
            elif tok.type == tokenize.FSTRING_END:
                fstring_depth -= 1
                brace_depth.pop()
            elif fstring_depth and tok.type == tokenize.OP and tok.string == "{":
                brace_depth[-1] += 1
            elif fstring_depth and tok.type == tokenize.OP and tok.string == "}":
                brace_depth[-1] -= 1
            elif fstring_depth and any(d > 0 for d in brace_depth) and "\\" in tok.string:
                # Covers both plain tokens in a replacement field and nested f-strings inside one.
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{tok.start[0]}")
    assert not violations, "Backslash inside f-string expression (needs Python 3.12+):\n" + "\n".join(violations)
