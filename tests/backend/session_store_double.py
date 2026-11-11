"""In-memory SessionStore for unit tests that run without a database.

The API treats the session store as the only authority for sessions and sign-in lockouts (no
per-worker fallback), so tests that exercise login, logout or session rotation need a working
store. This double follows the shared stores' rules: one list of session ids per user with
FIFO eviction and no duplicates, and lockout counters keyed by "<ip>:<username>".

It also records why a session ended (`ended`, as the PostgreSQL store's ended_sessions table
does) and counts touch_session calls; idle and absolute timeouts are not simulated here (the
PostgreSQL store is tested for those in tests/backend/test_sessions_v336.py and
tests/integration/test_sessions_idle.py).

Use it with `patch.object(driver_manager, "get_session_store", return_value=store)`; several
instances behave like separate processes only if they share `state`.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Dict, List, Optional

from app.services import session_policy
from app.services.session_store import SessionStore


class SharedSessionState:
    def __init__(self) -> None:
        self.sessions: Dict[str, List[str]] = {}
        self.ended: Dict[str, tuple] = {}
        self.touches: Dict[str, int] = {}
        self.devices: Dict[str, tuple] = {}
        self.failures: Dict[str, int] = {}
        self.locked_until: Dict[str, float] = {}


class InMemorySessionStore(SessionStore):
    def __init__(self, state: Optional[SharedSessionState] = None, lockout_threshold: int = 5) -> None:
        self.state = state or SharedSessionState()
        self.lockout_threshold = lockout_threshold

    async def register_session(
        self, user_id, jti, max_sessions=2, ttl_seconds=86400, client_ip=None, user_agent=None
    ) -> None:
        self.last_ttl_seconds = ttl_seconds
        self.state.devices[jti] = (client_ip, user_agent)
        sessions = self.state.sessions.setdefault(str(user_id), [])
        if jti not in sessions:
            sessions.append(jti)
        for evicted in sessions[:-max_sessions]:
            self.state.ended[evicted] = (session_policy.SIGNED_IN_ELSEWHERE, None)
        del sessions[:-max_sessions]

    async def is_session_active(self, user_id, jti) -> bool:
        return bool(jti) and jti in self.state.sessions.get(str(user_id), [])

    async def touch_session(self, user_id, jti) -> None:
        if await self.is_session_active(user_id, jti):
            self.state.touches[jti] = self.state.touches.get(jti, 0) + 1

    async def session_end_reason(self, user_id, jti):
        return self.state.ended.get(jti)

    # Row ids are a UUID derived from the jti (row_id_of); times are not simulated.
    @staticmethod
    def row_id_of(jti) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, str(jti)))

    async def list_sessions(self, user_id):
        return [
            {
                "id": self.row_id_of(jti), "jti": jti, "created_at": None, "last_activity_at": None,
                "ends_at": None, "client_ip": self.state.devices.get(jti, (None, None))[0],
                "user_agent": self.state.devices.get(jti, (None, None))[1],
            }
            for jti in reversed(self.state.sessions.get(str(user_id), []))
        ]

    async def active_session_counts(self):
        return {uid: len(jtis) for uid, jtis in self.state.sessions.items() if jtis}

    async def end_session_by_id(self, user_id, session_id, reason):
        jti = next((j for j in self.state.sessions.get(str(user_id), []) if self.row_id_of(j) == str(session_id)), None)
        if jti is None:
            return False
        await self.invalidate_session(user_id, jti, reason=reason)
        return True

    async def invalidate_session(self, user_id, jti, reason=session_policy.SIGNED_OUT) -> None:
        sessions = self.state.sessions.get(str(user_id), [])
        if jti in sessions:
            sessions.remove(jti)
            self.state.ended[jti] = (reason, None)

    async def invalidate_all_user_sessions(self, user_id, except_jti=None, reason=session_policy.SIGNED_OUT) -> None:
        sessions = self.state.sessions.get(str(user_id), [])
        for s in sessions:
            if not (except_jti and s == except_jti):
                self.state.ended[s] = (reason, None)
        sessions[:] = [s for s in sessions if except_jti and s == except_jti]

    @staticmethod
    def _key(client_ip: str, username: str) -> str:
        return f"{(client_ip or '').strip()}:{(username or '').strip().lower()}"

    async def record_failed_attempt(self, client_ip, username, max_attempts=None, lockout_seconds=900) -> None:
        key = self._key(client_ip, username)
        count = self.state.failures.get(key, 0) + 1
        self.state.failures[key] = count
        if count >= (max_attempts or self.lockout_threshold):
            self.state.locked_until[key] = time.time() + lockout_seconds

    async def is_account_locked(self, client_ip, username) -> bool:
        return self.state.locked_until.get(self._key(client_ip, username), 0) > time.time()

    async def clear_failed_attempts(self, client_ip, username) -> None:
        key = self._key(client_ip, username)
        self.state.failures.pop(key, None)
        self.state.locked_until.pop(key, None)


