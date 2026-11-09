"""app.core.background.spawn keeps fire-and-forget tasks alive and reports their failures."""
import asyncio
import gc
import logging

from app.core import background


def test_spawned_task_is_held_until_it_finishes():
    async def scenario():
        finished = asyncio.Event()

        async def work():
            await asyncio.sleep(0.05)
            finished.set()

        background.spawn(work(), "held")
        gc.collect()  # a task held only weakly could be collected here
        assert len(background._running) == 1
        await asyncio.wait_for(finished.wait(), timeout=1)
        await asyncio.sleep(0)  # let the done callback run
        assert not background._running

    asyncio.run(scenario())


def test_spawned_task_failure_is_logged(caplog):
    async def scenario():
        async def broken():
            raise RuntimeError("boom")

        task = background.spawn(broken(), "broken_job")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="app.core.background"):
        asyncio.run(scenario())
    assert "broken_job" in caplog.text and "boom" in caplog.text
