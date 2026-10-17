"""In-tree zero-dependency SQLAlchemy compatibility shim.

Re-exports core AST, DDL, and DML components from app.core.orm.
"""

from __future__ import annotations

from app.core.orm import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    TEXT,
    UniqueConstraint,
    VARCHAR,
    and_,
    asc,
    case,
    cast,
    delete,
    desc,
    func,
    insert,
    or_,
    schema,
    select,
    text,
    update,
)
from app.core.orm import INET as INET
from app.core.orm import JSONB as JSONB
from app.core.orm import UUIDType as UUID

from . import dialects as dialects
from . import exc as exc
from . import ext as ext
from . import orm as orm
from . import types as types

__all__ = [
    "BigInteger",
    "Boolean",
    "DateTime",
    "Integer",
    "Numeric",
    "SmallInteger",
    "String",
    "VARCHAR",
    "Text",
    "TEXT",
    "Float",
    "UUID",
    "INET",
    "JSONB",
    "ARRAY",
    "Column",
    "ForeignKey",
    "ForeignKeyConstraint",
    "PrimaryKeyConstraint",
    "UniqueConstraint",
    "CheckConstraint",
    "Index",
    "MetaData",
    "Table",
    "schema",
    "select",
    "insert",
    "update",
    "delete",
    "func",
    "text",
    "case",
    "or_",
    "and_",
    "desc",
    "asc",
    "cast",
    "dialects",
    "exc",
    "ext",
    "orm",
    "types",
]
