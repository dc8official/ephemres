import pytest
from app.core.asgi import EphemresApp, TestClient, BaseHTTPMiddleware, Request, Response, PlainTextResponse


def test_middleware_order_starlette_parity():
    app = EphemresApp()
    call_log = []

    class M1(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            call_log.append("m1_enter")
            response = await call_next(request)
            call_log.append("m1_exit")
            return response

    class M2(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            call_log.append("m2_enter")
            response = await call_next(request)
            call_log.append("m2_exit")
            return response

    # Adding M1 first, then M2.
    # In Starlette / FastAPI semantics, the last added middleware wraps the previous stack,
    # so M2 is outermost: M2 -> M1 -> endpoint -> M1 -> M2.
    app.add_middleware(M1)
    app.add_middleware(M2)

    @app.get("/test")
    async def endpoint():
        call_log.append("endpoint")
        return PlainTextResponse("ok")

    client = TestClient(app)
    resp = client.get("/test")
    assert resp.status_code == 200
    assert resp.text == "ok"
    assert call_log == ["m2_enter", "m1_enter", "endpoint", "m1_exit", "m2_exit"]
