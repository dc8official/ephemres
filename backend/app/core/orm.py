"""Zero-dependency SQL Compiler, PostgreSQL DDL Generator & asyncpg ORM Engine.

Ephemres Vertical Integration Architecture (v3.3.2v) - Stage 5C.
Eliminates sqlalchemy[asyncio] external dependency from backend/requirements.txt,
providing a 100% Python Standard Library + asyncpg drop-in engine covering:
1. Declarative Models (DeclarativeBase, Mapped, mapped_column, Column, relationship, joinedload)
2. SQL AST & Expression Compilation (Select, Insert, Update, Delete, func, case, text, CTEs, Window)
3. PostgreSQL DDL Compiler (CreateTable, CreateIndex, get_column_specification for migrator.py)
4. PostgreSQL Dialect Upserts (insert().on_conflict_do_update, .on_conflict_do_nothing, .excluded)
5. AsyncSession Runtime (Identity map, dirty attribute tracking, flush() RETURNING hydration)
6. Named :param to $1 Text Compiler with type cast / quote preservation and executemany batching
7. Hybrid Result / Row unpacking (scalars, mappings, attribute access, tuple unpacking)
8. asyncpg Type Marshaling (JSONB, UUID, INET, ARRAY(INET))
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import inspect
import ipaddress
import json
import logging
import re
import sys
import types
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from uuid import UUID as PyUUID, uuid4

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

logger = logging.getLogger("app.core.orm")

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound="DeclarativeBase")


# ---------------------------------------------------------------------------
# 1. Exceptions
# ---------------------------------------------------------------------------
class SQLAlchemyError(Exception):
    """Base exception for all ORM / SQL errors."""


class DBAPIError(SQLAlchemyError):
    """Database API driver error."""


class OperationalError(DBAPIError):
    """Operational connection or driver failure."""


class IntegrityError(DBAPIError):
    """Constraint violation or integrity error."""


class NoResultFound(SQLAlchemyError):
    """Expected a row, found none."""


class MultipleResultsFound(SQLAlchemyError):
    """Expected one row, found multiple."""


# ---------------------------------------------------------------------------
# 2. SQL Types
# ---------------------------------------------------------------------------
class SQLType:
    """Base class for SQL types."""

    def sql_type(self) -> str:
        return "TEXT"

    def __call__(self, *args: Any, **kwargs: Any) -> SQLType:
        return self

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


def normalize_type(t: Any) -> SQLType:
    if isinstance(t, SQLType):
        return t
    if isinstance(t, type) and issubclass(t, SQLType):
        return t()
    return NullType()


class String(SQLType):
    def __init__(self, length: Optional[int] = None) -> None:
        self.length = length

    def sql_type(self) -> str:
        return f"VARCHAR({self.length})" if self.length else "VARCHAR"


class VARCHAR(String):
    pass


class Text(SQLType):
    def sql_type(self) -> str:
        return "TEXT"


class TEXT(Text):
    pass


class Integer(SQLType):
    def sql_type(self) -> str:
        return "INTEGER"


class BigInteger(SQLType):
    def sql_type(self) -> str:
        return "BIGINT"


class SmallInteger(SQLType):
    def sql_type(self) -> str:
        return "SMALLINT"


class Boolean(SQLType):
    def sql_type(self) -> str:
        return "BOOLEAN"


class Float(SQLType):
    def sql_type(self) -> str:
        return "DOUBLE PRECISION"


class Numeric(SQLType):
    def __init__(self, precision: Optional[int] = None, scale: Optional[int] = None) -> None:
        self.precision = precision
        self.scale = scale

    def sql_type(self) -> str:
        if self.precision is not None and self.scale is not None:
            return f"NUMERIC({self.precision}, {self.scale})"
        if self.precision is not None:
            return f"NUMERIC({self.precision})"
        return "NUMERIC"


class DateTime(SQLType):
    def __init__(self, timezone: bool = False) -> None:
        self.timezone = timezone

    def sql_type(self) -> str:
        return "TIMESTAMP WITH TIME ZONE" if self.timezone else "TIMESTAMP WITHOUT TIME ZONE"


class UUIDType(SQLType):
    def __init__(self, as_uuid: bool = True) -> None:
        self.as_uuid = as_uuid

    def sql_type(self) -> str:
        return "UUID"


# Alias UUID
UUID_SQL = UUIDType


class INET(SQLType):
    def sql_type(self) -> str:
        return "INET"


class JSONB(SQLType):
    def __init__(self, astext_type: Any = None) -> None:
        self.astext_type = astext_type

    def sql_type(self) -> str:
        return "JSONB"


class ARRAY(SQLType):
    def __init__(self, item_type: Any) -> None:
        self.item_type = normalize_type(item_type)

    def sql_type(self) -> str:
        return f"{self.item_type.sql_type()}[]"


class NullType(SQLType):
    def sql_type(self) -> str:
        return "TEXT"


NULLTYPE = NullType()


# ---------------------------------------------------------------------------
# 3. Parameter Marshaling (asyncpg & Python)
# ---------------------------------------------------------------------------
def prepare_bind_param(val: Any, col_type: Optional[SQLType] = None) -> Any:
    """Prepares a Python value to be passed cleanly to asyncpg."""
    if val is None:
        return None
    if isinstance(val, Enum):
        return val.value
    if isinstance(col_type, JSONB) or (col_type is None and isinstance(val, (dict, list))):
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return val
    if isinstance(col_type, ARRAY) and isinstance(col_type.item_type, INET):
        if isinstance(val, (list, tuple, set)):
            return [str(x) for x in val]
        return val
    if isinstance(col_type, INET):
        return str(val)
    if isinstance(val, (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
        return str(val)
    return val


def hydrate_column_value(val: Any, col_type: Optional[SQLType] = None) -> Any:
    """Hydrates an asyncpg returned record value into a Python Model attribute."""
    if val is None:
        return None
    if isinstance(col_type, JSONB):
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val
    if isinstance(col_type, UUIDType) and col_type.as_uuid:
        if isinstance(val, str):
            try:
                return PyUUID(val)
            except Exception:
                return val
        return val
    if isinstance(col_type, INET):
        if isinstance(val, (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
            return str(val)
        return str(val)
    if isinstance(col_type, ARRAY) and isinstance(col_type.item_type, INET):
        if isinstance(val, list):
            return [str(x) for x in val]
        return val
    return val


# ---------------------------------------------------------------------------
# 4. AST Base & Context
# ---------------------------------------------------------------------------
class CompileContext:
    """Carries positional parameter state during AST compilation."""

    def __init__(self, inline_literals: bool = False, symbolic_binds: bool = False) -> None:
        self.params: List[Any] = []
        self.inline_literals = inline_literals
        self.symbolic_binds = symbolic_binds
        self.top_level: bool = True

    def add_param(self, val: Any, col_type: Optional[SQLType] = None) -> str:
        if self.inline_literals:
            return format_sql_literal(val)
        prepared = prepare_bind_param(val, col_type)
        self.params.append(prepared)
        return f"${len(self.params)}"


CompilationContext = CompileContext


def format_sql_literal(val: Any) -> str:
    """Formats a Python literal value directly into safe SQL syntax."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float, Decimal)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(val, (list, dict)):
        escaped = json.dumps(val).replace("'", "''")
        return f"'{escaped}'::jsonb"
    if isinstance(val, (datetime,)):
        return f"'{val.isoformat()}'"
    if isinstance(val, PyUUID):
        return f"'{str(val)}'::uuid"
    return f"'{str(val)}'"


def _unwrap_field_operand(other: Any) -> Tuple[Any, bool]:
    skip_where = False
    from app.core.asgi import _ParamInfo
    if isinstance(other, _ParamInfo):
        default_val = getattr(other, "default", None)
        if default_val is None:
            logger.warning("Unresolved query param object used in SQL comparison; filter skipped")
            skip_where = True
            other = None
        elif default_val is not ...:
            other = default_val
    return other, skip_where


class SQLElement:
    """Base expression AST element supporting Python operator overloading."""

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        raise NotImplementedError

    def compile_select_item(self, ctx: Optional[CompileContext] = None) -> str:
        return self.compile(ctx)

    def __str__(self) -> str:
        ctx = CompileContext(inline_literals=True, symbolic_binds=True)
        return self.compile(ctx).replace('"', '')

    # Comparisons
    def __eq__(self, other: Any) -> SQLElement:  # type: ignore[override]
        other, skip = _unwrap_field_operand(other)
        if other is None:
            expr = IsExpression(self, None, negated=False)
        else:
            expr = BinaryExpression(self, "=", other)
        if skip:
            expr._skip_where = True
        return expr

    def __ne__(self, other: Any) -> SQLElement:  # type: ignore[override]
        other, skip = _unwrap_field_operand(other)
        if other is None:
            expr = IsExpression(self, None, negated=True)
        else:
            expr = BinaryExpression(self, "!=", other)
        if skip:
            expr._skip_where = True
        return expr

    def __lt__(self, other: Any) -> SQLElement:
        other, skip = _unwrap_field_operand(other)
        expr = BinaryExpression(self, "<", other)
        if skip:
            expr._skip_where = True
        return expr

    def __le__(self, other: Any) -> SQLElement:
        other, skip = _unwrap_field_operand(other)
        expr = BinaryExpression(self, "<=", other)
        if skip:
            expr._skip_where = True
        return expr

    def __gt__(self, other: Any) -> SQLElement:
        other, skip = _unwrap_field_operand(other)
        expr = BinaryExpression(self, ">", other)
        if skip:
            expr._skip_where = True
        return expr

    def __ge__(self, other: Any) -> SQLElement:
        other, skip = _unwrap_field_operand(other)
        expr = BinaryExpression(self, ">=", other)
        if skip:
            expr._skip_where = True
        return expr

    # Arithmetic
    def __add__(self, other: Any) -> SQLElement:
        return BinaryExpression(self, "+", other, is_arithmetic=True)

    def __radd__(self, other: Any) -> SQLElement:
        return BinaryExpression(other, "+", self, is_arithmetic=True)

    def __sub__(self, other: Any) -> SQLElement:
        return BinaryExpression(self, "-", other, is_arithmetic=True)

    def __rsub__(self, other: Any) -> SQLElement:
        return BinaryExpression(other, "-", self, is_arithmetic=True)

    def __mul__(self, other: Any) -> SQLElement:
        return BinaryExpression(self, "*", other, is_arithmetic=True)

    def __rmul__(self, other: Any) -> SQLElement:
        return BinaryExpression(other, "*", self, is_arithmetic=True)

    def __truediv__(self, other: Any) -> SQLElement:
        return BinaryExpression(self, "/", other, is_arithmetic=True)

    def __rtruediv__(self, other: Any) -> SQLElement:
        return BinaryExpression(other, "/", self, is_arithmetic=True)

    # Logical
    def __and__(self, other: Any) -> SQLElement:
        return and_(self, other)

    def __or__(self, other: Any) -> SQLElement:
        return or_(self, other)

    def __invert__(self) -> SQLElement:
        return UnaryExpression("NOT ", self)

    # Methods
    def in_(self, other: Any) -> SQLElement:
        return InExpression(self, other, negated=False)

    def notin_(self, other: Any) -> SQLElement:
        return InExpression(self, other, negated=True)

    def not_in(self, other: Any) -> SQLElement:
        return self.notin_(other)

    def is_(self, other: Any) -> SQLElement:
        return IsExpression(self, other, negated=False)

    def is_not(self, other: Any) -> SQLElement:
        return IsExpression(self, other, negated=True)

    def isnot(self, other: Any) -> SQLElement:
        return self.is_not(other)

    def ilike(self, pattern: Any) -> SQLElement:
        return BinaryExpression(self, "ILIKE", pattern)

    def like(self, pattern: Any) -> SQLElement:
        return BinaryExpression(self, "LIKE", pattern)

    def asc(self) -> OrderByClause:
        return OrderByClause(self, "ASC")

    def desc(self) -> OrderByClause:
        return OrderByClause(self, "DESC")

    def label(self, name: str) -> LabelClause:
        return LabelClause(self, name)

    def over(
        self,
        partition_by: Optional[Any] = None,
        order_by: Optional[Any] = None,
    ) -> WindowExpression:
        return WindowExpression(self, partition_by=partition_by, order_by=order_by)


