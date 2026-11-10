"""The core ORM against SQLAlchemy 2.x, on a real migrated TimescaleDB.

twin_models.py loads the app's models a second time on SQLAlchemy. Each case below is one query
written once, against a namespace `o` (select, func, case, ...) and the models `M`, so the same
code builds a core statement and a SQLAlchemy statement. Both run on the same seeded database
and the returned rows must match, values and Python types alike. Queries use what the app uses
most (label, coalesce, sum, is_, in_, join, case, group_by, subquery, scalar_subquery, cte,
returning, update and delete); each runs in a transaction that is rolled back.

Needs EPHEMRES_TEST_TIMESCALE_DSN (tests/integration/timescale.sh start); skipped without it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import secrets
import types
import urllib.parse
import uuid

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine

from app.core import orm as core_orm
from twin_models import load_models

DSN_ENV = "EPHEMRES_TEST_TIMESCALE_DSN"
CORE_M, SA_M = load_models()


def _namespace(mod) -> types.SimpleNamespace:
    names = ("select", "update", "delete", "func", "and_", "or_", "case", "desc", "asc", "text",
             "literal_column")
    return types.SimpleNamespace(**{n: getattr(mod, n) for n in names if hasattr(mod, n)})


CORE_O, SA_O = _namespace(core_orm), _namespace(sa)


# --- database --------------------------------------------------------------------------------

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
ROLE: dict = {}  # filled from the migrated roles table
EP = [uuid.UUID(int=100 + i) for i in range(8)]

ENDPOINTS = [
    # hostname, ip, device_type, location, monitoring_enabled, deleted, exporter ips, aliases
    ("core-rtr-1", "10.0.0.1", "router", "dc1", True, False, ["10.0.0.1"], {"1": "wan"}),
    ("core-rtr-2", "10.0.0.2", "router", None, True, False, [], {}),
    ("sw-a", "10.0.1.1", "switch", "dc1", True, False, [], {}),
    ("sw-b", "10.0.1.2", "switch", "dc2", False, False, [], {}),
    ("srv-db", "10.0.2.10", "server", "dc2", True, False, [], {}),
    ("srv-web", "10.0.2.11", "server", None, True, False, [], {}),
    ("old-ap", "10.0.3.1", "access_point", "dc1", False, True, [], {}),
    ("fw-edge", "2001:db8::1", "firewall", "edge", True, False, [], {}),
]


async def _seed(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        # The migrations create the roles; use their ids.
        for row in await conn.fetch("SELECT id, role_name FROM roles WHERE role_name IN ('ADMIN', 'VIEWER')"):
            ROLE[row["role_name"]] = row["id"]
        for i, (user, role) in enumerate([("alice", "ADMIN"), ("bob", "VIEWER"), ("carol", "VIEWER")]):
            await conn.execute(
                "INSERT INTO users (id, username, password_hash, role_id, is_active, last_login) "
                "VALUES ($1, $2, 'x', $3, $4, $5)",
                uuid.UUID(int=10 + i), user, ROLE[role], i != 2, T0 + timedelta(hours=i) if i else None,
            )
        for i, (host, ip, dtype, loc, enabled, deleted, exp, aliases) in enumerate(ENDPOINTS):
            await conn.execute(
                "INSERT INTO endpoints (id, ip_address, hostname, device_type, location, monitoring_enabled, "
                "endpoint_status, deleted_at, flow_exporter_ips, flow_interface_aliases) "
                "VALUES ($1, $2::inet, $3, $4, $5, $6, $7, $8, $9::inet[], $10::jsonb)",
                EP[i], ip, host, dtype, loc, enabled, "DELETED" if deleted else "ACTIVE",
                T0 if deleted else None, exp, json.dumps(aliases),
            )
        # Events for the first five endpoints: alternating UP/DOWN, the last one still open.
        for i in range(5):
            for j in range(4 + i):
                start = T0 + timedelta(hours=j, minutes=i)
                is_open = j == 3 + i
                await conn.execute(
                    "INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count, "
                    "failed_count, health_score, avg_rtt_ms, start_time, end_time, duration_seconds) "
                    "VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8, $9)",
                    EP[i], "DOWN" if j % 2 else "UP", 10 - j, j, 100 - 7.5 * j,
                    None if j == 2 else 1.25 * (j + 1), start,
                    None if is_open else start + timedelta(minutes=50), None if is_open else 3000,
                )
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def dsn():
    admin = os.environ.get(DSN_ENV)
    if not admin:
        pytest.skip(f"{DSN_ENV} is not set (tests/integration/timescale.sh start)")
    name = f"ephemres_diff_{secrets.token_hex(4)}"
    db_dsn = urllib.parse.urlunsplit(urllib.parse.urlsplit(admin)._replace(path=f"/{name}"))

    async def setup():
        conn = await asyncpg.connect(admin)
        await conn.execute(f'CREATE DATABASE "{name}"')
        await conn.close()
        from app.core import migrator
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_dsn
        try:
            await migrator.upgrade_database("head")
        finally:
            os.environ.pop("DATABASE_URL", None) if previous is None else os.environ.__setitem__("DATABASE_URL", previous)
        await _seed(db_dsn)

    async def teardown():
        conn = await asyncpg.connect(admin)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()

    asyncio.run(setup())
    yield db_dsn
    asyncio.run(teardown())


# --- running one query on both ---------------------------------------------------------------

def _plain(value):
    """Rows and ORM objects as comparable plain values, keeping each value's Python type."""
    if hasattr(value, "__table__"):
        # Attribute names from the SQLAlchemy twin of the same model, so both sides are read
        # the same way (the core's __table__.columns holds names, SQLAlchemy's Column objects).
        attrs = sa.inspect(getattr(SA_M, type(value).__name__)).column_attrs
        return {a.key: getattr(value, a.key) for a in attrs}
    if isinstance(value, (tuple, list)) or type(value).__name__ in ("Row", "_Row", "RowProxy"):
        return tuple(_plain(v) for v in value)
    return value


