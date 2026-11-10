# Differential tests: the core against Pydantic and SQLAlchemy

Ephemres runs on its own hand-written framework, validator and ORM (`backend/app/core/`), which
imitate FastAPI, Pydantic and SQLAlchemy. Nobody else tests that code, so these tests borrow the
real libraries as reviewers: the same input goes through the core and through the library, and
the answers must match. Known, deliberate differences are listed in the tests with their reasons.
A new difference fails the run, and so does a listed one that no longer happens.

The real libraries are **dev-only**. They are installed in a separate venv and never reach
production: this folder is outside `backend/`, `monitoring/` and `tests/`, which
`tests/backend/test_no_removed_deps.py` keeps free of them, and outside pytest's default
`testpaths`, so a normal `pytest` run never collects it.

| File | Compares |
|---|---|
| `test_validator_vs_pydantic.py` | `_coerce_value` against `TypeAdapter`, for 16 types and 40 inputs |
| `test_schemas_vs_pydantic.py` | every model in `backend/app/schemas`, loaded twice (core and Pydantic): field by field, and the dumped JSON of a valid payload |
| `test_orm_vs_sqlalchemy.py` | every model in `backend/app/models`, loaded twice (core and SQLAlchemy): 21 queries on a migrated TimescaleDB, comparing rows and Python types |

## Running

Set up the venv once:

```bash
python3.12 -m venv .venv-diff
.venv-diff/bin/python -m pip install --require-hashes -r backend/requirements.lock
.venv-diff/bin/python -m pip install -r devtools/differential/requirements.txt
```

The validator and schema tests need nothing else:

```bash
.venv-diff/bin/python -m pytest devtools/differential
```

The ORM tests need a throwaway TimescaleDB and are skipped without one:

```bash
eval "$(tests/integration/timescale.sh start)"
.venv-diff/bin/python -m pytest devtools/differential
tests/integration/timescale.sh stop
```

## When a test fails

- **A new difference:** decide which side is right. If the core is wrong, fix it and add a test
  under `tests/backend/`. If the difference is deliberate, add it to the test's known-differences
  list with the reason.
- **A stale entry:** the core changed and now agrees with the library. Remove the entry.
- **After upgrading Pydantic or SQLAlchemy** in `requirements.txt`, expect a few of either. The
  core imitates the library's behaviour, not a pinned version of it.
