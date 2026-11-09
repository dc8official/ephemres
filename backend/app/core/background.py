"""Fire-and-forget tasks for the API, started from a route that answers without waiting for them.

asyncio keeps only a weak reference to a task, so a task nothing else holds can be garbage
collected before it finishes (Python's asyncio.create_task documentation warns about this).
spawn() holds each task until it is done and logs any exception it ends with, which would
otherwise go unnoticed until the task object was collected.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Set

logger = logging.getLogger(__name__)

_running: Set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Start `coro` as a task that is kept alive until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task %s failed: %s", task.get_name(), task.exception(), exc_info=task.exception())
