import pytest
from typing import Any
from unittest.mock import AsyncMock

from app.core.asgi import EphemresApp, TestClient, Depends, HTTPException
from app.core.orm import AsyncEngine, AsyncSession


class RecordingSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.is_active = True
        self._in_tx = True

    def in_transaction(self) -> bool:
        return self._in_tx

    async def commit(self) -> None:
        self.commits += 1
        self._in_tx = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._in_tx = False

    async def close(self) -> None:
        self.is_active = False


def test_asgi_tx_semantics_commit_and_rollback():
    app = EphemresApp()
    current_session = None

    async def get_test_db():
        nonlocal current_session
        current_session = RecordingSession()
        session = current_session
        try:
            yield session
            if session.is_active and session.in_transaction():
                await session.commit()
        except Exception:
            if session.is_active and session.in_transaction():
                await session.rollback()
            raise
        finally:
            await session.close()

    @app.post("/write-and-fail-http")
    async def write_fail_http(db: Any = Depends(get_test_db)):
        raise HTTPException(status_code=400, detail="Client Error")

    @app.post("/write-and-fail-runtime")
    async def write_fail_runtime(db: Any = Depends(get_test_db)):
        raise RuntimeError("Internal crash")

    @app.post("/write-and-succeed")
    async def write_succeed(db: Any = Depends(get_test_db)):
        return {"status": "ok"}

    client = TestClient(app)

    # 1. Route writes then raises HTTPException(400) -> rollback called, commit not called, response 400
    resp_http = client.post("/write-and-fail-http")
    assert resp_http.status_code == 400
    assert resp_http.json() == {"detail": "Client Error"}
    assert current_session.rollbacks == 1
    assert current_session.commits == 0
    assert not current_session.is_active

    # 2. Route raises RuntimeError -> rollback called, commit not called, response 500
    resp_runtime = client.post("/write-and-fail-runtime")
    assert resp_runtime.status_code == 500
    assert resp_runtime.json() == {"detail": "Internal Server Error"}
    assert current_session.rollbacks == 1
    assert current_session.commits == 0
    assert not current_session.is_active

    # 3. Route succeeds -> commit called once, rollback not called, response 200
    resp_ok = client.post("/write-and-succeed")
    assert resp_ok.status_code == 200
    assert resp_ok.json() == {"status": "ok"}
    assert current_session.commits == 1
    assert current_session.rollbacks == 0
    assert not current_session.is_active


@pytest.mark.anyio
async def test_async_session_aexit_rolls_back_uncommitted_tx():
    mock_conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_conn.transaction.return_value = mock_tx
    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = mock_conn

    engine = AsyncEngine("postgresql://mock/db")
    engine._pool = mock_pool

    session = AsyncSession(bind=engine)
    async with session:
        await session._get_conn()
        assert session.in_transaction()

    # On clean exit without explicit commit, uncommitted tx is rolled back
    assert mock_tx.rollback.called
    assert not mock_tx.commit.called
