# Ephemres Developer & Contributor Guide — Version 3.3.2.v

This guide details how to set up a local development environment for the Ephemres Network Monitoring Platform v3.3.2.v and outlines coding standards for the **Vertical Integration Architecture**.

---

## 1. Architectural Philosophy

Ephemres v3.3.2.v operates on a strict **zero-dependency runtime foundation**. Aside from the ASGI server (`uvicorn`) and asynchronous PostgreSQL driver (`asyncpg`), all backend infrastructure is implemented using the Python Standard Library and native Linux system primitives located in `backend/app/core/`.

> [!IMPORTANT]
> **No External Runtime Dependencies:** Pull Requests introducing external runtime frameworks, third-party ORMs, or validation libraries will be rejected. All HTTP routing, ORM compilation, schema validation, caching, cryptography, HTTP requests, PDF generation, and templating must leverage the 11 Layer 0 engines in `backend/app/core/`.

---

## 2. Project Structure

```
ephemres/
├── backend/
│   ├── app/
│   │   ├── core/           # Layer 0: 11 Pure-Stdlib Infrastructure Engines
│   │   │   ├── asgi.py         # Native ASGI 3.0 router, dependency injection, SSE
│   │   │   ├── orm.py          # Native Async ORM compiled directly over asyncpg
│   │   │   ├── schema.py       # Pure-stdlib SchemaModel validation & serialization
│   │   │   ├── faststore.py    # In-RAM /dev/shm SQLite WAL cache, pub/sub & streams
│   │   │   ├── migrator.py     # Native async migration engine (0001-0012)
│   │   │   ├── crypto.py       # Scrypt key derivation & AES-256-GCM encryption
│   │   │   ├── http_client.py  # Asynchronous socket HTTP client with SSRF defense
│   │   │   ├── netlink.py      # Linux kernel RTNETLINK & /proc stat readers
│   │   │   ├── pdf.py          # Direct PostScript/PDF vector compiler
│   │   │   ├── template.py     # Jinja2-compatible AST template compiler
│   │   │   └── watchdog.py     # Systemd sd_notify watchdog supervisor
│   │   ├── models/         # Declarative database models (app.core.orm)
│   │   ├── schemas/        # API request & response schemas (app.core.schema)
│   │   ├── repositories/   # Data access repositories
│   │   ├── routers/        # HTTP & SSE endpoints (app.core.asgi)
│   │   ├── services/       # Domain business logic & alert dispatchers
│   │   └── main.py         # EphemresApp initialization & lifecycle
│   ├── migrations/         # Migration versions (0001-0012)
│   └── requirements.txt    # Minimal dependencies (uvicorn, asyncpg)
├── frontend/               # Vue 3 (Composition API), Vite, PrimeVue
├── monitoring/             # Monitoring sweeper, synthetic probes, flow daemon
├── deploy/                 # Systemd units, install.sh, upgrade.sh, nginx config
└── tests/                  # Pytest unit & integration test suite
```

---

## 3. Local Environment Setup

### Prerequisites
1. **Python 3.10+**
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
1. **Virtual Environment & Dependencies:**
   ```bash
   cd backend
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
   *(Notice that dependency installation finishes in seconds because there are only 2 lightweight packages!)*

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
FastStore requires **zero external daemons**. You do not need to install, start, or configure `redis-server`. FastStore automatically creates and manages its in-RAM SQLite WAL database in `/dev/shm/ephemres_faststore.db` with sub-microsecond access times.

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
    
    name: str = Field(..., min_length=1, max_length=128)
    target_ip: str
    port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        return v.strip()
```

### 4.2 Data Models & Queries (`app.core.orm`)
Use `DeclarativeBase` and `app.core.orm` query builders:
```python
from app.core.orm import DeclarativeBase, Column, Integer, String, Boolean, DateTime, select

class Endpoint(DeclarativeBase):
    __tablename__ = "endpoints"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True)

# Querying via AsyncSession:
stmt = select(Endpoint).where(Endpoint.is_active == True).order_by(Endpoint.name)
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
Use `FastStoreClient`:
```python
from app.core.faststore import get_faststore_client

store = get_faststore_client()
await store.set("my_key", "value", ex=300)
val = await store.get("my_key")

# Pub/Sub Messaging
await store.publish("events_channel", json_payload)
```

### 4.5 SSRF-Immune HTTP Client (`app.core.http_client`)
All outbound webhooks and HTTP calls must use `AsyncHttpClient`:
```python
from app.core.http_client import AsyncHttpClient

client = AsyncHttpClient(timeout=5.0)
response = await client.post("https://webhook.site/...", json={"status": "alert"})
status_code = response.status_code
```

---

## 5. Verification & Testing

Always verify all tests pass before opening a Pull Request:
```bash
# Run unit & integration test suite
PYTHONPATH=.:backend pytest tests/ -q \
  --ignore=tests/e2e \
  --ignore=tests/test_snmp_live.py \
  --ignore=tests/backend/test_Phase2_verification.py \
  --ignore=tests/backend/test_dual_driver_integration.py \
  --ignore=tests/backend/test_migration_003.py \
  --ignore=tests/backend/test_reporting_api.py \
  --ignore=tests/backend/test_v3_1_features.py
```
All 346 tests must complete with 0 failures.
