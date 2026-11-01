# Ephemres Developer & Contributor Guide — Version 3.3.5

This guide details how to set up a local development environment for the Ephemres Network Monitoring Platform v3.3.5 and outlines coding standards for the **Vertical Integration Architecture**.

---

## 1. Architectural Philosophy

Ephemres v3.3.5 operates on a strict **zero-dependency runtime foundation**. Aside from the ASGI server (`uvicorn`) and asynchronous PostgreSQL driver (`asyncpg`), all backend infrastructure is implemented using the Python Standard Library and native Linux system primitives located in `backend/app/core/`.

> [!IMPORTANT]
> **No External Runtime Dependencies:** Pull Requests introducing external runtime frameworks, third-party ORMs, or validation libraries will be rejected. All HTTP routing, ORM compilation, schema validation, caching, cryptography, HTTP requests, PDF generation, and templating must leverage the 13 Layer 0 engines in `backend/app/core/`.

---

## 2. Project Structure

```
ephemres/
├── backend/
│   ├── app/
│   │   ├── core/           # Layer 0: 12 Pure-Stdlib Infrastructure Engines
│   │   │   ├── asgi.py         # Native ASGI 3.0 router, dependency injection, SSE
│   │   │   ├── orm.py          # Native Async ORM compiled directly over asyncpg
│   │   │   ├── schema.py       # Pure-stdlib SchemaModel validation & serialization
│   │   │   ├── faststore.py    # In-RAM /dev/shm SQLite WAL cache, pub/sub & streams
│   │   │   ├── migrator.py     # Native async migration engine (0001-0017)
│   │   │   ├── crypto.py       # Scrypt key derivation & AES-256-GCM encryption
│   │   │   ├── restore.py      # Cross-version database backup restoration engine
│   │   │   ├── http_client.py  # Asynchronous socket HTTP client with SSRF defense
│   │   │   ├── netlink.py      # Linux kernel RTNETLINK & /proc stat readers
│   │   │   ├── pdf.py          # Direct binary PDF 1.4 vector emitter
│   │   │   ├── template.py     # Jinja2-compatible AST template compiler
│   │   │   └── watchdog.py     # Systemd sd_notify watchdog supervisor
│   │   ├── models/         # Declarative database models (app.core.orm)
│   │   ├── schemas/        # API request & response schemas (app.core.schema)
│   │   ├── repositories/   # Data access repositories
│   │   ├── routers/        # HTTP & SSE endpoints (app.core.asgi)
│   │   ├── services/       # Domain business logic & alert dispatchers
│   │   ├── logging_config.py # Console + rotating file logging (setup_logging)
│   │   └── main.py         # EphemresApp initialization & lifecycle
│   ├── migrations/         # Migration versions (0001-0017)
│   └── requirements.txt    # Minimal dependencies (uvicorn, asyncpg)
├── frontend/               # Vue 3 (Composition API), Vite, PrimeVue Aura, native fetch
├── monitoring/             # Monitoring sweeper, synthetic probes, flow daemon
├── deploy/                 # Systemd units, install.sh, upgrade.sh, restore.sh, nginx config
└── tests/                  # Pytest unit & integration test suite
```

---

## 3. Local Environment Setup

### Prerequisites
1. **Python 3.11+** (`app.config` uses the stdlib `tomllib` module)
2. **PostgreSQL 14+ with TimescaleDB**
3. **Node.js 18+ and npm**
4. **Linux with `/dev/shm`** (or any POSIX OS; FastStore gracefully falls back to system temporary directories if `/dev/shm` is unavailable)

### 3.1 Database Setup
Connect to your local PostgreSQL instance and initialize the database:
```sql
CREATE DATABASE netmon;
\c netmon
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

### 3.2 Backend Setup
1. **Virtual Environment & Dependencies** (from the repository root):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r backend/requirements-dev.txt
   cd backend
   ```
   `requirements-dev.txt` installs the two runtime packages from `requirements.txt` (`uvicorn`, `asyncpg`) plus the test tools (`pytest`, `anyio`). Keep the environment limited to these: a stray legacy package (e.g. `fastapi`, `sqlalchemy`) would let an accidental import pass locally and fail in production.

