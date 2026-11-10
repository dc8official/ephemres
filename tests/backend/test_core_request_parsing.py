"""How the framework turns a request into route arguments (M3 coverage review, 2026-11-10).

_resolve_endpoint_kwargs is where untrusted input first meets the code: path, header, query and
body values are converted to the annotated types, and every problem must come back as one 422
listing all of them, never as a 500 or as a value the route didn't ask for. About a third of it
never ran in a test before this file. Writing these tests found two bugs, fixed in the same
change: a parameter without a default wasn't required (a missing value reached the route as
inspect._empty, and an empty POST to a model route crashed with 500), and a bare `dict` body
parameter read the query string instead of the body.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.asgi import EphemresApp, Header, Query, TestClient
from app.core.schema import BaseModel


class Item(BaseModel):
    name: str
    qty: int


app = EphemresApp()


@app.get("/q")
async def required_query(n: int):
    return {"n": n}


@app.get("/q-default")
async def optional_query(n: int = 5, tags: List[str] = Query(default=[])):
    return {"n": n, "tags": tags}


@app.get("/q-bounded")
async def bounded_query(limit: int = Query(10, ge=1, le=100)):
    return {"limit": limit}


@app.get("/q-list")
async def required_list(ids: List[int]):
    return {"ids": ids}


@app.get("/items/{item_id}")
async def path_param(item_id: int):
    return {"item_id": item_id}


@app.get("/h")
async def header_param(x_request_id: str = Header(...), x_count: int = Header(0)):
    return {"id": x_request_id, "count": x_count}


@app.post("/model")
async def model_body(item: Item):
    return {"name": item.name, "qty": item.qty}


@app.post("/model-optional")
async def optional_model_body(item: Item = None):
    return {"item": None if item is None else item.name}


@app.post("/dict")
async def bare_dict_body(payload: dict):
    return {"payload": payload}


@app.post("/typed-dict")
async def typed_dict_body(payload: Dict[str, int]):
    return {"payload": payload}


@app.post("/many/{item_id}")
async def many_inputs(item_id: int, item: Item, n: int, x_count: int = Header(...)):
    return {}


async def _override(*args: Any, **kwargs: Any):
    return {"args": list(args), "kwargs": kwargs}


app.get("/varargs")(_override)

client = TestClient(app)


def _errors(resp) -> list[tuple]:
    assert resp.status_code == 422, resp.text
    return [(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]]


# --- query parameters ---------------------------------------------------------------------------

def test_a_query_parameter_without_a_default_is_required():
    assert _errors(client.get("/q")) == [(("query", "n"), "missing")]
    assert client.get("/q?n=3").json() == {"n": 3}


def test_optional_query_parameters_fall_back_to_their_defaults():
    assert client.get("/q-default").json() == {"n": 5, "tags": []}
    assert client.get("/q-default?n=1&tags=a&tags=b").json() == {"n": 1, "tags": ["a", "b"]}


def test_a_query_value_of_the_wrong_type_is_a_422_not_a_500():
    assert _errors(client.get("/q?n=abc")) == [(("query", "n"), "value_error")]
    assert _errors(client.get("/q?n=1.5")) == [(("query", "n"), "value_error")]


def test_query_bounds_are_enforced():
    assert client.get("/q-bounded?limit=100").json() == {"limit": 100}
    for bad in ("0", "101", "-1"):
        assert _errors(client.get(f"/q-bounded?limit={bad}")) == [(("query", "limit"), "value_error")]


def test_a_required_list_needs_at_least_one_value_and_converts_each():
    assert _errors(client.get("/q-list")) == [(("query", "ids"), "missing")]
    assert client.get("/q-list?ids=1&ids=2").json() == {"ids": [1, 2]}
    assert _errors(client.get("/q-list?ids=1&ids=x")) == [(("query", "ids"), "value_error")]


# --- path and header parameters -----------------------------------------------------------------

def test_a_path_value_of_the_wrong_type_is_a_422():
    assert client.get("/items/7").json() == {"item_id": 7}
    assert _errors(client.get("/items/seven")) == [(("path", "item_id"), "value_error")]


def test_header_names_use_dashes_and_required_headers_are_enforced():
    assert _errors(client.get("/h")) == [(("header", "x-request-id"), "missing")]
    resp = client.get("/h", headers={"X-Request-Id": "abc", "X-Count": "3"})
    assert resp.json() == {"id": "abc", "count": 3}
    assert _errors(client.get("/h", headers={"X-Request-Id": "a", "X-Count": "many"})) == [
        (("header", "x-count"), "value_error")
    ]


# --- bodies -------------------------------------------------------------------------------------

def test_a_model_body_is_validated():
    assert client.post("/model", json={"name": "a", "qty": 2}).json() == {"name": "a", "qty": 2}


def test_an_empty_post_to_a_model_route_is_a_422():
    # Used to reach the route as inspect._empty and crash with 500.
    assert _errors(client.post("/model")) == [(("body",), "missing")]
    assert _errors(client.post("/model", content=b"   ")) == [(("body",), "missing")]


def test_an_optional_model_body_may_be_left_out():
    assert client.post("/model-optional").json() == {"item": None}


def test_invalid_json_is_a_422():
    resp = client.post("/model", content=b"{not json", headers={"Content-Type": "application/json"})
    assert _errors(resp) == [(("body",), "json_invalid")]


def test_model_errors_point_at_the_field():
    errors = _errors(client.post("/model", json={"name": "a", "qty": "lots"}))
    assert errors and all(loc[:2] == ("body", "qty") for loc, _ in errors)


def test_a_json_array_is_not_a_model():
    # The validator reports a non-object as an error on the whole model ("__root__").
    assert _errors(client.post("/model", json=[1, 2]))[0][0] == ("body", "__root__")


def test_a_bare_dict_parameter_reads_the_body():
    # Used to fall through to the query string and reach the route as inspect._empty.
    assert client.post("/dict", json={"a": 1}).json() == {"payload": {"a": 1}}
    assert _errors(client.post("/dict")) == [(("body",), "missing")]


def test_a_typed_dict_body_converts_its_values():
    assert client.post("/typed-dict", json={"a": "2"}).json() == {"payload": {"a": 2}}
    assert _errors(client.post("/typed-dict", json={"a": "two"}))[0][0][0] == "body"
    assert _errors(client.post("/typed-dict", json=[1]))[0][0] == ("body",)


# --- several problems at once -------------------------------------------------------------------

def test_every_problem_is_reported_together():
    errors = _errors(client.post("/many/x", json={"name": "a"}))
    locs = {loc for loc, _ in errors}
    assert ("path", "item_id") in locs
    assert ("header", "x-count") in locs
    assert ("query", "n") in locs
    assert any(loc[:2] == ("body", "qty") for loc in locs)


def test_var_args_and_kwargs_take_nothing_from_the_request():
    # Dependency overrides are often written as f(*args, **kwargs) (AsyncMock is one).
    assert client.get("/varargs?a=1").json() == {"args": [], "kwargs": {}}
