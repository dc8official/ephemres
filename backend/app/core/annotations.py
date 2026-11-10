"""Reading a class's annotations from inside a metaclass, on every Python Ephemres supports.

The schema models (core/schema.py) and the ORM models (core/orm.py) are both built by a
metaclass that turns the annotations in the class body into fields or columns.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping


def own_annotations(cls: type, namespace: Mapping[str, Any]) -> Dict[str, Any]:
    """The annotations written in the body of `cls` (not inherited ones), in order.

    `namespace` is the class body the metaclass was given. Up to Python 3.13, and on any
    version in a module that starts with `from __future__ import annotations`, the annotations
    are in it under "__annotations__". Python 3.14 otherwise works them out only when asked
    (PEP 649) and leaves a function in the namespace instead, so reading the key found nothing:
    a model in a module without the __future__ import had no fields. On 3.14 the settings and
    branding models (routers/settings.py) lost theirs and saving settings answered 500, while
    install.sh picks python3.14 first (found running the suite on 3.14, 2026-11-10).

    On 3.14 a name that is not defined yet comes back as a ForwardRef instead of raising, the
    nearest thing to the string an older Python required there."""
    if "__annotations__" in namespace:
        return namespace["__annotations__"]
    try:
        import annotationlib  # Python 3.14 and later
    except ImportError:
        # Before 3.14 a class body with annotations always has the key, so there are none.
        return {}
    return annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF)