2. **Environment Configuration:**
   Copy `.env.example` to `.env` and configure your credentials:
   ```bash
   cp .env.example .env
   # Ensure EPHEMRES_DB_PASSWORD and database host/port match your PostgreSQL instance
   ```

3. **Run Native Database Migrations:**
   Apply database schema revisions directly using the native async migrator:
   ```bash
   PYTHONPATH=. python3 -m app.core.migrator upgrade head
   ```

4. **Seed Default Admin User:**
   ```bash
   PYTHONPATH=. python3 -m app.seed_admin
   ```

5. **Start Development Server:**
   ```bash
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

### 3.3 In-RAM FastStore Cache
FastStore requires **zero external daemons**. You do not need to install, start, or configure `redis-server`. FastStore automatically creates and manages its in-RAM SQLite WAL database in `/dev/shm/ephemres_faststore_u<UID>_db0.sqlite3` with sub-microsecond access times.

### 3.4 Frontend Setup (Vue 3 / Vite)
1. **Install Dependencies:**
   ```bash
   cd frontend
   npm install
   ```

2. **Start Vite Dev Server:**
   ```bash
   npm run dev
   ```
   The frontend will be available at `http://localhost:5173`. Vite proxies `/api` calls directly to `http://localhost:8000`.

---

## 4. Coding Patterns & Core Idioms

### 4.1 Schema Definition (`app.core.schema`)
Use `SchemaModel` instead of Pydantic `BaseModel`:
```python
from app.core.schema import SchemaModel, Field, field_validator, ConfigDict, to_camel

class EndpointCreate(SchemaModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    
    hostname: str = Field(..., min_length=1, max_length=255)
    ip_address: str
    device_type: str = Field(default="ENDPOINT")
    device_role: str = Field(default="ACTIVE_HOST")

    @field_validator("hostname")
    def validate_hostname(cls, v: str) -> str:
        return v.strip()
```

### 4.2 Data Models & Queries (`app.core.orm`)
Use `DeclarativeBase` (or `app.database.Base`) and `app.core.orm` query builders:
```python
from app.core.orm import Boolean, DateTime, INET, Integer, Mapped, String, mapped_column, select
from app.database import Base

class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(INET, unique=True, nullable=False)
    device_role: Mapped[str] = mapped_column(String(30), default="ACTIVE_HOST")
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

# Querying via AsyncSession:
stmt = select(Endpoint).where(Endpoint.monitoring_enabled == True).order_by(Endpoint.hostname)
result = await session.execute(stmt)
endpoints = result.scalars().all()
```

### 4.3 HTTP Routes & Dependency Injection (`app.core.asgi`)
Use `APIRouter` and `Depends`:
```python
from app.core.asgi import APIRouter, Depends, HTTPException, status
from app.core.orm import AsyncSession
from app.database import get_db

router = APIRouter(prefix="/endpoints", tags=["endpoints"])

@router.get("/")
async def list_endpoints(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Endpoint))
    return result.scalars().all()
```

### 4.4 In-RAM Caching & Messaging (`app.core.faststore`)
Use `FastStoreClient` (an in-process SQLite store with a Redis-style API; `host`/`port` arguments are accepted for compatibility and ignored, and `db` selects the shared-memory database file):
```python
from app.core.faststore import FastStoreClient

store = FastStoreClient(db=0, decode_responses=True)
await store.set("my_key", "value", ex=300)
val = await store.get("my_key")

# Pub/Sub Messaging
await store.publish("events_channel", json_payload)
```

### 4.5 SSRF-Immune HTTP Client (`app.core.http_client`)
All outbound webhooks and HTTP calls must use the SSRF-safe client from `create_ssrf_safe_client()`. It resolves and validates the destination IP at connection time and refuses redirects:
```python
from app.core.http_client import create_ssrf_safe_client

async with create_ssrf_safe_client(timeout=5.0) as client:
    response = await client.post("https://hooks.example.com/...", json={"status": "alert"})
    status_code = response.status_code
```

---

## 5. Verification & Testing

Always verify that all tests pass before opening a Pull Request:
```bash
# Run the unit & integration test suite from the repository root
.venv/bin/python -m pytest
```
All tests must pass with 0 failures. `pytest.ini` sets `pythonpath = .` and `testpaths = tests`; `tests/backend/test_no_removed_deps.py` fails if any source or test file imports a removed framework.
