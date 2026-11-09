"""The hand-written core refuses what it used to accept and quietly ignore (scale review M2).

Each case below used to succeed with a different result than Pydantic or SQLAlchemy give, so
code written against those libraries' behaviour could go wrong without any error.
"""
import pytest

from app.core.orm import create_async_engine
from app.core.schema import BaseModel, ConfigDict, ValidationError, _coerce_value, to_camel


# --- int and bool conversion ------------------------------------------------------------------

@pytest.mark.parametrize("value", [5.7, -0.5, float("nan"), float("inf")])
def test_int_rejects_numbers_with_a_fractional_part(value):
    with pytest.raises(ValueError, match="fractional part"):
        _coerce_value(value, int, "f", {})


@pytest.mark.parametrize("value, expected", [(5, 5), (5.0, 5), ("7", 7)])
def test_int_still_accepts_whole_numbers(value, expected):
    assert _coerce_value(value, int, "f", {}) == expected


@pytest.mark.parametrize("value", [2, -1, 0.5, [1], {"a": 1}])
def test_bool_rejects_anything_but_0_1_and_boolean_words(value):
    with pytest.raises(ValueError, match="valid boolean"):
        _coerce_value(value, bool, "f", {})


@pytest.mark.parametrize("value, expected", [(1, True), (0, False), (True, True), ("on", True), ("false", False)])
def test_bool_still_accepts_0_1_and_boolean_words(value, expected):
    assert _coerce_value(value, bool, "f", {}) is expected


def test_a_model_field_reports_the_fractional_int():
    class Page(BaseModel):
        size: int

    with pytest.raises(ValidationError):
        Page(size=2.5)


# --- extra= ------------------------------------------------------------------------------------

class _Strict(BaseModel):
    name: str
    page_size: int = 1
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


def test_extra_forbid_rejects_unknown_keys():
    with pytest.raises(ValidationError) as exc:
        _Strict(name="a", colour="red")
    assert exc.value.errors()[0]["type"] == "extra_forbidden"
    assert exc.value.errors()[0]["loc"] == ("colour",)


def test_extra_forbid_accepts_field_names_and_their_aliases():
    assert _Strict(name="a", pageSize=2).page_size == 2
    assert _Strict(name="a", page_size=3).page_size == 3


def test_extra_ignore_is_still_the_default():
    class Loose(BaseModel):
        name: str

    assert Loose(name="a", colour="red").model_dump() == {"name": "a"}


def test_extra_allow_is_refused_rather_than_dropping_keys():
    class Open(BaseModel):
        name: str
        model_config = ConfigDict(extra="allow")

    with pytest.raises(TypeError, match="not supported"):
        Open(name="a", colour="red")


# --- engine options ----------------------------------------------------------------------------

@pytest.mark.parametrize("option", ["echo", "pool_pre_ping", "pool_recycle", "pool_timeout"])
def test_engine_refuses_options_it_would_ignore(option):
    with pytest.raises(TypeError, match=option):
        create_async_engine("postgresql+asyncpg://u:p@localhost/db", **{option: True})


def test_engine_accepts_its_pool_options():
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db", pool_size=3, max_overflow=1)
    assert (engine.pool_size, engine.max_overflow) == (3, 1)
