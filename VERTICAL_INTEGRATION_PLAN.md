# Ephemres Vertical Integration Architecture (`v3.3.2v`)

## Overview & Objective
Eliminate external Python library dependencies from `backend/requirements.txt` and external daemons (`redis-server`), replacing them with **100% Python Standard Library + Linux kernel primitives** (`ctypes`, `socket`, `struct`, `/proc`, `/dev/shm`, `sqlite3`, `tomllib`, `zlib`), leaving **ONLY `uvicorn` and `asyncpg`** as runtime dependencies.

### Workflow Constraint (Argon / Flash Split)
- **Lead Systems Architect (Argon):** Runs fast read/grep inspections, designs the exact zero-dependency architecture for the current sub-stage, and writes a detailed, self-contained implementation specification for Flash. Does NOT write or edit production code files unless explicitly asked.
- **Implementer (Flash):** Reads the Argon specification, creates/edits the files, and runs the verification scripts and `pytest` suites.

---

## Completed & Committed Stages on `v3.3.2v`

1. **Rebrand Complete (`v3.3.1n` -> `v3.3.2v`):** Rebranded from `lnmp` to `ephemres`.
2. **Stage 1 Complete (Core System & Crypto Dependencies):**
   - Eliminated `python-dateutil` (unused).
   - Eliminated `psutil`: Replaced with `backend/app/services/net_interfaces.py` reading `/proc/net/route`, `/proc/net/if_inet6`, and `SIOCGIFADDR` `fcntl.ioctl`.
   - Eliminated `pydantic-settings`: Replaced in `backend/app/config.py` with stdlib `tomllib` + `@dataclass` hierarchy preserving `SEC-02` and `SEC-04` security guards.
   - Eliminated `python-jose` & `argon2-cffi`: Replaced in `backend/app/services/auth_service.py` with a custom stdlib RFC 7519 HS256 JWT codec and OpenSSL `hashlib.scrypt` password hasher.
   - Eliminated `cryptography`: Replaced in `backend/app/services/crypto_service.py` with stdlib RFC 5869 `HKDF-SHA256` and `ctypes` bindings to in-process OpenSSL `libcrypto.so.3` (`EVP_aes_256_gcm`), preserving `ENC:v1:` wire format.
   - Eliminated `httpx` (& `httpcore`): Replaced in `backend/app/services/ssrf_validator.py` and `alert_dispatcher.py` with a zero-trust `asyncio.open_connection` + `ssl` HTTP client that validates resolved IPs against CWE-918 DNS rebinding before opening sockets.
3. **Stage 2 Complete (Network Telemetry & Raw Socket Engine):**
   - Eliminated `icmplib`: Replaced in `monitoring/ping.py` with a 3-tier stdlib engine (Tier 1: Multiplexed singleton `SOCK_RAW` ICMPv4/v6 transceiver via `loop.add_reader` + `recvfrom_into`; Tier 2: Unprivileged `SOCK_DGRAM` ICMP fallback; Tier 3: Subprocess `ping` fallback).
   - Added native in-process raw-socket ICMP traceroute (`_run_raw_icmp_traceroute` unpacking `ICMP_TIME_EXCEEDED` Type 11 and `ICMP_ECHOREPLY` Type 0) in `backend/app/services/diagnostics.py`.
4. **Stage 3 Complete (Template Compiler & PDF 1.4 Typesetting/Security Engine):**
   - Eliminated `jinja2`: Replaced with `backend/app/services/template_engine.py` compiling `backend/app/templates/reports/*.html` into native Python bytecode.
   - Eliminated `weasyprint` & `pypdf`: Replaced in `backend/app/services/pdf_engine.py` with a native HTML-to-PDF 1.4 vector/table typesetter (`zlib` FlateDecode, built-in Type1 Helvetica fonts, pagination) and ISO 32000-1 Standard Security Handler (`V=2, R=3` 128-bit RC4 + MD5 owner/user password encryption with `P = -1852` read-only permissions and `/Info` metadata), preserving `secure_url_fetcher` SSRF/LFI guards.
5. **Stage 4 Complete (In-RAM `/dev/shm` FastStore Replacing `redis` & `redis-server`):**
   - Eliminated `redis`: Replaced with `backend/app/core/faststore.py` (an in-RAM `/dev/shm` SQLite WAL store supporting KV+TTL, atomic INCR, Sets, Sorted Sets, Streams + Consumer Groups + PEL + `BUSYGROUP`, Pipelines, and cross-process PubSub) and exposed via an in-tree `backend/redis/` compatibility shim (`__init__.py`, `asyncio.py`, `exceptions.py`). Removed `redis-server.service` requirement from `deploy/*.service`.
