"""A failing endpoint gets one incident trace per state change or cooldown, not one per sweep."""

import asyncio
from unittest.mock import patch
from uuid import uuid4

from monitoring import registry as registry_mod
from monitoring.registry import EndpointRegistry

COOLDOWN = 900.0


def test_first_failure_is_traced_then_suppressed_within_cooldown():
    reg = EndpointRegistry()
    ep = uuid4()
    with patch.object(registry_mod.time, "monotonic", return_value=1000.0):
        assert reg.incident_trace_due(ep, "DOWN", COOLDOWN)
        reg.record_incident_trace(ep, "DOWN")
    with patch.object(registry_mod.time, "monotonic", return_value=1000.0 + COOLDOWN - 1):
        assert not reg.incident_trace_due(ep, "DOWN", COOLDOWN)
    with patch.object(registry_mod.time, "monotonic", return_value=1000.0 + COOLDOWN):
        assert reg.incident_trace_due(ep, "DOWN", COOLDOWN)


def test_state_change_is_traced_immediately():
    reg = EndpointRegistry()
    ep = uuid4()
    with patch.object(registry_mod.time, "monotonic", return_value=1000.0):
        reg.record_incident_trace(ep, "DEGRADED_LOSS")
        assert reg.incident_trace_due(ep, "DOWN", COOLDOWN)


def test_no_new_trace_while_one_is_in_flight():
    async def scenario():
        reg = EndpointRegistry()
        ep = uuid4()
        task = asyncio.create_task(asyncio.sleep(10))
        reg.register_diagnostic_task(ep, task)
        try:
            assert not reg.incident_trace_due(ep, "DOWN", COOLDOWN)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback drop the finished task
        assert reg.incident_trace_due(ep, "DOWN", COOLDOWN)

    asyncio.run(scenario())


def test_remove_endpoint_forgets_trace_history():
    async def scenario():
        reg = EndpointRegistry()
        ep = uuid4()
        reg.record_incident_trace(ep, "DOWN")
        await reg.remove_endpoint(ep)
        assert ep not in reg._last_incident_trace

    asyncio.run(scenario())
