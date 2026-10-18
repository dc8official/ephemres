import pytest
from unittest.mock import AsyncMock
from app.core.orm import (
    DeclarativeBase,
    Column,
    String,
    JSONB,
    AsyncSession,
    AsyncEngine,
    _snapshot_model,
)


class DummyUser(DeclarativeBase):
    __tablename__ = "dummy_users"
    id = Column(String, primary_key=True)
    name = Column(String)
    meta = Column(JSONB)


@pytest.mark.anyio
async def test_rollback_restores_identity_map_and_prevents_update():
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = "UPDATE 1"
    fake_conn.fetchrow.return_value = None

    fake_pool = AsyncMock()
    fake_pool.acquire.return_value = fake_conn

    engine = AsyncEngine("postgresql+asyncpg://user:pass@localhost:5432/db")
    engine._pool = fake_pool

    session = AsyncSession(bind=engine)

    # 1. Populate identity map simulating an already loaded entity
    user = DummyUser(id="u1", name="Original Name", meta={"theme": "dark"})
    user._orm_persisted = True
    user._orm_state = _snapshot_model(user)
    session._identity_map[(DummyUser, "u1")] = user

    # 2. Modify loaded object
    user.name = "Mutated Name"
    user.meta["theme"] = "light"
    assert user in session.dirty

    # 3. Rollback
    await session.rollback()

    # 4. Verify attributes restored
    assert user.name == "Original Name"
    assert user.meta == {"theme": "dark"}
    assert user not in session.dirty

    # 5. Flush and assert no UPDATE was issued
    await session.flush()
    assert not fake_conn.fetchrow.called
    assert not fake_conn.execute.called
