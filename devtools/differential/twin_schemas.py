"""Load the app's real schemas a second time, built on real Pydantic.

Every module in backend/app/schemas imports its base classes from app.core.schema. Here each
one is compiled again from its source with that import pointed at Pydantic, and with imports
of sibling schema modules pointed at the Pydantic copies, so both copies of every model are
the same code on different validators. The core copy is the normal import.
"""
from __future__ import annotations

import importlib
import inspect
import re
import sys
import types
from pathlib import Path

import pydantic
from pydantic import alias_generators

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "schemas"

# The names app code imports from app.core.schema, mapped to Pydantic's own.
_shim = types.ModuleType("pydantic_twin_shim")
for name in ("BaseModel", "ConfigDict", "Field", "ValidationError",
             "field_serializer", "field_validator", "model_validator"):
    setattr(_shim, name, getattr(pydantic, name))
_shim.to_camel = alias_generators.to_camel
sys.modules[_shim.__name__] = _shim

_IMPORT_CORE = re.compile(r"\bapp\.core\.schema\b")
_IMPORT_SIBLING = re.compile(r"\bapp\.schemas\.(\w+)")


def _load_twin(stem: str, loaded: dict) -> types.ModuleType:
    if stem in loaded:
        return loaded[stem]
    src = (SCHEMAS_DIR / f"{stem}.py").read_text()
    for sibling in set(_IMPORT_SIBLING.findall(src)):
        _load_twin(sibling, loaded)
    src = _IMPORT_CORE.sub(_shim.__name__, src)
    src = _IMPORT_SIBLING.sub(lambda m: f"pydantic_twin.{m.group(1)}", src)
    mod = types.ModuleType(f"pydantic_twin.{stem}")
    mod.__file__ = str(SCHEMAS_DIR / f"{stem}.py")
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    loaded[stem] = mod
    return mod


def model_pairs():
    """(qualified name, core model class, Pydantic model class) for every schema model."""
    from app.core.schema import BaseModel as CoreBase

    sys.modules.setdefault("pydantic_twin", types.ModuleType("pydantic_twin"))
    loaded: dict = {}
    pairs = []
    for path in sorted(SCHEMAS_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        core_mod = importlib.import_module(f"app.schemas.{path.stem}")
        twin_mod = _load_twin(path.stem, loaded)
        for name, core_cls in vars(core_mod).items():
            if not (inspect.isclass(core_cls) and issubclass(core_cls, CoreBase)):
                continue
            if core_cls.__module__ != core_mod.__name__:
                continue  # imported from elsewhere; compared where it is defined
            twin_cls = getattr(twin_mod, name, None)
            if inspect.isclass(twin_cls) and issubclass(twin_cls, pydantic.BaseModel):
                pairs.append((f"{path.stem}.{name}", core_cls, twin_cls))
    return pairs
