"""Zero-dependency schema validation and serialization engine for Ephemres.

Implements the Pydantic v2 interface using 100% Python Standard Library
(typing, datetime, uuid, re, inspect, copy, enum, json).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import inspect
import json
import re
import sys
import types
import typing
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)
from uuid import UUID

T = TypeVar("T")

try:
    from typing import Self
except ImportError:
    Self = Any  # type: ignore


# ---------------------------------------------------------------------------
# 1. Validation Error
# ---------------------------------------------------------------------------
class ValidationError(ValueError):
    """Raised when data validation against a schema fails."""

    def __init__(
        self,
        errors: Union[List[Dict[str, Any]], str],
        title: Optional[str] = None,
    ) -> None:
        if isinstance(errors, str):
            self.errors_list: List[Dict[str, Any]] = [
                {"loc": ("__root__",), "msg": errors, "type": "value_error"}
            ]
        else:
            self.errors_list = errors
        self.title = title or "Validation error"

        count = len(self.errors_list)
        lines = [f"{count} validation error{'s' if count != 1 else ''} for {self.title}"]
        for err in self.errors_list:
            loc_tuple = err.get("loc", ())
            loc_str = " -> ".join(str(x) for x in loc_tuple) if loc_tuple else "__root__"
            msg = err.get("msg", "")
            err_type = err.get("type", "value_error")
            lines.append(f"  {loc_str}\n    {msg} [type={err_type}]")
        super().__init__("\n".join(lines))

    def errors(self) -> List[Dict[str, Any]]:
        return self.errors_list


# ---------------------------------------------------------------------------
# 2. ConfigDict & Alias Generator
# ---------------------------------------------------------------------------
def ConfigDict(**kwargs: Any) -> Dict[str, Any]:
    """Configuration dictionary container for BaseModel schemas."""
    return dict(kwargs)


def to_camel(snake_str: str) -> str:
    """Convert snake_case identifier to camelCase.

    Examples:
        'some_field_name' -> 'someFieldName'
        'created_at' -> 'createdAt'
        'id' -> 'id'
    """
    if not snake_str:
        return snake_str
    leading_underscores = len(snake_str) - len(snake_str.lstrip("_"))
    trailing_underscores = len(snake_str) - len(snake_str.rstrip("_"))
    clean = snake_str.strip("_")
    if not clean:
        return snake_str
    parts = clean.split("_")
    camel = parts[0] + "".join(x.capitalize() for x in parts[1:])
    return "_" * leading_underscores + camel + "_" * trailing_underscores


# ---------------------------------------------------------------------------
# 3. Field & _FieldInfo
# ---------------------------------------------------------------------------
@dataclass
class _FieldInfo:
    default: Any = ...
    default_factory: Optional[Callable[[], Any]] = None
    alias: Optional[str] = None
    ge: Optional[Union[int, float]] = None
    gt: Optional[Union[int, float]] = None
    le: Optional[Union[int, float]] = None
    lt: Optional[Union[int, float]] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[Union[str, Pattern[str]]] = None
    description: Optional[str] = None
    title: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_required(self) -> bool:
        return self.default is ... and self.default_factory is None

    def get_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is not ...:
            return copy.deepcopy(self.default)
        return ...


FieldInfo = _FieldInfo
alias_generators = types.SimpleNamespace(to_camel=to_camel)
sys.modules["app.core.schema.alias_generators"] = alias_generators


def Field(
    default: Any = ...,
    *,
    default_factory: Optional[Callable[[], Any]] = None,
    alias: Optional[str] = None,
    ge: Optional[Union[int, float]] = None,
    gt: Optional[Union[int, float]] = None,
    le: Optional[Union[int, float]] = None,
    lt: Optional[Union[int, float]] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[Union[str, Pattern[str]]] = None,
    description: Optional[str] = None,
    title: Optional[str] = None,
    **kwargs: Any,
) -> _FieldInfo:
    """Define schema metadata, default values, and validation bounds on a field."""
    return _FieldInfo(
        default=default,
        default_factory=default_factory,
        alias=alias,
        ge=ge,
        gt=gt,
        le=le,
        lt=lt,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        description=description,
        title=title,
        extra=kwargs,
    )


# ---------------------------------------------------------------------------
# 4. Decorators
# ---------------------------------------------------------------------------
def field_validator(
    *fields: str,
    mode: Literal["before", "after"] = "after",
    check_fields: bool = True,
) -> Callable[[Any], Any]:
    """Decorator for validating specific fields."""

    def decorator(fn: Any) -> Any:
        if isinstance(fn, classmethod):
            func = fn.__func__
            func.__field_validator__ = (fields, mode)
            fn.__field_validator__ = (fields, mode)
        else:
            fn.__field_validator__ = (fields, mode)
        return fn

    return decorator


def field_serializer(
    *fields: str,
    mode: str = "plain",
    when_used: str = "always",
) -> Callable[[Any], Any]:
    """Decorator for custom field serialization during model_dump()."""

    def decorator(fn: Any) -> Any:
        if isinstance(fn, classmethod):
            func = fn.__func__
            func.__field_serializer__ = fields
            fn.__field_serializer__ = fields
        else:
            fn.__field_serializer__ = fields
        return fn

    return decorator


def model_validator(
    *,
    mode: Literal["before", "after"] = "after",
) -> Callable[[Any], Any]:
    """Decorator for whole-model validation after all fields are populated."""

    def decorator(fn: Any) -> Any:
        if isinstance(fn, classmethod):
            func = fn.__func__
            func.__model_validator__ = mode
            fn.__model_validator__ = mode
        else:
            fn.__model_validator__ = mode
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Type Coercion & Constraint Checking Helpers
# ---------------------------------------------------------------------------
def _coerce_value(
    val: Any,
    target_type: Any,
    field_name: str,
    model_config: Dict[str, Any],
) -> Any:
    """Recursively validate and coerce input value to the target type."""
    if target_type is Any or target_type is inspect._empty:
        return val

    # None handling
    if val is None:
        origin = get_origin(target_type)
        if origin in (Union, getattr(types, "UnionType", Union)):
            args = get_args(target_type)
            if type(None) in args:
                return None
        raise ValueError("Input should be not None")

    origin = get_origin(target_type)
    args = get_args(target_type)

    # 1. TypeVar (from generic parameterization)
    if isinstance(target_type, TypeVar):
        return val

    # 2. Union / Optional
    if origin in (Union, getattr(types, "UnionType", Union)):
        if val is None and type(None) in args:
            return None
        non_none_args = [a for a in args if a is not type(None)]
        errors = []
        for arg in non_none_args:
            try:
                return _coerce_value(val, arg, field_name, model_config)
            except (ValueError, TypeError) as exc:
                errors.append(exc)
        raise ValueError(f"Input '{val}' failed Union validation against {args}")

    # 3. Literal[...]
    if origin is Literal:
        if val not in args:
            raise ValueError(f"Input should be one of {args}, got '{val}'")
        return val

    # 4. Primitive int
    if target_type is int:
        if isinstance(val, bool):
            raise ValueError("Input should be a valid integer, not boolean")
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except Exception:
            raise ValueError(f"Input should be a valid integer, got '{val}'")

    # 5. Primitive float
    if target_type is float:
        if isinstance(val, bool):
            raise ValueError("Input should be a valid float, not boolean")
        if isinstance(val, (int, float)):
            return float(val)
        try:
            return float(val)
        except Exception:
            raise ValueError(f"Input should be a valid float, got '{val}'")

    # 6. Primitive bool
    if target_type is bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            low = val.lower().strip()
            if low in ("true", "1", "yes", "on", "t"):
                return True
            if low in ("false", "0", "no", "off", "f"):
                return False
            raise ValueError(f"Input should be a valid boolean, got '{val}'")
        if isinstance(val, (int, float)):
            return bool(val)
        return bool(val)

    # 7. Primitive str
    if target_type is str:
        if isinstance(val, str):
            return val
        if isinstance(val, UUID):
            return str(val)
        import ipaddress
        if isinstance(val, (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return str(val)
        raise ValueError(f"Input should be a valid string, got {type(val).__name__}")

    # 8. Primitive bytes
    if target_type is bytes:
        if isinstance(val, bytes):
            return val
        if isinstance(val, str):
            return val.encode("utf-8")
        raise ValueError(f"Input should be valid bytes, got '{val}'")

    # 8b. Decimal
    if target_type is Decimal:
        if isinstance(val, Decimal):
            return val
        try:
            return Decimal(str(val))
        except Exception as exc:
            raise ValueError(f"Input should be a valid Decimal: {exc}")

    # 9. UUID
    if target_type is UUID:
        if isinstance(val, UUID):
            return val
        if isinstance(val, str):
            try:
                return UUID(val)
            except Exception as exc:
                raise ValueError(f"Input should be a valid UUID: {exc}")
        raise ValueError(f"Input should be a valid UUID, got '{val}'")

    # 9b. date
    if target_type is date:
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        if isinstance(val, str):
            try:
                return date.fromisoformat(val)
            except Exception as exc:
                raise ValueError(f"Input should be a valid date: {exc}")
        raise ValueError(f"Input should be a valid date, got '{val}'")

    # 10. datetime
    if target_type is datetime:
        if isinstance(val, datetime):
            return val
        if isinstance(val, date):
            return datetime(val.year, val.month, val.day)
        if isinstance(val, str):
            try:
                clean_str = val.replace("Z", "+00:00")
                return datetime.fromisoformat(clean_str)
            except Exception as exc:
                raise ValueError(f"Input should be a valid datetime: {exc}")
        raise ValueError(f"Input should be a valid datetime, got '{val}'")

    # 11. List / Sequence / Iterable
    if origin in (list, List, Sequence, getattr(typing, "Iterable", Sequence)):
        if not isinstance(val, (list, tuple, set)):
            raise ValueError(f"Input should be a valid list, got '{type(val).__name__}'")
        item_type = args[0] if args else Any
        return [_coerce_value(item, item_type, field_name, model_config) for item in val]

    # 12. Dict / Mapping
    if origin in (dict, Dict, Mapping):
        if not isinstance(val, dict):
            raise ValueError(f"Input should be a valid dict, got '{type(val).__name__}'")
        k_type = args[0] if args else Any
        v_type = args[1] if len(args) > 1 else Any
        return {
            _coerce_value(k, k_type, field_name, model_config): _coerce_value(
                v, v_type, field_name, model_config
            )
            for k, v in val.items()
        }

    # 13. Enum
    if isinstance(target_type, type) and issubclass(target_type, Enum):
        if isinstance(val, target_type):
            return val
        try:
            return target_type(val)
        except Exception:
            try:
                return target_type[val]
            except Exception:
                raise ValueError(f"Input '{val}' is not a valid {target_type.__name__}")

    # 14. Nested BaseModel
    if isinstance(target_type, type) and issubclass(target_type, BaseModel):
        if isinstance(val, target_type):
            return val
        if isinstance(val, dict):
            return target_type.model_validate(val)
        if model_config.get("from_attributes") or target_type.model_config.get("from_attributes"):
            return target_type.model_validate(val, from_attributes=True)
        raise ValueError(f"Input should be a valid {target_type.__name__} or dict, got '{type(val).__name__}'")

    # Fallback
    return val


def _validate_constraints(val: Any, f_info: _FieldInfo, field_name: str) -> None:
    """Enforce numeric bounds, string/collection lengths, and regex patterns."""
    if val is None:
        return

    # Numeric bounds
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if f_info.ge is not None and val < f_info.ge:
            raise ValueError(f"Input should be greater than or equal to {f_info.ge}")
        if f_info.gt is not None and val <= f_info.gt:
            raise ValueError(f"Input should be greater than {f_info.gt}")
        if f_info.le is not None and val > f_info.le:
            raise ValueError(f"Input should be less than or equal to {f_info.le}")
        if f_info.lt is not None and val >= f_info.lt:
            raise ValueError(f"Input should be less than {f_info.lt}")

    # Length bounds for str, list, dict, set, tuple
    if isinstance(val, (str, list, dict, set, tuple)):
        v_len = len(val)
        if f_info.min_length is not None and v_len < f_info.min_length:
            raise ValueError(f"Length should be greater than or equal to {f_info.min_length}")
        if f_info.max_length is not None and v_len > f_info.max_length:
            raise ValueError(f"Length should be less than or equal to {f_info.max_length}")

    # Pattern for str
    if isinstance(val, str) and f_info.pattern is not None:
        pat = f_info.pattern
        if isinstance(pat, str):
            if not re.search(pat, val):
                raise ValueError(f"String should match pattern '{pat}'")
        elif hasattr(pat, "search"):
            if not pat.search(val):
                raise ValueError(f"String should match pattern '{pat.pattern}'")


def _resolve_type_vars(hint: Any, type_map: Dict[TypeVar, Any]) -> Any:
    """Replace TypeVars with concrete types inside generic type hints."""
    if not type_map:
        return hint
    if isinstance(hint, TypeVar):
        return type_map.get(hint, hint)
    origin = get_origin(hint)
    if origin is not None:
        args = tuple(_resolve_type_vars(a, type_map) for a in get_args(hint))
        if origin in (Union, getattr(types, "UnionType", Union)):
            return Union[args]
        if hasattr(origin, "__class_getitem__"):
            return origin[args]
    return hint


# ---------------------------------------------------------------------------
# 5. BaseModel Metaclass & Base Class
# ---------------------------------------------------------------------------
class ModelMetaclass(type):
    """Metaclass that collects annotations, defaults, validators, and config across MRO."""

    _generic_cache: Dict[Tuple[Any, ...], Type[BaseModel]] = {}

    def __new__(
        mcls,
        name: str,
        bases: Tuple[type, ...],
        namespace: Dict[str, Any],
        **kw: Any,
    ) -> type:
        cls = super().__new__(mcls, name, bases, namespace, **kw)

        if name in ("BaseModel", "SchemaModel") and not any(isinstance(b, ModelMetaclass) for b in bases):
            cls.model_fields = {}
            cls.model_config = {}
            cls._field_types = {}
            cls._field_validators_before = {}
            cls._field_validators_after = {}
            cls._field_serializers = {}
            cls._model_validators_after = []
            return cls

        # Collect model_config across MRO
        merged_config: Dict[str, Any] = {}
        for base in reversed(bases):
            if hasattr(base, "model_config") and isinstance(base.model_config, dict):
                merged_config.update(base.model_config)
        if "model_config" in namespace and isinstance(namespace["model_config"], dict):
            merged_config.update(namespace["model_config"])
        cls.model_config = merged_config

        # Collect inherited fields & types
        fields: Dict[str, _FieldInfo] = {}
        field_types: Dict[str, Any] = {}
        for base in bases:
            if hasattr(base, "model_fields"):
                for k, v in base.model_fields.items():
                    fields[k] = copy.copy(v)
            if hasattr(base, "_field_types"):
                field_types.update(base._field_types)

        # Collect current annotations
        raw_annotations = namespace.get("__annotations__", {})

        # Resolve type hints with fallback
        resolved_hints: Dict[str, Any] = {}
        try:
            resolved_hints = typing.get_type_hints(cls)
        except Exception:
            mod_globals = sys.modules[cls.__module__].__dict__ if cls.__module__ in sys.modules else {}
            for k, v in raw_annotations.items():
                if isinstance(v, str):
                    try:
                        resolved_hints[k] = eval(v, mod_globals, namespace)
                    except Exception:
                        resolved_hints[k] = v
                else:
                    resolved_hints[k] = v

        for f_name, raw_type in raw_annotations.items():
            if f_name.startswith("_"):
                continue
            f_type = resolved_hints.get(f_name, raw_type)
            field_types[f_name] = f_type

            if f_name in namespace:
                attr_val = namespace[f_name]
                if isinstance(attr_val, _FieldInfo):
                    fields[f_name] = attr_val
                elif isinstance(attr_val, (classmethod, staticmethod, property)) or inspect.isroutine(attr_val):
                    # Method shadowed a field with the same name (e.g. error: Optional[ErrorDetail] = None and @classmethod def error)
                    origin = get_origin(f_type)
                    args = get_args(f_type)
                    if origin in (Union, getattr(types, "UnionType", Union)) and type(None) in args:
                        fields[f_name] = Field(default=None)
                    else:
                        fields[f_name] = Field(default=...)
                else:
                    fields[f_name] = Field(default=attr_val)
            elif f_name in fields:
                pass
            else:
                fields[f_name] = Field(default=...)

        cls.model_fields = fields
        cls._field_types = field_types

        # Collect validators across MRO and namespace
        val_before: Dict[str, List[Callable]] = {}
        val_after: Dict[str, List[Callable]] = {}
        serializers: Dict[str, List[Callable]] = {}
        model_vals_before: List[Callable] = []
        model_vals_after: List[Callable] = []

        for base in bases:
            if hasattr(base, "_field_validators_before"):
                for k, v in base._field_validators_before.items():
                    val_before.setdefault(k, []).extend(v)
            if hasattr(base, "_field_validators_after"):
                for k, v in base._field_validators_after.items():
                    val_after.setdefault(k, []).extend(v)
            if hasattr(base, "_field_serializers"):
                for k, v in base._field_serializers.items():
                    serializers.setdefault(k, []).extend(v)
            if hasattr(base, "_model_validators_before"):
                model_vals_before.extend(base._model_validators_before)
            if hasattr(base, "_model_validators_after"):
                model_vals_after.extend(base._model_validators_after)

        for attr_name, attr_val in namespace.items():
            func = attr_val.__func__ if isinstance(attr_val, classmethod) else attr_val

            f_val_meta = getattr(func, "__field_validator__", None)
            if f_val_meta:
                f_names, mode = f_val_meta
                for fn in f_names:
                    if mode == "before":
                        val_before.setdefault(fn, []).append(attr_val)
                    else:
                        val_after.setdefault(fn, []).append(attr_val)

            ser_meta = getattr(func, "__field_serializer__", None)
            if ser_meta:
                for fn in ser_meta:
                    serializers.setdefault(fn, []).append(attr_val)

            m_val_meta = getattr(func, "__model_validator__", None)
            if m_val_meta:
                if m_val_meta == "before":
                    model_vals_before.append(attr_val)
                else:
                    model_vals_after.append(attr_val)

        cls._field_validators_before = val_before
        cls._field_validators_after = val_after
        cls._field_serializers = serializers
        cls._model_validators_before = model_vals_before
        cls._model_validators_after = model_vals_after

        return cls

    def __getitem__(cls, item: Any) -> Type[BaseModel]:
        """Support Generic[T] parameterization like APIResponse[List[EndpointSummary]]."""
        args = item if isinstance(item, tuple) else (item,)
        cache_key = (cls, args)
        if cache_key in cls._generic_cache:
            return cls._generic_cache[cache_key]

        arg_names = ", ".join(getattr(a, "__name__", str(a)) for a in args)
        sub_name = f"{cls.__name__}[{arg_names}]"
        subclass = type(
            sub_name,
            (cls,),
            {
                "_generic_type_args": args,
                "model_config": dict(cls.model_config),
            },
        )
        cls._generic_cache[cache_key] = subclass
        return subclass


class SchemaModel(metaclass=ModelMetaclass):
    """High-performance zero-dependency schema validation and serialization base class."""

    model_config: Dict[str, Any] = {}
    model_fields: Dict[str, _FieldInfo] = {}
    _field_types: Dict[str, Any] = {}
    _field_validators_before: Dict[str, List[Callable]] = {}
    _field_validators_after: Dict[str, List[Callable]] = {}
    _field_serializers: Dict[str, List[Callable]] = {}
    _model_validators_before: List[Callable] = []
    _model_validators_after: List[Callable] = []

    def __init__(self, **data: Any) -> None:
        self.model_fields_set: Set[str] = set()
        errors: List[Dict[str, Any]] = []

        cfg = self.model_config
        alias_gen = cfg.get("alias_generator")

        input_data = dict(data)

        # Run model validators (mode="before")
        for m_val in self._model_validators_before:
            try:
                func = m_val.__func__ if isinstance(m_val, classmethod) else m_val
                sig = inspect.signature(func)
                params = list(sig.parameters.values())
                if len(params) >= 2 or (len(params) == 1 and isinstance(m_val, classmethod)):
                    res = func(self.__class__, input_data)
                else:
                    res = func(input_data)
                if isinstance(res, dict):
                    input_data = res
            except Exception as exc:
                raise ValidationError(
                    [{"loc": ("__root__",), "msg": str(exc), "type": "value_error"}],
                    title=self.__class__.__name__,
                )

        generic_map = self._get_generic_map()

        for field_name, f_info in self.model_fields.items():
            alias = f_info.alias or (alias_gen(field_name) if alias_gen else None)

            # Match provided value by field_name or alias
            key_found = None
            raw_val = None
            if field_name in input_data:
                key_found = field_name
                raw_val = input_data[field_name]
            elif alias and alias in input_data and (f_info.alias is not None or alias not in self.model_fields):
                key_found = alias
                raw_val = input_data[alias]

            if key_found is not None:
                self.model_fields_set.add(field_name)
            else:
                if f_info.default_factory is not None:
                    raw_val = f_info.default_factory()
                elif f_info.default is not ...:
                    if isinstance(f_info.default, (list, dict, set)):
                        raw_val = copy.deepcopy(f_info.default)
                    else:
                        raw_val = f_info.default
                else:
                    errors.append({
                        "loc": (field_name,),
                        "msg": "Field required",
                        "type": "missing",
                    })
                    continue

            # Run 'before' validators
            val = raw_val
            for v_fn in self._get_validators(field_name, mode="before"):
                try:
                    val = self._invoke_validator(v_fn, val)
                except Exception as exc:
                    errors.append({
                        "loc": (field_name,),
                        "msg": str(exc),
                        "type": "value_error",
                    })
                    val = None
                    break

            if errors and errors[-1]["loc"] == (field_name,):
                continue

            # Type resolution & coercion
            raw_type = self._field_types.get(field_name, Any)
            resolved_type = _resolve_type_vars(raw_type, generic_map)

            try:
                coerced = _coerce_value(val, resolved_type, field_name, cfg)
            except Exception as exc:
                errors.append({
                    "loc": (field_name,),
                    "msg": str(exc),
                    "type": "value_error",
                })
                continue

            # Bounds & pattern constraints
            try:
                _validate_constraints(coerced, f_info, field_name)
            except Exception as exc:
                errors.append({
                    "loc": (field_name,),
                    "msg": str(exc),
                    "type": "value_error",
                })
                continue

            # Run 'after' validators
            for v_fn in self._get_validators(field_name, mode="after"):
                try:
                    coerced = self._invoke_validator(v_fn, coerced)
                except Exception as exc:
                    errors.append({
                        "loc": (field_name,),
                        "msg": str(exc),
                        "type": "value_error",
                    })
                    coerced = None
                    break

            if errors and errors[-1]["loc"] == (field_name,):
                continue

            setattr(self, field_name, coerced)

        if errors:
            raise ValidationError(errors, title=self.__class__.__name__)

        # Run model validators (mode="after")
        for m_val in self._model_validators_after:
            try:
                func = m_val.__func__ if isinstance(m_val, classmethod) else m_val
                sig = inspect.signature(func)
                params = list(sig.parameters.values())
                if len(params) >= 2 or (len(params) == 1 and isinstance(m_val, classmethod)):
                    res = func(self.__class__, self)
                else:
                    res = func(self)
                if res is not None and isinstance(res, BaseModel):
                    self.__dict__.update(res.__dict__)
                    self.model_fields_set.update(res.model_fields_set)
            except Exception as exc:
                raise ValidationError(
                    [{"loc": ("__root__",), "msg": str(exc), "type": "value_error"}],
                    title=self.__class__.__name__,
                )

    def _get_generic_map(self) -> Dict[TypeVar, Any]:
        """Construct TypeVar to concrete type mapping if parameterized."""
        args = getattr(self.__class__, "_generic_type_args", None)
        if not args:
            orig = getattr(self, "__orig_class__", None)
            if orig is not None:
                args = get_args(orig)

        if not args:
            return {}

        params = getattr(self.__class__, "__parameters__", ())
        if not params:
            for base in self.__class__.__mro__:
                if hasattr(base, "__parameters__") and base.__parameters__:
                    params = base.__parameters__
                    break

        if params and args:
            return dict(zip(params, args))
        return {}

    def _get_validators(self, field_name: str, mode: str) -> List[Callable]:
        container = (
            self._field_validators_before if mode == "before" else self._field_validators_after
        )
        return container.get(field_name, [])

    def _invoke_validator(self, validator_fn: Callable, value: Any) -> Any:
        func = validator_fn.__func__ if isinstance(validator_fn, classmethod) else validator_fn
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if len(params) >= 2 or (len(params) == 1 and isinstance(validator_fn, classmethod)):
            return func(self.__class__, value)
        return func(value)

    # -----------------------------------------------------------------------
    # Validation & Deserialization
    # -----------------------------------------------------------------------
    @classmethod
    def model_validate(
        cls: Type[T],
        obj: Any,
        *,
        from_attributes: Optional[bool] = None,
    ) -> T:
        """Validate input object (dict, ORM instance, or model) against schema."""
        if isinstance(obj, cls):
            return obj

        cfg = getattr(cls, "model_config", {})
        from_attrs = (
            from_attributes if from_attributes is not None else cfg.get("from_attributes", False)
        )

        if isinstance(obj, dict):
            return cls(**obj)

        if from_attrs or hasattr(obj, "__dict__"):
            extracted = {}
            for f_name, f_info in cls.model_fields.items():
                if hasattr(obj, f_name):
                    extracted[f_name] = getattr(obj, f_name)
                elif f_info.alias and hasattr(obj, f_info.alias):
                    extracted[f_info.alias] = getattr(obj, f_info.alias)
            return cls(**extracted)

        if isinstance(obj, (bytes, str)):
            try:
                parsed = json.loads(obj)
                if isinstance(parsed, dict):
                    return cls(**parsed)
            except Exception:
                pass

        raise ValidationError(
            [
                {
                    "loc": ("__root__",),
                    "msg": f"Expected dict or object with attributes, got {type(obj).__name__}",
                    "type": "value_error",
                }
            ],
            title=cls.__name__,
        )

    @classmethod
    def from_orm(cls: Type[T], obj: Any) -> T:
        """Pydantic v1 backward compatibility alias for model_validate(obj, from_attributes=True)."""
        return cls.model_validate(obj, from_attributes=True)

    @classmethod
    def model_validate_json(
        cls: Type[T],
        json_data: Union[str, bytes],
        *,
        from_attributes: Optional[bool] = None,
    ) -> T:
        """Parse JSON string and validate against schema."""
        parsed = json.loads(json_data)
        if not isinstance(parsed, dict):
            raise ValidationError(
                [{"loc": ("__root__",), "msg": "Expected JSON dictionary", "type": "value_error"}],
                title=cls.__name__,
            )
        return cls.model_validate(parsed, from_attributes=from_attributes)

    # -----------------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------------
    def model_dump(
        self,
        *,
        mode: str = "python",
        by_alias: bool = False,
        exclude_none: bool = False,
        exclude_unset: bool = False,
    ) -> Dict[str, Any]:
        """Serialize model fields to dictionary."""
        result: Dict[str, Any] = {}
        alias_gen = self.model_config.get("alias_generator")

        for f_name, f_info in self.model_fields.items():
            if exclude_unset and f_name not in self.model_fields_set:
                continue

            if not hasattr(self, f_name):
                continue

            val = getattr(self, f_name)

            if exclude_none and val is None:
                continue

            # Field serializers
            for ser in self._field_serializers.get(f_name, []):
                func = ser.__func__ if isinstance(ser, classmethod) else ser
                sig = inspect.signature(func)
                params = list(sig.parameters.values())
                if len(params) >= 2 or (len(params) == 1 and params[0].name in ("self", "cls")):
                    val = func(self, val)
                else:
                    val = func(val)

            # Determine key
            if by_alias:
                if f_info.alias:
                    key = f_info.alias
                elif alias_gen:
                    key = alias_gen(f_name)
                else:
                    key = f_name
            else:
                key = f_name

            result[key] = self._serialize_val(val, mode, by_alias, exclude_none, exclude_unset)

        return result

    def dict(self, **kwargs: Any) -> Dict[str, Any]:
        """Pydantic v1 compatibility alias for model_dump()."""
        return self.model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        """Serialize model to JSON string."""
        data = self.model_dump(mode="json", **kwargs)
        return json.dumps(data, default=str)

    def _serialize_val(
        self,
        val: Any,
        mode: str,
        by_alias: bool,
        exclude_none: bool,
        exclude_unset: bool,
    ) -> Any:
        if val is None:
            return None
        if isinstance(val, BaseModel):
            return val.model_dump(
                mode=mode,
                by_alias=by_alias,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
            )
        if isinstance(val, (list, tuple, set)):
            return [
                self._serialize_val(item, mode, by_alias, exclude_none, exclude_unset)
                for item in val
            ]
        if isinstance(val, dict):
            return {
                (k if not by_alias else str(k)): self._serialize_val(
                    v, mode, by_alias, exclude_none, exclude_unset
                )
                for k, v in val.items()
            }
        if mode == "json":
            if isinstance(val, UUID):
                return str(val)
            if isinstance(val, (datetime, date)):
                return val.isoformat()
            if isinstance(val, Enum):
                return val.value
            if isinstance(val, Decimal):
                return float(val)
        return val

    def model_copy(
        self,
        *,
        update: Optional[Dict[str, Any]] = None,
        deep: bool = False,
    ) -> Self:
        """Create a copy of the model with optional field updates."""
        data = dict(self.__dict__)
        if update:
            data.update(update)
        if deep:
            data = copy.deepcopy(data)
        new_inst = self.__class__.__new__(self.__class__)
        new_inst.__dict__.update(data)
        new_inst.model_fields_set = set(self.model_fields_set)
        if update:
            new_inst.model_fields_set.update(update.keys())
        return new_inst

    def copy(self, *args: Any, **kwargs: Any) -> Self:
        return self.model_copy(*args, **kwargs)

    # -----------------------------------------------------------------------
    # Dunder Helpers
    # -----------------------------------------------------------------------
    def __repr__(self) -> str:
        fields_repr = ", ".join(f"{k}={getattr(self, k, None)!r}" for k in self.model_fields)
        return f"{self.__class__.__name__}({fields_repr})"

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, self.__class__):
            return False
        return all(getattr(self, k, None) == getattr(other, k, None) for k in self.model_fields)

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __iter__(self) -> Any:
        yield from self.model_dump().items()

    def __contains__(self, item: str) -> bool:
        return item in self.model_fields

    def __len__(self) -> int:
        return len(self.model_fields)


BaseModel = SchemaModel


class TypeAdapter(Generic[T]):
    """Zero-dependency Pydantic v2 TypeAdapter implementation."""

    def __init__(self, type_: Any) -> None:
        self.type_ = type_

    def validate_python(self, data: Any) -> Any:
        if isinstance(self.type_, type) and issubclass(self.type_, BaseModel):
            if isinstance(data, self.type_):
                return data
            if isinstance(data, dict):
                return self.type_.model_validate(data)
            return self.type_.model_validate(data)
        origin = get_origin(self.type_)
        if origin in (list, List, Sequence):
            args = get_args(self.type_)
            item_type = args[0] if args else Any
            if isinstance(data, (list, tuple)):
                adapter = TypeAdapter(item_type)
                return [adapter.validate_python(x) for x in data]
            return data
        return data

    def dump_python(self, data: Any, mode: str = "python", by_alias: bool = False) -> Any:
        if isinstance(data, BaseModel):
            return data.model_dump(mode=mode, by_alias=by_alias)
        if isinstance(data, (list, tuple)):
            return [TypeAdapter(Any).dump_python(x, mode=mode, by_alias=by_alias) for x in data]
        return data


__all__ = [
    "SchemaModel",
    "BaseModel",
    "ConfigDict",
    "Field",
    "FieldInfo",
    "ValidationError",
    "field_serializer",
    "field_validator",
    "model_validator",
    "to_camel",
    "alias_generators",
    "TypeAdapter",
    "_FieldInfo",
    "_coerce_value",
    "_validate_constraints",
]