6. **Stage 5A Complete (Zero-Dependency Migration Engine Replacing `alembic` & `psycopg2-binary`):**
   - Eliminated `alembic` & `psycopg2-binary`: Removed eager `sync_engine` from `backend/app/database.py`, built `backend/app/core/migrator.py` compiling all 10 `op.*` methods across `backend/migrations/versions/0001..0012` and executing over `asyncpg`, and added the in-tree `backend/alembic/` compatibility package.

7. **Stage 5B-1 Complete (Zero-Dependency Schema Engine — `backend/app/core/schema.py`):**
   - Built `backend/app/core/schema.py` implementing a 100% Python stdlib drop-in replacement for Pydantic v2 (`BaseModel`, `Field`, `ConfigDict`, `ValidationError`, `field_validator`, `field_serializer`, `model_validator`, `to_camel`, `Generic[T]` parameterization, `from_attributes=True`, `alias_generator=to_camel`, `populate_by_name=True`, `model_dump`, `model_validate`, `UUID`/`datetime`/`Literal`/`Enum` coercion).

---

## Remaining Roadmap (`v3.3.2v`)

### Stage 5B-2: Zero-Dependency ASGI Framework (`backend/app/core/asgi.py`) & Atomic Cutover — **CURRENT STAGE**
- Build `backend/app/core/asgi.py` implementing:
  - `FastAPI` (ASGI3 `async def __call__(scope, receive, send)`, `lifespan` context manager, middleware stack, exception handlers, `dependency_overrides`, `include_router`, route decorators `.get`, `.post`, `.put`, `.patch`, `.delete`).
  - `APIRouter` (`prefix`, `tags`, `include_router`, path parameter regex compilation `{id}`, multiple decorator stacking on a single handler).
  - Recursive async/sync Dependency Injection (`Depends`, async/sync generator cleanup for `get_db`, `app.dependency_overrides` for tests).
  - Automatic request JSON body parsing & validation into `BaseModel` parameters or `Dict[K, BaseModel]` (e.g., `Dict[str, InterfaceConfig]` in `PUT /bandwidth/endpoints/{id}/interfaces`), returning HTTP 422 on `ValidationError`.
  - `Query`, `Path`, `Header`, `Cookie`, `Body` extraction with type coercion (`UUID`, `int`, `float`, `bool`, `str`, `Optional`) and constraint validation (`ge`, `le`, `gt`, `lt`, `min_length`, `max_length`) returning HTTP 422 on invalid parameters.
  - `Request` (`.headers`, `.cookies`, `.query_params`, `.path_params`, `.url.path`, `.url.scheme`, `.client.host`, `.method`, `await request.json()`, `await request.body()`, `await request.is_disconnected()`), `Response` (`.set_cookie()`, `.delete_cookie()`, `.headers`, `.status_code`), `JSONResponse`, `StreamingResponse` (supporting async and sync generators as well as `io.BytesIO` for PDF/CSV/SSE).
  - `HTTPException`, `status` constants, `CORSMiddleware`, `BaseHTTPMiddleware`.
  - In-process ASGI `TestClient` (supporting `.get`, `.post`, `.put`, `.patch`, `.delete`, `.cookies`, `.headers`, `json=...`, `params=...`, `content=...`, `.stream(...)`, `follow_redirects=...`) compatible with `tests/`.
- Create in-tree compatibility packages:
  - `backend/pydantic/` (`__init__.py`, `alias_generators.py`) re-exporting `app.core.schema`.
  - `backend/fastapi/` (`__init__.py`, `responses.py`, `middleware/cors.py`, `testclient.py`, `security.py`) re-exporting `app.core.asgi`.
  - `backend/starlette/` (`__init__.py`, `middleware/base.py`, `requests.py`, `responses.py`, `status.py`, `testclient.py`) re-exporting `app.core.asgi`.
- Remove `fastapi>=0.115.0` and `pydantic>=2.7.0` from `backend/requirements.txt` and verify all router & schema tests pass.

### Stage 5C: Zero-Dependency SQL Compiler & `asyncpg` ORM Session Adapter (`backend/app/core/orm.py` + `backend/sqlalchemy/`)
- Inspect all SQLAlchemy ORM models (`backend/app/models/`) and query patterns (`select`, `insert`, `update`, `delete`, `func`, `text`, `case`, `or_`, `and_`, `desc`, `asc`, `on_conflict_do_update`, `AsyncSession`, `async_engine`).
- Build an in-tree `backend/sqlalchemy/` lightweight SQL compiler + `asyncpg` session adapter (or direct `asyncpg` layer), removing `sqlalchemy[asyncio]>=2.0.0` from `backend/requirements.txt`.
- Final `backend/requirements.txt` contains ONLY `uvicorn[standard]>=0.30.0` and `asyncpg>=0.29.0`.
