"""Load the app's real ORM models a second time, built on real SQLAlchemy.

Every module in backend/app/models imports its column types and helpers from app.core.orm and
its base class from app.database. Here each one is compiled again from its source with those
imports pointed at SQLAlchemy (and a fresh DeclarativeBase), and imports of sibling model
modules pointed at the SQLAlchemy copies. Both copies describe the same tables, so the same
query written against each can run on one database and the rows compared.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MODELS_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "models"


class Base(DeclarativeBase):
    pass


_shim = types.ModuleType("sqlalchemy_twin_shim")
for name in ("BigInteger", "Boolean", "DateTime", "ForeignKey", "Index", "Integer", "Numeric",
             "SmallInteger", "String", "Text", "func", "text"):
    setattr(_shim, name, getattr(sa, name))
for name in ("ARRAY", "INET", "JSONB", "UUID"):
    setattr(_shim, name, getattr(postgresql, name))
_shim.Mapped, _shim.mapped_column, _shim.relationship = Mapped, mapped_column, relationship
_shim.Base = Base
sys.modules[_shim.__name__] = _shim

_IMPORT_CORE = re.compile(r"\bapp\.core\.orm\b")
_IMPORT_BASE = re.compile(r"^from app\.database import Base$", re.M)
_IMPORT_SIBLING = re.compile(r"\bapp\.models\.(\w+)")


def _load_twin(stem: str, loaded: dict) -> types.ModuleType:
    if stem in loaded:
        return loaded[stem]
    src = (MODELS_DIR / f"{stem}.py").read_text()
    for sibling in set(_IMPORT_SIBLING.findall(src)):
        _load_twin(sibling, loaded)
    src = _IMPORT_CORE.sub(_shim.__name__, src)
    src = _IMPORT_BASE.sub(f"from {_shim.__name__} import Base", src)
    src = _IMPORT_SIBLING.sub(lambda m: f"sqlalchemy_twin.{m.group(1)}", src)
    mod = types.ModuleType(f"sqlalchemy_twin.{stem}")
    mod.__file__ = str(MODELS_DIR / f"{stem}.py")
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    loaded[stem] = mod
    return mod


def load_models():
    """Two namespaces with the same model names: (core models, SQLAlchemy models)."""
    import app.models as core_models

    sys.modules.setdefault("sqlalchemy_twin", types.ModuleType("sqlalchemy_twin"))
    loaded: dict = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        if path.stem != "__init__":
            _load_twin(path.stem, loaded)
    sa.orm.configure_mappers()  # resolve relationship("User") and friends now, not mid-test

    names = [n for n in dir(core_models) if not n.startswith("_")]
    core_ns, twin_ns = types.SimpleNamespace(), types.SimpleNamespace()
    for mod in loaded.values():
        for name in names:
            if hasattr(mod, name) and isinstance(getattr(mod, name), type):
                setattr(twin_ns, name, getattr(mod, name))
                setattr(core_ns, name, getattr(core_models, name))
    return core_ns, twin_ns
