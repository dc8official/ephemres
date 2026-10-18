import logging
import pytest
from app.core.orm import DeclarativeBase, Column, String, Integer, select
from app.core.asgi import Query


class DummyModel(DeclarativeBase):
    __tablename__ = "dummy_models"
    id = Column(Integer, primary_key=True)
    name = Column(String, default=None)
    role = Column(String)


class DummyColumnLike:
    default = None


def test_orm_param_filter_query_param_skipped(caplog):
    # Model.col == Query(None) should be skipped and emit a warning
    with caplog.at_level(logging.WARNING):
        expr = DummyModel.role == Query(None)
        assert getattr(expr, "_skip_where", False) is True
        assert "Unresolved query param object used in SQL comparison; filter skipped" in caplog.text

    stmt = select(DummyModel).where(DummyModel.role == Query(None))
    assert len(stmt._where_clauses) == 0


def test_orm_param_filter_column_like_preserved(caplog):
    # Model.col == Column(...)-like object with .default=None produces a real predicate
    caplog.clear()
    col_like = DummyColumnLike()
    expr = DummyModel.role == col_like
    assert getattr(expr, "_skip_where", False) is False
    assert "Unresolved query param object used in SQL comparison; filter skipped" not in caplog.text

    stmt = select(DummyModel).where(expr)
    assert len(stmt._where_clauses) == 1
    assert stmt._where_clauses[0] is expr

    stmt2 = select(DummyModel).where(DummyModel.role == col_like)
    assert len(stmt2._where_clauses) == 1


def test_orm_param_filter_other_comparisons(caplog):
    for op in ["__ne__", "__lt__", "__le__", "__gt__", "__ge__"]:
        # Query(None)
        caplog.clear()
        expr_query = getattr(DummyModel.id, op)(Query(None))
        assert getattr(expr_query, "_skip_where", False) is True
        assert "Unresolved query param object used in SQL comparison; filter skipped" in caplog.text

        # Column-like object with default=None
        col_like = DummyColumnLike()
        expr_col = getattr(DummyModel.id, op)(col_like)
        assert getattr(expr_col, "_skip_where", False) is False
