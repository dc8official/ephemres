from __future__ import annotations

import argparse
import asyncio
import glob
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from app.core import orm as sa
from app.core.orm import NULLTYPE, postgresql

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore


# ---------------------------------------------------------------------------
# OperationsProxy: Emulates alembic.op operations and compiles PostgreSQL DDL
# ---------------------------------------------------------------------------
class OperationsProxy:
    """Captures migration operations during a revision's upgrade() or downgrade()
    and compiles them into pure PostgreSQL DDL/DML statements."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sqltext: Any) -> None:
        """Capture and append raw SQL statement."""
        stmt = str(sqltext).strip()
        if stmt:
            if not stmt.endswith(";"):
                stmt += ";"
            self.statements.append(stmt)

    def _ensure_fk_target(self, metadata: sa.MetaData, fk: Any) -> None:
        """Create placeholder Table/Column in metadata if FK target is not yet present."""
        target = getattr(fk, "_colspec", None)
        if target is None and hasattr(fk, "target_fullname"):
            target = fk.target_fullname
        if target and isinstance(target, str) and "." in target:
            tname, cname = target.rsplit(".", 1)
            if tname not in metadata.tables:
                rel_t = sa.Table(tname, metadata)
            else:
                rel_t = metadata.tables[tname]
            if cname not in rel_t.c:
                rel_t.append_column(sa.Column(cname, NULLTYPE))

    def create_table(self, table_name: str, *columns_and_constraints: Any, **kw: Any) -> None:
        """Compile a complete CREATE TABLE statement for PostgreSQL."""
        meta = sa.MetaData()
        cols = []
        constraints = []

        for item in columns_and_constraints:
            if isinstance(item, sa.Column):
                if item.table is not None:
                    item = item.copy()
                cols.append(item)
                for fk in item.foreign_keys:
                    self._ensure_fk_target(meta, fk)
            else:
                constraints.append(item)
                if isinstance(item, sa.ForeignKeyConstraint):
                    for fk in item.elements:
                        self._ensure_fk_target(meta, fk)

        tbl = sa.Table(table_name, meta, *(cols + constraints), **kw)
        for fk in tbl.foreign_keys:
            self._ensure_fk_target(meta, fk)

        ddl = str(sa.schema.CreateTable(tbl).compile(dialect=postgresql.dialect())).strip()
        if not ddl.endswith(";"):
            ddl += ";"
        self.statements.append(ddl)

        # Also emit indexes attached directly to columns/table if any
        for idx in tbl.indexes:
            idx_ddl = str(sa.schema.CreateIndex(idx).compile(dialect=postgresql.dialect())).strip()
            if not idx_ddl.endswith(";"):
                idx_ddl += ";"
            self.statements.append(idx_ddl)

    def drop_table(self, table_name: str, **kw: Any) -> None:
        """Emit DROP TABLE IF EXISTS CASCADE."""
        self.statements.append(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')

    def add_column(self, table_name: str, column: sa.Column, **kw: Any) -> None:
        """Emit ALTER TABLE ... ADD COLUMN using dialect DDL compiler."""
        compiler = postgresql.dialect().ddl_compiler(postgresql.dialect(), None)
        col_spec = compiler.get_column_specification(column)
        self.statements.append(f'ALTER TABLE "{table_name}" ADD COLUMN {col_spec};')

    def drop_column(self, table_name: str, column_name: str, **kw: Any) -> None:
        """Emit ALTER TABLE ... DROP COLUMN IF EXISTS."""
        self.statements.append(f'ALTER TABLE "{table_name}" DROP COLUMN IF EXISTS "{column_name}";')

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: Sequence[Union[str, Any]],
        unique: bool = False,
        **kw: Any,
    ) -> None:
        """Emit CREATE [UNIQUE] INDEX IF NOT EXISTS with postgresql_using / where support."""
        uniq = "UNIQUE " if unique else ""
        pg_using = kw.get("postgresql_using")
        using_clause = f" USING {pg_using}" if pg_using else ""

        col_strs = []
        for c in columns:
            if isinstance(c, str):
                col_strs.append(f'"{c}"' if c.isidentifier() else c)
            else:
                col_strs.append(str(c))
        cols_joined = ", ".join(col_strs)

        pg_where = kw.get("postgresql_where")
        where_clause = f" WHERE {pg_where}" if pg_where else ""

        self.statements.append(
            f'CREATE {uniq}INDEX IF NOT EXISTS "{index_name}" ON "{table_name}"{using_clause} ({cols_joined}){where_clause};'
        )

    def drop_index(self, index_name: str, table_name: Optional[str] = None, **kw: Any) -> None:
        """Emit DROP INDEX IF EXISTS."""
        self.statements.append(f'DROP INDEX IF EXISTS "{index_name}";')

    def create_check_constraint(self, name: str, table_name: str, condition: str, **kw: Any) -> None:
        """Emit ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)."""
        self.statements.append(f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{name}" CHECK ({condition});')

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        referent_table: str,
        local_cols: Sequence[str],
        remote_cols: Sequence[str],
        ondelete: Optional[str] = None,
        **kw: Any,
    ) -> None:
        """Emit ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ... REFERENCES."""
        loc = ", ".join(f'"{c}"' for c in local_cols)
        rem = ", ".join(f'"{c}"' for c in remote_cols)
        on_del = f" ON DELETE {ondelete}" if ondelete else ""
        self.statements.append(
            f'ALTER TABLE "{source_table}" ADD CONSTRAINT "{name}" FOREIGN KEY ({loc}) REFERENCES "{referent_table}" ({rem}){on_del};'
        )

    def drop_constraint(self, name: str, table_name: str, type_: Optional[str] = None, **kw: Any) -> None:
        """Emit ALTER TABLE ... DROP CONSTRAINT IF EXISTS."""
        self.statements.append(f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{name}";')


_active_proxy: Optional[OperationsProxy] = None


def set_current_proxy(proxy: Optional[OperationsProxy]) -> None:
    global _active_proxy
    _active_proxy = proxy


def get_current_proxy() -> Optional[OperationsProxy]:
    return _active_proxy


class _DynamicOpProxy:
    """Routes op.<method>(...) calls to the active migration's OperationsProxy."""

    def __getattr__(self, name: str) -> Any:
        target = _active_proxy
        if target is None:
            raise RuntimeError(f"op.{name}() called outside an active migration context")
        return getattr(target, name)


