"""Models keep their fields in a module that has no `from __future__ import annotations`.

This file leaves that import out on purpose; the first test fails if it is ever added. Up to
Python 3.13 it makes no difference. On Python 3.14 a class body without the import no longer
carries "__annotations__", and the schema and ORM metaclasses, which read that key, built
models with no fields: on 3.14 saving settings answered 500 (routers/settings.py has no
__future__ import) and 13 tests failed, while install.sh picks python3.14 first (found
2026-11-10). The fix is core/annotations.own_annotations.

Run the suite on the newest Python that install.sh accepts to exercise the 3.14 path; on
older ones these tests still check the same behaviour, which must not differ.
"""
from pathlib import Path
from typing import Optional

import pytest

from app.core.annotations import own_annotations
from app.core.orm import DeclarativeBase, Integer, Mapped, String, mapped_column
from app.core.schema import BaseModel, ConfigDict, Field, ValidationError


def test_this_module_has_no_future_import():
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("from __future__ import") for line in lines)


# --- schema models -------------------------------------------------------------------------------

class Limits(BaseModel):
    name: str
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")


class MoreLimits(Limits):
    note: Optional[str] = None


class SameLimits(Limits):
    """Adds no field of its own."""


def test_a_schema_model_has_its_fields_in_order():
    assert list(Limits.model_fields) == ["name", "port", "enabled"]
    assert Limits(name="wan").model_dump() == {"name": "wan", "port": None, "enabled": True}


def test_its_constraints_and_required_fields_are_enforced():
    # With no fields these all passed in silence: nothing was required and nothing was checked.
    with pytest.raises(ValidationError):
        Limits()
    with pytest.raises(ValidationError):
        Limits(name="wan", port=70000)
    with pytest.raises(ValidationError):
        Limits(name="wan", unknown=1)
    assert Limits(name="wan", port="2055").port == 2055


def test_subclasses_inherit_fields_and_add_their_own():
    assert list(MoreLimits.model_fields) == ["name", "port", "enabled", "note"]
    assert list(SameLimits.model_fields) == ["name", "port", "enabled"]


def test_the_settings_models_have_their_fields():
    # The models that broke: the only ones in the app defined without the __future__ import.
    from app.routers import settings as settings_router

    assert "session_timeout" in settings_router.SettingsUpdate.model_fields
    assert "performance_mode" in settings_router.SettingsPayload.model_fields
    assert settings_router.OrganizationBranding.model_fields
    assert settings_router.OrganizationBrandingUpdate.model_fields
    with pytest.raises(ValidationError):
        settings_router.SettingsUpdate(session_timeout=0)  # ge=1


# --- ORM models ----------------------------------------------------------------------------------

class Probe(DeclarativeBase):
    __tablename__ = "test_probes_no_future_import"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column()  # no type given: inferred from the annotation
    retries: Mapped[int]                  # no mapped_column at all: a column from the annotation


def test_an_orm_model_has_the_columns_its_annotations_declare():
    columns = Probe.__table_columns__
    assert list(columns) == ["id", "label", "retries"]
    assert isinstance(columns["label"].type, String)
    assert isinstance(columns["retries"].type, Integer)


# --- the reader itself ---------------------------------------------------------------------------

def test_own_annotations_reads_the_namespace_when_the_annotations_are_there():
    class Plain:
        pass

    assert own_annotations(Plain, {"__annotations__": {"x": "int"}}) == {"x": "int"}


def test_own_annotations_is_empty_for_a_class_without_any_and_never_inherited():
    class Parent:
        x: int

    class Child(Parent):
        pass

    assert own_annotations(Child, {}) == {}
