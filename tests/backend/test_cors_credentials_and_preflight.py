import pytest
from app.core.asgi import EphemresApp, TestClient, CORSMiddleware


def test_cors_credentials_wildcard_headers_echo_and_origin_filtering():
    app = EphemresApp()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://allowed.example.com"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/data")
    async def get_data():
        return {"data": "secret"}

    client = TestClient(app)

    # 1. Preflight from allowed origin with Access-Control-Request-Headers
    preflight_allowed = client.options(
        "/data",
        headers={
            "Origin": "https://allowed.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Custom-Token, Authorization",
        },
    )
    assert preflight_allowed.status_code == 200
    assert preflight_allowed.headers.get("access-control-allow-origin") == "https://allowed.example.com"
    assert preflight_allowed.headers.get("access-control-allow-credentials") == "true"
    # When allow_credentials=True and allow_headers=["*"], echo request headers instead of "*"
    assert preflight_allowed.headers.get("access-control-allow-headers") == "X-Custom-Token, Authorization"
    assert "Access-Control-Request-Headers" in preflight_allowed.headers.get("vary", "")

    # 2. Preflight from disallowed origin passes through to application (not intercepted)
    preflight_disallowed = client.options(
        "/data",
        headers={
            "Origin": "https://malicious.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # The route /data only defined GET, so unhandled OPTIONS passes through to app route dispatcher returning 405
    assert preflight_disallowed.status_code == 405
    assert "access-control-allow-origin" not in preflight_disallowed.headers

    # 3. Simple GET from disallowed origin should not receive CORS allow header
    get_disallowed = client.get(
        "/data",
        headers={"Origin": "https://malicious.example.com"},
    )
    assert get_disallowed.status_code == 200
    assert "access-control-allow-origin" not in get_disallowed.headers
