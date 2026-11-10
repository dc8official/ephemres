"""The type-and-input grid shared by the validator comparison and its exploration script.

Each value is something a request could carry (query strings and headers arrive as text, JSON
bodies as numbers, booleans, lists and objects), plus a few Python objects the app itself
passes in. Each type is one the core's _coerce_value handles.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Literal, Optional, Union
from uuid import UUID

U = "12345678-1234-5678-1234-567812345678"


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


TYPES = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "bytes": bytes,
    "Decimal": Decimal,
    "UUID": UUID,
    "date": date,
    "datetime": datetime,
    "Optional[int]": Optional[int],
    "Union[int,str]": Union[int, str],
    "Literal['a','b']": Literal["a", "b"],
    "Literal[1,2]": Literal[1, 2],
    "List[int]": List[int],
    "Dict[str,int]": Dict[str, int],
    "Colour": Colour,
}

INPUTS = {
    "None": None,
    "0": 0,
    "1": 1,
    "2": 2,
    "-1": -1,
    "5.0": 5.0,
    "5.7": 5.7,
    "nan": float("nan"),
    "inf": float("inf"),
    "True": True,
    "False": False,
    "'5'": "5",
    "' 5 '": " 5 ",
    "'5.0'": "5.0",
    "'5.7'": "5.7",
    "'1_000'": "1_000",
    "'abc'": "abc",
    "''": "",
    "'true'": "true",
    "'yes'": "yes",
    "'y'": "y",
    "'on'": "on",
    "'t'": "t",
    "'a'": "a",
    "'red'": "red",
    "'RED'": "RED",
    "'2026-11-10'": "2026-11-10",
    "'2026-11-10T05:00:00Z'": "2026-11-10T05:00:00Z",
    "'2026-11-10T05:00:00'": "2026-11-10T05:00:00",
    "'2026-11-10 05:00'": "2026-11-10 05:00",
    "epoch": 1762750800,
    "b'x'": b"x",
    "[1,'2']": [1, "2"],
    "(1,2)": (1, 2),
    "{'a':'1'}": {"a": "1"},
    "uuid-str": U,
    "UUID": UUID(U),
    "Decimal('1.5')": Decimal("1.5"),
    "date": date(2026, 11, 10),
    "datetime": datetime(2026, 11, 10, 5, 0),
}


def outcome(fn, value):
    """("ok", result) or ("error", None): the two answers being compared."""
    try:
        return ("ok", fn(value))
    except Exception:  # ValueError from the core, ValidationError from Pydantic
        return ("error", None)


def same(a, b) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "error":
        return True
    x, y = a[1], b[1]
    if isinstance(x, float) and isinstance(y, float) and x != x and y != y:
        return True  # both NaN
    return type(x) is type(y) and x == y
