# Ephemres v3.3.2v Vertical Integration Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all Critical, High, and Medium bugs, security vulnerabilities, and multi-process regressions introduced in the `v3.3.2v` Vertical Integration release while preserving its zero-external-dependency architecture.

**Architecture:** Fixes are grouped into 8 self-contained, independently testable tasks across the custom ASGI layer (`asgi.py`), custom ORM (`orm.py`), custom FastStore IPC engine (`faststore.py` / `driver_manager.py`), SSRF/HTTP & Synthetic probing (`http_client.py` / `synthetic.py`), Raw ICMP & Diagnostics (`ping.py` / `diagnostics.py` / `netlink.py`), Template & PDF engines (`template.py` / `pdf.py`), Frontend `fetch` & SSE clients (`api.js` / `EndpointDetailView.vue`), and Deployment/Systemd configs (`deploy/*`).

**Tech Stack:** Python 3.12 (`asyncio`, `asyncpg`, `sqlite3`, `ctypes`), Vue 3 (Vite, native `fetch`, `EventSource`), PostgreSQL / TimescaleDB, Linux `systemd`.

## Global Constraints

- **Zero External Python Package Additions:** Do not re-introduce `fastapi`, `starlette`, `pydantic`, `sqlalchemy`, `alembic`, `redis`, `httpx`, `jinja2`, `weasyprint`, `pypdf`, or `icmplib`. Use only Python stdlib and existing dependencies in `backend/requirements.txt` (`asyncpg`, `uvicorn`, `argon2-cffi`, `PyJWT`, `psutil`).
- **Test Runner Command:** Run backend tests from `/home/creme/Learn/ephemres` using `./backend/venv/bin/pytest`.

## Review Focus

1. **Infinite `StreamingResponse` (SSE) through `BaseHTTPMiddleware`:** `GET /api/v1/events/stream` must flush `http.response.start` and yield SSE chunks immediately without buffering until stream termination.
2. **Batch `executemany` with `list[dict]` in `AsyncSession.execute`:** Passing a `list[dict]` to `session.execute(text(...), chunk)` in `FlowAggregator.flush_to_timescaledb` must compile `:param` placeholders from `chunk[0]` and invoke `conn.executemany(sql, args_list)`.
3. **Cross-process Pub/Sub in `FastStore`:** Events published in `ephemres-engine` or `ephemres-api` must be received by subscribers in other OS processes (`ephemres-api` workers, `ephemres-flowd`).
4. **`Insert`/`Update`/`Delete` with `.returning(Model)`:** Repository methods calling `.returning(Endpoint)` or `.returning(User)` followed by `.scalar_one_or_none()` must receive a hydrated `DeclarativeBase` model instance, not a bare `UUID`.
5. **Dual-stack & IPv4-mapped IPv6 SSRF validation:** `validate_probe_target` in `monitoring/synthetic.py` must block `::1`, `fe80::/10`, `fd00:ec2::254`, and `::ffff:169.254.169.254`, and fail closed on DNS resolution errors.

---

### Task 1: Fix Custom ORM Execution, Batching, CTEs, Hydration & Type Binding

**Files:**
- Modify: `backend/app/core/orm.py`
- Modify: `backend/migrations/env.py`
- Test: `backend/tests/test_orm_vertical_fixes.py`

**Interfaces:**
- Produces:
  - `AsyncConnection.execute(self, statement: Any, parameters: Any = None) -> Result` (returns fetched `Row`s for `SELECT`/`WITH`/`SHOW`/`VALUES`/`RETURNING` queries, and supports `list[dict]` batch parameters via `executemany`)
  - `AsyncSession.execute(self, statement: Any, parameters: Any = None) -> Result` (supports `list[dict]` batch parameters via `conn.executemany`, recognizes `WITH ... SELECT` as row-returning, and hydrates `DeclarativeBase` models when `statement._returning` contains a model class)
  - `Expression` & `FunctionCall` arithmetic operators (`__add__`, `__radd__`, `__sub__`, `__rsub__`, `__mul__`, `__rmul__`, `__truediv__`)
  - `CTE.compile(self, ctx: CompilationContext) -> str` and `Select` support for `select(cte)`

- [ ] **Step 1: Write the failing tests in `backend/tests/test_orm_vertical_fixes.py`**

```python
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
    assert "WITH incidents_agg_cte AS" in sql
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
    assert isinstance(ctx.params[1], list)
    assert ctx.params[1] == ["10.0.0.1", "10.0.0.2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_orm_vertical_fixes.py -v`
Expected: FAIL with `TypeError: unsupported operand type(s) for *: 'FunctionCall' and 'int'`

- [ ] **Step 3: Implement ORM fixes in `backend/app/core/orm.py` and `backend/migrations/env.py`**

