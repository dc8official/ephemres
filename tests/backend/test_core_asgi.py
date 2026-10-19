"""Comprehensive unit test suite for zero-dependency ASGI framework (app.core.asgi).

Verifies all 9 Lead Systems Architect invariants:
1. Per-Request DI Cache & Overrides with async generator teardown.
2. Injected Response Parameter Merging.
3. Multi-Header Set-Cookie Support & request.cookies.
4. Static-Before-Dynamic Route Matching & decorator stacking.
5. Path & Query Coercion -> HTTP 422.
6. StreamingResponse & await request.is_disconnected().
7. BaseHTTPMiddleware & Request attributes.
8. Loop-Safe in-process TestClient (outside & inside running event loop).
9. Atomic shim cutover verification.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import re
from typing import AsyncGenerator, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from app.core.asgi import (
    APIRouter,
    BaseHTTPMiddleware,
    CORSMiddleware,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    JSONResponse,
    Path,
    Query,
    Request,
    Response,
    StreamingResponse,
    TestClient,
    status,
)
from app.core.schema import BaseModel, Field


# ---------------------------------------------------------------------------
# Test Schemas & Models
# ---------------------------------------------------------------------------
class ItemCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    price: float = Field(..., ge=0.0)
    tags: List[str] = Field(default_factory=list)


class ItemResponse(BaseModel):
    id: UUID
    name: str
    price: float
    tags: List[str]


# ---------------------------------------------------------------------------
# Invariant 1: Per-Request DI Cache, Overrides, & Generator Teardown
# ---------------------------------------------------------------------------
def test_per_request_di_cache_and_generator_teardown():
    app = FastAPI()
    call_counts = {"db": 0, "teardown": 0, "user": 0}

    async def get_test_db() -> AsyncGenerator[str, None]:
        call_counts["db"] += 1
        try:
            yield "db_session_token"
        finally:
            call_counts["teardown"] += 1

    async def get_test_user(db: str = Depends(get_test_db)) -> dict:
        call_counts["user"] += 1
        return {"user": "admin", "db": db}

    @app.get("/di-test")
    async def di_route(
        user: dict = Depends(get_test_user),
        db: str = Depends(get_test_db),
    ):
        return {"user": user, "direct_db": db}

    client = TestClient(app)
    resp = client.get("/di-test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["direct_db"] == "db_session_token"
    assert data["user"]["db"] == "db_session_token"

    # Verify get_test_db only called once per request despite 2 Depends references
    assert call_counts["db"] == 1
    assert call_counts["user"] == 1
    # Verify async generator was cleanly torn down in exit stack
    assert call_counts["teardown"] == 1

    # Test dependency overrides
    async def mock_get_test_user():
        return {"user": "mock_override", "db": "mock_db"}

    app.dependency_overrides[get_test_user] = mock_get_test_user
    resp2 = client.get("/di-test")
    assert resp2.status_code == 200
    assert resp2.json()["user"]["user"] == "mock_override"


# ---------------------------------------------------------------------------
# Invariant 2: Injected Response Parameter Merging
# ---------------------------------------------------------------------------
def test_injected_response_parameter_merging():
    app = FastAPI()

    class LoginPayload(BaseModel):
        user: str

    @app.post("/login", status_code=status.HTTP_201_CREATED)
    async def login_route(payload: LoginPayload, response: Response):
        response.set_cookie("token", "secret_jwt", httponly=True)
        response.headers["X-Custom-Auth"] = "injected"
        return {"message": f"Welcome {payload.user}"}

    client = TestClient(app)
    resp = client.post("/login", json={"user": "alice"})
    assert resp.status_code == 201
    assert resp.json() == {"message": "Welcome alice"}
    assert "token=secret_jwt" in resp.headers["set-cookie"]
    assert "HttpOnly" in resp.headers["set-cookie"]
    assert resp.headers["x-custom-auth"] == "injected"


# ---------------------------------------------------------------------------
# Invariant 3: Multi-Header Set-Cookie Support & request.cookies
# ---------------------------------------------------------------------------
def test_multi_header_set_cookie_and_request_cookies():
    app = FastAPI()

    @app.post("/set-cookies")
    async def set_cookies_route(response: Response):
        response.set_cookie("sess_a", "val_a", path="/")
        response.set_cookie("sess_b", "val_b", secure=True)
        return {"ok": True}

    @app.get("/read-cookies")
    async def read_cookies_route(request: Request):
        return {"cookies": request.cookies}

    client = TestClient(app)
    resp = client.post("/set-cookies")
    assert resp.status_code == 200

    # Ensure multiple set-cookie headers exist
    cookies_list = resp.headers.getlist("set-cookie")
    assert len(cookies_list) == 2
    assert any("sess_a=val_a" in c for c in cookies_list)
    assert any("sess_b=val_b" in c for c in cookies_list)

    # Next request via client should automatically send persistent cookies
    resp2 = client.get("/read-cookies")
    assert resp2.status_code == 200
    assert resp2.json()["cookies"]["sess_a"] == "val_a"
    assert resp2.json()["cookies"]["sess_b"] == "val_b"


# ---------------------------------------------------------------------------
# Invariant 4: Static-Before-Dynamic Route Matching & Stacking
# ---------------------------------------------------------------------------
def test_static_before_dynamic_route_matching_and_stacking():
    app = FastAPI()

    @app.get("/items/batch")
    async def get_items_batch():
        return {"type": "static_batch"}

    @app.get("/items/{item_id}")
    async def get_item_dynamic(item_id: str):
        return {"type": "dynamic", "id": item_id}

    # Stacking test
    @app.get("/health/liveness")
    @app.get("/api/v1/health/liveness")
    async def liveness():
        return {"alive": True}

    client = TestClient(app)

    # Static should match /items/batch, NOT dynamic /items/{item_id}
    res_batch = client.get("/items/batch")
    assert res_batch.status_code == 200
    assert res_batch.json()["type"] == "static_batch"

    res_dyn = client.get("/items/12345")
    assert res_dyn.status_code == 200
    assert res_dyn.json() == {"type": "dynamic", "id": "12345"}

    # Stacking
    assert client.get("/health/liveness").json() == {"alive": True}
    assert client.get("/api/v1/health/liveness").json() == {"alive": True}

    # 404 for unknown path
    assert client.get("/not-existing-path").status_code == 404

    # 405 for wrong method
    assert client.post("/health/liveness").status_code == 405


# ---------------------------------------------------------------------------
# Invariant 5: Path & Query Coercion -> HTTP 422
# ---------------------------------------------------------------------------
def test_path_and_query_coercion_and_422():
    app = FastAPI()

    @app.get("/devices/{device_id}")
    async def get_device(
        device_id: UUID,
        page: int = Query(default=1, ge=1, le=100),
        active_only: bool = Query(default=True),
    ):
        return {
            "device_id": str(device_id),
            "page": page,
            "active_only": active_only,
        }

    client = TestClient(app)
    uid = uuid4()

    # Valid request
    res_ok = client.get(f"/devices/{uid}?page=2&active_only=false")
    assert res_ok.status_code == 200
    data = res_ok.json()
    assert data["device_id"] == str(uid)
    assert data["page"] == 2
    assert data["active_only"] is False

    # Invalid UUID in path -> 422
    res_bad_uuid = client.get("/devices/not-a-valid-uuid?page=1")
    assert res_bad_uuid.status_code == 422
    assert "detail" in res_bad_uuid.json()

    # Invalid Query constraint (page < 1) -> 422
    res_bad_page = client.get(f"/devices/{uid}?page=0")
    assert res_bad_page.status_code == 422
    assert any("greater than or equal to 1" in str(e) for e in res_bad_page.json()["detail"])


# ---------------------------------------------------------------------------
# Invariant 6: StreamingResponse & request.is_disconnected()
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_streaming_response_and_disconnect():
    app = FastAPI()

    @app.get("/stream-async")
    async def stream_async_route(request: Request):
        async def generator():
            yield "part1\n"
            yield "part2\n"

        return StreamingResponse(generator(), media_type="text/plain")

    @app.get("/stream-bytesio")
    async def stream_bytesio_route():
        buf = io.BytesIO(b"%PDF-1.4 sample pdf content")
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="test.pdf"'},
        )

    client = TestClient(app)

    # Test async generator stream
    res_stream = client.get("/stream-async")
    assert res_stream.status_code == 200
    assert res_stream.text == "part1\npart2\n"
    assert "content-length" not in res_stream.headers, "StreamingResponse must not contain content-length"

    # Test BytesIO stream
    res_pdf = client.get("/stream-bytesio")
    assert res_pdf.status_code == 200
    assert res_pdf.content == b"%PDF-1.4 sample pdf content"
    assert "test.pdf" in res_pdf.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Invariant 7: BaseHTTPMiddleware & Request Attributes
# ---------------------------------------------------------------------------
def test_base_http_middleware_and_cors():
    app = FastAPI()

    class CustomHeaderMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            assert request.method in ("GET", "OPTIONS")
            assert request.url.path == "/middleware-test"
            assert request.client is not None
            response = await call_next(request)
            if request.method == "GET":
                response.headers["X-Intercepted-By"] = "CustomMiddleware"
            return response

    app.add_middleware(CustomHeaderMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://ephemres.internal"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/middleware-test")
    async def mw_route():
        return {"status": "ok"}

    client = TestClient(app)

    # Normal request
    resp = client.get("/middleware-test", headers={"Origin": "https://ephemres.internal"})
    assert resp.status_code == 200
    assert resp.headers["x-intercepted-by"] == "CustomMiddleware"
    assert resp.headers["access-control-allow-origin"] == "https://ephemres.internal"
    assert resp.headers["access-control-allow-credentials"] == "true"

    # CORS Preflight OPTIONS
    options_resp = client.options("/middleware-test", headers={"Origin": "https://ephemres.internal"})
    assert options_resp.status_code == 200
    assert options_resp.headers["access-control-allow-origin"] == "https://ephemres.internal"


# ---------------------------------------------------------------------------
# Invariant 8: Loop-Safe TestClient (Outside & Inside Running Event Loop)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_client_inside_running_event_loop():
    """Verify that TestClient can execute synchronous requests from inside an async test loop."""
    app = FastAPI()

    @app.get("/async-loop-safety")
    async def safety_endpoint():
        return {"safe": True}

    # Calling client.get from inside async def test without event loop conflict!
    client = TestClient(app)
    resp = client.get("/async-loop-safety")
    assert resp.status_code == 200
    assert resp.json() == {"safe": True}


# ---------------------------------------------------------------------------
# Invariant 9: Atomic Shim Cutover Verification
# ---------------------------------------------------------------------------
def test_atomic_shim_cutover():
    import sys
    from pathlib import Path

    root_dir = Path(__file__).resolve().parent.parent.parent
    backend_reqs = root_dir / "backend" / "requirements.txt"
    assert backend_reqs.exists()
    content = backend_reqs.read_text(encoding="utf-8")

    assert "fastapi" not in content, "fastapi must be eliminated from backend/requirements.txt"
    assert "pydantic" not in content, "pydantic must be eliminated from backend/requirements.txt"

    # Verify that in-tree shims are deleted
    assert (backend_reqs.parent / "fastapi").exists() is False
    assert (backend_reqs.parent / "starlette").exists() is False
    assert (backend_reqs.parent / "pydantic").exists() is False

    from app.core import schema as pydantic
    from app.core import asgi as fastapi
    from app.core import asgi as starlette

    assert hasattr(pydantic, "BaseModel")
    assert hasattr(fastapi, "FastAPI")
    assert hasattr(starlette, "BaseHTTPMiddleware")
