"""Type conversion in the validator, for the types tests didn't reach (M3 coverage review,
2026-11-10).

_coerce_value converts every model field and route parameter, so a type it gets wrong is wrong
everywhere. test_core_strictness.py pins int and bool (M2); this file covers the rest: Optional
and Union, Literal, float, str, bytes, Decimal, UUID, date, datetime, lists, dicts, enums and
nested models. Each case checks both a value that converts and one that must be refused with
ValueError (which becomes a 422), never passed through unchanged.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import ipaddress
from typing import Dict, List, Literal, Optional, Set, Union
from uuid import UUID

import pytest

from app.core.schema import BaseModel, _coerce_value

U = "12345678-1234-5678-1234-567812345678"


def ok(value, target):
    return _coerce_value(value, target, "f", {})


def refused(value, target):
    with pytest.raises(ValueError):
        _coerce_value(value, target, "f", {})


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


class Inner(BaseModel):
    n: int


class Row:
    """An ORM-like object with attributes, for from_attributes."""

    def __init__(self, n):
        self.n = n


# --- None, Optional, Union, Literal -------------------------------------------------------------

def test_none_is_refused_unless_the_type_is_optional():
    refused(None, int)
    assert ok(None, Optional[int]) is None
    assert ok(None, int | None) is None


def test_a_union_takes_the_first_type_that_converts():
    # Order matters (unlike Pydantic's smart mode): "5" becomes 5 because int comes first.
    assert ok("5", Union[int, str]) == 5
    assert ok("five", Union[int, str]) == "five"
    refused("five", Union[int, float])


def test_literal_accepts_only_its_values():
    assert ok("wan", Literal["wan", "lan"]) == "wan"
    refused("WAN", Literal["wan", "lan"])


# --- numbers ------------------------------------------------------------------------------------

def test_float_conversion():
    assert ok(2, float) == 2.0 and isinstance(ok(2, float), float)
    assert ok("2.5", float) == 2.5
    refused(True, float)
    refused("two", float)
    refused([1], float)


def test_decimal_conversion_goes_through_text_so_floats_keep_their_shortest_form():
    assert ok("1.10", Decimal) == Decimal("1.10")
    assert ok(0.1, Decimal) == Decimal("0.1")  # not 0.1000000000000000055511151231257827...
    assert ok(Decimal("3"), Decimal) == Decimal("3")
    refused("ten", Decimal)


# --- text, bytes, ids ---------------------------------------------------------------------------

def test_str_accepts_text_uuids_and_ip_objects_but_not_numbers():
    assert ok("x", str) == "x"
    assert ok(UUID(U), str) == U
    assert ok(ipaddress.ip_address("10.0.0.1"), str) == "10.0.0.1"
    assert ok(ipaddress.ip_network("10.0.0.0/8"), str) == "10.0.0.0/8"
    refused(5, str)
    refused(b"x", str)


def test_bytes_accepts_bytes_and_utf8_text():
    assert ok(b"\x00", bytes) == b"\x00"
    assert ok("é", bytes) == "é".encode()
    refused(5, bytes)


def test_uuid_conversion():
    assert ok(U, UUID) == UUID(U)
    assert ok(UUID(U), UUID) == UUID(U)
    refused("not-a-uuid", UUID)
    refused(12345, UUID)


# --- dates and times ----------------------------------------------------------------------------

def test_date_conversion():
    assert ok("2026-11-10", date) == date(2026, 11, 10)
    assert ok(datetime(2026, 11, 10, 12, 30), date) == date(2026, 11, 10)
    refused("10/11/2026", date)
    refused(20261110, date)


def test_datetime_accepts_z_and_offsets_and_keeps_naive_values_naive():
    assert ok("2026-11-10T05:00:00Z", datetime) == datetime(2026, 11, 10, 5, tzinfo=timezone.utc)
    plus_one = ok("2026-11-10T06:00:00+01:00", datetime)
    assert plus_one.utcoffset() == timedelta(hours=1)
    assert ok("2026-11-10T05:00:00", datetime).tzinfo is None  # no time zone is assumed
    assert ok(date(2026, 11, 10), datetime) == datetime(2026, 11, 10)
    refused("yesterday", datetime)
    refused(1762750800, datetime)  # epoch seconds are not accepted


# --- collections --------------------------------------------------------------------------------

def test_lists_convert_each_item_and_accept_tuples_and_sets():
    assert ok(["1", 2], List[int]) == [1, 2]
    assert ok((1, 2), List[int]) == [1, 2]
    assert sorted(ok({1, 2}, List[int])) == [1, 2]
    refused("12", List[int])  # a string is not a list of its characters
    refused({"a": 1}, List[int])
    refused([1, "x"], List[int])


def test_dicts_convert_keys_and_values():
    assert ok({"1": "2"}, Dict[int, int]) == {1: 2}
    assert ok({"a": [1]}, Dict[str, List[int]]) == {"a": [1]}
    refused([("a", 1)], Dict[str, int])
    refused({"a": "x"}, Dict[str, int])


def test_types_the_validator_does_not_list_are_passed_through_unchecked():
    # Documented in schema.py's header: set, tuple and others are not checked. Pinned here so
    # adding a check is a deliberate change.
    assert ok({"x"}, Set[int]) == {"x"}


# --- enums and nested models --------------------------------------------------------------------

def test_enum_accepts_its_values_and_its_member_names():
    assert ok("red", Colour) is Colour.RED
    assert ok(Colour.BLUE, Colour) is Colour.BLUE
    # Pydantic accepts only values; this validator also accepts the member name.
    assert ok("RED", Colour) is Colour.RED
    refused("green", Colour)


def test_nested_models_from_dicts_objects_and_instances():
    assert ok({"n": "3"}, Inner).n == 3
    inner = Inner(n=1)
    assert ok(inner, Inner) is inner
    refused(Row(4), Inner)  # an object needs from_attributes
    assert _coerce_value(Row(4), Inner, "f", {"from_attributes": True}).n == 4
    refused("n=3", Inner)
