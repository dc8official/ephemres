import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.core.asgi import EphemresApp, TestClient, Depends, Request, Response
from app.core.faststore import FastStoreClient
from app.routers.auth import login, get_current_user, LoginRequest, cookie_name
from app.services.driver_manager import driver_manager
from app.services.session_store import FastStoreSessionStore
from app.services.auth_service import (
    register_session_async,
    invalidate_session_async,
    invalidate_all_user_sessions_async,
    hash_password,
    _active_user_sessions,
)


@pytest.fixture(autouse=True)
def setup_faststore_session_store():
    # Configure driver_manager to use FastStoreSessionStore
    faststore_client = FastStoreClient()
    driver_manager._session_store = FastStoreSessionStore(faststore_client)
    _active_user_sessions.clear()
    yield
    _active_user_sessions.clear()


@pytest.mark.anyio
async def test_session_async_methods_lifecycle():
    user_id = str(uuid4())
    jti_1 = "sess-1"
    jti_2 = "sess-2"
    store = driver_manager.get_session_store()

    # 1. Register session 1
    await register_session_async(user_id, jti_1, max_sessions=2)
    assert await store.is_session_active(user_id, jti_1) is True
    assert jti_1 in _active_user_sessions.get(user_id, [])

    # 2. Register session 2
    await register_session_async(user_id, jti_2, max_sessions=2)
    assert await store.is_session_active(user_id, jti_2) is True
    assert len(_active_user_sessions.get(user_id, [])) == 2

    # 3. Invalidate session 1
    await invalidate_session_async(user_id, jti_1)
    assert await store.is_session_active(user_id, jti_1) is False
    assert await store.is_session_active(user_id, jti_2) is True
    assert jti_1 not in _active_user_sessions.get(user_id, [])

    # 4. Invalidate all sessions
    await invalidate_all_user_sessions_async(user_id)
    assert await store.is_session_active(user_id, jti_2) is False
    assert user_id not in _active_user_sessions


@pytest.mark.anyio
async def test_invalidate_all_user_sessions_async_with_except_jti():
    user_id = str(uuid4())
    jti_keep = "current-session"
    jti_drop = "other-session"
    store = driver_manager.get_session_store()

    await register_session_async(user_id, jti_keep, max_sessions=2)
    await register_session_async(user_id, jti_drop, max_sessions=2)

    assert await store.is_session_active(user_id, jti_keep) is True
    assert await store.is_session_active(user_id, jti_drop) is True

    # Invalidate all except jti_keep
    await invalidate_all_user_sessions_async(user_id, except_jti=jti_keep)

    assert await store.is_session_active(user_id, jti_keep) is True
    assert await store.is_session_active(user_id, jti_drop) is False
    assert _active_user_sessions.get(user_id) == [jti_keep]


def test_login_then_immediate_authenticated_request_200():
    app = EphemresApp()
    user_id = uuid4()
    plain_pass = "TestSecret123!"
    hashed_pass = hash_password(plain_pass)

    user_row = MagicMock()
    user_row.id = user_id
    user_row.username = "testuser"
    user_row.password_hash = hashed_pass
    user_row.is_active = True
    user_row.must_change_password = False
    user_row.role_name = "OPERATOR"

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    db_res = MagicMock()
    db_res.scalar_one_or_none.return_value = user_row
    db_res.fetchone.return_value = user_row
    mock_db.execute.return_value = db_res

    async def get_test_db():
        yield mock_db

    @app.post("/auth/login")
    async def route_login(payload: LoginRequest, response: Response, request: Request, db=Depends(get_test_db)):
        return await login(payload=payload, response=response, http_request=request, db=db)

    @app.get("/api/protected")
    async def route_protected(current_user: dict = Depends(get_current_user)):
        return {"status": "ok", "user": current_user["username"]}

    # Override get_db dependency inside get_current_user
    from app.database import get_db
    app.dependency_overrides[get_db] = get_test_db

    client = TestClient(app)

    # Step 1: Login
    login_resp = client.post(
        "/auth/login",
        json={"username": "testuser", "password": plain_pass},
    )
    assert login_resp.status_code == 200

    # Step 2: Immediately call protected endpoint (cookie is automatically retained by client)
    prot_resp = client.get("/api/protected")
    assert prot_resp.status_code == 200
    assert prot_resp.json() == {"status": "ok", "user": "testuser"}
