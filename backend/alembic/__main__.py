"""Entrypoint for python3 -m alembic."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend root is on sys.path
backend_dir = str(Path(__file__).resolve().parent.parent)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.core.migrator import cli_main

if __name__ == "__main__":
    cli_main()
