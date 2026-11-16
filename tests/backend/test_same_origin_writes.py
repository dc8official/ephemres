"""State-changing API requests from another site's page are refused (first-phase review M1).

Before: a POST with `Origin: http://evil.example` and `Content-Type: text/plain` created an ADMIN
account on a signed-in admin's cookie; only SameSite=Lax stood in the way.
"""
import pytest

from app.core.asgi import FastAPI, SameOriginWritesMiddleware, TestClient, cross_origin_write

ALLOWED = ["http://localhost:5173"]


@pytest.mark.parametrize("headers, refused", [
    # This server's own page, as nginx passes it (Host without the port) and directly.
    ({"origin": "https://monitor.example", "host": "monitor.example"}, False),
    ({"origin": "https://monitor.example:8443", "host": "monitor.example"}, False),
    ({"origin": "http://10.0.0.5:8000", "host": "10.0.0.5:8000"}, False),
    ({"origin": "http://[::1]:8000", "host": "[::1]:8000"}, False),
    ({"origin": "https://Monitor.Example", "host": "monitor.example"}, False),
    # A front end the admin named (the Vite dev server proxies with another Host).
    ({"origin": "http://localhost:5173", "host": "127.0.0.1:8000"}, False),
    # A script or the deploy tools: no browser, no Origin.
    ({"host": "monitor.example"}, False),
    ({"host": "monitor.example", "sec-fetch-site": "same-origin"}, False),
    # Another site, a sibling subdomain, a look-alike host, a sandboxed frame.
    ({"origin": "http://evil.example", "host": "monitor.example"}, True),
    ({"origin": "https://wiki.example", "host": "monitor.example"}, True),
    ({"origin": "https://monitor.example.evil.net", "host": "monitor.example"}, True),
    ({"origin": "https://evil.net/?monitor.example", "host": "monitor.example"}, True),
    ({"origin": "null", "host": "monitor.example"}, True),
    ({"origin": "", "host": ""}, True),
    # No Origin, but the browser says another site sent it.
    ({"host": "monitor.example", "sec-fetch-site": "cross-site"}, True),
    ({"host": "monitor.example", "sec-fetch-site": "same-site"}, True),
])
def test_which_writes_count_as_another_sites(headers, refused):
    assert (cross_origin_write("POST", "/api/v1/users", headers, ALLOWED) is not None) is refused


def test_reads_and_paths_outside_the_api_are_not_judged():
    evil = {"origin": "http://evil.example", "host": "monitor.example"}
    assert cross_origin_write("GET", "/api/v1/users", evil) is None
    assert cross_origin_write("OPTIONS", "/api/v1/users", evil) is None
    assert cross_origin_write("POST", "/health", evil) is None
    for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
        assert cross_origin_write(method, "/api/v1/users", evil) == "http://evil.example"


def test_a_write_from_another_site_never_reaches_its_handler():
    app = FastAPI()
    made = []

    @app.post("/api/v1/users")
    async def create_user():
        made.append(1)
        return {"ok": True}

    @app.get("/api/v1/users")
    async def list_users():
        return {"users": len(made)}

    app.add_middleware(SameOriginWritesMiddleware, allow_origins=ALLOWED)
    client = TestClient(app)

    # The request the review made: a form on another site, posting text/plain.
    res = client.post("/api/v1/users", content=b'{"username":"x","role":"ADMIN"}',
                      headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
    assert res.status_code == 403 and "another site" in res.json()["detail"]
    assert made == []

    assert client.post("/api/v1/users", json={}).status_code == 200  # no Origin: a script
    assert client.post("/api/v1/users", json={}, headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert client.get("/api/v1/users", headers={"Origin": "http://evil.example"}).status_code == 200
    assert len(made) == 2