1. **Arithmetic operators on `Expression` & `FunctionCall` (`orm.py:424-465`, `553-623`):**
   - Make `FunctionCall` inherit from `Expression` (instead of directly from `SQLElement`).
   - On `Expression`, implement `__add__`, `__radd__`, `__sub__`, `__rsub__`, `__mul__`, `__rmul__`, `__truediv__` returning `BinaryExpression(self, op, other)` (or `BinaryExpression(other, op, self)` for reflected ops).
2. **`select(cte)` compilation (`orm.py:783-813`, `1395-1415`, `1487-1511`):**
   - In `Select._compile_select_list` (`orm.py:1395-1415`), handle `isinstance(ent, CTE)` by appending `f'"{ent.name}".*'`.
   - In `Select._derive_from_clause` (`orm.py:1487-1511`), when `isinstance(ent, CTE)`, set `base_table = f'"{ent.name}"'`.
   - In `Select._collect_ctes` (`orm.py:1349-1377`), also inspect `InExpression.target` (if `Subquery` or `Select`) and `Subquery` / `ScalarSubquery`.
3. **Pass `col_type` in `Insert.compile` and `Update.compile` (`orm.py:1570-1605`, `1655-1670`):**
   - Look up `col_type = model_cols[k].type if k in model_cols else None` and call `ctx.add_param(v, col_type)` so `ARRAY(INET)` lists are not `json.dumps()`-encoded by `prepare_bind_param`.
   - Copy `vals = dict(self._values)` in `Update.compile` (`orm.py:1657`) instead of mutating `self._values` in-place.
4. **Support `list[dict]` batch `executemany`, `WITH ... SELECT` CTE queries, and `AsyncConnection.execute` row fetching (`orm.py:841-889`, `2082-2096`, `2461-2505`):**
   - Add a helper `_is_row_returning_sql(sql: str) -> bool` that strips leading whitespace/comments and checks `upper.startswith(("SELECT", "WITH", "SHOW", "VALUES", "TABLE", "EXPLAIN")) or "RETURNING" in upper`.
   - When `isinstance(statement, TextClause)` and `isinstance(parameters, (list, tuple))` (non-empty sequence of `dict`s), compile placeholder names using `parameters[0]`, build the parameter value tuple for each dict in `parameters` via `prepare_bind_param`, and execute `await conn.executemany(sql, args_list)`.
   - In `AsyncConnection.execute` (`orm.py:2082-2096`), if `_is_row_returning_sql(sql)` is true, call `records = await self._conn.fetch(sql, *params)` and return `Result( [Row(list(r.values()), list(r.keys())) for r in records], rowcount=len(records) )`.
5. **Hydrate `.returning(Model)` in `AsyncSession.execute` (`orm.py:2490-2498`):**
   - When `has_returning` is true, check if `statement._returning` has a `DeclarativeBase` subclass (e.g. `ret_models = [r for r in getattr(statement, "_returning", []) if isinstance(r, type) and issubclass(r, DeclarativeBase)]`). If `len(ret_models) == 1`, hydrate each `rec` in `records` via `self._hydrate_instance_from_record(ret_models[0], dict(rec))` (and update `self._identity_map` and `inst._orm_state`) before wrapping into `Row([inst], [ret_models[0].__name__])`.
6. **Fix `_hydrate_select_results` lowercase function lookup & dirty state (`orm.py:2548-2566`):**
   - In `_hydrate_select_results` (`orm.py:2564`), fall back to `rec_dict.get(col_key, rec_dict.get(col_key.lower()))` or positional `vals[slot_idx]`.
   - When refreshing an existing identity-map instance (`orm.py:2550-2555`), skip overwriting attributes that are currently dirty if `inst in self._dirty`, and update `inst._orm_state[c_name] = val` for clean attributes.
7. **Fix `backend/migrations/env.py` (`env.py:8-16`):**
   - Guard `if context is not None:` before accessing `context.config` and `getattr(Base, "metadata", None)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/venv/bin/pytest backend/tests/test_orm_vertical_fixes.py backend/tests/test_reports_api.py backend/tests/test_alert_dispatcher.py -v`
Expected: PASS

---

### Task 2: Fix ASGI Streaming (`BaseHTTPMiddleware`), Router Dependencies, `response_model`, and Query/BackgroundTasks Injection

**Files:**
- Modify: `backend/app/core/asgi.py`
- Modify: `backend/app/core/schema.py`
- Test: `backend/tests/test_asgi_vertical_fixes.py`

**Interfaces:**
- Produces:
  - Streaming-compatible `BaseHTTPMiddleware.__call__` in `backend/app/core/asgi.py`
  - Execution of `APIRouter.dependencies` and `Route.dependencies` in `EphemresApp._dispatch_request`
  - Response filtering/validation through `Route.response_model`
  - Shared `BackgroundTasks` injection regardless of parameter name

- [ ] **Step 1: Write the failing tests in `backend/tests/test_asgi_vertical_fixes.py`**

