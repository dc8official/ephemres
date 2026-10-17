import pytest
from app.core.orm import (
    text, select, func, insert as sa_insert, update as sa_update,
    compile_text_clause, CompilationContext
)
from app.models.endpoint import Endpoint
from app.models.event import EndpointEvent

def test_function_call_arithmetic_and_select_cte():
    cte = (
        select(
            EndpointEvent.endpoint_id,
            (func.count() * 60).label("duration_seconds"),
        )
        .group_by(EndpointEvent.endpoint_id)
        .cte("incidents_agg_cte")
    )
    stmt = select(cte).order_by(cte.c.endpoint_id.asc()).limit(10)
    ctx = CompilationContext()
    sql = stmt.compile(ctx)
    assert ("WITH incidents_agg_cte AS" in sql or 'WITH "incidents_agg_cte" AS' in sql)
    assert '(COUNT(*) * 60) AS "duration_seconds"' in sql
    assert 'FROM "incidents_agg_cte"' in sql

def test_update_compile_preserves_array_inet_type():
    stmt = (
        sa_update(Endpoint)
        .where(Endpoint.hostname == "router-1")
        .values(flow_exporter_ips=["10.0.0.1", "10.0.0.2"])
        .returning(Endpoint)
    )
    ctx = CompilationContext()
    sql = stmt.compile(ctx)
    # ARRAY(INET) must remain a Python list for asyncpg, NOT a JSON string
    assert isinstance(ctx.params[0], list)
    assert ctx.params[0] == ["10.0.0.1", "10.0.0.2"]
