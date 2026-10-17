import asyncio
import pytest
from app.core.faststore import FastStoreClient, _PUBSUB_WAITERS


@pytest.mark.anyio
async def test_faststore_set_expiration_evicts_set_members():
    client = FastStoreClient(db=9, decode_responses=True)
    await client.flushdb()
    await client.sadd("flow:exporter:1:interfaces", "10", "20")
    await client.expire("flow:exporter:1:interfaces", 1)

    # Set key should NOT pollute string kv_store with empty string before expiry
    assert await client.get("flow:exporter:1:interfaces") is None

    # Members should be present before expiration
    assert await client.smembers("flow:exporter:1:interfaces") == {"10", "20"}

    # After expiration, members must be completely evicted
    await asyncio.sleep(1.1)
    assert await client.smembers("flow:exporter:1:interfaces") == set()
    assert await client.exists("flow:exporter:1:interfaces") == 0


@pytest.mark.anyio
async def test_faststore_ping_and_pipeline():
    client = FastStoreClient(db=9, decode_responses=True)
    await client.flushdb()

    # Ping real DB execution
    assert await client.ping() is True

    # Pipeline under single transaction
    pipe = client.pipeline(transaction=True)
    pipe.set("a", "1").set("b", "2").get("a").get("b")
    results = await pipe.execute()
    assert results == [True, True, "1", "2"]
    assert await client.get("a") == "1"
    assert await client.get("b") == "2"


@pytest.mark.anyio
async def test_faststore_xreadgroup_blocking():
    client = FastStoreClient(db=9, decode_responses=True)
    await client.flushdb()
    await client.xgroup_create("mystream", "mygroup", id="$", mkstream=True)

    # xreadgroup with block should wait and receive new entries added concurrently
    async def delayed_add():
        await asyncio.sleep(0.1)
        await client.xadd("mystream", {"k": "v"})

    asyncio.create_task(delayed_add())

    # Should block and receive the entry rather than returning immediately empty
    entries = await client.xreadgroup("mygroup", "c1", {"mystream": ">"}, count=1, block=500)
    assert len(entries) == 1
    assert entries[0][0] == "mystream"
    assert len(entries[0][1]) == 1
    assert entries[0][1][0][1] == {"k": "v"}
