"""What /api/openapi.json gives away (M3 coverage review, 2026-11-10).

The document is served without signing in (install.sh prints its URL), so a pen test will read
it. These tests pin what it holds: route paths, methods, names and tags, the title and version,
and nothing else. No parameter or body schemas, so it reveals no field names, and no
/docs or /redoc page. Before this file nothing ever requested it.
"""
from __future__ import annotations

from app.core.asgi import EphemresApp, TestClient
from app.main import app


def test_the_real_document_lists_routes_and_nothing_more():
    resp = TestClient(app).get("/api/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    assert set(doc) == {"openapi", "info", "paths"}
    assert set(doc["info"]) == {"title", "description", "version"}
    assert "/api/v1/auth/login" in doc["paths"]
    for path, item in doc["paths"].items():
        assert path.startswith(("/api/", "/health/")), path
        for method, op in item.items():
            assert method in {"get", "post", "put", "patch", "delete", "head", "options"}
            # Only these keys: no "parameters" or "requestBody" that would describe inputs.
            assert set(op) == {"summary", "operationId", "tags", "responses"}, (path, method)


def test_there_is_no_interactive_docs_page():
    client = TestClient(app)
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/redoc").status_code == 404


def test_hidden_routes_stay_out_and_the_document_can_be_switched_off():
    demo = EphemresApp(title="t", version="1")

    @demo.get("/shown", tags=["a"], status_code=201)
    async def shown():
        return {}

    @demo.get("/hidden", include_in_schema=False)
    async def hidden():
        return {}

    doc = TestClient(demo).get("/openapi.json").json()
    assert doc["paths"] == {
        "/shown": {"get": {
            "summary": "shown", "operationId": "get_shown", "tags": ["a"],
            "responses": {"201": {"description": "Successful Response"}},
        }}
    }

    off = EphemresApp(openapi_url=None)
    assert TestClient(off).get("/openapi.json").status_code == 404