op = _DynamicOpProxy()


# ---------------------------------------------------------------------------
# MigrationRevision Representation
# ---------------------------------------------------------------------------
@dataclass
class MigrationRevision:
    revision: str
    down_revision: Optional[str]
    file_path: str
    module_name: str
    description: str = ""
    upgrade_fn: Any = None
    downgrade_fn: Any = None

    def get_upgrade_statements(self) -> List[str]:
        if self.upgrade_fn is None:
            return []
        proxy = OperationsProxy()
        set_current_proxy(proxy)
        try:
            self.upgrade_fn()
        finally:
            set_current_proxy(None)
        return proxy.statements

    def get_downgrade_statements(self) -> List[str]:
        if self.downgrade_fn is None:
            return []
        proxy = OperationsProxy()
        set_current_proxy(proxy)
        try:
            self.downgrade_fn()
        finally:
            set_current_proxy(None)
        return proxy.statements


# ---------------------------------------------------------------------------
# Directory Discovery & Revision Graph Ordering
# ---------------------------------------------------------------------------
def find_versions_dir(config_path: Optional[str] = None) -> Path:
    """Locate the backend/migrations/versions directory."""
    if config_path and os.path.exists(config_path):
        try:
            here = os.path.dirname(os.path.abspath(config_path))
            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("version_locations"):
                        _, val = line.split("=", 1)
                        val = val.strip().replace("%(here)s", here)
                        p = Path(val)
                        if p.is_dir():
                            return p
                    elif line.startswith("script_location"):
                        _, val = line.split("=", 1)
                        val = val.strip().replace("%(here)s", here)
                        p = Path(val) / "versions"
                        if p.is_dir():
                            return p
        except Exception:
            pass

    # Relative to this file: backend/app/core/migrator.py -> backend/migrations/versions
    rel_path = Path(__file__).resolve().parent.parent.parent / "migrations" / "versions"
    if rel_path.is_dir():
        return rel_path

    # Current working directory fallbacks
    cwd_versions = Path.cwd() / "migrations" / "versions"
    if cwd_versions.is_dir():
        return cwd_versions

    backend_cwd_versions = Path.cwd() / "backend" / "migrations" / "versions"
    if backend_cwd_versions.is_dir():
        return backend_cwd_versions

    raise FileNotFoundError("Could not locate migrations/versions directory.")


