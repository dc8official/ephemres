"""Compatibility stub for alembic.context."""

from __future__ import annotations

from typing import Any, Optional


class _ConfigStub:
    def __init__(self) -> None:
        self.config_file_name: Optional[str] = None

    def get_main_option(self, name: str, default: Any = None) -> Any:
        return default

    def set_main_option(self, name: str, value: str) -> None:
        pass


config = _ConfigStub()


class _TransactionContext:
    def __enter__(self) -> "_TransactionContext":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def configure(*args: Any, **kwargs: Any) -> None:
    pass


def begin_transaction() -> _TransactionContext:
    return _TransactionContext()


def run_migrations(*args: Any, **kwargs: Any) -> None:
    pass


def is_offline_mode() -> bool:
    return False