def compile_operand(val: Any, ctx: CompileContext, col_type: Optional[SQLType] = None) -> str:
    """Compiles an expression operand (either SQLElement or Python literal)."""
    if isinstance(val, SQLElement):
        return val.compile(ctx)
    if isinstance(val, (int, float, Decimal)) and not isinstance(val, bool):
        # Inline numbers directly to avoid Postgres/asyncpg IndeterminateDatatypeError
        return str(val)
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    return ctx.add_param(val, col_type)


class ColumnClause(SQLElement):
    """Represents a column reference table.name or just name."""

    def __init__(
        self,
        table: Optional[str] = None,
        name: str = "",
        type: Optional[SQLType] = None,
        model: Optional[Type[Any]] = None,
        cte: Optional[Any] = None,
    ) -> None:
        self.table = table
        self.name = name
        self.type = normalize_type(type)
        self.model = model
        self._cte = cte

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        if self.table:
            return f'"{self.table}"."{self.name}"' if self.table.isidentifier() else f"{self.table}.{self.name}"
        return f'"{self.name}"' if self.name.isidentifier() else self.name


class BinaryExpression(SQLElement):
    def __init__(
        self,
        left: Any,
        op: str,
        right: Any,
        is_arithmetic: bool = False,
    ) -> None:
        self.left = left
        self.op = op
        self.right = right
        self.is_arithmetic = is_arithmetic

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        col_type = getattr(self.left, "type", None) or getattr(self.right, "type", None)
        l_sql = compile_operand(self.left, ctx, col_type)
        if ctx.symbolic_binds and not self.is_arithmetic:
            if not isinstance(self.right, SQLElement) and hasattr(self.left, "name") and isinstance(self.left.name, str) and self.left.name:
                r_sql = f":{self.left.name}"
            elif not isinstance(self.left, SQLElement) and hasattr(self.right, "name") and isinstance(self.right.name, str) and self.right.name:
                l_sql = f":{self.right.name}"
                r_sql = compile_operand(self.right, ctx, col_type)
            else:
                r_sql = compile_operand(self.right, ctx, col_type)
        else:
            r_sql = compile_operand(self.right, ctx, col_type)
        if self.is_arithmetic:
            return f"({l_sql} {self.op} {r_sql})"
        return f"{l_sql} {self.op} {r_sql}"


class UnaryExpression(SQLElement):
    def __init__(self, op: str, expr: Any) -> None:
        self.op = op
        self.expr = expr

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        return f"{self.op}({compile_operand(self.expr, ctx)})"


class IsExpression(SQLElement):
    def __init__(self, expr: Any, val: Any, negated: bool = False) -> None:
        self.expr = expr
        self.val = val
        self.negated = negated

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        e_sql = compile_operand(self.expr, ctx)
        if self.val is None:
            return f"{e_sql} IS NOT NULL" if self.negated else f"{e_sql} IS NULL"
        if self.val is True:
            return f"{e_sql} IS NOT TRUE" if self.negated else f"{e_sql} IS TRUE"
        if self.val is False:
            return f"{e_sql} IS NOT FALSE" if self.negated else f"{e_sql} IS FALSE"
        target = compile_operand(self.val, ctx)
        return f"{e_sql} IS NOT {target}" if self.negated else f"{e_sql} IS {target}"


class InExpression(SQLElement):
    def __init__(self, expr: Any, target: Any, negated: bool = False) -> None:
        self.expr = expr
        self.target = target
        self.negated = negated

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        e_sql = compile_operand(self.expr, ctx)
        col_type = getattr(self.expr, "type", None)

        if isinstance(self.target, (Select, Subquery)):
            sub_sql = self.target.compile_subquery_sql(ctx)
            return f"{e_sql} NOT IN ({sub_sql})" if self.negated else f"{e_sql} IN ({sub_sql})"

        # Iterable
        items = list(self.target) if isinstance(self.target, Iterable) and not isinstance(self.target, (str, bytes, dict)) else [self.target]
        if not items:
            return "TRUE" if self.negated else "FALSE"

        placeholders = [compile_operand(x, ctx, col_type) for x in items]
        joined = ", ".join(placeholders)
        return f"{e_sql} NOT IN ({joined})" if self.negated else f"{e_sql} IN ({joined})"


class BooleanClauseList(SQLElement):
    def __init__(self, operator: str, clauses: Sequence[Any]) -> None:
        self.operator = operator
        self.clauses = [c for c in clauses if c is not None and not getattr(c, "_skip_where", False)]
        if not self.clauses:
            self._skip_where = True

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        if not self.clauses:
            return "TRUE" if self.operator == "AND" else "FALSE"
        compiled = [c.compile(ctx) if isinstance(c, SQLElement) else compile_operand(c, ctx) for c in self.clauses]
        if len(compiled) == 1:
            return compiled[0]
        return f"({f' {self.operator} '.join(compiled)})"


def and_(*clauses: Any) -> BooleanClauseList:
    return BooleanClauseList("AND", clauses)


def or_(*clauses: Any) -> BooleanClauseList:
    return BooleanClauseList("OR", clauses)


class OrderByClause(SQLElement):
    def __init__(self, expr: Any, direction: str) -> None:
        self.expr = expr
        self.direction = direction.upper()

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        e_sql = self.expr.compile(ctx) if isinstance(self.expr, SQLElement) else str(self.expr)
        return f"{e_sql} {self.direction}"


def asc(expr: Any) -> OrderByClause:
    return OrderByClause(expr, "ASC")


def desc(expr: Any) -> OrderByClause:
    return OrderByClause(expr, "DESC")


class LabelClause(SQLElement):
    def __init__(self, expr: Any, name: str) -> None:
        self.expr = expr
        self.name = name
        self.type = getattr(expr, "type", NULLTYPE)

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        return self.expr.compile(ctx) if isinstance(self.expr, SQLElement) else str(self.expr)

    def compile_select_item(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        base = self.compile(ctx)
        return f'{base} AS "{self.name}"'


class CastExpression(SQLElement):
    def __init__(self, expr: Any, target_type: Any) -> None:
        self.expr = expr
        self.target_type = normalize_type(target_type)
        self.type = self.target_type

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        e_sql = compile_operand(self.expr, ctx)
        return f"CAST({e_sql} AS {self.target_type.sql_type()})"


def cast(expr: Any, target_type: Any) -> CastExpression:
    return CastExpression(expr, target_type)


class CaseExpression(SQLElement):
    def __init__(self, *whens: Any, else_: Optional[Any] = None) -> None:
        # Accepts case((cond, val), (cond2, val2), else_=...)
        clean_whens: List[Tuple[Any, Any]] = []
        for w in whens:
            if isinstance(w, (list, tuple)) and len(w) == 2:
                clean_whens.append((w[0], w[1]))
            elif isinstance(w, dict):
                for k, v in w.items():
                    clean_whens.append((k, v))
        self.whens = clean_whens
        self.else_ = else_

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        parts: List[str] = ["CASE"]
        for cond, val in self.whens:
            c_sql = cond.compile(ctx) if isinstance(cond, SQLElement) else str(cond)
            v_sql = format_case_val(val, ctx)
            parts.append(f"WHEN {c_sql} THEN {v_sql}")
        if self.else_ is not None:
            e_sql = format_case_val(self.else_, ctx)
            parts.append(f"ELSE {e_sql}")
        parts.append("END")
        return " ".join(parts)


def format_case_val(val: Any, ctx: CompileContext) -> str:
    if isinstance(val, SQLElement):
        return val.compile(ctx)
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float, Decimal)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    return ctx.add_param(val)


def case(*whens: Any, else_: Optional[Any] = None) -> CaseExpression:
    return CaseExpression(*whens, else_=else_)


class WindowExpression(SQLElement):
    def __init__(
        self,
        expr: Any,
        partition_by: Optional[Any] = None,
        order_by: Optional[Any] = None,
    ) -> None:
        self.expr = expr
        self.partition_by = partition_by
        self.order_by = order_by

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        e_sql = compile_operand(self.expr, ctx)
        clauses: List[str] = []
        if self.partition_by is not None:
            p_items = self.partition_by if isinstance(self.partition_by, (list, tuple)) else [self.partition_by]
            p_sql = ", ".join(compile_operand(p, ctx) for p in p_items)
            clauses.append(f"PARTITION BY {p_sql}")
        if self.order_by is not None:
            o_items = self.order_by if isinstance(self.order_by, (list, tuple)) else [self.order_by]
            o_sql = ", ".join(o.compile(ctx) if isinstance(o, SQLElement) else str(o) for o in o_items)
            clauses.append(f"ORDER BY {o_sql}")
        over_content = " ".join(clauses)
        return f"{e_sql} OVER ({over_content})"


class Expression(SQLElement):
    """Base expression AST element supporting Python operator overloading."""
    pass


class FunctionCall(Expression):
    def __init__(self, name: str, args: Sequence[Any]) -> None:
        self.name = name.upper()
        self.args = list(args)

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        if self.name == "COUNT":
            if not self.args:
                return "COUNT(*)"
            return f"COUNT({compile_operand(self.args[0], ctx)})"
        if self.name == "DISTINCT":
            return f"DISTINCT {compile_operand(self.args[0], ctx)}"
        if self.name == "EXTRACT" and len(self.args) >= 2:
            field = self.args[0]
            expr = self.args[1]
            return f"EXTRACT({field} FROM {compile_operand(expr, ctx)})"
        compiled_args = [compile_operand(a, ctx) for a in self.args]
        return f"{self.name}({', '.join(compiled_args)})"


class FunctionNamespace:
    """Provides SQL function builders under sa.func.*."""

    def __getattr__(self, name: str) -> Any:
        def factory(*args: Any) -> FunctionCall:
            return FunctionCall(name, args)
        return factory

    def text(self, sql_str: str) -> TextClause:
        return TextClause(sql_str)


func = FunctionNamespace()


class ColumnNamespace:
    """Namespace returning ColumnClause for subqueries, CTEs, or tables."""

    def __init__(self, table_name: str, cte: Optional[Any] = None) -> None:
        self._table_name = table_name
        self._cte = cte

    def __getattr__(self, name: str) -> ColumnClause:
        return ColumnClause(table=self._table_name, name=name, cte=self._cte)

    def __getitem__(self, name: str) -> ColumnClause:
        return ColumnClause(table=self._table_name, name=name, cte=self._cte)


class Subquery(SQLElement):
    def __init__(self, select_stmt: Select, name: str) -> None:
        self.select_stmt = select_stmt
        self.name = name
        self.c = ColumnNamespace(name)

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        inner_ctx = CompileContext()
        inner_ctx.top_level = False
        sql = self.select_stmt.compile_core(ctx)
        return f'({sql}) AS "{self.name}"'

    def compile_subquery_sql(self, ctx: CompileContext) -> str:
        return self.select_stmt.compile_core(ctx)


class ScalarSubquery(SQLElement):
    def __init__(self, select_stmt: Select) -> None:
        self.select_stmt = select_stmt

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        sql = self.select_stmt.compile_core(ctx)
        return f"({sql})"