def load_revisions(versions_dir: Path) -> List[MigrationRevision]:
    """Load all migration files and return them sorted topologically from base to head."""
    backend_dir = str(versions_dir.parent.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    raw_revisions: Dict[str, MigrationRevision] = {}
    py_files = sorted(versions_dir.glob("*.py"))

    for filepath in py_files:
        if filepath.name.startswith("__") or filepath.name.startswith("."):
            continue
        module_name = f"ephemres_migration_{filepath.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(filepath))
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        rev_id = getattr(module, "revision", None)
        if not rev_id:
            continue
        down_rev = getattr(module, "down_revision", None)
        doc = getattr(module, "__doc__", "") or filepath.stem
        raw_revisions[str(rev_id)] = MigrationRevision(
            revision=str(rev_id),
            down_revision=str(down_rev) if down_rev is not None else None,
            file_path=str(filepath),
            module_name=module_name,
            description=doc.strip().split("\n")[0] if doc else filepath.stem,
            upgrade_fn=getattr(module, "upgrade", None),
            downgrade_fn=getattr(module, "downgrade", None),
        )

    by_down_rev: Dict[Optional[str], MigrationRevision] = {}
    root: Optional[MigrationRevision] = None
    for r in raw_revisions.values():
        if r.down_revision is None:
            if root is not None:
                raise ValueError(f"Multiple roots detected in migration tree: {root.revision} and {r.revision}")
            root = r
        else:
            by_down_rev[r.down_revision] = r

    if root is None:
        raise ValueError("No root migration revision (down_revision = None) found.")

    ordered: List[MigrationRevision] = [root]
    curr = root
    while curr.revision in by_down_rev:
        curr = by_down_rev[curr.revision]
        ordered.append(curr)

    if len(ordered) != len(raw_revisions):
        orphaned = set(raw_revisions.keys()) - {r.revision for r in ordered}
        raise ValueError(f"Orphaned or branched migrations detected: {orphaned}")

    return ordered


# ---------------------------------------------------------------------------
# Database Connectivity & asyncpg Execution
# ---------------------------------------------------------------------------
def get_db_url() -> str:
    """Resolve PostgreSQL connection URL for asyncpg."""
    env_url = os.environ.get("DATABASE_URL") or os.environ.get("EPHEMRES_DATABASE_URL")
    if env_url:
        return (
            env_url.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgresql+psycopg2://", "postgresql://")
        )

    try:
        from app.config import settings

        if hasattr(settings, "db_url"):
            return settings.db_url.replace("postgresql+asyncpg://", "postgresql://")
    except Exception:
        pass

    host = os.environ.get("EPHEMRES_DB_HOST") or os.environ.get("NETMON_DB_HOST", "localhost")
    port = os.environ.get("EPHEMRES_DB_PORT") or os.environ.get("NETMON_DB_PORT", "5432")
    user = os.environ.get("EPHEMRES_DB_USER") or os.environ.get("NETMON_DB_USER", "ephemres")
    password = os.environ.get("EPHEMRES_DB_PASSWORD") or os.environ.get("NETMON_DB_PASSWORD", "")
    name = os.environ.get("EPHEMRES_DB_NAME") or os.environ.get("NETMON_DB_NAME", "ephemres")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def get_db_connection() -> Any:
    """Connect to PostgreSQL using asyncpg."""
    if asyncpg is None:
        raise RuntimeError("asyncpg is required to execute migrations against PostgreSQL.")
    url = get_db_url()
    return await asyncpg.connect(url, timeout=30.0)


async def ensure_version_table(conn: Any) -> None:
    """Ensure standard alembic_version table exists."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alembic_version (
            version_num VARCHAR(32) NOT NULL PRIMARY KEY
        );
        """
    )


async def get_current_revision(conn: Any) -> Optional[str]:
    """Fetch current revision from alembic_version table."""
    await ensure_version_table(conn)
    row = await conn.fetchrow("SELECT version_num FROM alembic_version LIMIT 1;")
    return row["version_num"] if row else None


async def set_current_revision(conn: Any, revision: Optional[str]) -> None:
    """Update current revision in alembic_version table."""
    await ensure_version_table(conn)
    await conn.execute("DELETE FROM alembic_version;")
    if revision:
        await conn.execute("INSERT INTO alembic_version (version_num) VALUES ($1);", revision)


