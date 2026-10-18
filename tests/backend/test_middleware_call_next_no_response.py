import pytest
from app.core.asgi import EphemresApp, TestClient, BaseHTTPMiddleware, Request, Response, PlainTextResponse


def test_base_http_middleware_no_response_raises_runtime_error():
    captured_error = None

    class CaptureMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            nonlocal captured_error
            try:
                return await call_next(request)
            except Exception as e:
                captured_error = e
                return PlainTextResponse("Caught error", status_code=500)

    # Inner dummy ASGI app that completes without sending http.response.start
    async def dummy_silent_app(scope, receive, send):
        pass

    middleware = CaptureMiddleware(app=dummy_silent_app)
    app = EphemresApp()
    app.add_middleware(lambda app: middleware)

    client = TestClient(app)
    resp = client.get("/any-path")
    assert resp.status_code == 500
    assert resp.text == "Caught error"
    assert isinstance(captured_error, RuntimeError)
    assert str(captured_error) == "No response returned"
