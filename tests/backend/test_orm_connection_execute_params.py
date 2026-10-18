import pytest
from unittest.mock import AsyncMock
from app.core.orm import (
    DeclarativeBase,
    Column,
    String,
    update,
    select,
    delete,
    AsyncConnection,
)


class DummyModel(DeclarativeBase):
    __tablename__ = "dummy_models"
    id = Column(String, primary_key=True)
    name = Column(String)


@pytest.mark.anyio
async def test_async_connection_execute_passes_bind_params():
    fake_conn = AsyncMock()
    fake_conn.execute.return_value = "UPDATE 1"
    fake_conn.fetch.return_value = []

    conn = AsyncConnection(conn=fake_conn)

    # 1. Test update statement (non-row-returning)
    stmt_update = update(DummyModel).where(DummyModel.id == "x").values(name="y")
    res = await conn.execute(stmt_update)
    assert res.rowcount == 1
    assert fake_conn.execute.called
    call_args = fake_conn.execute.call_args[0]
    sql, params = call_args[0], call_args[1:]
    assert "UPDATE" in sql
    assert params == ("y", "x")

    # 2. Test select statement (row-returning)
    stmt_select = select(DummyModel).where(DummyModel.id == "target_id")
    await conn.execute(stmt_select)
    assert fake_conn.fetch.called
    fetch_call_args = fake_conn.fetch.call_args[0]
    fetch_sql, fetch_params = fetch_call_args[0], fetch_call_args[1:]
    assert "SELECT" in fetch_sql
    assert fetch_params == ("target_id",)

    # 3. Test delete statement
    fake_conn.execute.return_value = "DELETE 2"
    stmt_del = delete(DummyModel).where(DummyModel.id == "del_id")
    res_del = await conn.execute(stmt_del)
    assert res_del.rowcount == 2
    del_call_args = fake_conn.execute.call_args[0]
    assert del_call_args[1:] == ("del_id",)
