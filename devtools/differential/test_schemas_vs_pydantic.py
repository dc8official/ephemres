"""The app's real schemas on the core against the same schemas on Pydantic.

twin_schemas.py loads every module in backend/app/schemas a second time on real Pydantic, so
both copies run the same Field() constraints, validators, aliases and defaults. Two checks:

- Field by field: each field gets every value in grid.INPUTS on its own. Whether that field
  ends up with an error must match, unless the bare type already differs for that value
  (test_validator_vs_pydantic.py covers those).
- Whole models: a payload valid for Pydantic must be valid for the core too, and dump to the
  same JSON (by name, by alias, and without None values).
"""
from __future__ import annotations

from datetime import date, datetime
import json
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

import pydantic
import pytest
from pydantic import TypeAdapter

from app.core.schema import _coerce_value
from grid import INPUTS, U, outcome, same
from twin_schemas import model_pairs

PAIRS = model_pairs()
IDS = [q for q, _, _ in PAIRS]


def _error_fields(cls, payload) -> frozenset:
    try:
        cls.model_validate(payload)
        return frozenset()
    except (ValueError, TypeError) as exc:  # both ValidationErrors are ValueErrors
        return frozenset(str(e["loc"][0]) for e in exc.errors() if e.get("loc"))


def _type_level_difference(annotation, value) -> bool:
    try:
        adapter = TypeAdapter(annotation)
    except Exception:
        return False
    core = outcome(lambda v: _coerce_value(v, annotation, "f", {}), value)
    return not same(core, outcome(adapter.validate_python, value))


def test_all_schema_modules_were_compared():
    assert len(PAIRS) >= 40, IDS  # 45 models on 2026-11-10; guards against a broken loader


@pytest.mark.parametrize("qname, core, twin", PAIRS, ids=IDS)
def test_each_field_accepts_and_refuses_the_same_values(qname, core, twin):
    differences = []
    for name, info in twin.model_fields.items():
        keys = {name} | ({info.alias} if info.alias else set())
        for key in keys:
            for label, value in INPUTS.items():
                if _type_level_difference(info.annotation, value):
                    continue
                c = bool(_error_fields(core, {key: value}) & {name, key})
                p = bool(_error_fields(twin, {key: value}) & {name, key})
                if c != p:
                    differences.append(f"{name} (sent as {key!r}) <- {label}: core error={c}, pydantic error={p}")
    assert not differences, "\n".join(differences)


# --- whole models -----------------------------------------------------------------------------

def _sample(annotation) -> Any:
    """A value Pydantic should accept for this annotation."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return args[0]
    if origin in (Union, types.UnionType):
        return _sample(next(a for a in args if a is not type(None)))
    if origin in (list, typing.List, set, tuple):
        return []
    if origin in (dict, typing.Dict) or annotation is dict:
        return {}
    if isinstance(annotation, typing.TypeVar) or annotation is Any:
        return None
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return _valid_payload(annotation)
    return {
        UUID: U, datetime: "2026-11-10T05:00:00+00:00", date: "2026-11-10", int: 3,
        float: 1.5, bool: True, str: "alice_01",
    }.get(annotation, "x")


# Values for fields whose validators need more than a plausible sample of their type.
OVERRIDES = {
    "ip_address": "10.0.0.1", "primary_ip": "10.0.0.1", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2",
    "password": "Str0ng!Passw0rd#", "new_password": "Str0ng!Passw0rd#", "old_password": "x",
    "config": {"webhook_url": "https://example.com/hook"},
    "active_gap": False,  # True requires gap_start_time (a model_validator)
}

# Output differences that are understood, by model: (field, reason). The field is left out of
# the comparison, and the test checks the two sides still differ there, so a stale entry fails.
KNOWN_DUMP_DIFFERENCES = {
    "common.APIResponse": (
        "error",
        "The classmethod APIResponse.error() is defined after the field `error: ... = None` and "
        "replaces its default in the class body. Pydantic then uses the method object as the "
        "default (dumped as {}); the core keeps None, which is what the schema means.",
    ),
}


def _valid_payload(twin) -> dict:
    payload = {}
    for name, info in twin.model_fields.items():
        if info.is_required():
            payload[name] = OVERRIDES.get(name, _sample(info.annotation))
    return payload


def _dump(model, **kw) -> Any:
    return json.loads(json.dumps(model.model_dump(mode="json", **kw), default=str, sort_keys=True))


def _utc_z(value: Any) -> Any:
    """The one known output difference: the core writes UTC as +00:00, Pydantic as Z. Both are
    ISO 8601 and the browser's Date() reads both, so normalise it before comparing."""
    if isinstance(value, str) and value.endswith("+00:00") and "T" in value:
        return value[:-6] + "Z"
    if isinstance(value, dict):
        return {k: _utc_z(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_utc_z(v) for v in value]
    return value


@pytest.mark.parametrize("qname, core, twin", PAIRS, ids=IDS)
def test_a_valid_payload_validates_and_dumps_the_same(qname, core, twin):
    payload = _valid_payload(twin)
    try:
        expected = twin.model_validate(payload)
    except pydantic.ValidationError as exc:
        pytest.skip(f"no generated payload satisfies this model's validators: {exc.errors()[0]['msg']}")
    actual = core.model_validate(payload)
    known = KNOWN_DUMP_DIFFERENCES.get(qname)
    for kw in ({}, {"by_alias": True}, {"exclude_none": True}):
        a, b = _utc_z(_dump(actual, **kw)), _utc_z(_dump(expected, **kw))
        if known and kw == {}:
            assert a.get(known[0]) != b.get(known[0]), f"stale KNOWN_DUMP_DIFFERENCES entry: {qname}"
        if known:
            a.pop(known[0], None)
            b.pop(known[0], None)
        assert a == b, kw
