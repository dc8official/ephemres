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

@pytest.mark.anyio
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

@pytest.mark.anyio
async def test_router_dependencies_and_query_defaults():
    app = FastAPI()
    guard_called = False
    bg_task_called = False

    async def verify_guard():
        nonlocal guard_called
        guard_called = True

    def run_bg():
        nonlocal bg_task_called
        bg_task_called = True

    router = APIRouter(dependencies=[Depends(verify_guard)])

    @router.get("/items")
    async def list_items(limit: int = 25, bg: BackgroundTasks = None):
        if bg:
            bg.add_task(run_bg)
        return {"limit": limit}

    app.include_router(router)
    from app.core.asgi import TestClient
    client = TestClient(app)
    resp = client.get("/items?limit=10")
    assert resp.status_code == 200
    assert resp.json() == {"limit": 10}
    assert guard_called is True
    assert bg_task_called is True


@pytest.mark.anyio
async def test_response_model_coercion_and_decimal():
    from decimal import Decimal
    from app.core.asgi import TestClient

    class ItemOut(BaseModel):
        id: int
        price: float
        name: str

    app = FastAPI()

    @app.get("/item", response_model=ItemOut)
    async def get_item():
        # Returns dict with Decimal and string id that gets coerced to int/float
        return {"id": "42", "price": Decimal("19.99"), "name": "Widget"}

    @app.get("/items-list", response_model=list[ItemOut])
    async def get_items():
        return [{"id": 1, "price": Decimal("10.50"), "name": "Item A"}]

    client = TestClient(app)
    resp = client.get("/item")
    assert resp.status_code == 200
    assert resp.json() == {"id": 42, "price": 19.99, "name": "Widget"}

    resp2 = client.get("/items-list")
    assert resp2.status_code == 200
    assert resp2.json() == [{"id": 1, "price": 10.5, "name": "Item A"}]

