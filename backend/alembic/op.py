"""Drop-in replacement for alembic.op delegating to app.core.migrator.OperationsProxy."""

from __future__ import annotations

from typing import Any, Optional, Sequence, Union

_current_proxy: Optional[Any] = None


def set_current_proxy(proxy: Optional[Any]) -> None:
    """Set active OperationsProxy for the currently executing migration."""
    global _current_proxy
    _current_proxy = proxy


def get_current_proxy() -> Optional[Any]:
    """Retrieve active OperationsProxy."""
    return _current_proxy


def execute(sqltext: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.execute(sqltext)


def create_table(table_name: str, *columns_and_constraints: Any, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.create_table(table_name, *columns_and_constraints, **kw)


def drop_table(table_name: str, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.drop_table(table_name, **kw)


def add_column(table_name: str, column: Any, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.add_column(table_name, column, **kw)


def drop_column(table_name: str, column_name: str, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.drop_column(table_name, column_name, **kw)


def create_index(
    index_name: str,
    table_name: str,
    columns: Sequence[Union[str, Any]],
    unique: bool = False,
    **kw: Any,
) -> None:
    if _current_proxy is not None:
        _current_proxy.create_index(index_name, table_name, columns, unique=unique, **kw)


def drop_index(index_name: str, table_name: Optional[str] = None, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.drop_index(index_name, table_name=table_name, **kw)


def create_check_constraint(name: str, table_name: str, condition: str, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.create_check_constraint(name, table_name, condition, **kw)


def create_foreign_key(
    name: str,
    source_table: str,
    referent_table: str,
    local_cols: Sequence[str],
    remote_cols: Sequence[str],
    ondelete: Optional[str] = None,
    **kw: Any,
) -> None:
    if _current_proxy is not None:
        _current_proxy.create_foreign_key(
            name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            ondelete=ondelete,
            **kw,
        )


def drop_constraint(name: str, table_name: str, type_: Optional[str] = None, **kw: Any) -> None:
    if _current_proxy is not None:
        _current_proxy.drop_constraint(name, table_name, type_=type_, **kw)


def __getattr__(name: str) -> Any:
    """Forward any other attribute call to the active OperationsProxy if defined."""
    if _current_proxy is not None and hasattr(_current_proxy, name):
        return getattr(_current_proxy, name)
    raise AttributeError(f"module 'alembic.op' has no attribute '{name}'")
