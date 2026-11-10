"""Differential tests: the hand-written core against the real libraries it imitates.

Scale and core review M3/M4: the core has no upstream, so nobody else finds where it quietly
differs from Pydantic or SQLAlchemy. These tests give both the same input and compare the
answers. Every known difference is listed with its reason; a new one, or a listed one that
went away, fails the run, so the list stays true.

Runs only in the dev venv that has the real libraries (see README.md). Pydantic and SQLAlchemy
never reach production: this folder is outside backend/, monitoring/ and tests/, which
tests/backend/test_no_removed_deps.py keeps free of them, and outside pytest's testpaths.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "backend", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

pytest.importorskip("pydantic", reason="run in .venv-diff (devtools/differential/README.md)")
pytest.importorskip("sqlalchemy", reason="run in .venv-diff (devtools/differential/README.md)")
