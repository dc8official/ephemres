import asyncio
import sqlite3
import time
import pytest
from app.core.faststore import FastStoreClient, FastStoreError


@pytest.mark.anyio
async def test_faststore_lock_does_not_block_event_loop():
    client = FastStoreClient(db=14, decode_responses=True)
    await client.flushdb()

    # Second connection to hold a lock
    conn2 = sqlite3.connect(client._store.db_path, timeout=30.0, isolation_level=None)
    conn2.execute("BEGIN IMMEDIATE")

    slept_count = 0
    stop_ticker = False

    async def ticker():
        nonlocal slept_count
        while not stop_ticker:
            await asyncio.sleep(0.01)
            slept_count += 1

    ticker_task = asyncio.create_task(ticker())

    async def release_lock_later():
        await asyncio.sleep(0.2)
        conn2.execute("COMMIT")
        conn2.close()

    release_task = asyncio.create_task(release_lock_later())

    # This write will be blocked in the worker thread until conn2 commits after 0.2s
    t0 = time.time()
    await client.set("key_blocked", "val_blocked")
    duration = time.time() - t0

    stop_ticker = True
    await ticker_task
    await release_task

    # Verify that the operation actually waited for the lock
    assert duration >= 0.15
    # Verify that the event loop kept spinning while the SQLite operation was blocked
    assert slept_count >= 5
    # Verify value was set
    assert await client.get("key_blocked") == "val_blocked"


@pytest.mark.anyio
async def test_faststore_pipeline_single_thread_transaction():
    client = FastStoreClient(db=14, decode_responses=True)
    await client.flushdb()

    pipe = client.pipeline(transaction=True)
    pipe.set("k1", "v1").set("k2", "v2").incr("counter", 5).get("k1").get("counter")
    res = await pipe.execute()
    assert res == [True, True, 5, "v1", "5"]

    # Test error rollback in pipeline transaction
    pipe_err = client.pipeline(transaction=True)
    pipe_err.set("should_not_exist", "val")
    # Non-existent op or trigger error
    pipe_err._ops.append(("non_existent_op", (), {}))
    with pytest.raises(FastStoreError):
        await pipe_err.execute()

    assert await client.get("should_not_exist") is None