# ---------------------------------------------------------------------------
# High-Level Migration Operations: upgrade, downgrade, current, heads, history
# ---------------------------------------------------------------------------
async def upgrade_database(
    target: str = "head",
    config_path: Optional[str] = None,
    sql_only: bool = False,
) -> List[str]:
    """Execute pending schema migrations up to target revision."""
    versions_dir = find_versions_dir(config_path)
    ordered = load_revisions(versions_dir)
    head_rev = ordered[-1].revision

    if target in ("head", "heads"):
        target_rev = head_rev
    else:
        target_rev = target
        if not any(r.revision == target_rev for r in ordered):
            raise ValueError(f"Revision '{target_rev}' not found in migrations.")

    all_sql: List[str] = []

    if sql_only:
        target_idx = [i for i, r in enumerate(ordered) if r.revision == target_rev][0]
        to_apply = ordered[: target_idx + 1]
        for rev in to_apply:
            stmts = rev.get_upgrade_statements()
            all_sql.extend(stmts)
        return all_sql

    conn = await get_db_connection()
    try:
        current_rev = await get_current_revision(conn)
        if current_rev == target_rev:
            print(f"INFO  [alembic.runtime.migration] Database is already at target revision: {current_rev}")
            return []

        if current_rev is None:
            start_idx = 0
        else:
            current_indices = [i for i, r in enumerate(ordered) if r.revision == current_rev]
            if not current_indices:
                raise ValueError(f"Current database revision '{current_rev}' is not in migration chain.")
            start_idx = current_indices[0] + 1

        target_indices = [i for i, r in enumerate(ordered) if r.revision == target_rev]
        target_idx = target_indices[0]

        if start_idx > target_idx:
            print(f"INFO  [alembic.runtime.migration] Current revision {current_rev} is ahead of target {target_rev}.")
            return []

        to_apply = ordered[start_idx : target_idx + 1]
        for rev in to_apply:
            print(f"INFO  [alembic.runtime.migration] Running upgrade {rev.down_revision or '<base>'} -> {rev.revision}, {rev.description}")
            stmts = rev.get_upgrade_statements()
            async with conn.transaction():
                for stmt in stmts:
                    await conn.execute(stmt)
                await set_current_revision(conn, rev.revision)
            all_sql.extend(stmts)
        return all_sql
    finally:
        await conn.close()


async def downgrade_database(
    target: str = "-1",
    config_path: Optional[str] = None,
    sql_only: bool = False,
) -> List[str]:
    """Revert database schema down to target revision."""
    versions_dir = find_versions_dir(config_path)
    ordered = load_revisions(versions_dir)

    all_sql: List[str] = []

    if sql_only:
        return all_sql

    conn = await get_db_connection()
    try:
        current_rev = await get_current_revision(conn)
        if current_rev is None:
            print("INFO  [alembic.runtime.migration] Database is already at base (no revisions applied).")
            return []

        current_indices = [i for i, r in enumerate(ordered) if r.revision == current_rev]
        if not current_indices:
            raise ValueError(f"Current database revision '{current_rev}' not found in chain.")
        curr_idx = current_indices[0]

        if target == "-1":
            target_rev = ordered[curr_idx].down_revision
        elif target == "base":
            target_rev = None
        else:
            target_rev = target

        if target_rev is None:
            to_downgrade = list(reversed(ordered[: curr_idx + 1]))
        else:
            target_indices = [i for i, r in enumerate(ordered) if r.revision == target_rev]
            if not target_indices:
                raise ValueError(f"Target revision '{target_rev}' not found in chain.")
            t_idx = target_indices[0]
            if t_idx >= curr_idx:
                print(f"INFO  [alembic.runtime.migration] Current revision {current_rev} is already at or behind target {target_rev}.")
                return []
            to_downgrade = list(reversed(ordered[t_idx + 1 : curr_idx + 1]))

        for rev in to_downgrade:
            print(f"INFO  [alembic.runtime.migration] Running downgrade {rev.revision} -> {rev.down_revision or '<base>'}, {rev.description}")
            stmts = rev.get_downgrade_statements()
            async with conn.transaction():
                for stmt in stmts:
                    await conn.execute(stmt)
                await set_current_revision(conn, rev.down_revision)
            all_sql.extend(stmts)
        return all_sql
    finally:
        await conn.close()


