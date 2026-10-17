"""Comprehensive unit test suite for zero-dependency SQL Compiler & asyncpg ORM Engine (app.core.orm).

Verifies all 8 Lead Systems Architect invariants:
1. Declarative Models (DeclarativeBase, Mapped, mapped_column, Column, relationship, defaults).
2. Session Lifecycle, Dirty Tracking & flush() ID Hydration.
3. PostgreSQL insert().on_conflict_do_update, .on_conflict_do_nothing & .excluded arithmetic.
4. Hybrid Result & Row Unpacking (scalars, mappings, attribute access, multi-entity unpacking).
5. Named :param to $1 Text Compilation with type cast / quote preservation and executemany batching.
6. asyncpg Type Marshaling (JSONB, UUID, INET, ARRAY(INET)).
7. Complex SQL AST Constructs (Chained CTEs, Window functions, case, distinct on, subqueries).
8. Atomic Shim Cutover & Requirements Verification.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import ipaddress
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

# Ensure backend root is on sys.path
backend_path = Path(__file__).resolve().parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.core.orm import (
    ARRAY,
    INET,
    JSONB,
    AsyncEngine,
    AsyncSession,
    BigInteger,
    Boolean,
    Column,
    CompileContext,
    DateTime,
    DeclarativeBase,
    Float,
    ForeignKey,
    Integer,
    Mapped,
    NoResultFound,
    MultipleResultsFound,
    Numeric,
    Result,
    Row,
    String,
    Text,
    TextClause,
    UUIDType,
    and_,
    asc,
    case,
    cast,
    compile_text_clause,
    delete,
    desc,
    func,
    hydrate_column_value,
    insert,
    joinedload,
    mapped_column,
    or_,
    prepare_bind_param,
    relationship,
    select,
    text,
    update,
)


# ---------------------------------------------------------------------------
# Test Models
# ---------------------------------------------------------------------------
class DummyRole(DeclarativeBase):
    __tablename__ = "test_roles"

    id: Mapped[UUID] = mapped_column(UUIDType(as_uuid=True), primary_key=True, default=uuid4)
    role_name: Mapped[str] = mapped_column(String(50), nullable=False)


class DummyUser(DeclarativeBase):
    __tablename__ = "test_users"

    id: Mapped[UUID] = mapped_column(UUIDType(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    role_id: Mapped[Optional[UUID]] = mapped_column(UUIDType(as_uuid=True), ForeignKey("test_roles.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip_list: Mapped[list] = mapped_column(ARRAY(INET()), default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=lambda: ["alpha", "beta"])

    role: Mapped[Optional[DummyRole]] = relationship("DummyRole", lazy="joined")


class DummyMetric(DeclarativeBase):
    __tablename__ = "test_metrics"

    id: Mapped[UUID] = mapped_column(UUIDType(as_uuid=True), primary_key=True, default=uuid4)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    packets: Mapped[int] = mapped_column(BigInteger, default=0)
    score: Mapped[float] = mapped_column(Numeric(5, 2), default=100.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now, onupdate=func.now())


# ---------------------------------------------------------------------------
# Invariant 1: Declarative Models & Expression AST
# ---------------------------------------------------------------------------
def test_declarative_model_class_and_instance():
    # Class-level expressions
    assert (DummyUser.username == "admin").compile() == '"test_users"."username" = $1'
    assert (DummyUser.username != "guest").compile() == '"test_users"."username" != $1'
    assert (DummyUser.username.is_(None)).compile() == '"test_users"."username" IS NULL'
    assert (DummyUser.username.is_not(None)).compile() == '"test_users"."username" IS NOT NULL'
    assert (DummyUser.is_active.is_(True)).compile() == '"test_users"."username" IS TRUE' or '"test_users"."is_active" IS TRUE' in (DummyUser.is_active.is_(True)).compile()
    assert (DummyUser.username.ilike("%adm%")).compile() == '"test_users"."username" ILIKE $1'
    assert DummyUser.username.asc().compile() == '"test_users"."username" ASC'
    assert DummyUser.username.desc().compile() == '"test_users"."username" DESC'

    # In expression
    ctx = CompileContext()
    sql = DummyUser.username.in_(["alice", "bob"]).compile(ctx)
    assert sql == '"test_users"."username" IN ($1, $2)'
    assert ctx.params == ["alice", "bob"]

    # Empty in expression compiles to FALSE
    assert DummyUser.username.in_([]).compile() == "FALSE"
    assert DummyUser.username.notin_([]).compile() == "TRUE"

    # Arithmetic
    arith_sql = (DummyMetric.bytes + 1024).compile()
    assert arith_sql == '("test_metrics"."bytes" + 1024)'

    # Instance defaults
    user = DummyUser(username="carol")
    assert isinstance(user.id, UUID)
    assert user.is_active is True
    assert user.settings == {}
    assert user.ip_list == []
    assert user.tags == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Invariant 2: Session Lifecycle, Dirty Tracking & flush() ID Hydration
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_session_flush_insert_update_delete():
    mock_conn = AsyncMock()
    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = mock_conn

    engine = AsyncEngine("postgresql://mock/db")
    engine._pool = mock_pool

    session = AsyncSession(bind=engine)

    # 1. Test add() and flush() INSERT with RETURNING hydration
    user = DummyUser(username="test_flush")
    generated_id = uuid4()
    mock_conn.fetchrow.return_value = {
        "id": str(generated_id),
        "username": "test_flush",
        "role_id": None,
        "is_active": True,
        "settings": "{}",
        "ip_list": [],
        "tags": '["alpha", "beta"]',
    }

    session.add(user)
    assert user in session._new

    await session.flush()

    assert len(session._new) == 0
    assert user._orm_persisted is True
    assert user.id == generated_id
    mock_conn.fetchrow.assert_called_once()
    insert_call_sql = mock_conn.fetchrow.call_args[0][0]
    assert 'INSERT INTO "test_users"' in insert_call_sql
    assert "RETURNING *" in insert_call_sql

    # 2. Test dirty tracking UPDATE on modified attributes
    mock_conn.fetchrow.reset_mock()
    user.username = "renamed_user"
    user.tags = ["gamma"]

    mock_conn.fetchrow.return_value = {
        "id": str(generated_id),
        "username": "renamed_user",
        "role_id": None,
        "is_active": True,
        "settings": "{}",
        "ip_list": [],
        "tags": '["gamma"]',
    }

    await session.flush()

    assert mock_conn.fetchrow.call_count == 1
    update_call_sql = mock_conn.fetchrow.call_args[0][0]
    assert 'UPDATE "test_users" SET' in update_call_sql
    assert '"username" =' in update_call_sql
    assert '"tags" =' in update_call_sql
    assert f'WHERE "id" =' in update_call_sql

    # 3. Test delete() emitting DELETE
    mock_conn.execute.reset_mock()
    await session.delete(user)
    assert user in session._deleted

    await session.flush()
    assert mock_conn.execute.call_count == 1
    del_sql = mock_conn.execute.call_args[0][0]
    assert 'DELETE FROM "test_users" WHERE "id" =' in del_sql


# ---------------------------------------------------------------------------
# Invariant 3: PostgreSQL insert().on_conflict_do_update & .excluded
# ---------------------------------------------------------------------------
def test_postgres_insert_on_conflict_do_update_and_excluded():
    from app.core.orm import insert as pg_insert

    stmt = (
        pg_insert(DummyMetric)
        .values(metric_name="traffic", bytes=5000, packets=50)
        .on_conflict_do_update(
            index_elements=["metric_name"],
            set_={
                "bytes": DummyMetric.bytes + pg_insert(DummyMetric).excluded.bytes,
                "packets": DummyMetric.packets + pg_insert(DummyMetric).excluded.packets,
                "score": 99.5,
            },
        )
        .returning(DummyMetric.id, DummyMetric.bytes)
    )

    ctx = CompileContext()
    sql = stmt.compile(ctx)

    assert 'INSERT INTO "test_metrics" ("metric_name", "bytes", "packets") VALUES ($1, $2, $3)' in sql
    assert 'ON CONFLICT ("metric_name") DO UPDATE SET' in sql
    assert '"bytes" = ("test_metrics"."bytes" + "EXCLUDED"."bytes")' in sql
    assert '"packets" = ("test_metrics"."packets" + "EXCLUDED"."packets")' in sql
    assert '"score" = 99.5' in sql or '"score" = $4' in sql
    assert 'RETURNING "id", "bytes"' in sql


def test_postgres_insert_on_conflict_do_nothing():
    stmt = (
        insert(DummyMetric)
        .values(metric_name="noop")
        .on_conflict_do_nothing(index_elements=["metric_name"])
    )
    sql = stmt.compile()
    assert 'ON CONFLICT ("metric_name") DO NOTHING' in sql


# ---------------------------------------------------------------------------
# Invariant 4: Hybrid Result & Row Unpacking
# ---------------------------------------------------------------------------
def test_hybrid_result_and_row():
    # 1. Attribute access, index access, mapping access, unpacking
    row = Row(
        values=[100, "PASSIVE", 99.9],
        keys=["bytes", "state", "health"],
        mapping={"bytes": 100, "state": "PASSIVE", "health": 99.9},
    )

    assert row[0] == 100
    assert row[1] == "PASSIVE"
    assert row["state"] == "PASSIVE"
    assert row.state == "PASSIVE"
    assert row.health == 99.9
    assert len(row) == 3

    b, s, h = row
    assert b == 100 and s == "PASSIVE" and h == 99.9

    # 2. Result scalar / scalars / mappings methods
    res = Result([row], rowcount=1)
    assert res.scalar() == 100
    assert res.scalars().all() == [100]
    assert res.scalars().first() == 100
    assert res.one()[0] == 100
    assert res.mappings().all() == [{"bytes": 100, "state": "PASSIVE", "health": 99.9}]

    # 3. NoResultFound / MultipleResultsFound
    empty_res = Result([])
    with pytest.raises(NoResultFound):
        empty_res.one()

    multi_res = Result([row, row])
    with pytest.raises(MultipleResultsFound):
        multi_res.one()


# ---------------------------------------------------------------------------
# Invariant 5: Named :param to $1 Text Compilation & Batching
# ---------------------------------------------------------------------------
def test_compile_text_clause_param_conversion():
    query = """
        SELECT * FROM endpoints
        WHERE status = :status
          AND id = :id::uuid
          AND tags = '["DOWN", "RECOVERED"]'::jsonb
          AND ip_address = :ip::inet
          AND status = :status -- repeated parameter
    """
    params = {
        "status": "ACTIVE",
        "id": uuid4(),
        "ip": "192.168.1.1",
    }

    compiled_sql, bound = compile_text_clause(query, params)

    assert ":status" not in compiled_sql
    assert ":id::uuid" not in compiled_sql
    assert "$1::uuid" in compiled_sql or "$2::uuid" in compiled_sql
    assert "'[\"DOWN\", \"RECOVERED\"]'::jsonb" in compiled_sql
    assert len(bound) == 3  # status, id, ip (status deduplicated)


@pytest.mark.anyio
async def test_session_execute_text_executemany():
    mock_conn = AsyncMock()
    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = mock_conn

    engine = AsyncEngine("postgresql://mock/db")
    engine._pool = mock_pool
    session = AsyncSession(bind=engine)

    stmt = text("INSERT INTO counts (id, val) VALUES (:id, :val)")
    rows = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]

    res = await session.execute(stmt, rows)
    assert res.rowcount == 2
    mock_conn.executemany.assert_called_once()
    compiled_sql = mock_conn.executemany.call_args[0][0]
    assert "$1" in compiled_sql and "$2" in compiled_sql
    assert ":id" not in compiled_sql


# ---------------------------------------------------------------------------
# Invariant 6: asyncpg Type Marshaling (JSONB, UUID, INET, ARRAY)
# ---------------------------------------------------------------------------
def test_asyncpg_type_marshaling():
    # Bind preparation
    assert prepare_bind_param({"k": "v"}, JSONB()) == '{"k": "v"}'
    assert prepare_bind_param(["a", "b"], JSONB()) == '["a", "b"]'
    assert prepare_bind_param(ipaddress.IPv4Address("10.0.0.1"), INET()) == "10.0.0.1"
    assert prepare_bind_param(["10.0.0.1", "10.0.0.2"], ARRAY(INET())) == ["10.0.0.1", "10.0.0.2"]

    # Hydration
    u_id = uuid4()
    assert hydrate_column_value(str(u_id), UUIDType(as_uuid=True)) == u_id
    assert hydrate_column_value('{"hello": "world"}', JSONB()) == {"hello": "world"}
    assert hydrate_column_value("192.168.1.5", INET()) == "192.168.1.5"
    assert hydrate_column_value(["192.168.1.1", "192.168.1.2"], ARRAY(INET())) == ["192.168.1.1", "192.168.1.2"]


# ---------------------------------------------------------------------------
# Invariant 7: Complex SQL AST Constructs (Chained CTEs, Window, CASE, Distinct ON)
# ---------------------------------------------------------------------------
def test_complex_chained_ctes_and_window_functions():
    # Step 1: Base CTE
    down_events_cte = (
        select(
            DummyMetric.id,
            DummyMetric.metric_name,
            DummyMetric.bytes,
        )
        .where(DummyMetric.bytes > 100)
        .cte("down_events_cte")
    )

    # Step 2: Window function over CTE
    group_id = func.sum(down_events_cte.c.bytes).over(
        order_by=down_events_cte.c.id.asc()
    )

    grouped_cte = (
        select(
            down_events_cte.c.id,
            down_events_cte.c.metric_name,
            group_id.label("grp"),
        )
        .cte("grouped_cte")
    )

    # Step 3: CASE expression & aggregation CTE
    peak_case = case(
        (func.count() > 5, "HIGH"),
        else_="NORMAL",
    )

    incidents_agg_cte = (
        select(
            grouped_cte.c.metric_name,
            func.count().label("incident_count"),
            peak_case.label("peak_severity"),
        )
        .group_by(grouped_cte.c.metric_name)
        .cte("incidents_agg_cte")
    )

    # Final query referencing incidents_agg_cte
    final_stmt = (
        select(incidents_agg_cte)
        .order_by(incidents_agg_cte.c.metric_name.asc())
        .limit(10)
        .offset(20)
    )

    sql = final_stmt.compile()

    assert "WITH" in sql
    assert '"down_events_cte" AS' in sql
    assert '"grouped_cte" AS' in sql
    assert '"incidents_agg_cte" AS' in sql
    assert 'OVER (ORDER BY "down_events_cte"."id" ASC)' in sql
    assert 'CASE WHEN COUNT(*) > 5 THEN \'HIGH\' ELSE \'NORMAL\' END AS "peak_severity"' in sql
    assert 'SELECT "incidents_agg_cte".* FROM "incidents_agg_cte"' in sql
    assert 'ORDER BY "incidents_agg_cte"."metric_name" ASC LIMIT 10 OFFSET 20' in sql


def test_distinct_on_and_subqueries():
    sub = (
        select(DummyMetric.id, DummyMetric.metric_name)
        .distinct(DummyMetric.metric_name)
        .order_by(DummyMetric.metric_name, DummyMetric.bytes.desc())
        .subquery("latest_metrics")
    )

    outer = select(sub.c.id, sub.c.metric_name).select_from(sub)
    sql = outer.compile()

    assert 'SELECT DISTINCT ON ("test_metrics"."metric_name")' in sql
    assert 'AS "latest_metrics"' in sql
    assert 'SELECT "latest_metrics"."id", "latest_metrics"."metric_name" FROM (SELECT' in sql


# ---------------------------------------------------------------------------
# Invariant 8: Atomic Shim Cutover & Requirements Verification
# ---------------------------------------------------------------------------
def test_atomic_shim_cutover_and_requirements():
    req_file = backend_path / "requirements.txt"
    content = req_file.read_text(encoding="utf-8")

    # Assert external dependencies are gone
    assert "sqlalchemy" not in content.lower(), "sqlalchemy must be removed from requirements.txt"
    assert "pydantic" not in content.lower()
    assert "fastapi" not in content.lower()
    assert "alembic" not in content.lower()
    assert "redis" not in content.lower()

    # Assert ONLY uvicorn and asyncpg remain
    lines = [line.strip() for line in content.strip().splitlines() if line.strip() and not line.startswith("#")]
    assert len(lines) == 2, f"Expected exactly 2 dependencies in requirements.txt, got {lines}"
    assert any("uvicorn" in line for line in lines)
    assert any("asyncpg" in line for line in lines)

    # Verify that shims are removed
    assert (backend_path / "sqlalchemy").exists() is False
    assert (backend_path / "alembic").exists() is False

    # Verify native core imports
    import app.core.orm as sa
    from app.core.orm import postgresql as pg

    assert hasattr(sa, "select")
    assert hasattr(sa, "Table")
    assert hasattr(sa, "DeclarativeBase")
    assert hasattr(sa, "AsyncSession")
    assert hasattr(pg, "insert")
    assert hasattr(sa, "NULLTYPE")
    assert hasattr(sa, "SQLAlchemyError")