```python
import asyncio
import pytest
from app.core.asgi import (
    FastAPI, APIRouter, Depends, HTTPException, Request,
    StreamingResponse, BaseHTTPMiddleware, BackgroundTasks
)
from app.core.schema import BaseModel

class DummyHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Test-Middleware"] = "1"
        return resp

@pytest.mark.asyncio
async def test_streaming_response_through_base_http_middleware():
    app = FastAPI()
    app.add_middleware(DummyHeaderMiddleware)

    async def infinite_sse():
        yield b"data: first\n\n"
        await asyncio.sleep(3600)

    @app.get("/stream")
    async def stream():
        return StreamingResponse(infinite_sse(), media_type="text/event-stream")

    first_chunk_received = asyncio.Event()
    headers_received = {}

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            headers_received["headers"] = dict(msg["headers"])
        elif msg["type"] == "http.response.body" and msg.get("body"):
            first_chunk_received.set()

    scope = {"type": "http", "method": "GET", "path": "/stream", "headers": [], "query_string": b""}
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first_chunk_received.wait(), timeout=1.0)
    assert headers_received["headers"][b"x-test-middleware"] == b"1"
    task.cancel()

@pytest.mark.asyncio
async def test_router_dependencies_and_query_defaults():
    app = FastAPI()
    guard_called = False

    async def verify_guard():
        nonlocal guard_called
        guard_called = True

    router = APIRouter(dependencies=[Depends(verify_guard)])

    @router.get("/items")
    async def list_items(limit: int = 25, bg: BackgroundTasks = None):
        if bg:
            bg.add_task(lambda: None)
        return {"limit": limit}

    app.include_router(router)
    from app.core.asgi import TestClient
    client = TestClient(app)
    resp = client.get("/items?limit=10")
    assert resp.status_code == 200
    assert resp.json() == {"limit": 10}
    assert guard_called is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_asgi_vertical_fixes.py -v`
Expected: FAIL (`TimeoutError` on `test_streaming_response_through_base_http_middleware` and assertion failures on `limit == 25` / `guard_called is False`)

- [ ] **Step 3: Implement ASGI & Schema fixes in `backend/app/core/asgi.py` and `backend/app/core/schema.py`**

1. **Stream-safe `BaseHTTPMiddleware.__call__` (`asgi.py:1278-1318`):**
   - Replace the full-body buffering `call_next` with an `asyncio.Event` (`start_event`) and `asyncio.Queue` (`body_queue`).
   - Run `self.app(req.scope, req._receive, inner_send)` in a background `asyncio.Task`. When `inner_send` receives `http.response.start`, store `status` and `headers` and set `start_event`. When `inner_send` receives `http.response.body`, put `(body, more_body)` onto `body_queue` (and if `not more_body`, put a sentinel `None`).
   - `call_next` awaits `start_event.wait()` (or task completion if an exception was raised before `http.response.start`), and returns a `StreamingResponse` whose `content` async generator yields chunks from `body_queue` until `None` and awaits the inner task in `finally:` (cancelling it on client disconnect).
2. **Execute `APIRouter` and `Route` `dependencies` (`asgi.py:831-914`, `1590-1645`):**
   - In `APIRouter.add_api_route` and `APIRouter.include_router`, prepend `list(self.dependencies)` to each route's `dependencies`.
   - In `EphemresApp._dispatch_request` (`asgi.py:1615`), create a shared `dep_cache: Dict[Any, Any] = {}` and `bg_tasks = BackgroundTasks()`, resolve every `dep` in `route.dependencies` using `_resolve_dependency(dep, request, response, path_params, dep_cache, bg_tasks)`, and pass the same `dep_cache` and `bg_tasks` into `_resolve_endpoint_kwargs`.
3. **Fix Query Parameter & `BackgroundTasks` Resolution (`asgi.py:1045-1105`, `1654-1656`):**
   - Inject the shared `bg_tasks` instance whenever `annotation is BackgroundTasks` or `isinstance(default, BackgroundTasks)`, and execute `await bg_tasks.run()` after the response finishes sending (`asgi.py:1654`).
   - For any parameter not in `path_params` and not a special type (`Request`, `Response`, `BackgroundTasks`, `Depends`, `HeaderParamInfo`, `CookieParamInfo`) and not a `SchemaModel`/`dict` body parameter, resolve it from `request.query_params` if `param_name in request.query_params` (coercing via `_coerce_query_val` / `_coerce_value`), falling back to `default`.
4. **Enforce `Route.response_model` (`asgi.py:1633-1645`):**
   - When `route.response_model is not None` and `not isinstance(result, Response)`, coerce `result` via `TypeAdapter(route.response_model).validate_python(result)` before `JSONResponse` serialization.
