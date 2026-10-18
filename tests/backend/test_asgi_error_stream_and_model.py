import pytest
from app.core.asgi import EphemresApp, TestClient, StreamingResponse
from app.core.schema import SchemaModel


class ItemModel(SchemaModel):
    name: str
    count: int


def test_streaming_error_after_first_chunk_sends_only_one_start():
    app = EphemresApp()

    async def fail_stream():
        yield b"chunk 1"
        raise RuntimeError("Mid-stream failure")

    @app.get("/stream-fail")
    async def stream_fail():
        return StreamingResponse(fail_stream(), media_type="text/plain")

    # Use a custom send mock to track messages and enforce no double start
    start_messages = []

    async def mock_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def strict_send(message):
        if message["type"] == "http.response.start":
            if start_messages:
                raise RuntimeError("http.response.start called more than once!")
            start_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream-fail",
        "raw_path": b"/stream-fail",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    client = TestClient(app)
    # 1. Run through TestClient to verify no unhandled crash in client loop
    resp = client.get("/stream-fail")
    assert resp.status_code == 200
    assert resp.content == b"chunk 1"

    # 2. Run directly through ASGI with strict send to ensure exactly one start message
    import asyncio
    asyncio.run(app(scope, mock_receive, strict_send))
    assert len(start_messages) == 1


def test_response_model_validation_failure_returns_500_without_field_details():
    app = EphemresApp()

    @app.get("/bad-model", response_model=ItemModel)
    async def bad_model():
        # count violates int requirement of ItemModel
        return {"name": "widget", "count": "not-an-integer"}

    client = TestClient(app)
    resp = client.get("/bad-model")

    assert resp.status_code == 500
    data = resp.json()
    assert data == {"detail": "Internal Server Error"}
    assert "count" not in resp.text
    assert "not-an-integer" not in resp.text