class CTE(SQLElement):
    def __init__(self, select_stmt: Select, name: str) -> None:
        self.select_stmt = select_stmt
        self.name = name
        self.c = ColumnNamespace(name, cte=self)

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        return f'"{self.name}"'

    def compile_cte_definition(self, ctx: CompileContext) -> str:
        inner_sql = self.select_stmt.compile_core(ctx)
        return f'"{self.name}" AS ({inner_sql})'


# ---------------------------------------------------------------------------
# 5. Raw SQL Text & Parameter Regex Compiler
# ---------------------------------------------------------------------------
_TEXT_TOKEN_RE = re.compile(
    r"('(?:''|[^'])*')"                      # Group 1: string literal
    r"|(--[^\r\n]*)"                         # Group 2: comment
    r"|(::[a-zA-Z_][a-zA-Z0-9_\[\]]*)"      # Group 3: ::type cast
    r"|(?<![:\w]):([a-zA-Z_][a-zA-Z0-9_]*)"  # Group 4: named parameter
)


def compile_text_clause(sql: str, params: Optional[Mapping[str, Any]]) -> Tuple[str, List[Any]]:
    """Compiles a text query with :param_name bind variables into $1, $2 for asyncpg.

    Deduplicates identical parameter names, preserves single-quoted literals and
    PostgreSQL ::type casts without corruption.
    """
    if not params:
        return sql, []

    param_map: Dict[str, int] = {}
    ordered_args: List[Any] = []

    def replace_token(match: re.Match[str]) -> str:
        # Group 1: string literal
        if match.group(1):
            return match.group(1)
        # Group 2: comment
        if match.group(2):
            return match.group(2)
        # Group 3: type cast
        if match.group(3):
            return match.group(3)
        # Group 4: named parameter
        param_name = match.group(4)
        if param_name not in param_map:
            if param_name not in params:
                return match.group(0)  # leave unreplaced if not in params
            val = params[param_name]
            prepared = prepare_bind_param(val, None)
            ordered_args.append(prepared)
            param_map[param_name] = len(ordered_args)
        return f"${param_map[param_name]}"

    compiled_sql = _TEXT_TOKEN_RE.sub(replace_token, sql)
    return compiled_sql, ordered_args


class CompiledText(str):
    params: Dict[str, Any]

    def __new__(cls, sql_text: str, params: Optional[Dict[str, Any]] = None) -> CompiledText:
        obj = super().__new__(cls, sql_text)
        obj.params = params if params is not None else {}
        return obj


class TextClause(SQLElement):
    def __init__(self, text_str: str) -> None:
        self.text_str = text_str

    def compile(self, ctx: Optional[CompileContext] = None) -> CompiledText:
        extracted_params: Dict[str, Any] = {}
        for match in _TEXT_TOKEN_RE.finditer(self.text_str):
            param_name = match.group(4)
            if param_name:
                extracted_params[param_name] = None
        return CompiledText(self.text_str, extracted_params)


def text(sql_str: str) -> TextClause:
    return TextClause(sql_str)


# ---------------------------------------------------------------------------
# 6. DDL Constructs & Compiler (migrator.py support)
# ---------------------------------------------------------------------------
class ForeignKey:
    def __init__(self, target: str, ondelete: Optional[str] = None) -> None:
        self._colspec = target
        self.target_fullname = target
        self.ondelete = ondelete


class Constraint:
    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name


class PrimaryKeyConstraint(Constraint):
    def __init__(self, *columns: str, name: Optional[str] = None) -> None:
        super().__init__(name)
        self.columns = list(columns)


class UniqueConstraint(Constraint):
    def __init__(self, *columns: str, name: Optional[str] = None) -> None:
        super().__init__(name)
        self.columns = list(columns)


class CheckConstraint(Constraint):
    def __init__(self, sqltext: str, name: Optional[str] = None) -> None:
        super().__init__(name)
        self.sqltext = sqltext


class ForeignKeyConstraint(Constraint):
    def __init__(
        self,
        columns: Sequence[str],
        refcolumns: Sequence[str],
        name: Optional[str] = None,
        ondelete: Optional[str] = None,
    ) -> None:
        super().__init__(name)
        self.columns = list(columns)
        self.refcolumns = list(refcolumns)
        self.ondelete = ondelete
        self.elements = [ForeignKey(ref, ondelete=ondelete) for ref in refcolumns]


class Column:
    def __init__(
        self,
        *args: Any,
        primary_key: bool = False,
        nullable: bool = True,
        server_default: Optional[Any] = None,
        unique: bool = False,
        index: bool = False,
        default: Optional[Any] = None,
        onupdate: Optional[Any] = None,
        **kw: Any,
    ) -> None:
        self.name: str = ""
        self.type: SQLType = NullType()
        self.foreign_keys: Set[ForeignKey] = set()

        for arg in args:
            if isinstance(arg, str):
                self.name = arg
            elif isinstance(arg, (SQLType, type)) and (isinstance(arg, SQLType) or issubclass(arg, SQLType)):
                self.type = normalize_type(arg)
            elif isinstance(arg, ForeignKey):
                self.foreign_keys.add(arg)

        self.primary_key = primary_key
        self.nullable = not primary_key if nullable is True and primary_key else nullable
        self.server_default = server_default
        self.unique = unique
        self.index = index
        self.default = default
        self.onupdate = onupdate
        self.table: Optional[Table] = None

    def copy(self) -> Column:
        new_col = Column(
            self.name,
            self.type,
            *list(self.foreign_keys),
            primary_key=self.primary_key,
            nullable=self.nullable,
            server_default=self.server_default,
            unique=self.unique,
            index=self.index,
            default=self.default,
            onupdate=self.onupdate,
        )
        return new_col


