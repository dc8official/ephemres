import pytest
from app.core.asgi import EphemresApp, TestClient, JSONResponse, StreamingResponse


def test_asgi_head_suppresses_body():
    app = EphemresApp()

    stream_closed = False

    async def sample_stream():
        nonlocal stream_closed
        try:
            yield b"part1"
            yield b"part2"
        finally:
            stream_closed = True

    @app.get("/items")
    async def get_items():
        return JSONResponse({"message": "hello world"}, headers={"X-Custom": "test"})

    @app.get("/stream")
    async def get_stream():
        return StreamingResponse(sample_stream(), media_type="text/plain")

    client = TestClient(app)

    # 1. Standard JSON response HEAD vs GET
    get_res = client.get("/items")
    assert get_res.status_code == 200
    assert get_res.json() == {"message": "hello world"}
    assert get_res.headers.get("X-Custom") == "test"
    assert len(get_res.content) > 0

    head_res = client.head("/items")
    assert head_res.status_code == 200
    assert head_res.content == b""
    assert head_res.text == ""
    assert head_res.headers.get("X-Custom") == "test"
    assert head_res.headers.get("content-type") == get_res.headers.get("content-type")

    # 2. StreamingResponse HEAD vs GET
    get_stream_res = client.get("/stream")
    assert get_stream_res.status_code == 200
    assert get_stream_res.content == b"part1part2"

    head_stream_res = client.head("/stream")
    assert head_stream_res.status_code == 200
    assert head_stream_res.content == b""
    assert head_stream_res.text == ""
    assert stream_closed is True