async def _run(engine_kind: str, dsn: str, case) -> list:
    url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    o, M = (CORE_O, CORE_M) if engine_kind == "core" else (SA_O, SA_M)
    if engine_kind == "core":
        engine = core_orm.create_async_engine(url, pool_size=1, max_overflow=0)
        session = core_orm.AsyncSession(bind=engine)
    else:
        engine = sa_create_async_engine(url)
        session = SAAsyncSession(bind=engine)
    try:
        return await case(o, M, session)
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


def _typed(rows):
    """Each value with its type name, so 1 and 1.0, or a UUID and its text, don't compare equal."""
    def tag(v):
        if isinstance(v, dict):
            return {k: tag(x) for k, x in v.items()}
        if isinstance(v, tuple):
            return tuple(tag(x) for x in v)
        return (type(v).__name__, v)
    return [tag(r) for r in rows]


# --- the queries -----------------------------------------------------------------------------

async def live_endpoints_as_objects(o, M, s):
    q = o.select(M.Endpoint).where(M.Endpoint.deleted_at.is_(None)).order_by(M.Endpoint.hostname)
    return [_plain(e) for e in (await s.execute(q)).scalars().all()]


async def columns_with_in_desc_limit_offset(o, M, s):
    q = (o.select(M.Endpoint.hostname, M.Endpoint.device_type)
         .where(M.Endpoint.device_type.in_(["router", "switch", "server"]))
         .order_by(o.desc(M.Endpoint.hostname)).limit(3).offset(1))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def count_by_type(o, M, s):
    q = (o.select(M.Endpoint.device_type, o.func.count(M.Endpoint.id).label("n"))
         .group_by(M.Endpoint.device_type).order_by(M.Endpoint.device_type))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def coalesce_and_label(o, M, s):
    q = (o.select(M.Endpoint.hostname, o.func.coalesce(M.Endpoint.location, "unknown").label("loc"))
         .order_by(M.Endpoint.hostname))
    return [dict(r) for r in (await s.execute(q)).mappings().all()]


