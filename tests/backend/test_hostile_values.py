"""Values no database column holds are the client's mistake, not a 500 (first-phase review L1).

6,900 hostile requests over the GET routes found two: a NUL character in the shared filter
(site, location, tag, type, q), which routes read from the query string themselves, and a page
or offset of 99999999999999999999. Both reached PostgreSQL and came back as its error.
"""
from typing import List, Optional

from app.core.asgi import FastAPI, Query, Request, TestClient
from app.core.schema import BaseModel

app = FastAPI()
reached = []


class Note(BaseModel):
    title: str
    tags: List[str] = []
    count: Optional[int] = None


@app.get("/api/v1/things")
async def things(request: Request, page: int = Query(default=1, ge=1), idx: Optional[int] = Query(default=None)):
    # As the shared filter does: straight from the query string, past the declared parameters.
    reached.append((request.query_params.get("q"), page, idx))
    return {"ok": True}


@app.get("/api/v1/things/{name}")
async def thing(name: str):
    reached.append(name)
    return {"ok": True}


@app.post("/api/v1/things")
async def make(note: Note):
    reached.append(note.title)
    return {"ok": True}


client = TestClient(app)


def test_a_nul_character_in_the_address_is_refused_before_any_route():
    reached.clear()
    for url in (
        "/api/v1/things?q=a%00b",
        "/api/v1/things?site=%00",            # a key no parameter declares
        "/api/v1/things?tag=x&tag=y%00",      # the second of a repeated key
        "/api/v1/things?a%00b=1",             # in the key itself
    ):
        res = client.get(url)
        assert res.status_code == 400 and "NUL" in res.json()["detail"], url
    assert reached == []
    assert client.get("/api/v1/things?q=a%2500b").status_code == 200  # a literal "%00" in a value is text
    assert reached == [("a%00b", 1, None)]
    # In the path: the server hands the path over decoded (this test client does not), and a
    # path parameter is checked like any declared value.
    from app.core.schema import _FieldInfo, _validate_constraints
    import pytest
    with pytest.raises(ValueError):
        _validate_constraints("abc\x00", _FieldInfo(), "name")


def test_a_nul_character_in_a_body_is_a_validation_error():
    reached.clear()
    for body in ({"title": "a\x00b"}, {"title": "ok", "tags": ["x", "y\x00"]}):
        assert client.post("/api/v1/things", json=body).status_code == 422, body
    assert reached == []
    assert client.post("/api/v1/things", json={"title": "ok", "tags": ["x"]}).status_code == 200


def test_an_integer_wider_than_64_bits_is_a_validation_error():
    reached.clear()
    huge = "99999999999999999999"
    assert client.get(f"/api/v1/things?page={huge}").status_code == 422
    assert client.get(f"/api/v1/things?idx={huge}").status_code == 422   # no bounds declared
    assert client.get(f"/api/v1/things?idx=-{huge}").status_code == 422
    assert client.post("/api/v1/things", json={"title": "ok", "count": int(huge)}).status_code == 422
    assert reached == []
    assert client.get(f"/api/v1/things?idx={2 ** 63 - 1}").status_code == 200
