import pytest
from app.core.orm import DeclarativeBase, Column, Integer, String, select


class DummyModel(DeclarativeBase):
    __tablename__ = "dummy_models"
    id = Column(Integer, primary_key=True)
    name = Column(String)


def test_orm_limit_offset_invalid_values_raise():
    # String injection attempt
    with pytest.raises(ValueError):
        select(DummyModel).limit("5; DROP")

    with pytest.raises(ValueError):
        select(DummyModel).offset("10; DELETE")

    # Negative values
    with pytest.raises(ValueError, match="LIMIT cannot be negative"):
        select(DummyModel).limit(-1)

    with pytest.raises(ValueError, match="OFFSET cannot be negative"):
        select(DummyModel).offset(-5)


def test_orm_limit_offset_valid_values():
    s = select(DummyModel).limit(10).offset(20)
    assert s._limit == 10
    assert s._offset == 20

    s_str = select(DummyModel).limit("15").offset("25")
    assert s_str._limit == 15
    assert s_str._offset == 25

    s_none = s.limit(None).offset(None)
    assert s_none._limit is None
    assert s_none._offset is None