async def or_and_is_not(o, M, s):
    q = (o.select(M.Endpoint.hostname)
         .where(o.or_(M.Endpoint.monitoring_enabled.is_(False),
                      o.and_(M.Endpoint.device_type == "server", M.Endpoint.location.is_not(None))))
         .order_by(M.Endpoint.hostname))
    return (await s.execute(q)).scalars().all()


async def case_expression(o, M, s):
    kind = o.case((M.Endpoint.device_type == "router", "L3"),
                  (M.Endpoint.device_type == "switch", "L2"), else_="host").label("layer")
    q = o.select(M.Endpoint.hostname, kind).order_by(M.Endpoint.hostname)
    return [_plain(r) for r in (await s.execute(q)).all()]


async def join_sum_max_min(o, M, s):
    E, V = M.Endpoint, M.EndpointEvent
    q = (o.select(E.hostname, o.func.sum(V.duration_seconds).label("down"),
                  o.func.max(V.health_score).label("best"), o.func.min(V.avg_rtt_ms).label("rtt"))
         .join(V, V.endpoint_id == E.id).group_by(E.hostname).order_by(E.hostname))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def outerjoin_keeps_endpoints_without_events(o, M, s):
    E, V = M.Endpoint, M.EndpointEvent
    q = (o.select(E.hostname, o.func.count(V.id).label("events"))
         .outerjoin(V, V.endpoint_id == E.id).group_by(E.hostname).order_by(E.hostname))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def subquery_latest_event(o, M, s):
    V = M.EndpointEvent
    latest = (o.select(V.endpoint_id, o.func.max(V.start_time).label("last_start"))
              .group_by(V.endpoint_id).subquery())
    q = (o.select(M.Endpoint.hostname, latest.c.last_start)
         .join(latest, latest.c.endpoint_id == M.Endpoint.id).order_by(M.Endpoint.hostname))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def scalar_subquery_count(o, M, s):
    V = M.EndpointEvent
    n = (o.select(o.func.count(V.id)).where(V.endpoint_id == M.Endpoint.id)
         .scalar_subquery().label("n"))
    q = o.select(M.Endpoint.hostname, n).order_by(M.Endpoint.hostname)
    return [_plain(r) for r in (await s.execute(q)).all()]


async def distinct_types(o, M, s):
    q = o.select(M.Endpoint.device_type).distinct().order_by(M.Endpoint.device_type)
    return (await s.execute(q)).scalars().all()


async def cte_of_down_events(o, M, s):
    V = M.EndpointEvent
    down = o.select(V.endpoint_id, V.start_time).where(V.operational_state == "DOWN").cte("down")
    q = (o.select(down.c.endpoint_id, o.func.count().label("n"))
         .group_by(down.c.endpoint_id).order_by(down.c.endpoint_id))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def open_events_and_now(o, M, s):
    V = M.EndpointEvent
    q = (o.select(V.endpoint_id, V.start_time, V.end_time, V.duration_seconds)
         .where(V.end_time.is_(None), V.start_time < o.func.now()).order_by(V.endpoint_id))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def numeric_and_null_values(o, M, s):
    V = M.EndpointEvent
    q = (o.select(V.health_score, V.avg_rtt_ms, V.is_split_event)
         .where(V.endpoint_id == EP[0]).order_by(V.start_time))
    return [_plain(r) for r in (await s.execute(q)).all()]


async def inet_array_and_jsonb_columns(o, M, s):
    E = M.Endpoint
    q = o.select(E.ip_address, E.flow_exporter_ips, E.flow_interface_aliases).order_by(E.hostname)
    return [_plain(r) for r in (await s.execute(q)).all()]


async def text_with_parameters(o, M, s):
    q = o.text("SELECT hostname, device_type FROM endpoints WHERE device_type = :t ORDER BY hostname")
    return [_plain(r) for r in (await s.execute(q, {"t": "router"})).all()]


