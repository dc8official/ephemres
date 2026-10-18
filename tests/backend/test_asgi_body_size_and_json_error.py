import pytest
from app.core.asgi import EphemresApp, TestClient, BaseModel


class Item(BaseModel):
    name: str


def test_asgi_invalid_json_body_returns_422_json_invalid():
    app = EphemresApp()

    @app.post("/items")
    async def create_item(item: Item):
        return {"name": item.name}

    client = TestClient(app)

    # Post invalid JSON
    res = client.post(
        "/items",
        content="{bad",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 422
    data = res.json()
    assert "detail" in data
    errors = data["detail"]
    assert any(e.get("type") == "json_invalid" and e.get("msg") == "Invalid JSON body" for e in errors)


def test_asgi_oversized_body_returns_413(monkeypatch):
    import app.core.asgi as asgi_module

    # Set limit to 50 bytes for test
    monkeypatch.setattr(asgi_module, "MAX_BODY_BYTES", 50)

    app = EphemresApp()

    @app.post("/upload")
    async def upload_data(item: Item):
        return {"name": item.name}

    client = TestClient(app)

    # Body larger than 50 bytes
    large_payload = '{"name": "' + "a" * 100 + '"}'
    res = client.post(
        "/upload",
        content=large_payload,
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413
    assert "Request body too large" in res.text