class Index:
    def __init__(
        self,
        name: str,
        *columns: Any,
        unique: bool = False,
        postgresql_using: Optional[str] = None,
        postgresql_ops: Optional[Dict[str, str]] = None,
        postgresql_where: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.columns = list(columns)
        self.unique = unique
        self.postgresql_using = postgresql_using
        self.postgresql_ops = postgresql_ops or {}
        self.postgresql_where = postgresql_where
        self.table: Optional[Table] = None


class Table:
    def __init__(
        self,
        name: str,
        metadata: MetaData,
        *columns_and_constraints: Any,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.metadata = metadata
        self.columns: Dict[str, Column] = {}
        self.c = ColumnNamespace(name)
        self.constraints: List[Constraint] = []
        self.foreign_keys: Set[ForeignKey] = set()
        self.indexes: List[Index] = []
        self.primary_key = PrimaryKeyConstraint()

        metadata.tables[name] = self

        for item in columns_and_constraints:
            if isinstance(item, Column):
                self.append_column(item)
            elif isinstance(item, PrimaryKeyConstraint):
                self.primary_key = item
                self.constraints.append(item)
            elif isinstance(item, Constraint):
                self.constraints.append(item)
                if isinstance(item, ForeignKeyConstraint):
                    self.foreign_keys.update(item.elements)
            elif isinstance(item, Index):
                item.table = self
                self.indexes.append(item)

    def append_column(self, col: Column) -> None:
        col.table = self
        self.columns[col.name] = col
        if col.primary_key and col.name not in self.primary_key.columns:
            self.primary_key.columns.append(col.name)
        self.foreign_keys.update(col.foreign_keys)
        for fk in col.foreign_keys:
            target = getattr(fk, "_colspec", None) or getattr(fk, "target_fullname", None)
            if target:
                self.constraints.append(
                    ForeignKeyConstraint([col.name], [target], ondelete=fk.ondelete)
                )
        if col.index:
            idx = Index(f"ix_{self.name}_{col.name}", col.name)
            idx.table = self
            self.indexes.append(idx)


class MetaData:
    def __init__(self) -> None:
        self.tables: Dict[str, Table] = {}


class PostgreSQLDDLCompiler:
    """PostgreSQL DDL statement compiler."""

    def __init__(self, dialect: Any = None, statement: Any = None) -> None:
        self.dialect = dialect
        self.statement = statement

    def get_column_specification(self, col: Column) -> str:
        parts: List[str] = [col.name, col.type.sql_type()]
        if col.server_default is not None:
            sd = col.server_default
            sd_sql = sd.text_str if isinstance(sd, TextClause) else (sd.compile() if isinstance(sd, SQLElement) else str(sd))
            sd_clean = sd_sql.strip()

            # PostgreSQL Syntax Protection:
            # In PostgreSQL DDL, an unquoted identifier in DEFAULT (e.g., DEFAULT FLOW_EXPORTER)
            # is parsed by the PostgreSQL query planner as a column reference rather than a string literal,
            # triggering: "ERROR: cannot use column reference in DEFAULT expression".
            # We detect unquoted alphanumeric string literals and wrap them in single quotes, while exempting:
            # - Already single- or double-quoted literals ('...' or "...")
            # - SQL boolean and null literals (TRUE, FALSE, NULL)
            # - Standard parameterless SQL temporal/session functions (CURRENT_TIMESTAMP, CURRENT_DATE, etc.)
            # - Function calls and cast expressions containing '(' or '::'
            # - Numeric constants (integers and floats)
            sql_keywords = (
                "true", "false", "null",
                "current_timestamp", "current_date", "current_time",
                "localtime", "localtimestamp", "current_user", "session_user", "user"
            )
            if (
                not (sd_clean.startswith("'") and sd_clean.endswith("'"))
                and not (sd_clean.startswith('"') and sd_clean.endswith('"'))
                and sd_clean.lower() not in sql_keywords
                and "(" not in sd_clean
                and "::" not in sd_clean
            ):
                try:
                    float(sd_clean)
                except ValueError:
                    sd_sql = f"'{sd_clean}'"
            parts.append(f"DEFAULT {sd_sql}")
        if not col.nullable or col.primary_key:
            parts.append("NOT NULL")
        if col.unique:
            parts.append("UNIQUE")
        return " ".join(parts)


class PostgreSQLDialect:
    def ddl_compiler(self, dialect: Any = None, statement: Any = None) -> PostgreSQLDDLCompiler:
        return PostgreSQLDDLCompiler(dialect, statement)


class DialectNamespace:
    def dialect(self) -> PostgreSQLDialect:
        return PostgreSQLDialect()


postgresql_dialect = PostgreSQLDialect()


class CompiledDDL:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def __str__(self) -> str:
        return self.sql


class CreateTable:
    def __init__(self, table: Table) -> None:
        self.table = table

    def compile(self, dialect: Any = None) -> CompiledDDL:
        lines: List[str] = []
        compiler = PostgreSQLDDLCompiler()

        for col in self.table.columns.values():
            lines.append(f"    {compiler.get_column_specification(col)}")

        if self.table.primary_key and self.table.primary_key.columns:
            cols = ", ".join(self.table.primary_key.columns)
            name_clause = f'CONSTRAINT "{self.table.primary_key.name}" ' if self.table.primary_key.name else ""
            lines.append(f"    {name_clause}PRIMARY KEY ({cols})")

        for c in self.table.constraints:
            if isinstance(c, UniqueConstraint):
                cols = ", ".join(c.columns)
                name_clause = f'CONSTRAINT "{c.name}" ' if c.name else ""
                lines.append(f"    {name_clause}UNIQUE ({cols})")
            elif isinstance(c, CheckConstraint):
                name_clause = f'CONSTRAINT "{c.name}" ' if c.name else ""
                lines.append(f"    {name_clause}CHECK ({c.sqltext})")
            elif isinstance(c, ForeignKeyConstraint):
                # Table-level FOREIGN KEY constraint compilation:
                # Converts SQLAlchemy/native ForeignKeyConstraint specifications into valid PostgreSQL DDL.
                # In PostgreSQL, table-level foreign keys require:
                #   FOREIGN KEY ("local_col1", "local_col2") REFERENCES "target_table" ("remote_col1", "remote_col2")
                # We decompose strings formatted as "target_table.remote_col" or Column objects into
                # quoted target table and quoted target column identifiers.
                loc = ", ".join(f'"{col.strip(chr(34))}"' for col in c.columns)
                target_table = None
                target_cols = []
                for ref in c.refcolumns:
                    if isinstance(ref, str):
                        if "." in ref:
                            t_name, col_name = ref.rsplit(".", 1)
                            target_table = t_name.strip('"')
                            target_cols.append(col_name.strip('"'))
                        else:
                            target_cols.append(ref.strip('"'))
                    elif hasattr(ref, "table") and hasattr(ref, "name"):
                        target_table = getattr(ref.table, "name", "").strip('"')
                        target_cols.append(getattr(ref, "name", "").strip('"'))
                    else:
                        target_cols.append(str(ref).strip('"'))

                on_del = f" ON DELETE {c.ondelete}" if c.ondelete else ""
                name_clause = f'CONSTRAINT "{c.name}" ' if c.name else ""
                if target_table:
                    rem_cols = ", ".join(f'"{tc}"' for tc in target_cols)
                    lines.append(f'    {name_clause}FOREIGN KEY ({loc}) REFERENCES "{target_table}" ({rem_cols}){on_del}')
                else:
                    rem = ", ".join(c.refcolumns)
                    lines.append(f"    {name_clause}FOREIGN KEY ({loc}) REFERENCES {rem}{on_del}")

        body = ",\n".join(lines)
        ddl = f"CREATE TABLE {self.table.name} (\n{body}\n)"
        return CompiledDDL(ddl)


class CreateIndex:
    def __init__(self, index: Index) -> None:
        self.index = index

    def compile(self, dialect: Any = None) -> CompiledDDL:
        uniq = "UNIQUE " if self.index.unique else ""
        table_name = self.index.table.name if self.index.table else "table"
        using_clause = f" USING {self.index.postgresql_using}" if self.index.postgresql_using else ""

        col_parts: List[str] = []
        for c in self.index.columns:
            c_name = str(c)
            opclass = self.index.postgresql_ops.get(c_name)
            if opclass:
                col_parts.append(f"{c_name} {opclass}")
            else:
                col_parts.append(c_name)
        cols_joined = ", ".join(col_parts)
        where_clause = f" WHERE {self.index.postgresql_where}" if self.index.postgresql_where else ""

        ddl = f'CREATE {uniq}INDEX "{self.index.name}" ON "{table_name}"{using_clause} ({cols_joined}){where_clause}'
        return CompiledDDL(ddl)


class SchemaNamespace:
    CreateTable = CreateTable
    CreateIndex = CreateIndex


schema = SchemaNamespace()


# ---------------------------------------------------------------------------
# 7. Query Builders (Select, Insert, Update, Delete)
# ---------------------------------------------------------------------------
def _find_table_from_entity(e: Any) -> Optional[str]:
    if isinstance(e, type) and issubclass(e, DeclarativeBase):
        return f'"{e.__tablename__}"'
    if isinstance(e, CTE):
        return f'"{e.name}"'
    if isinstance(e, Subquery):
        return f'"{e.name}"'
    if isinstance(e, Table):
        return f'"{e.name}"'
    if isinstance(e, ColumnClause) and e.table:
        return f'"{e.table}"' if e.table.isidentifier() else e.table
    if isinstance(e, LabelClause):
        return _find_table_from_entity(e.expr)
    if isinstance(e, FunctionCall):
        for arg in e.args:
            tbl = _find_table_from_entity(arg)
            if tbl:
                return tbl
    if isinstance(e, BinaryExpression):
        return _find_table_from_entity(e.left) or _find_table_from_entity(e.right)
    if isinstance(e, BooleanClauseList):
        for clause in e.clauses:
            tbl = _find_table_from_entity(clause)
            if tbl:
                return tbl
    if isinstance(e, (InExpression, IsExpression)):
        return _find_table_from_entity(e.expr)
    if isinstance(e, WindowExpression):
        return _find_table_from_entity(e.expr)
    if isinstance(e, CastExpression):
        return _find_table_from_entity(e.expr)
    return None


class Select(SQLElement):
    """SQL SELECT statement builder supporting joins, CTEs, subqueries & pagination."""

    def __init__(self, *entities: Any) -> None:
        self._select_entities = list(entities)
        self._from_target: Optional[Any] = None
        self._where_clauses: List[Any] = []
        self._joins: List[Tuple[str, Any, Any]] = []
        self._order_by: List[Any] = []
        self._group_by: List[Any] = []
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._distinct: Optional[Union[bool, List[Any]]] = None
        self._options: List[Any] = []
        self._for_update: bool = False
        self._skip_locked: bool = False
        self._nowait: bool = False

    @property
    def selected_columns(self) -> List[Any]:
        cols: List[Any] = []
        for entity in self._select_entities:
            if isinstance(entity, type) and issubclass(entity, DeclarativeBase):
                cols.extend(entity.__table_columns__.values())
            else:
                cols.append(entity)
        return cols

    def clone(self) -> Select:
        new_sel = Select(*self._select_entities)
        new_sel._from_target = self._from_target
        new_sel._where_clauses = list(self._where_clauses)
        new_sel._joins = list(self._joins)
        new_sel._order_by = list(self._order_by)
        new_sel._group_by = list(self._group_by)
        new_sel._limit = self._limit
        new_sel._offset = self._offset
        new_sel._distinct = copy.copy(self._distinct) if isinstance(self._distinct, list) else self._distinct
        new_sel._options = list(self._options)
        new_sel._for_update = self._for_update
        new_sel._skip_locked = self._skip_locked
        new_sel._nowait = self._nowait
        return new_sel

    def where(self, *conditions: Any) -> Select:
        s = self.clone()
        s._where_clauses.extend(c for c in conditions if c is not None and not getattr(c, "_skip_where", False))
        return s

    def filter(self, *conditions: Any) -> Select:
        return self.where(*conditions)

    def select_from(self, target: Any) -> Select:
        s = self.clone()
        s._from_target = target
        return s

    def join(self, target: Any, onclause: Optional[Any] = None, isouter: bool = False) -> Select:
        s = self.clone()
        join_type = "LEFT OUTER JOIN" if isouter else "INNER JOIN"
        s._joins.append((join_type, target, onclause))
        return s

    def outerjoin(self, target: Any, onclause: Optional[Any] = None) -> Select:
        return self.join(target, onclause=onclause, isouter=True)

    def order_by(self, *clauses: Any) -> Select:
        s = self.clone()
        s._order_by.extend(c for c in clauses if c is not None)
        return s

    def group_by(self, *clauses: Any) -> Select:
        s = self.clone()
        s._group_by.extend(c for c in clauses if c is not None)
        return s

    def limit(self, n: Optional[Any]) -> Select:
        s = self.clone()
        if n is None:
            s._limit = None
        else:
            val = int(n)
            if val < 0:
                raise ValueError("LIMIT cannot be negative")
            s._limit = val
        return s

    def offset(self, n: Optional[Any]) -> Select:
        s = self.clone()
        if n is None:
            s._offset = None
        else:
            val = int(n)
            if val < 0:
                raise ValueError("OFFSET cannot be negative")
            s._offset = val
        return s

    def distinct(self, *on_exprs: Any) -> Select:
        s = self.clone()
        if on_exprs:
            s._distinct = list(on_exprs)
        else:
            s._distinct = True
        return s

    def options(self, *opts: Any) -> Select:
        s = self.clone()
        s._options.extend(opts)
        return s

    def with_for_update(self, skip_locked: bool = False, nowait: bool = False) -> Select:
        s = self.clone()
        s._for_update = True
        s._skip_locked = skip_locked
        s._nowait = nowait
        return s

    def subquery(self, name: Optional[str] = None) -> Subquery:
        sub_name = name or f"anon_{abs(id(self))}"
        return Subquery(self, sub_name)

    def scalar_subquery(self) -> ScalarSubquery:
        return ScalarSubquery(self)

    def cte(self, name: str) -> CTE:
        return CTE(self, name)

    def _collect_ctes(self) -> List[CTE]:
        """Collects all referenced CTEs recursively in topological definition order."""
        ordered: List[CTE] = []
        seen: Set[str] = set()

        def visit(obj: Any) -> None:
            if isinstance(obj, CTE):
                if obj.name not in seen:
                    # Visit upstream CTEs inside this CTE first
                    for up_cte in obj.select_stmt._collect_ctes():
                        if up_cte.name not in seen:
                            seen.add(up_cte.name)
                            ordered.append(up_cte)
                    if obj.name not in seen:
                        seen.add(obj.name)
                        ordered.append(obj)
            elif isinstance(obj, ColumnClause):
                if getattr(obj, "_cte", None) is not None:
                    visit(obj._cte)
            elif isinstance(obj, Select):
                for e in obj._select_entities:
                    visit(e)
                if obj._from_target:
                    visit(obj._from_target)
                for _, target, onclause in obj._joins:
                    visit(target)
                    visit(onclause)
                for w in obj._where_clauses:
                    visit(w)
                for g in obj._group_by:
                    visit(g)
                for o in obj._order_by:
                    visit(o)
            elif isinstance(obj, (Subquery, ScalarSubquery)):
                if hasattr(obj, "select_stmt"):
                    visit(obj.select_stmt)
            elif isinstance(obj, SQLElement):
                if hasattr(obj, "expr"):
                    visit(getattr(obj, "expr"))
                if hasattr(obj, "target"):
                    visit(getattr(obj, "target"))
                if hasattr(obj, "select_stmt"):
                    visit(getattr(obj, "select_stmt"))
                if hasattr(obj, "clauses"):
                    for c in getattr(obj, "clauses"):
                        visit(c)
                if hasattr(obj, "left"):
                    visit(getattr(obj, "left"))
                if hasattr(obj, "right"):
                    visit(getattr(obj, "right"))
                if hasattr(obj, "args"):
                    for a in getattr(obj, "args"):
                        visit(a)
                if hasattr(obj, "whens"):
                    for c, v in getattr(obj, "whens"):
                        visit(c)
                        visit(v)
                if hasattr(obj, "else_") and getattr(obj, "else_") is not None:
                    visit(getattr(obj, "else_"))

        visit(self)
        return ordered

    def compile_core(self, ctx: CompileContext) -> str:
        """Compiles the SELECT statement body without the outer WITH ... clause."""
        parts: List[str] = ["SELECT"]

        # Distinct
        if self._distinct is True:
            parts.append("DISTINCT")
        elif isinstance(self._distinct, list) and self._distinct:
            on_sql = ", ".join(e.compile(ctx) if isinstance(e, SQLElement) else str(e) for e in self._distinct)
            parts.append(f"DISTINCT ON ({on_sql})")

        # Projections
        col_sqls: List[str] = []
        for entity in self._select_entities:
            if isinstance(entity, type) and issubclass(entity, DeclarativeBase):
                # Expand model columns
                tbl = entity.__tablename__
                for c in entity.__table_columns__.values():
                    col_sqls.append(f'"{tbl}"."{c.name}"')
            elif isinstance(entity, (CTE, Subquery)):
                col_sqls.append(f'"{entity.name}".*')
            elif isinstance(entity, SQLElement):
                col_sqls.append(entity.compile_select_item(ctx))
            else:
                col_sqls.append(str(entity))
        parts.append(", ".join(col_sqls))

        # FROM
        from_tbl = self._determine_from(ctx)
        if from_tbl:
            parts.append(f"FROM {from_tbl}")

        # JOINs
        for j_type, target, onclause in self._joins:
            t_sql = self._format_table_target(target, ctx)
            on_sql = onclause.compile(ctx) if isinstance(onclause, SQLElement) else str(onclause)
            parts.append(f"{j_type} {t_sql} ON {on_sql}")

        # WHERE
        if self._where_clauses:
            w_sql = " AND ".join(c.compile(ctx) if isinstance(c, SQLElement) else str(c) for c in self._where_clauses)
            parts.append(f"WHERE {w_sql}")

        # GROUP BY
        if self._group_by:
            g_sql = ", ".join(g.compile(ctx) if isinstance(g, SQLElement) else str(g) for g in self._group_by)
            parts.append(f"GROUP BY {g_sql}")

        # ORDER BY
        if self._order_by:
            o_sql = ", ".join(o.compile(ctx) if isinstance(o, SQLElement) else str(o) for o in self._order_by)
            parts.append(f"ORDER BY {o_sql}")

        # LIMIT / OFFSET
        if self._limit is not None:
            parts.append(f"LIMIT {self._limit}")
        if self._offset is not None:
            parts.append(f"OFFSET {self._offset}")

        # FOR UPDATE
        if self._for_update:
            lock = "FOR UPDATE"
            if self._nowait:
                lock += " NOWAIT"
            elif self._skip_locked:
                lock += " SKIP LOCKED"
            parts.append(lock)

        return " ".join(parts)

    def _determine_from(self, ctx: CompileContext) -> Optional[str]:
        if self._from_target is not None:
            return self._format_table_target(self._from_target, ctx)
        for e in self._select_entities:
            tbl = _find_table_from_entity(e)
            if tbl:
                return tbl
        for w in self._where_clauses:
            tbl = _find_table_from_entity(w)
            if tbl:
                return tbl
        return None

    def _format_table_target(self, target: Any, ctx: CompileContext) -> str:
        if isinstance(target, type) and issubclass(target, DeclarativeBase):
            return f'"{target.__tablename__}"'
        if isinstance(target, Subquery):
            return target.compile(ctx)
        if isinstance(target, CTE):
            return f'"{target.name}"'
        if isinstance(target, Table):
            return f'"{target.name}"'
        return str(target)

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        if ctx.top_level:
            ctx.top_level = False
            ctes = self._collect_ctes()
            if ctes:
                cte_defs = [cte.compile_cte_definition(ctx) for cte in ctes]
                with_block = f"WITH {', '.join(cte_defs)} "
                return with_block + self.compile_core(ctx)
        return self.compile_core(ctx)


def select(*entities: Any) -> Select:
    return Select(*entities)


class ExcludedNamespace:
    """Namespace providing ColumnClause references under insert().excluded.<col>."""

    def __init__(self, table_or_model: Any) -> None:
        self._target = table_or_model

    def __getattr__(self, name: str) -> ColumnClause:
        return ColumnClause(table="EXCLUDED", name=name)


class Insert(SQLElement):
    """SQL INSERT builder with PostgreSQL ON CONFLICT support."""

    def __init__(self, model_or_table: Any) -> None:
        self._target = model_or_table
        self._table_name = getattr(model_or_table, "__tablename__", getattr(model_or_table, "name", str(model_or_table)))
        self._values: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None
        self._on_conflict_update: Optional[Tuple[List[str], Dict[str, Any]]] = None
        self._on_conflict_nothing: Optional[List[str]] = None
        self._returning: List[Any] = []
        self.excluded = ExcludedNamespace(model_or_table)

    def values(self, *args: Any, **kwargs: Any) -> Insert:
        if args and isinstance(args[0], list):
            self._values = args[0]
        elif args and isinstance(args[0], dict):
            self._values = args[0]
        else:
            self._values = kwargs
        return self

    def on_conflict_do_update(
        self,
        index_elements: Optional[Sequence[Any]] = None,
        set_: Optional[Dict[Any, Any]] = None,
        **kwargs: Any,
    ) -> Insert:
        elements: List[str] = []
        if index_elements:
            for el in index_elements:
                elements.append(el.name if hasattr(el, "name") else str(el))
        set_dict: Dict[str, Any] = {}
        if set_:
            for k, v in set_.items():
                k_name = k.name if hasattr(k, "name") else str(k)
                set_dict[k_name] = v
        self._on_conflict_update = (elements, set_dict)
        return self

    def on_conflict_do_nothing(
        self,
        index_elements: Optional[Sequence[Any]] = None,
        **kwargs: Any,
    ) -> Insert:
        elements: List[str] = []
        if index_elements:
            for el in index_elements:
                elements.append(el.name if hasattr(el, "name") else str(el))
        self._on_conflict_nothing = elements
        return self

    def returning(self, *cols_or_models: Any) -> Insert:
        self._returning.extend(cols_or_models)
        return self

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        parts: List[str] = [f'INSERT INTO "{self._table_name}"']

        model_cols = getattr(self._target, "__table_columns__", {})
        if isinstance(self._values, list):
            if not self._values:
                return f'INSERT INTO "{self._table_name}" DEFAULT VALUES'
            columns = list(self._values[0].keys())
            cols_joined = ", ".join(f'"{c}"' for c in columns)
            parts.append(f"({cols_joined}) VALUES")
            row_tuples: List[str] = []
            for row in self._values:
                row_placeholders = [
                    row[c].compile(ctx) if isinstance(row.get(c), SQLElement) else ctx.add_param(row.get(c), getattr(model_cols.get(c), "type", None))
                    for c in columns
                ]
                row_tuples.append(f"({', '.join(row_placeholders)})")
            parts.append(", ".join(row_tuples))
        elif isinstance(self._values, dict):
            columns = list(self._values.keys())
            cols_joined = ", ".join(f'"{c}"' for c in columns)
            placeholders = [
                self._values[c].compile(ctx) if isinstance(self._values[c], SQLElement) else ctx.add_param(self._values[c], getattr(model_cols.get(c), "type", None))
                for c in columns
            ]
            parts.append(f"({cols_joined}) VALUES ({', '.join(placeholders)})")
        else:
            parts.append("DEFAULT VALUES")

        # Conflict clause
        if self._on_conflict_update:
            idx_els, set_dict = self._on_conflict_update
            idx_sql = f"({', '.join(f'\"{c}\"' for c in idx_els)})" if idx_els else ""
            set_parts: List[str] = []
            for k, v in set_dict.items():
                v_sql = v.compile(ctx) if isinstance(v, SQLElement) else ctx.add_param(v, getattr(model_cols.get(k), "type", None))
                set_parts.append(f'"{k}" = {v_sql}')
            parts.append(f"ON CONFLICT {idx_sql} DO UPDATE SET {', '.join(set_parts)}")
        elif self._on_conflict_nothing is not None:
            idx_els = self._on_conflict_nothing
            idx_sql = f"({', '.join(f'\"{c}\"' for c in idx_els)})" if idx_els else ""
            parts.append(f"ON CONFLICT {idx_sql} DO NOTHING")

        # RETURNING
        if self._returning:
            ret_cols: List[str] = []
            for r in self._returning:
                if isinstance(r, type) and issubclass(r, DeclarativeBase):
                    ret_cols.append("*")
                elif hasattr(r, "name"):
                    ret_cols.append(f'"{r.name}"')
                else:
                    ret_cols.append(str(r))
            parts.append(f"RETURNING {', '.join(ret_cols)}")

        return " ".join(parts)


def insert(model_or_table: Any) -> Insert:
    return Insert(model_or_table)


class Update(SQLElement):
    """SQL UPDATE builder."""

    def __init__(self, model_or_table: Any) -> None:
        self._target = model_or_table
        self._table_name = getattr(model_or_table, "__tablename__", getattr(model_or_table, "name", str(model_or_table)))
        self._values: Dict[str, Any] = {}
        self._where_clauses: List[Any] = []
        self._returning: List[Any] = []

    def where(self, *conditions: Any) -> Update:
        self._where_clauses.extend(c for c in conditions if c is not None)
        return self

    def values(self, *args: Any, **kwargs: Any) -> Update:
        if args and isinstance(args[0], dict):
            self._values.update(args[0])
        self._values.update(kwargs)
        return self

    def returning(self, *cols_or_models: Any) -> Update:
        self._returning.extend(cols_or_models)
        return self

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        parts: List[str] = [f'UPDATE "{self._table_name}" SET']

        # Auto-update columns with onupdate=func.now()
        model_cols = getattr(self._target, "__table_columns__", {})
        vals = dict(self._values)
        for col_name, col_obj in model_cols.items():
            if col_obj.onupdate is not None and col_name not in vals:
                vals[col_name] = func.now()

        set_pairs: List[str] = []
        for k, v in vals.items():
            col_type = model_cols[k].type if k in model_cols else None
            v_sql = v.compile(ctx) if isinstance(v, SQLElement) else ctx.add_param(v, col_type)
            set_pairs.append(f'"{k}" = {v_sql}')
        parts.append(", ".join(set_pairs))

        if self._where_clauses:
            w_sql = " AND ".join(c.compile(ctx) if isinstance(c, SQLElement) else str(c) for c in self._where_clauses)
            parts.append(f"WHERE {w_sql}")

        if self._returning:
            ret_cols: List[str] = []
            for r in self._returning:
                if isinstance(r, type) and issubclass(r, DeclarativeBase):
                    ret_cols.append("*")
                elif hasattr(r, "name"):
                    ret_cols.append(f'"{r.name}"')
                else:
                    ret_cols.append(str(r))
            parts.append(f"RETURNING {', '.join(ret_cols)}")

        return " ".join(parts)


def update(model_or_table: Any) -> Update:
    return Update(model_or_table)


class Delete(SQLElement):
    """SQL DELETE builder."""

    def __init__(self, model_or_table: Any) -> None:
        self._target = model_or_table
        self._table_name = getattr(model_or_table, "__tablename__", getattr(model_or_table, "name", str(model_or_table)))
        self._where_clauses: List[Any] = []
        self._returning: List[Any] = []

    def where(self, *conditions: Any) -> Delete:
        self._where_clauses.extend(c for c in conditions if c is not None)
        return self

    def returning(self, *cols_or_models: Any) -> Delete:
        self._returning.extend(cols_or_models)
        return self

    def compile(self, ctx: Optional[CompileContext] = None) -> str:
        ctx = ctx or CompileContext()
        parts: List[str] = [f'DELETE FROM "{self._table_name}"']
        if self._where_clauses:
            w_sql = " AND ".join(c.compile(ctx) if isinstance(c, SQLElement) else str(c) for c in self._where_clauses)
            parts.append(f"WHERE {w_sql}")
        if self._returning:
            ret_cols: List[str] = []
            for r in self._returning:
                if isinstance(r, type) and issubclass(r, DeclarativeBase):
                    ret_cols.append("*")
                elif hasattr(r, "name"):
                    ret_cols.append(f'"{r.name}"')
                else:
                    ret_cols.append(str(r))
            parts.append(f"RETURNING {', '.join(ret_cols)}")
        return " ".join(parts)


def delete(model_or_table: Any) -> Delete:
    return Delete(model_or_table)


# ---------------------------------------------------------------------------
# 8. Declarative Models & ORM Metaclass
# ---------------------------------------------------------------------------
class Mapped(Generic[T]):
    """Type annotation marker for declarative model attributes."""


class ColumnDescriptor:
    """Dual-mode descriptor returning ColumnClause on class and value on instance."""

    def __init__(self, col: Column, table_name: str, model_cls: Type[Any]) -> None:
        self.col = col
        self.table_name = table_name
        self.model_cls = model_cls
        self.name = col.name
        self._clause = ColumnClause(table=table_name, name=col.name, type=col.type, model=model_cls)

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            return self._clause
        return instance.__dict__.get(self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        instance.__dict__[self.name] = value


class RelationshipProperty:
    def __init__(self, target: str, lazy: str = "select", back_populates: Optional[str] = None, cascade: Optional[str] = None) -> None:
        self.target = target
        self.lazy = lazy
        self.back_populates = back_populates
        self.cascade = cascade


class RelationshipDescriptor:
    def __init__(self, prop: RelationshipProperty, name: str) -> None:
        self.prop = prop
        self.name = name

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            return self.prop
        return instance.__dict__.get(self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        instance.__dict__[self.name] = value


def relationship(
    target: str,
    lazy: str = "select",
    back_populates: Optional[str] = None,
    cascade: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    return RelationshipProperty(target, lazy=lazy, back_populates=back_populates, cascade=cascade)


def joinedload(rel_prop: Any) -> Any:
    return rel_prop


def selectinload(rel_prop: Any) -> Any:
    return rel_prop


def mapped_column(*args: Any, **kwargs: Any) -> Column:
    return Column(*args, **kwargs)


def _infer_type_from_annotation(ann: Any) -> SQLType:
    if ann is None:
        return NullType()
    ann_str = str(ann)
    if "UUID" in ann_str:
        return UUIDType(as_uuid=True)
    if "bool" in ann_str:
        return Boolean()
    if "int" in ann_str:
        return Integer()
    if "float" in ann_str:
        return Float()
    if "datetime" in ann_str:
        return DateTime(timezone=True)
    if "dict" in ann_str or "list" in ann_str or "Any" in ann_str:
        return JSONB()
    if "str" in ann_str:
        return String()
    return NullType()


class DeclarativeMeta(type):
    """Metaclass constructing table schema and descriptors on DeclarativeBase models."""

    def __new__(mcls, name: str, bases: Tuple[type, ...], namespace: Dict[str, Any]) -> Any:
        cls = super().__new__(mcls, name, bases, namespace)
        if name == "DeclarativeBase" or not namespace.get("__tablename__"):
            return cls

        table_name = namespace["__tablename__"]
        columns: Dict[str, Column] = {}
        relationships: Dict[str, RelationshipProperty] = {}
        primary_keys: List[str] = []

        annotations = namespace.get("__annotations__", {})

        # Inspect class attributes & annotations
        for attr_name, val in list(namespace.items()):
            if isinstance(val, Column):
                val.name = val.name or attr_name
                if isinstance(val.type, NullType) and attr_name in annotations:
                    val.type = _infer_type_from_annotation(annotations[attr_name])
                columns[attr_name] = val
                if val.primary_key:
                    primary_keys.append(attr_name)
            elif isinstance(val, RelationshipProperty):
                relationships[attr_name] = val

        # Handle annotations without explicit assignment
        for ann_name, ann_type in annotations.items():
            if ann_name not in columns and ann_name not in relationships and not ann_name.startswith("_"):
                col = Column(ann_name, _infer_type_from_annotation(ann_type))
                columns[ann_name] = col

        # Bind descriptors
        for attr_name, col in columns.items():
            desc = ColumnDescriptor(col, table_name, cls)
            setattr(cls, attr_name, desc)

        for rel_name, rel_prop in relationships.items():
            r_desc = RelationshipDescriptor(rel_prop, rel_name)
            setattr(cls, rel_name, r_desc)

        # Store model metadata
        cls.__table_columns__ = columns
        cls.__primary_keys__ = primary_keys
        cls.__mapper_relationships__ = relationships

        # Construct Table metadata object
        cls.__table__ = Table(
            table_name,
            MetaData(),
            *list(columns.values()),
            *list(namespace.get("__table_args__", ())),
        )
        return cls


class DeclarativeBase(metaclass=DeclarativeMeta):
    """Base class for all application ORM models."""

    __tablename__: str = ""
    __table_columns__: Dict[str, Column] = {}
    __primary_keys__: List[str] = []
    __mapper_relationships__: Dict[str, RelationshipProperty] = {}
    __table__: Table

    def __init__(self, **kwargs: Any) -> None:
        self._orm_persisted: bool = False
        self._orm_state: Dict[str, Any] = {}

        # Set declared column values or defaults
        for col_name, col in self.__table_columns__.items():
            if col_name in kwargs:
                setattr(self, col_name, kwargs[col_name])
            elif col.default is not None:
                default_val = col.default() if callable(col.default) else col.default
                setattr(self, col_name, default_val)
            else:
                setattr(self, col_name, None)

        # Set any extra attributes (relationships, kwargs)
        for k, v in kwargs.items():
            if k not in self.__table_columns__:
                setattr(self, k, v)

    def __repr__(self) -> str:
        pks = ", ".join(f"{pk}={getattr(self, pk, None)!r}" for pk in self.__primary_keys__)
        return f"<{self.__class__.__name__}({pks})>"


def _snapshot_model(obj: DeclarativeBase) -> Dict[str, Any]:
    """Snapshots deep copy of column attributes for dirty tracking."""
    snap: Dict[str, Any] = {}
    for col_name in obj.__table_columns__:
        val = getattr(obj, col_name, None)
        if isinstance(val, (dict, list)):
            snap[col_name] = copy.deepcopy(val)
        else:
            snap[col_name] = val
    return snap


# ---------------------------------------------------------------------------
# 9. Hybrid Result & Row Runtime
# ---------------------------------------------------------------------------
class Row:
    """Represents a database result row supporting indexing, attribute access, and mapping."""

    def __init__(
        self,
        values: Sequence[Any],
        keys: Sequence[str],
        mapping: Dict[str, Any],
    ) -> None:
        self._values = tuple(values)
        self._keys = list(keys)
        self._mapping = mapping

    def __getitem__(self, item: Union[int, str]) -> Any:
        if isinstance(item, int):
            return self._values[item]
        if item in self._mapping:
            return self._mapping[item]
        raise KeyError(item)

    def __getattr__(self, name: str) -> Any:
        if name in self._mapping:
            return self._mapping[name]
        raise AttributeError(f"'Row' object has no attribute '{name}'")

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(self._values)

    def keys(self) -> List[str]:
        return list(self._keys)

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)


class ScalarResult:
    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows

    def all(self) -> List[Any]:
        return [r[0] for r in self._rows if len(r) > 0]

    def first(self) -> Optional[Any]:
        return self._rows[0][0] if self._rows and len(self._rows[0]) > 0 else None

    def one(self) -> Any:
        if not self._rows:
            raise NoResultFound("No row was found when one was required")
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows were found when one was required")
        return self._rows[0][0]

    def one_or_none(self) -> Optional[Any]:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows were found when one was required")
        return self._rows[0][0]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.all())


class MappingResult:
    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows

    def all(self) -> List[Dict[str, Any]]:
        return [dict(r._mapping) for r in self._rows]

    def first(self) -> Optional[Dict[str, Any]]:
        return dict(self._rows[0]._mapping) if self._rows else None

    def one(self) -> Dict[str, Any]:
        if not self._rows:
            raise NoResultFound("No row was found")
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows found")
        return dict(self._rows[0]._mapping)

    def one_or_none(self) -> Optional[Dict[str, Any]]:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows found")
        return dict(self._rows[0]._mapping)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.all())


class Result:
    """Async query execution result."""

    def __init__(self, rows: List[Row], rowcount: int = 0) -> None:
        self._rows = rows
        self._rowcount = rowcount

    def all(self) -> List[Row]:
        return list(self._rows)

    def fetchall(self) -> List[Row]:
        return self.all()

    def first(self) -> Optional[Row]:
        return self._rows[0] if self._rows else None

    def fetchone(self) -> Optional[Row]:
        return self.first()

    def one(self) -> Row:
        if not self._rows:
            raise NoResultFound("No row was found")
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows found")
        return self._rows[0]

    def one_or_none(self) -> Optional[Row]:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows found")
        return self._rows[0]

    def scalar(self) -> Any:
        return self._rows[0][0] if self._rows and len(self._rows[0]) > 0 else None

    def scalars(self) -> ScalarResult:
        return ScalarResult(self._rows)

    def scalar_one(self) -> Any:
        return self.scalars().one()

    def scalar_one_or_none(self) -> Optional[Any]:
        return self.scalars().one_or_none()

    def mappings(self) -> MappingResult:
        return MappingResult(self._rows)

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def __iter__(self) -> Iterator[Row]:
        return iter(self._rows)


def _is_row_returning_sql(sql: str) -> bool:
    """Returns True if the SQL query returns tabular rows (SELECT, WITH, SHOW, etc.)."""
    cleaned = re.sub(r"--[^\n]*\n", "\n", sql).strip()
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    upper = cleaned.upper()
    return upper.startswith(("SELECT", "WITH", "SHOW", "VALUES", "TABLE", "EXPLAIN")) or "RETURNING" in upper


# ---------------------------------------------------------------------------
# 10. AsyncEngine & Connection Pool Management
# ---------------------------------------------------------------------------
class AsyncConnection:
    """Managed async database connection."""

    def __init__(self, conn: Any, in_tx: bool = False, engine: Optional[Any] = None) -> None:
        self._conn = conn
        self._in_tx = in_tx
        self._tx: Optional[Any] = None
        self.engine = engine

    async def execute(self, stmt: Any, params: Optional[Any] = None) -> Result:
        if isinstance(stmt, TextClause):
            if isinstance(params, (list, tuple)) and params and isinstance(params[0], dict):
                # Batch executemany
                token_matches = [m.group(4) for m in _TEXT_TOKEN_RE.finditer(stmt.text_str) if m.group(4)]
                seen_tokens: Set[str] = set()
                param_order: List[str] = []
                for name in token_matches:
                    if name not in seen_tokens:
                        seen_tokens.add(name)
                        param_order.append(name)
                sql, _ = compile_text_clause(stmt.text_str, params[0])
                args_list = [[prepare_bind_param(row.get(k), None) for k in param_order] for row in params]
                await self._conn.executemany(sql, args_list)
                return Result([], rowcount=len(params))

            sql, bound = compile_text_clause(stmt.text_str, params if isinstance(params, dict) else None)
            if _is_row_returning_sql(sql):
                records = await self._conn.fetch(sql, *bound)
                return Result([Row(list(r.values()), list(r.keys()), dict(r)) for r in records], rowcount=len(records))
            res = await self._conn.execute(sql, *bound)
            return Result([], rowcount=parse_rowcount(res))

        if isinstance(stmt, SQLElement):
            ctx = CompileContext()
            sql = stmt.compile(ctx)
            stmt_params = ctx.params
        else:
            sql = str(stmt)
            stmt_params = params if isinstance(params, (list, tuple)) else []

        if _is_row_returning_sql(sql):
            records = await self._conn.fetch(sql, *stmt_params)
            return Result([Row(list(r.values()), list(r.keys()), dict(r)) for r in records], rowcount=len(records))
        res = await self._conn.execute(sql, *stmt_params)
        return Result([], rowcount=parse_rowcount(res))

    async def commit(self) -> None:
        if self._tx:
            await self._tx.commit()
            self._tx = None

    async def rollback(self) -> None:
        if self._tx:
            await self._tx.rollback()
            self._tx = None

    async def close(self) -> None:
        if self._conn is not None:
            if self.engine is not None and getattr(self.engine, "_pool", None) is not None:
                await self.engine._pool.release(self._conn)
            self._conn = None

    async def __aenter__(self) -> "AsyncConnection":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self._tx:
                if exc_type is not None:
                    await self.rollback()
                else:
                    await self.commit()
        finally:
            await self.close()


def parse_rowcount(status_tag: Any) -> int:
    """Parses affected row count from asyncpg command status (e.g. 'DELETE 5', 'UPDATE 1')."""
    if not isinstance(status_tag, str):
        return 0
    parts = status_tag.split()
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


class AsyncEngine:
    """Engine managing asyncpg connection pools and connection lifetimes."""

    def __init__(self, url: str, **kwargs: Any) -> None:
        self.url = self._normalize_url(url)
        self.pool_size = kwargs.get("pool_size", 10)
        self.max_overflow = kwargs.get("max_overflow", 10)
        self.echo = kwargs.get("echo", False)
        self.dialect = types.SimpleNamespace(name="postgresql")
        self._pool: Optional[Any] = None
        self._loop_locks: Dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _get_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Lock()
        for l in list(self._loop_locks.keys()):
            if l.is_closed():
                del self._loop_locks[l]
        if loop not in self._loop_locks:
            self._loop_locks[loop] = asyncio.Lock()
        return self._loop_locks[loop]

    @property
    def _lock(self) -> asyncio.Lock:
        return self._get_lock()

    def _normalize_url(self, url: str) -> str:
        # asyncpg requires postgresql:// rather than postgresql+asyncpg://
        return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

    async def _get_pool(self) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        old_pool = None
        if self._pool is not None:
            pool_loop = getattr(self._pool, "_loop", None)
            if isinstance(pool_loop, asyncio.AbstractEventLoop) and (pool_loop is not loop or pool_loop.is_closed()):
                old_pool = self._pool
                self._pool = None
            else:
                return self._pool
        lock = self._get_lock()
        async with lock:
            if self._pool is not None:
                pool_loop = getattr(self._pool, "_loop", None)
                if isinstance(pool_loop, asyncio.AbstractEventLoop) and (pool_loop is not loop or pool_loop.is_closed()):
                    old_pool = self._pool
                    self._pool = None
                else:
                    return self._pool
            if old_pool is not None:
                try:
                    close_coro = old_pool.close()
                    if inspect.isawaitable(close_coro):
                        await close_coro
                except Exception:
                    pass
                old_pool = None
            if self._pool is None:
                if asyncpg is None:
                    raise OperationalError("asyncpg is not installed. Please install asyncpg.")
                min_size = 1
                max_size = max(1, self.pool_size + self.max_overflow)
                self._pool = await asyncpg.create_pool(
                    self.url,
                    min_size=min_size,
                    max_size=max_size,
                    timeout=5.0,
                    command_timeout=10.0,
                )
        return self._pool

    def connect(self) -> _AsyncEngineConnectContext:
        return _AsyncEngineConnectContext(self, begin_tx=False)

    def begin(self) -> _AsyncEngineConnectContext:
        return _AsyncEngineConnectContext(self, begin_tx=True)

    async def dispose(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = None
        self._loop_locks.clear()


class _AsyncEngineConnectContext:
    def __init__(self, engine: AsyncEngine, begin_tx: bool = False) -> None:
        self.engine = engine
        self.begin_tx = begin_tx
        self.conn: Optional[Any] = None
        self.wrapped: Optional[AsyncConnection] = None

    def __await__(self):
        return self._acquire().__await__()

    async def _acquire(self) -> "AsyncConnection":
        pool = await self.engine._get_pool()
        conn = await pool.acquire()
        wrapped = AsyncConnection(conn, in_tx=self.begin_tx, engine=self.engine)
        if self.begin_tx:
            wrapped._tx = conn.transaction()
            await wrapped._tx.start()
        return wrapped

    async def __aenter__(self) -> AsyncConnection:
        pool = await self.engine._get_pool()
        self.conn = await pool.acquire()
        self.wrapped = AsyncConnection(self.conn, in_tx=self.begin_tx, engine=self.engine)
        if self.begin_tx:
            self.wrapped._tx = self.conn.transaction()
            await self.wrapped._tx.start()
        return self.wrapped

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self.wrapped and self.wrapped._tx:
                if exc_type is not None:
                    await self.wrapped._tx.rollback()
                else:
                    await self.wrapped._tx.commit()
        finally:
            if self.wrapped and self.wrapped._conn is None:
                pass
            elif self.conn and self.engine._pool:
                await self.engine._pool.release(self.conn)
                if self.wrapped:
                    self.wrapped._conn = None


def create_async_engine(url: str, **kwargs: Any) -> AsyncEngine:
    return AsyncEngine(url, **kwargs)


# ---------------------------------------------------------------------------
# 11. AsyncSession Runtime
# ---------------------------------------------------------------------------
class AsyncSession:
    """Asynchronous database session with identity mapping, dirty tracking, and flush hydration."""

    def __init__(self, bind: Optional[AsyncEngine] = None, **kwargs: Any) -> None:
        self.bind = bind
        self._new: List[DeclarativeBase] = []
        self._deleted: List[DeclarativeBase] = []
        self._identity_map: Dict[Tuple[Type[Any], Any], DeclarativeBase] = {}
        self._conn: Optional[Any] = None
        self._tx: Optional[Any] = None
        self._is_active: bool = True
        self._in_transaction: bool = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def __aenter__(self) -> AsyncSession:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                await self.rollback()
        finally:
            await self.close()

    def add(self, obj: DeclarativeBase) -> None:
        if obj not in self._new and not getattr(obj, "_orm_persisted", False):
            self._new.append(obj)

    def add_all(self, objs: Iterable[DeclarativeBase]) -> None:
        for obj in objs:
            self.add(obj)

    def delete(self, obj: DeclarativeBase) -> Any:
        if obj not in self._deleted:
            self._deleted.append(obj)

        class _AwaitableDelete:
            def __await__(self) -> Any:
                async def _noop() -> None: pass
                return _noop().__await__()
        return _AwaitableDelete()

    async def _get_conn(self) -> Any:
        if self._conn is None:
            if self.bind is None:
                raise OperationalError("Session has no bound engine.")
            pool = await self.bind._get_pool()
            self._conn = await pool.acquire()
            tx = self._conn.transaction()
            if asyncio.iscoroutine(tx):
                self._tx = await tx
            else:
                self._tx = tx
            if hasattr(self._tx, "start"):
                start_res = self._tx.start()
                if asyncio.iscoroutine(start_res):
                    await start_res
            self._in_transaction = True
        return self._conn

    async def flush(self) -> None:
        """Executes pending INSERTs, UPDATEs, and DELETEs against the database."""
        conn = await self._get_conn()

        # 1. INSERTs for new instances
        while self._new:
            obj = self._new.pop(0)
            model_cls = obj.__class__
            tbl_name = model_cls.__tablename__

            cols_to_insert: List[str] = []
            vals_to_insert: List[Any] = []
            for col_name, col in model_cls.__table_columns__.items():
                val = getattr(obj, col_name, None)
                if val is not None or (col.default is not None and col.server_default is None):
                    cols_to_insert.append(col_name)
                    vals_to_insert.append(val)

            if cols_to_insert:
                cols_sql = ", ".join(f'"{c}"' for c in cols_to_insert)
                ctx = CompileContext()
                placeholders = [ctx.add_param(v, model_cls.__table_columns__[c].type) for c, v in zip(cols_to_insert, vals_to_insert)]
                sql = f'INSERT INTO "{tbl_name}" ({cols_sql}) VALUES ({", ".join(placeholders)}) RETURNING *'
            else:
                sql = f'INSERT INTO "{tbl_name}" DEFAULT VALUES RETURNING *'
                ctx = CompileContext()

            record = await conn.fetchrow(sql, *ctx.params)
            if record:
                self._hydrate_instance_from_record(obj, record, model_cls)

            obj._orm_persisted = True
            obj._orm_state = _snapshot_model(obj)
            pk_val = self._extract_pk(obj)
            if pk_val:
                self._identity_map[(model_cls, pk_val)] = obj

        # 2. UPDATEs for mutated instances in identity map
        for (model_cls, pk_val), obj in list(self._identity_map.items()):
            if obj in self._deleted:
                continue
            mutated_cols: Dict[str, Any] = {}
            for col_name, col in model_cls.__table_columns__.items():
                curr_val = getattr(obj, col_name, None)
                orig_val = obj._orm_state.get(col_name)
                if curr_val != orig_val:
                    mutated_cols[col_name] = curr_val

            if mutated_cols:
                # Include onupdate columns
                for col_name, col in model_cls.__table_columns__.items():
                    if col.onupdate is not None and col_name not in mutated_cols:
                        mutated_cols[col_name] = func.now()

                ctx = CompileContext()
                set_pairs: List[str] = []
                for c_name, val in mutated_cols.items():
                    c_type = model_cls.__table_columns__[c_name].type
                    if isinstance(val, SQLElement):
                        set_pairs.append(f'"{c_name}" = {val.compile(ctx)}')
                    else:
                        param = ctx.add_param(val, c_type)
                        set_pairs.append(f'"{c_name}" = {param}')

                pk_cols = model_cls.__primary_keys__
                pk_clauses = [f'"{pk}" = {ctx.add_param(getattr(obj, pk), model_cls.__table_columns__[pk].type)}' for pk in pk_cols]
                sql = f'UPDATE "{model_cls.__tablename__}" SET {", ".join(set_pairs)} WHERE {" AND ".join(pk_clauses)} RETURNING *'

                record = await conn.fetchrow(sql, *ctx.params)
                if record:
                    self._hydrate_instance_from_record(obj, record, model_cls)
                obj._orm_state = _snapshot_model(obj)

        # 3. DELETEs
        while self._deleted:
            obj = self._deleted.pop(0)
            model_cls = obj.__class__
            pk_cols = model_cls.__primary_keys__
            ctx = CompileContext()
            pk_clauses = [f'"{pk}" = {ctx.add_param(getattr(obj, pk), model_cls.__table_columns__[pk].type)}' for pk in pk_cols]
            sql = f'DELETE FROM "{model_cls.__tablename__}" WHERE {" AND ".join(pk_clauses)}'
            await conn.execute(sql, *ctx.params)
            pk_val = self._extract_pk(obj)
            if pk_val and (model_cls, pk_val) in self._identity_map:
                del self._identity_map[(model_cls, pk_val)]

    async def commit(self) -> None:
        await self.flush()
        if self._tx is not None:
            if hasattr(self._tx, "commit"):
                commit_res = self._tx.commit()
                if asyncio.iscoroutine(commit_res):
                    await commit_res
            self._tx = None
        self._in_transaction = False
        if self._conn is not None and self.bind and self.bind._pool:
            await self.bind._pool.release(self._conn)
            self._conn = None

    async def rollback(self) -> None:
        self._new.clear()
        self._deleted.clear()
        for (model_cls, _), obj in list(self._identity_map.items()):
            orm_state = getattr(obj, "_orm_state", None)
            if isinstance(orm_state, dict):
                for col_name, orig_val in orm_state.items():
                    if isinstance(orig_val, (dict, list)):
                        setattr(obj, col_name, copy.deepcopy(orig_val))
                    else:
                        setattr(obj, col_name, orig_val)
        if self._tx is not None:
            try:
                if hasattr(self._tx, "rollback"):
                    rb_res = self._tx.rollback()
                    if asyncio.iscoroutine(rb_res):
                        await rb_res
            except Exception:
                pass
            self._tx = None
        self._in_transaction = False
        if self._conn is not None and self.bind and self.bind._pool:
            await self.bind._pool.release(self._conn)
            self._conn = None

    async def close(self) -> None:
        self._is_active = False
        if self._tx is not None:
            await self.rollback()
        elif self._conn is not None and self.bind and self.bind._pool:
            await self.bind._pool.release(self._conn)
            self._conn = None

    async def refresh(self, obj: DeclarativeBase) -> None:
        model_cls = obj.__class__
        pk_val = self._extract_pk(obj)
        if not pk_val:
            return
        fresh = await self.get(model_cls, pk_val)
        if fresh is not None:
            for c_name in model_cls.__table_columns__:
                setattr(obj, c_name, getattr(fresh, c_name))
            obj._orm_state = _snapshot_model(obj)

    async def get(self, model_cls: Type[ModelT], ident: Any) -> Optional[ModelT]:
        pk_cols = model_cls.__primary_keys__
        if not pk_cols:
            return None
        pk_col = getattr(model_cls, pk_cols[0])
        stmt = select(model_cls).where(pk_col == ident)
        res = await self.execute(stmt)
        return res.scalar_one_or_none()

    @property
    def dirty(self) -> Set[DeclarativeBase]:
        dirty_objs: Set[DeclarativeBase] = set()
        for (model_cls, _), obj in self._identity_map.items():
            if obj in self._deleted:
                continue
            for col_name in getattr(model_cls, "__table_columns__", {}):
                if getattr(obj, col_name, None) != getattr(obj, "_orm_state", {}).get(col_name):
                    dirty_objs.add(obj)
                    break
        return dirty_objs

    def _get_dirty_attributes(self, obj: DeclarativeBase) -> Set[str]:
        dirty_cols: Set[str] = set()
        model_cls = obj.__class__
        for col_name in getattr(model_cls, "__table_columns__", {}):
            if getattr(obj, col_name, None) != getattr(obj, "_orm_state", {}).get(col_name):
                dirty_cols.add(col_name)
        return dirty_cols

    async def execute(self, stmt: Any, params: Optional[Any] = None) -> Result:
        conn = await self._get_conn()

        # 1. TextClause with batch params (executemany)
        if isinstance(stmt, TextClause) and isinstance(params, (list, tuple)):
            if not params:
                return Result([], rowcount=0)
            if isinstance(params[0], dict):
                sql, _ = compile_text_clause(stmt.text_str, params[0])
                # Build positional args matrix for executemany
                token_matches = [m.group(4) for m in _TEXT_TOKEN_RE.finditer(stmt.text_str) if m.group(4)]
                # Deduplicate order
                seen: Set[str] = set()
                param_order: List[str] = []
                for name in token_matches:
                    if name not in seen:
                        seen.add(name)
                        param_order.append(name)
                args_list = [[prepare_bind_param(row.get(k), None) for k in param_order] for row in params]
                await conn.executemany(sql, args_list)
                return Result([], rowcount=len(params))

        # 2. TextClause with single dict params or None
        if isinstance(stmt, TextClause):
            sql, bound = compile_text_clause(stmt.text_str, params if isinstance(params, dict) else None)
            if _is_row_returning_sql(sql):
                records = await conn.fetch(sql, *bound)
                rows = self._records_to_rows(records)
                return Result(rows, rowcount=len(rows))
            res = await conn.execute(sql, *bound)
            return Result([], rowcount=parse_rowcount(res))

        # 3. Select Statement
        if isinstance(stmt, Select):
            ctx = CompileContext()
            sql = stmt.compile(ctx)
            records = await conn.fetch(sql, *ctx.params)
            rows = await self._hydrate_select_results(stmt, records)
            return Result(rows, rowcount=len(rows))

        # 4. Insert / Update / Delete
        if isinstance(stmt, (Insert, Update, Delete)):
            ctx = CompileContext()
            sql = stmt.compile(ctx)
            has_returning = bool(getattr(stmt, "_returning", []))
            if has_returning:
                records = await conn.fetch(sql, *ctx.params)
                ret_models = [r for r in getattr(stmt, "_returning", []) if isinstance(r, type) and issubclass(r, DeclarativeBase)]
                if len(ret_models) == 1:
                    model_cls = ret_models[0]
                    rows: List[Row] = []
                    for rec in records:
                        rec_dict = dict(rec)
                        inst = model_cls()
                        self._hydrate_instance_from_record(inst, rec_dict, model_cls)
                        inst._orm_persisted = True
                        inst._orm_state = _snapshot_model(inst)
                        pk_val = self._extract_pk(inst)
                        if pk_val:
                            self._identity_map[(model_cls, pk_val)] = inst
                        rows.append(Row([inst], [model_cls.__name__], rec_dict))
                    return Result(rows, rowcount=len(rows))
                rows = self._records_to_rows(records)
                return Result(rows, rowcount=len(rows))
            res = await conn.execute(sql, *ctx.params)
            return Result([], rowcount=parse_rowcount(res))

        # Raw SQL string
        sql = str(stmt)
        if _is_row_returning_sql(sql):
            records = await conn.fetch(sql)
            rows = self._records_to_rows(records)
            return Result(rows, rowcount=len(rows))
        res = await conn.execute(sql)
        return Result([], rowcount=parse_rowcount(res))

    async def _hydrate_select_results(self, stmt: Select, records: List[Any]) -> List[Row]:
        """Hydrates database records into hybrid Row objects, hydrating models & relationships."""
        rows: List[Row] = []
        if not records:
            return rows

        # Analyze entities in projection
        entity_slots: List[Tuple[str, Any]] = []
        for e in stmt._select_entities:
            if isinstance(e, type) and issubclass(e, DeclarativeBase):
                entity_slots.append(("model", e))
            elif isinstance(e, LabelClause):
                entity_slots.append(("scalar", e.name))
            elif isinstance(e, ColumnClause):
                entity_slots.append(("scalar", e.name))
            elif isinstance(e, SQLElement):
                entity_slots.append(("scalar", getattr(e, "name", "expr")))
            else:
                entity_slots.append(("scalar", str(e)))

        # Hydrate each record
        users_to_resolve_role: List[DeclarativeBase] = []
        sessions_to_resolve_user: List[DeclarativeBase] = []

        for rec in records:
            rec_dict = dict(rec)
            slot_values: List[Any] = []
            row_mapping: Dict[str, Any] = {}

            # Populate model or scalar for each entity slot
            for slot_type, target in entity_slots:
                if slot_type == "model":
                    model_cls = target
                    inst = model_cls()
                    self._hydrate_instance_from_record(inst, rec_dict, model_cls)
                    inst._orm_persisted = True
                    inst._orm_state = _snapshot_model(inst)
                    pk_val = self._extract_pk(inst)
                    if pk_val:
                        if (model_cls, pk_val) in self._identity_map:
                            inst = self._identity_map[(model_cls, pk_val)]
                            if inst in self.dirty:
                                dirty_attrs = self._get_dirty_attributes(inst)
                                for c_name, val in rec_dict.items():
                                    if c_name not in dirty_attrs and hasattr(inst, c_name):
                                        col_type = getattr(model_cls, "__table_columns__", {}).get(c_name, None)
                                        hydrated = hydrate_column_value(val, getattr(col_type, "type", None))
                                        setattr(inst, c_name, hydrated)
                                        if hasattr(inst, "_orm_state") and c_name in inst._orm_state:
                                            inst._orm_state[c_name] = hydrated
                            else:
                                self._hydrate_instance_from_record(inst, rec_dict, model_cls)
                                inst._orm_state = _snapshot_model(inst)
                        else:
                            self._identity_map[(model_cls, pk_val)] = inst
                    slot_values.append(inst)

                    # Check for eager relationships (User.role, UserSession.user)
                    if model_cls.__name__ == "User" and getattr(inst, "role", None) is None:
                        users_to_resolve_role.append(inst)
                    elif model_cls.__name__ == "UserSession" and getattr(inst, "user", None) is None:
                        sessions_to_resolve_user.append(inst)
                else:
                    col_name = target
                    val = rec_dict.get(col_name)
                    if val is None and isinstance(col_name, str):
                        val = rec_dict.get(col_name.lower())
                    if val is None and len(slot_values) < len(rec):
                        val = list(rec.values())[len(slot_values)]
                    slot_values.append(val)
                    row_mapping[col_name] = val

            # Also add all record keys to mapping for raw lookup access
            for k, v in rec_dict.items():
                if k not in row_mapping:
                    row_mapping[k] = v

            keys = [s[1] if s[0] == "scalar" else s[1].__name__ for s in entity_slots]
            row = Row(slot_values, keys, row_mapping)
            rows.append(row)

        # Batch resolve eager relationships
        if users_to_resolve_role:
            role_ids = {u.role_id for u in users_to_resolve_role if getattr(u, "role_id", None)}
            if role_ids:
                from app.models.user import Role
                role_res = await self.execute(select(Role).where(Role.id.in_(list(role_ids))))
                roles_map = {r.id: r for r in role_res.scalars().all()}
                for u in users_to_resolve_role:
                    if u.role_id in roles_map:
                        setattr(u, "role", roles_map[u.role_id])

        if sessions_to_resolve_user:
            user_ids = {s.user_id for s in sessions_to_resolve_user if getattr(s, "user_id", None)}
            if user_ids:
                from app.models.user import User
                user_res = await self.execute(select(User).where(User.id.in_(list(user_ids))))
                users_map = {u.id: u for u in user_res.scalars().all()}
                for s in sessions_to_resolve_user:
                    if s.user_id in users_map:
                        setattr(s, "user", users_map[s.user_id])

        return rows

    def _records_to_rows(self, records: List[Any]) -> List[Row]:
        rows: List[Row] = []
        for rec in records:
            mapping = dict(rec)
            values = list(mapping.values())
            keys = list(mapping.keys())
            rows.append(Row(values, keys, mapping))
        return rows

    def _hydrate_instance_from_record(
        self,
        inst: DeclarativeBase,
        record: Mapping[str, Any],
        model_cls: Type[DeclarativeBase],
    ) -> None:
        for col_name, col in model_cls.__table_columns__.items():
            if col_name in record:
                raw_val = record[col_name]
                hydrated = hydrate_column_value(raw_val, col.type)
                setattr(inst, col_name, hydrated)

    def _extract_pk(self, obj: DeclarativeBase) -> Any:
        pk_cols = obj.__primary_keys__
        if not pk_cols:
            return None
        if len(pk_cols) == 1:
            return getattr(obj, pk_cols[0], None)
        return tuple(getattr(obj, pk, None) for pk in pk_cols)


# Session factories
class sessionmaker:
    def __init__(
        self,
        bind: Optional[Any] = None,
        class_: Type[AsyncSession] = AsyncSession,
        **kwargs: Any,
    ) -> None:
        self.bind = bind
        self.class_ = class_
        self.kwargs = kwargs

    def __call__(self, **local_kwargs: Any) -> AsyncSession:
        kw = {**self.kwargs, **local_kwargs}
        return self.class_(bind=self.bind, **kw)


class async_sessionmaker(sessionmaker):
    pass


# Alias Session and UUID for imports
Session = AsyncSession
UUID = UUIDType


def _pg_dialect() -> PostgreSQLDialect:
    return postgresql_dialect


postgresql = types.SimpleNamespace(
    UUID=UUIDType,
    INET=INET,
    JSONB=JSONB,
    ARRAY=ARRAY,
    insert=insert,
    Insert=Insert,
    dialect=_pg_dialect,
    PostgreSQLDialect=PostgreSQLDialect,
    PostgreSQLDDLCompiler=PostgreSQLDDLCompiler,
)
dialects = types.SimpleNamespace(postgresql=postgresql)
ext = types.SimpleNamespace(
    asyncio=types.SimpleNamespace(
        AsyncSession=AsyncSession,
        async_sessionmaker=async_sessionmaker,
    )
)

sys.modules["app.core.orm.postgresql"] = postgresql
sys.modules["app.core.orm.dialects"] = dialects
sys.modules["app.core.orm.dialects.postgresql"] = postgresql
sys.modules["app.core.orm.ext"] = ext
sys.modules["app.core.orm.ext.asyncio"] = ext.asyncio