5. **Clean up `asgi.__all__` (`asgi.py:2017-2047`) & `Decimal`/`date`/`model_validator(mode="before")` in `schema.py`:**
   - Remove undefined names from `__all__` in `asgi.py` and handle `Decimal` / `datetime.date` in `_json_serialize` (`asgi.py:599-613`) and `_serialize_val` / `_coerce_value` in `schema.py` (`schema.py:353, 590, 925`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/venv/bin/pytest backend/tests/test_asgi_vertical_fixes.py -v`
Expected: PASS

---

### Task 3: Fix FastStore Expiration, `/dev/shm` WAL Memory Bounding, Pipeline Batching & `xreadgroup` Blocking

**Files:**
- Modify: `backend/app/core/faststore.py`
- Test: `backend/tests/test_faststore_vertical_fixes.py`

**Interfaces:**
- Produces:
  - Multi-table expiration eviction in `FastStore._evict_expired_sync` (`key_expiry` table cascading delete across `kv_store`, `set_store`, `zset_store`, `stream_entries`)
  - Bounded `/dev/shm` WAL memory footprint (`PRAGMA wal_autocheckpoint=1000; PRAGMA journal_size_limit=16777216;`) and `weakref.WeakSet` for `_PUBSUB_WAITERS`
  - Single-transaction `Pipeline.execute()` and throttled `xadd` `maxlen` trimming

- [ ] **Step 1: Write the failing tests in `backend/tests/test_faststore_vertical_fixes.py`**

```python
import asyncio
import pytest
from app.core.faststore import FastStoreClient

@pytest.mark.asyncio
async def test_faststore_set_expiration_evicts_set_members():
    client = FastStoreClient(db=9)
    await client.flushdb()
    await client.sadd("flow:exporter:1:interfaces", "10", "20")
    await client.expire("flow:exporter:1:interfaces", 1)
    # Set key should not pollute string kv_store with empty string before expiry
    assert await client.get("flow:exporter:1:interfaces") is None
    await asyncio.sleep(1.1)
    assert await client.smembers("flow:exporter:1:interfaces") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_faststore_vertical_fixes.py -v`
Expected: FAIL (`await client.get(...)` returns `""` and `smembers` still returns `{"10", "20"}` after expiry)

- [ ] **Step 3: Implement FastStore fixes in `backend/app/core/faststore.py`**

1. **Dedicated Expiration Tracking & Full Eviction (`faststore.py:64-122`, `424-426`, `555-583`):**
   - Add table `CREATE TABLE IF NOT EXISTS key_expiry (k TEXT PRIMARY KEY, expires_at REAL NOT NULL)` in `_init_schema`.
   - In `_init_schema` (`faststore.py:67-70`), add `cur.execute("PRAGMA wal_autocheckpoint=1000;")` and `cur.execute("PRAGMA journal_size_limit=16777216;")` to cap `/dev/shm` WAL file growth at 16 MB.
   - In `expire(name, time_sec)`, update `kv_store.expires_at` if `name` is in `kv_store`, and always `INSERT OR REPLACE INTO key_expiry (k, expires_at) VALUES (?, ?)` without inserting a dummy `b""` row into `kv_store`.
   - In `_evict_expired_sync(conn)`, query `SELECT k FROM key_expiry WHERE expires_at <= ?` and delete expired keys from `kv_store`, `set_store`, `zset_store`, `stream_entries`, `stream_groups`, `stream_pel`, and `key_expiry`.
   - Clear `key_expiry` when `delete()` or `flushdb()` is called.
2. **Prevent `_PUBSUB_WAITERS` Queue Leak (`faststore.py:42, 236`):**
   - Use `weakref.WeakSet` for `_PUBSUB_WAITERS[channel]` values so unclosed `PubSub` instances do not leak queues in memory.
3. **Batch `Pipeline.execute()` & Throttle `xadd` Trimming (`faststore.py:277-350`, `1022-1071`):**
   - In `xadd` (`faststore.py:1060-1068`), when `approximate=True` and `maxlen` is set, only execute the `DELETE FROM stream_entries ...` trim query every 100 inserts instead of on every `xadd`.
   - In `Pipeline.execute()`, execute the batch under a single SQLite `BEGIN` / `COMMIT` transaction block rather than committing each command individually.
4. **Fix `FastStoreClient.xreadgroup` `block` & `ping()` (`faststore.py:381-383`, `1115-1122`):**
   - In `xreadgroup`, loop in `0.05s` increments up to `block / 1000.0` seconds while `not res`.
   - In `ping()`, execute a real `SELECT 1` check on `self._store`.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/venv/bin/pytest backend/tests/test_faststore_vertical_fixes.py -v`
Expected: PASS

---

### Task 4: Fix SSRF Validation, `follow_redirects`, Chunked Decoding & Multi-Packet Reads in HTTP Client & Synthetic Monitor

**Files:**
- Modify: `backend/app/core/http_client.py`
- Modify: `monitoring/synthetic.py`
- Modify: `backend/app/services/alert_dispatcher.py`
- Test: `backend/tests/test_http_and_synthetic_fixes.py`

**Interfaces:**
- Produces:
  - `SSRFSafeHttpClient.get(self, url: str, headers: Optional[Dict[str, str]] = None, follow_redirects: Optional[bool] = None, timeout: Optional[float] = None) -> HttpResponse`
  - `SSRFSafeHttpClient.request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None, content: Optional[ bytes | str ] = None, json_data: Any = None, follow_redirects: Optional[bool] = None, timeout: Optional[float] = None) -> HttpResponse`
  - Fail-closed dual-stack `validate_probe_target(target_host: str) -> str` in `monitoring/synthetic.py`

- [ ] **Step 1: Write the failing tests in `backend/tests/test_http_and_synthetic_fixes.py`**

```python
import pytest
from app.core.http_client import _decode_chunked_body, SSRFSafeHttpClient
from monitoring.synthetic import validate_probe_target

def test_decode_chunked_body_negative_chunk_size_does_not_hang():
    malformed = b"-2\r\nXX\r\n0\r\n\r\n"
    res = _decode_chunked_body(malformed)
    assert res == b""

def test_validate_probe_target_blocks_ipv4_mapped_ipv6_and_metadata():
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("::ffff:169.254.169.254")
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("::ffff:127.0.0.1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("fd00:ec2::254")

@pytest.mark.asyncio
async def test_ssrf_http_client_get_accepts_follow_redirects(monkeypatch):
    client = SSRFSafeHttpClient(timeout=2.0)
    # Verify signature accepts follow_redirects=False without TypeError
    import inspect
    sig = inspect.signature(client.get)
    assert "follow_redirects" in sig.parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_http_and_synthetic_fixes.py -v`
Expected: FAIL (infinite loop or failure on negative chunk size, `::ffff:169.254.169.254` not blocked, `follow_redirects` missing from `client.get`)

- [ ] **Step 3: Implement HTTP Client & Synthetic Probe fixes**

1. **Fix `_decode_chunked_body`, Body Reading & Async DNS in `backend/app/core/http_client.py`:**
   - In `_decode_chunked_body` (`http_client.py:175`), change `if chunk_size == 0:` to `if chunk_size <= 0 or chunk_start + chunk_size > n: break`.
   - In `SSRFSafeBackend.resolve_and_validate` (`http_client.py:120`), use `loop = asyncio.get_running_loop(); addr_info = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)`.
   - In `SSRFSafeHttpClient.request`, `get`, and `post` (`http_client.py:191-265`), accept `follow_redirects: Optional[bool] = None` and `timeout: Optional[float] = None`.
   - In `_raw_request` (`http_client.py:370`), read chunks in a `while len(body_buf) < 65536:` loop until EOF or `Content-Length` is reached.
2. **Harden `validate_probe_target` in `monitoring/synthetic.py:18-59`:**
   - Add `ipaddress.ip_network("fd00:ec2::254/128")` to `RESTRICTED_NETWORKS`.
   - Add a helper `_check_ip_restricted(ip_obj)` that unwraps `if getattr(ip_obj, "ipv4_mapped", None): ip_obj = ip_obj.ipv4_mapped` before checking `any(ip_obj in net for net in RESTRICTED_NETWORKS)`.
   - Replace `socket.gethostbyname(cleaned)` with `socket.getaddrinfo(cleaned, None, socket.AF_UNSPEC, socket.SOCK_STREAM)`, validate **every** resolved IP via `_check_ip_restricted`, and do not swallow `socket.gaierror` into returning an unvalidated hostname (either raise `ValueError(f"DNS resolution failed for {cleaned}: {exc}")` or return only validated IPs).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/venv/bin/pytest backend/tests/test_http_and_synthetic_fixes.py backend/tests/test_synthetic_ssrf.py -v`
Expected: PASS

---

### Task 5: Fix Raw Socket ICMP Multiplexer Spoofing/Collisions, Subprocess Cleanup & Netlink Caching

**Files:**
- Modify: `monitoring/ping.py`
- Modify: `backend/app/services/diagnostics.py`
- Modify: `backend/app/core/netlink.py`
- Test: `backend/tests/test_ping_and_diagnostics_fixes.py`

**Interfaces:**
- Produces:
  - Source-IP and timestamp verified `_RawICMPMultiplexer` in `monitoring/ping.py`
  - Subprocess kill/reap on timeout/cancellation in `monitoring/ping.py` and `backend/app/services/diagnostics.py`
  - IPv6-capable `_parse_trace_output` in `backend/app/services/diagnostics.py`
  - TTL-cached `get_local_interfaces_and_subnets()` in `backend/app/core/netlink.py`

- [ ] **Step 1: Write the failing tests in `backend/tests/test_ping_and_diagnostics_fixes.py`**

```python
import asyncio
import struct
import socket
import pytest
from monitoring.ping import _RawICMPMultiplexer, _pack_icmp_echo
from app.services.diagnostics import _parse_trace_output

@pytest.mark.asyncio
async def test_raw_icmp_multiplexer_ignores_wrong_source_ip():
    mux = _RawICMPMultiplexer(is_ipv6=False)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    mux._pending[(1234, 1)] = (fut, "1.1.1.1", 1000)

    # Craft an Echo Reply (type 0) with matching ident=1234, seq=1, ts=1000 from a spoofed IP 9.9.9.9
    reply_pkt = struct.pack("!BBHHH", 0, 0, 0, 1234, 1) + struct.pack("!Q", 1000)
    # Simulate _handle_packet(reply_pkt, "9.9.9.9")
    mux._handle_incoming_packet(reply_pkt, "9.9.9.9")
    assert not fut.done()

    # Now from expected IP 1.1.1.1
    mux._handle_incoming_packet(reply_pkt, "1.1.1.1")
    assert fut.done()

def test_parse_trace_output_supports_ipv6():
    sample = " 1  2001:db8::1 (2001:db8::1)  1.234 ms  1.100 ms  1.050 ms\n"
    hops = _parse_trace_output(sample, "2001:db8::2")
    assert len(hops) == 1
    assert hops[0].ip == "2001:db8::1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_ping_and_diagnostics_fixes.py -v`
Expected: FAIL (`_handle_incoming_packet` not defined / `_parse_trace_output` returns `ip=None` for IPv6)

- [ ] **Step 3: Implement Ping, Diagnostics & Netlink fixes**

1. **Verify Source IP & Embedded Timestamp in `_RawICMPMultiplexer` (`monitoring/ping.py:177-270`):**
   - Store `(fut, target_ip, send_ns)` in `self._pending[(ident, seq)]` (or key by `(ident, seq)` and inspect before popping).
   - Extract `_handle_incoming_packet(self, raw_bytes: bytes, responder_ip: str) -> None` called by `_on_readable`.
   - Look up `entry = self._pending.get((r_ident, r_seq))`. Only pop and resolve `fut` if `responder_ip == expected_ip` (comparing normalized `ipaddress.ip_address` forms) and (`_embedded_ns == 0 or _embedded_ns == send_ns`).
   - In `_run_dgram_socket_ping_cycle` (`monitoring/ping.py:360`), catch `(asyncio.TimeoutError, OSError)` per probe so a single `OSError` does not abort the entire cycle.
   - In `run_system_ping_fallback` (`monitoring/ping.py:403-413`), wrap `asyncio.wait_for(proc.communicate(), timeout=timeout * count + 5.0)` in `try ... except (asyncio.TimeoutError, asyncio.CancelledError): proc.kill(); await proc.wait(); raise`.
2. **Support IPv6 Hops & Reap Timed-Out Subprocesses in `backend/app/services/diagnostics.py`:**
   - In `_parse_trace_output` (`diagnostics.py:173`), update the IP extraction regex to match both IPv4 and IPv6 addresses (validating candidate tokens with `ipaddress.ip_address`).
   - In `_run_subprocess_traceroute` (`diagnostics.py:443, 466, 485`), ensure `proc.kill(); await proc.wait()` is called whenever `wait_for(proc.communicate(), ...)` raises an exception or `CancelledError`.
3. **Cache `get_local_interfaces_and_subnets()` & Fix Default Netmask in `backend/app/core/netlink.py`:**
   - Add a 30-second monotonic TTL cache around `get_local_interfaces_and_subnets()`.
   - Change the fallback netmask on line 69 from `"255.255.255.0"` to `"255.255.255.255"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/venv/bin/pytest backend/tests/test_ping_and_diagnostics_fixes.py backend/tests/test_ping_parsing.py backend/tests/test_diagnostics.py -v`
Expected: PASS

---

### Task 6: Fix Template Engine Path Traversal/AST Sandbox & Native PDF Report Truncation + Corporate Logo Embedding

**Files:**
- Modify: `backend/app/core/template.py`
- Modify: `backend/app/core/pdf.py`
- Test: `backend/tests/test_template_and_pdf_fixes.py`

**Interfaces:**
- Produces:
  - Boundary-safe path checks (`Path.is_relative_to`) in `TemplateEnvironment._read_template_file` and `pdf.secure_url_fetcher`
  - Dunder-attribute blocking in `template._eval_ast_node`
  - Full PNG/JPEG PDF `/Subtype /Image` XObject (`/ImLogo`) embedding and side-by-side header placement (`x = margin_x` for logo, `text_x = margin_x + logo_draw_w + 10pt` for `company_name`, `department`, `report_type_label`) in `backend/app/core/pdf.py`
  - Multi-line paragraph wrapping, proportional table column widths, and `html.unescape` footer text in `backend/app/core/pdf.py`

- [ ] **Step 1: Write the failing tests in `backend/tests/test_template_and_pdf_fixes.py`**

```python
import base64
import pytest
from app.core.template import Template
from app.core.pdf import HTML

# Minimal valid 1x1 RGB PNG
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)

def test_template_blocks_dunder_attribute_access():
    tmpl = Template("{{ user.__class__.__name__ }}")
    rendered = tmpl.render(user="alice")
    assert rendered == ""

def test_pdf_embeds_logo_xobject_and_preserves_long_cells_and_footer():
    html_str = f"""
    <html>
      <head>
        <style>@page {{ @bottom-left {{ content: "Acme &amp; Co &#x27;2026&#x27;"; }} }}</style>
      </head>
      <body>
        <div class="doc-header">
          <img src="data:image/png;base64,{TINY_PNG_B64}" class="org-logo" />
          <h1>Acme Telecom</h1>
        </div>
        <table>
          <thead>
            <tr><th>Node</th><th>IP</th><th>Start</th><th>End</th><th>Dur</th><th>State</th><th>Loss</th><th>Jitter</th><th>RCA Summary</th></tr>
          </thead>
          <tbody>
            <tr>
              <td>core-router-lon-01</td>
              <td>192.0.2.254</td>
              <td>2026-10-17 11:00:00</td>
              <td>2026-10-17 11:15:00</td>
              <td>900s</td>
              <td>DOWN</td>
              <td>100%</td>
              <td>0ms</td>
              <td>Upstream transit hop 3 packet loss</td>
            </tr>
          </tbody>
        </table>
      </body>
    </html>
    """
    pdf_bytes = HTML(string=html_str).write_pdf()
    assert b"/Subtype /Image" in pdf_bytes
    assert b"/ImLogo Do" in pdf_bytes
    assert b"Acme & Co '2026'" in pdf_bytes
    assert b"core-router-lon-01" in pdf_bytes
    assert b"2026-10-17 11:00:00" in pdf_bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/venv/bin/pytest backend/tests/test_template_and_pdf_fixes.py -v`
Expected: FAIL (`rendered == "str"`, `b"/Subtype /Image"` missing, `b"core-router-lon-01"` truncated to 13 chars, footer contains raw `b"Acme &amp; Co"`)

- [ ] **Step 3: Implement Template & PDF fixes in `backend/app/core/template.py` and `backend/app/core/pdf.py`**

1. **Path Traversal & AST Hardening in `backend/app/core/template.py`:**
   - In `_eval_ast_node` (`template.py:106-110`), if `node.attr.startswith("_"): return ""`.
   - In `_eval_ast_node` (`template.py:154`), cap `ast.Mult` on `str`/`list`/`tuple` to a maximum multiplier of `10000`.
   - In `TemplateEnvironment._read_template_file` (`template.py:428`), replace `str(candidate).startswith(str(self.search_path))` with `candidate.is_relative_to(self.search_path.resolve())`.
2. **Path Traversal, Corporate Logo PDF Image XObject Embedding, Side-by-Side Header Placement & Table Layout in `backend/app/core/pdf.py`:**
   - In `secure_url_fetcher` (`pdf.py:103-105`), use `Path(abs_path).resolve().is_relative_to(Path(allowed_dir).resolve())`.
   - In `_ParsedReportDocument` (`pdf.py:204-225`), add `logo_bytes: Optional[bytes] = None` and `logo_mime: str = ""`.
   - In `_ReportHTMLParser.handle_starttag` (`pdf.py:262-266`), fetch `res = secure_url_fetcher(src)` (catching exceptions if `getattr(secure_url_fetcher, "_fail_on_errors", True)` is `False`) and store `self.doc.logo_bytes = res.raw_bytes` and `self.doc.logo_mime = res.content_type`.
   - Add a pure-Python image parser `_extract_pdf_image_info(raw_bytes: bytes, mime: str)` supporting:
     - **JPEG** (`\xff\xd8`): parse SOF0/SOF2 (`\xff\xc0` / `\xff\xc2`) for `(width, height, components)` and embed raw JPEG bytes with `/Filter /DCTDecode`.
     - **PNG** (`\x89PNG\r\n\x1a\n`): parse `IHDR` for `(width, height, bit_depth, color_type)`, decompress concatenated `IDAT` chunks (`zlib.decompress`), unfilter scanlines into RGB bytes (dropping or compositing alpha against white `#ffffff`), and re-compress via `zlib.compress` with `/Filter /FlateDecode /ColorSpace /DeviceRGB /BitsPerComponent 8`.
   - **Header Placement (`pdf.py:467-498`):**
     - If a valid logo image is decoded, compute aspect-ratio-preserving draw dimensions fitting within `max_h = 30.0pt, max_w = 95.0pt`.
     - Emit `q {draw_w:.2f} 0 0 {draw_h:.2f} {margin_x:.2f} {y - draw_h + 10:.2f} cm /ImLogo Do Q` in the page header stream and set `header_text_x = margin_x + draw_w + 10.0` for `company_name`, `department`, and `report_type_label` (defaulting to `header_text_x = margin_x` when no logo is present).
     - Register `/XObject << /ImLogo {logo_obj_id} 0 R >>` in the page `/Resources` dictionary inside `_assemble_pdf_binary`.
   - In `_ReportHTMLParser.handle_data` (`pdf.py:328-329`), unescape HTML entities via `self.doc.footer_text = html.unescape(m.group(1).strip())`.
   - In `_typeset_document_pages` (`pdf.py:559-610`):
     - Wrap `p` blocks across multiple lines using `textwrap.wrap(payload, width=105)` instead of slicing `payload[:115]`.
     - Compute proportional column widths based on the maximum string length of each column across header + rows (with font size `6.5pt` for wide tables `num_cols >= 7` and `max_chars = max(20, int(col_w / 3.1))`) so hostnames, IPs, and timestamps are not truncated to 13 characters.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/venv/bin/pytest backend/tests/test_template_and_pdf_fixes.py backend/tests/test_reporting_engine.py -v`
Expected: PASS

---

### Task 7: Fix Frontend Native `fetch` Client (`api.js`) & SSE Parsing (`EndpointDetailView.vue`)

**Files:**
- Modify: `frontend/src/services/api.js`
- Modify: `frontend/src/views/EndpointDetailView.vue`

**Interfaces:**
- Produces:
  - JSON error parsing on failed `blob` requests, single `401` redirect handling, array query param serialization, and `signal`/`timeout` support in `frontend/src/services/api.js`
  - Proper `JSON.parse(event.data)` handling in `frontend/src/views/EndpointDetailView.vue`

- [ ] **Step 1: Update `frontend/src/services/api.js`**

1. **Array query parameters (`api.js:15-21`):**
   - When `Array.isArray(value)`, loop `for (const item of value) { if (item !== undefined && item !== null) searchParams.append(key, item) }`.
2. **Forward `signal` and `timeout` (`api.js:40-45`):**
   - Set `const signal = config.signal || (config.timeout ? AbortSignal.timeout(config.timeout) : undefined)` and include `...(signal ? { signal } : {})` in `fetchOptions`.
3. **Parse JSON error payloads even when `config.responseType === 'blob'` (`api.js:55-68`):**
   - Check `const contentType = response.headers.get('content-type') || ''`.
   - Only read `resData = await response.blob()` when `config.responseType === 'blob' && (response.ok || !contentType.includes('application/json'))`. Otherwise, when `contentType.includes('application/json')`, parse `resData = await response.json()`.
4. **Remove duplicate 401 redirect in `catch (err)` (`api.js:92-99`):**
   - Remove the duplicate `if (err.response && err.response.status === 401)` block inside `catch (err)` since lines 76–81 already handle 401 responses before throwing.

- [ ] **Step 2: Update SSE callback in `frontend/src/views/EndpointDetailView.vue:1529-1538`**

```javascript
  unsubscribeSSE = subscribe((rawEvent) => {
    try {
      const event = typeof rawEvent?.data === 'string' ? JSON.parse(rawEvent.data) : rawEvent
      if (event?.type === 'NODE_STATE_CHANGE' && event?.data?.endpoint_id === endpointId) {
        if (endpoint.value && event.data.avg_rtt_ms != null) {
          endpoint.value.avg_rtt_ms = event.data.avg_rtt_ms
        }
        if (activePerspective.value === 'flow') {
          loadFlowTelemetry()
        }
      }
    } catch (e) {
      // Ignore malformed SSE heartbeats
    }
  })
```

- [ ] **Step 3: Verify frontend build succeeds**

Run (from `frontend/`): `npm run build`
Expected: Vite production build succeeds with 0 errors.

---

### Task 8: Fix Systemd Watchdog Integration & Tracked `backend/venv`

**Files:**
- Modify: `deploy/ephemres-api.service`
- Modify: `deploy/ephemres-engine.service`
- Modify: `deploy/ephemres-flowd.service`
- Modify: `monitoring/engine.py`
- Modify: `monitoring/flow/daemon.py`
- Modify: `.gitignore` / git index (`backend/venv`)

- [ ] **Step 1: Enable Systemd Watchdog in `monitoring/engine.py`, `monitoring/flow/daemon.py`, and `deploy/*.service`**

1. In `monitoring/engine.py` and `monitoring/flow/daemon.py`, import and start `start_systemd_watchdog()` on startup (and cancel/await it on shutdown), matching `backend/app/main.py:80-85`.
2. In `deploy/ephemres-api.service:7`, `deploy/ephemres-engine.service:7`, and `deploy/ephemres-flowd.service:7`, add `WatchdogSec=45` and `NotifyAccess=all`.

- [ ] **Step 2: Untrack `backend/venv` from git index**

Run: `git rm --cached backend/venv` and ensure `backend/venv` is listed in `.gitignore`.

- [ ] **Step 3: Run full test suite**

Run: `./backend/venv/bin/pytest`
Expected: All existing and new tests pass with 0 failures.