async def text_returns_jsonb_inet_and_arrays(o, M, s):
    # Raw SQL, as most of the app's queries are written: nothing tells either ORM the column types.
    q = o.text("SELECT hostname, ip_address, flow_exporter_ips, flow_interface_aliases "
               "FROM endpoints ORDER BY hostname")
    return [_plain(r) for r in (await s.execute(q)).all()]


async def get_by_primary_key(o, M, s):
    return [_plain(await s.get(M.Endpoint, EP[4])), await s.get(M.Endpoint, uuid.UUID(int=999))]


async def users_with_role_relationship(o, M, s):
    q = o.select(M.User).order_by(M.User.username)
    users = (await s.execute(q)).scalars().all()
    return [(_plain(u), u.role.role_name if u.role else None) for u in users]


async def update_returning_then_read(o, M, s):
    E = M.Endpoint
    q = (o.update(E).where(E.location.is_(None)).values(location="moved")
         .returning(E.hostname))
    changed = sorted((await s.execute(q)).scalars().all())
    after = (await s.execute(o.select(E.hostname, E.location).order_by(E.hostname))).all()
    return [changed, [_plain(r) for r in after]]


async def delete_returning_then_count(o, M, s):
    V = M.EndpointEvent
    q = o.delete(V).where(V.operational_state == "DOWN", V.endpoint_id.in_(EP[:2])).returning(V.endpoint_id)
    gone = sorted((await s.execute(q)).scalars().all())
    left = (await s.execute(o.select(o.func.count(V.id)))).scalar()
    return [gone, left]


CASES = [
    live_endpoints_as_objects, columns_with_in_desc_limit_offset, count_by_type,
    coalesce_and_label, or_and_is_not, case_expression, join_sum_max_min,
    outerjoin_keeps_endpoints_without_events, subquery_latest_event, scalar_subquery_count,
    distinct_types, cte_of_down_events, open_events_and_now, numeric_and_null_values,
    inet_array_and_jsonb_columns, text_with_parameters, text_returns_jsonb_inet_and_arrays,
    get_by_primary_key,
    users_with_role_relationship, update_returning_then_read, delete_returning_then_count,
]


# --- known differences ------------------------------------------------------------------------

def _map(value, fn):
    if isinstance(value, dict):
        return {k: _map(v, fn) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return type(value)(_map(v, fn) for v in value)
    return fn(value)


def _ip_as_text(value):
    return str(value) if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else value


def _json_text_parsed(value):
    if isinstance(value, str) and value[:1] in "{[":
        return json.loads(value)
    return value


# case name -> (side to normalise, how, why the difference is kept). The test checks the raw
# results really differ first, so an entry the core no longer needs fails as stale.
KNOWN_DIFFERENCES = {
    **{name: ("sqlalchemy", _ip_as_text,
              "The core hands inet values on models and ORM-selected columns to the app as text "
              "(orm.hydrate_column_value), and the app's code and schemas expect text; "
              "SQLAlchemy returns ipaddress objects.")
       for name in ("live_endpoints_as_objects", "inet_array_and_jsonb_columns", "get_by_primary_key")},
    "text_returns_jsonb_inet_and_arrays": (
        "core", _json_text_parsed,
        "In raw text() SQL the core returns jsonb as JSON text (asyncpg's default) while "
        "SQLAlchemy installs a JSON codec and returns dicts. The app's text() queries are written "
        "for text (json.loads where they read jsonb), so changing it means auditing every one."),
}


@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_the_core_returns_what_sqlalchemy_returns(dsn, case):
    expected = asyncio.run(_run("sqlalchemy", dsn, case))
    actual = asyncio.run(_run("core", dsn, case))
    assert expected, "the query returned nothing, so it compares nothing; fix the seed data"
    known = KNOWN_DIFFERENCES.get(case.__name__)
    if known:
        assert _typed(actual) != _typed(expected), f"stale KNOWN_DIFFERENCES entry: {case.__name__}"
        side, fn, _reason = known
        if side == "core":
            actual = _map(actual, fn)
        else:
            expected = _map(expected, fn)
    assert _typed(actual) == _typed(expected)
