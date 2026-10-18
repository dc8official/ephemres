import ast
import os
from pathlib import Path

REMOVED_DEPENDENCIES = {
    "fastapi",
    "starlette",
    "pydantic",
    "sqlalchemy",
    "alembic",
    "redis",
    "httpx",
    "jinja2",
    "weasyprint",
    "pypdf",
    "psutil",
    "jose",
    "icmplib",
    "dateutil",
    "argon2",
}


def test_no_removed_dependencies_in_source_tree():
    project_root = Path(__file__).resolve().parents[2]
    source_dirs = [project_root / "backend", project_root / "monitoring"]

    violations = []

    for base_dir in source_dirs:
        assert base_dir.exists(), f"Source directory does not exist: {base_dir}"
        for root, dirs, files in os.walk(base_dir):
            # Prune virtual environments and build cache directories
            dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "__pycache__", ".pytest_cache", ".git")]

            for file in files:
                if not file.endswith(".py"):
                    continue

                py_file = Path(root) / file
                rel_path = py_file.relative_to(project_root)

                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(py_file))

                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            base_pkg = alias.name.split(".")[0]
                            if base_pkg in REMOVED_DEPENDENCIES:
                                violations.append(
                                    f"{rel_path}:{node.lineno} imports '{alias.name}'"
                                )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            base_pkg = node.module.split(".")[0]
                            if base_pkg in REMOVED_DEPENDENCIES:
                                violations.append(
                                    f"{rel_path}:{node.lineno} imports from '{node.module}'"
                                )

    assert not violations, (
        f"Found {len(violations)} prohibited import(s) of removed dependencies:\n"
        + "\n".join(violations)
    )
