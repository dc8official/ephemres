"""A sign-in flood must not starve the shared thread pool (scale review S3).

scrypt ran in asyncio's default thread pool, and its concurrency limit (_SCRYPT_SLOTS) was taken
inside the pool threads. In a flood most pool threads sat waiting for a slot, and everything else
that uses the pool waited behind them: every FastStore command, PDF rendering, SMTP. Measured on
a test server, FastStore took 3.6 s to answer during a 32-way sign-in flood.
"""
import asyncio
import time

from app.services import auth_service


class _SlowHasher:
    """Stands in for scrypt: 0.2 s of work per check, like a real one on a small server."""

    def verify(self, hashed, plain):
        time.sleep(0.2)
        return False

    def hash(self, password):
        time.sleep(0.2)
        return "$scrypt$fake"


def test_sign_in_flood_leaves_the_default_pool_free(monkeypatch):
    monkeypatch.setattr(auth_service, "_ph", _SlowHasher())

    async def scenario():
        flood = [asyncio.create_task(auth_service.verify_password_async("guess", "$scrypt$x")) for _ in range(24)]
        await asyncio.sleep(0.05)  # let the flood reach the thread pool
        started = time.monotonic()
        await asyncio.to_thread(lambda: None)  # what every FastStore command does
        waited = time.monotonic() - started
        await asyncio.gather(*flood)
        return waited

    waited = asyncio.run(scenario())
    assert waited < 0.2, f"the default thread pool was busy for {waited:.2f} s"


def test_scrypt_still_runs_a_bounded_number_at_once(monkeypatch):
    """The flood is queued for scrypt's own threads: never more than the slot count at once."""
    running = 0
    peak = 0

    class _CountingHasher(_SlowHasher):
        def verify(self, hashed, plain):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            time.sleep(0.05)
            running -= 1
            return False

    monkeypatch.setattr(auth_service, "_ph", _CountingHasher())

    async def scenario():
        await asyncio.gather(*(auth_service.verify_password_async("guess", "$scrypt$x") for _ in range(16)))

    asyncio.run(scenario())
    assert 1 <= peak <= auth_service.SCRYPT_CONCURRENCY
