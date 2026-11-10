"""app.core.schema._coerce_value against Pydantic's TypeAdapter, type by type.

Every pair in grid.TYPES x grid.INPUTS goes through both. They must agree (both refuse, or both
accept with an equal value of the same type) unless a rule below explains the difference. Most
rules are the core being stricter than Pydantic's lax mode, which is the safe direction: input
the core refuses gets a 422, and the client can send it in the plain form instead.
"""
from __future__ import annotations

from pydantic import TypeAdapter

from app.core.schema import _coerce_value
from grid import INPUTS, TYPES, outcome, same

NUMERIC = {"int", "float", "Optional[int]", "Union[int,str]", "Literal[1,2]"}
NUMBERS = {"0", "1", "2", "-1", "5.0", "5.7", "epoch", "'5'", "'5.0'", "'5.7'", "Decimal('1.5')"}

# (rule name, does it explain this pair, what the core does, why the difference is kept)
RULES = [
    ("bool-is-not-a-number",
     lambda t, i: t in NUMERIC and i in {"True", "False"}, "error",
     "True/False sent for a number is far more likely a client bug than a wish for 1/0."),
    ("no-decimal-point-for-int",
     lambda t, i: t in {"int", "Optional[int]"} and i == "'5.0'", "error",
     "Integer text must be an integer; the number 5.0 itself is still accepted."),
    ("no-epoch-seconds-for-dates",
     lambda t, i: t in {"date", "datetime"} and i in NUMBERS, "error",
     "Ephemres sends and accepts ISO 8601 text only; a bare number is ambiguous (s or ms?)."),
    ("bytes-are-not-text",
     lambda t, i: t in {"str", "Union[int,str]"} and i == "b'x'", "error",
     "JSON has no bytes; only server code could pass them, and should decode first."),
    ("uuid-objects-are-text",
     lambda t, i: t in {"str", "Union[int,str]"} and i == "UUID", "ok",
     "Response models declare ids as str and are filled from ORM rows holding UUID objects."),
    ("union-tries-types-in-order",
     lambda t, i: t == "Union[int,str]" and i in {"'5'", "' 5 '", "'1_000'"}, "ok",
     "Documented in schema.py: the first type that converts wins (Pydantic's smart mode "
     "prefers an exact type match). No Ephemres schema has an int/str union."),
]


def _compare():
    unexplained, used = [], set()
    for tname, t in TYPES.items():
        adapter = TypeAdapter(t)
        for iname, value in INPUTS.items():
            core = outcome(lambda v: _coerce_value(v, t, "f", {}), value)
            pyd = outcome(adapter.validate_python, value)
            if same(core, pyd):
                continue
            rule = next((r for r in RULES if r[1](tname, iname)), None)
            if rule is None or core[0] != rule[2]:
                unexplained.append(f"{tname} <- {iname}: core={core}, pydantic={pyd}")
            else:
                used.add(rule[0])
    return unexplained, used


def test_the_core_agrees_with_pydantic_except_for_the_listed_rules():
    unexplained, _ = _compare()
    assert not unexplained, "New difference(s) from Pydantic:\n" + "\n".join(unexplained)


def test_every_listed_rule_still_explains_a_real_difference():
    # A rule that matches nothing any more is stale: the core changed, so the list must too.
    _, used = _compare()
    assert used == {r[0] for r in RULES}
