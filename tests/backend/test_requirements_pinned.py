from pathlib import Path


def test_requirements_pinned():
    req_path = Path(__file__).resolve().parents[2] / "backend" / "requirements.txt"
    lines = [line.strip() for line in req_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]

    assert len(lines) == 2
    for line in lines:
        assert "==" in line, f"Requirement not strictly pinned: {line}"
        pkg, ver = line.split("==")
        assert len(ver.strip()) > 0