async def show_current(config_path: Optional[str] = None, verbose: bool = False) -> None:
    """Print the currently applied migration revision."""
    versions_dir = find_versions_dir(config_path)
    ordered = load_revisions(versions_dir)
    head_rev = ordered[-1].revision
    conn = await get_db_connection()
    try:
        current_rev = await get_current_revision(conn)
        if current_rev:
            suffix = " (head)" if current_rev == head_rev else ""
            if verbose:
                matching = [r for r in ordered if r.revision == current_rev]
                desc = matching[0].description if matching else ""
                print(f"Current revision: {current_rev}{suffix} - {desc}")
            else:
                print(f"{current_rev}{suffix}")
        else:
            print("None")
    finally:
        await conn.close()


def show_heads(config_path: Optional[str] = None, verbose: bool = False) -> None:
    """Print available migration heads."""
    versions_dir = find_versions_dir(config_path)
    ordered = load_revisions(versions_dir)
    head = ordered[-1]
    if verbose:
        print(f"{head.down_revision or '<base>'} -> {head.revision} (head) : {head.description}")
    else:
        print(f"{head.revision} (head)")


def show_history(config_path: Optional[str] = None, verbose: bool = False) -> None:
    """Print migration history from base to head."""
    versions_dir = find_versions_dir(config_path)
    ordered = load_revisions(versions_dir)
    head_rev = ordered[-1].revision
    for r in ordered:
        tag = " (head)" if r.revision == head_rev else ""
        if verbose:
            print(f"Rev: {r.revision}{tag}")
            print(f"Parent: {r.down_revision or '<base>'}")
            print(f"Path: {r.file_path}")
            print(f"Description: {r.description}\n")
        else:
            print(f"{r.down_revision or '<base>'} -> {r.revision}{tag}: {r.description}")


# ---------------------------------------------------------------------------
# CLI Entrypoint (alembic CLI compatibility)
# ---------------------------------------------------------------------------
def cli_main(argv: Optional[List[str]] = None) -> None:
    """CLI dispatcher compatible with standard alembic commands and flags."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="alembic",
        description="Native PostgreSQL Async Migration Engine (Ephemres)",
    )
    parser.add_argument("-c", "--config", default="alembic.ini", help="Path to alembic.ini config")
    parser.add_argument("-x", action="append", help="Custom configuration options (ignored)")
    parser.add_argument("-n", "--name", help="Alembic section name (ignored)")
    parser.add_argument("--raiseerr", action="store_true", help="Raise exception on error")
    subparsers = parser.add_subparsers(dest="command")

    # upgrade
    p_up = subparsers.add_parser("upgrade", help="Upgrade database schema to a revision")
    p_up.add_argument("revision", default="head", nargs="?", help="Target revision (default: head)")
    p_up.add_argument("--sql", action="store_true", help="Compile and dump SQL to stdout without connecting")

    # downgrade
    p_down = subparsers.add_parser("downgrade", help="Revert database schema to a revision")
    p_down.add_argument("revision", help="Target revision (e.g. -1, base, or revision ID)")
    p_down.add_argument("--sql", action="store_true", help="Compile and dump SQL to stdout without connecting")

    # current
    p_cur = subparsers.add_parser("current", help="Display current revision in database")
    p_cur.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # heads
    p_heads = subparsers.add_parser("heads", help="Show available migration heads")
    p_heads.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # history
    p_hist = subparsers.add_parser("history", help="List migrations in chronological order")
    p_hist.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "upgrade":
            stmts = asyncio.run(
                upgrade_database(target=args.revision, config_path=args.config, sql_only=args.sql)
            )
            if args.sql:
                for s in stmts:
                    print(s)
                    if not s.endswith(";"):
                        print(";")
        elif args.command == "downgrade":
            stmts = asyncio.run(
                downgrade_database(target=args.revision, config_path=args.config, sql_only=args.sql)
            )
            if args.sql:
                for s in stmts:
                    print(s)
                    if not s.endswith(";"):
                        print(";")
        elif args.command == "current":
            asyncio.run(show_current(config_path=args.config, verbose=args.verbose))
        elif args.command == "heads":
            show_heads(config_path=args.config, verbose=args.verbose)
        elif args.command == "history":
            show_history(config_path=args.config, verbose=args.verbose)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if getattr(args, "raiseerr", False):
            raise
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
