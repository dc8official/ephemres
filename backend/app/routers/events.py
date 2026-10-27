from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from app.core.asgi import APIRouter, Depends, Request
from app.core.asgi import StreamingResponse

from app.routers.auth import get_current_user_sse
from app.services.driver_manager import driver_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# In-memory broadcast queue for active SSE client streams
_active_sse_queues: set[asyncio.Queue] = set()
_sse_lock = asyncio.Lock()


async def broadcast_sse_event(event_type: str, data: dict) -> None:
    """Broadcasts a telemetry event to all connected SSE clients."""
    payload = {"type": event_type, **data}
    async with _sse_lock:
        for q in list(_active_sse_queues):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


async def sse_event_generator(request: Request) -> AsyncGenerator[str, None]:
    """
    Asynchronous event generator streaming live telemetry envelopes
    and 15-second heartbeat keep-alive comments.
    """
    queue: asyncio.Queue = asyncio.Queue()
    async with _sse_lock:
        _active_sse_queues.add(queue)

    logger.info("SSE client connected (Active streams: %d)", len(_active_sse_queues))

    try:
        # Initial greeting event
        init_payload = json.dumps({"type": "CONNECTED", "message": "Ephemres v3.3.5 SSE stream established."})
        yield f"data: {init_payload}\n\n"

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                # Wait up to 15 seconds for a new event
                event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                json_str = json.dumps(event_data)
                yield f"data: {json_str}\n\n"
            except asyncio.TimeoutError:
                # 15-second heartbeat comment to keep proxy connection alive
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
