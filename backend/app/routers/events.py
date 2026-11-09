from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Awaitable, Callable

from app.core.asgi import APIRouter, Depends, Request
from app.core.asgi import StreamingResponse

from app.routers.auth import get_current_user_sse
from app.services.driver_manager import driver_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

HEARTBEAT_SECONDS = 15.0
# The stream re-checks its session this often, so signing out, a password reset, or an
# admin disabling the account ends an open stream within this time, not when it closes.
SESSION_RECHECK_SECONDS = 15.0
# Events waiting for one client. A client that falls this far behind is disconnected; its
# browser reconnects and reloads the current state instead of the server holding a backlog.
CLIENT_QUEUE_LIMIT = 1000


class _ClientQueue(asyncio.Queue):
    """One SSE client's events; `overflowed` is set when it fell too far behind."""

    def __init__(self) -> None:
        super().__init__(maxsize=CLIENT_QUEUE_LIMIT)
        self.overflowed = False


# In-memory broadcast queue for active SSE client streams
_active_sse_queues: set[_ClientQueue] = set()
_sse_lock = asyncio.Lock()


async def broadcast_sse_event(event_type: str, data: dict) -> None:
    """Broadcasts a telemetry event to all connected SSE clients."""
    payload = {"type": event_type, **data}
    async with _sse_lock:
        for q in list(_active_sse_queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                q.overflowed = True
                _active_sse_queues.discard(q)
            except Exception:
                pass


async def _session_still_valid(request: Request) -> bool:
    """True while the stream's cookie still names an active session of an enabled account.

    Fails closed: when the session cannot be confirmed (also while the database is
    unavailable) the stream ends, and the browser reconnects once it can sign in again.
    """
    try:
        await get_current_user_sse(request)
        return True
    except Exception as exc:
        logger.info("SSE stream closed: session no longer valid (%s)", getattr(exc, "detail", type(exc).__name__))
        return False


async def sse_event_generator(
    request: Request,
    session_check: Callable[[Request], Awaitable[bool]] = _session_still_valid,
) -> AsyncGenerator[str, None]:
    """
    Asynchronous event generator streaming live telemetry envelopes
    and 15-second heartbeat keep-alive comments.
    """
    queue = _ClientQueue()
    async with _sse_lock:
        _active_sse_queues.add(queue)

    logger.info("SSE client connected (Active streams: %d)", len(_active_sse_queues))

    try:
        # Initial greeting event
        init_payload = json.dumps({"type": "CONNECTED", "message": "Ephemres v3.3.5 SSE stream established."})
        yield f"data: {init_payload}\n\n"

        next_check = time.monotonic() + SESSION_RECHECK_SECONDS
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break
            if queue.overflowed:
                logger.warning("SSE client fell %d events behind; disconnecting it", CLIENT_QUEUE_LIMIT)
                break
            now = time.monotonic()
            if now >= next_check:
                if not await session_check(request):
                    break
                next_check = time.monotonic() + SESSION_RECHECK_SECONDS
                now = time.monotonic()

            try:
                # Wait for a new event, at most until the next heartbeat or session check
                timeout = min(HEARTBEAT_SECONDS, max(0.0, next_check - now))
                event_data = await asyncio.wait_for(queue.get(), timeout=timeout)
                json_str = json.dumps(event_data)
                yield f"data: {json_str}\n\n"
            except asyncio.TimeoutError:
                # Heartbeat comment to keep proxy connection alive
                yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break
    finally:
        async with _sse_lock:
            _active_sse_queues.discard(queue)
        logger.info("SSE client disconnected (Remaining streams: %d)", len(_active_sse_queues))


@router.get("/stream")
async def stream_events(
    request: Request,
    current_user: dict = Depends(get_current_user_sse),
):
    """
    Server-Sent Events (SSE) telemetry stream.
    Broadcasts real-time state transitions, node changes, and RCA alerts.
    The session is re-checked every SESSION_RECHECK_SECONDS; the stream ends when it is gone.
    """
    return StreamingResponse(
        sse_event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
