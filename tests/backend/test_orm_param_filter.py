"""A route's Query(...) default reaching a SQL comparison (scale review M2).

That only happens when a route function is called directly (tests); the router always resolves
parameters first. A Query(None) used to drop the condition from WHERE with a warning, so the
query quietly returned rows the caller meant to filter out (fail-open). It now raises.
"""
import pytest

from app.core.asgi import Query
from app.core.orm import Column, DeclarativeBase, Integer, String, and_, select

OPERATORS = ["__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"]


class DummyModel(DeclarativeBase):
    __tablename__ = "dummy_models"
    id = Column(Integer, primary_key=True)
    name = Column(String, default=None)
    role = Column(String)


class DummyColumnLike:
    default = None


@pytest.mark.parametrize("op", OPERATORS)
def test_unresolved_query_none_raises_instead_of_dropping_the_filter(op):
    with pytest.raises(TypeError, match="unresolved Query"):
        getattr(DummyModel.id, op)(Query(None))


def test_required_query_parameter_raises_too():
    with pytest.raises(TypeError, match="unresolved Query"):
        DummyModel.role == Query(...)  # noqa: B015


def test_query_with_a_concrete_default_uses_it_like_the_router():
    stmt = select(DummyModel).where(DummyModel.role == Query("ADMIN"))
    assert len(stmt._where_clauses) == 1
    assert stmt._where_clauses[0].right == "ADMIN"


@pytest.mark.parametrize("op", OPERATORS)
def test_column_like_object_is_compared_not_dropped(op):
    expr = getattr(DummyModel.id, op)(DummyColumnLike())
    stmt = select(DummyModel).where(expr)
    assert len(stmt._where_clauses) == 1 and stmt._where_clauses[0] is expr


def test_empty_and_adds_no_condition():
    # Optional filters that are all unset: and_() with nothing in it is left out on purpose.
    stmt = select(DummyModel).where(and_())
    assert stmt._where_clauses == []
