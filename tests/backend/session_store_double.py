"""In-memory SessionStore for unit tests that run without a database.

The API treats the session store as the only authority for sessions and sign-in lockouts (no
per-worker fallback), so tests that exercise login, logout or session rotation need a working
store. This double follows the shared stores' rules: one list of session ids per user with
FIFO eviction and no duplicates, and lockout counters keyed by "<ip>:<username>".

Use it with `patch.object(driver_manager, "get_session_store", return_value=store)`; several
instances behave like separate processes only if they share `state`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from app.services.session_store import SessionStore


class SharedSessionState:
    def __init__(self) -> None:
        self.sessions: Dict[str, List[str]] = {}
        self.failures: Dict[str, int] = {}
        self.locked_until: Dict[str, float] = {}


class InMemorySessionStore(SessionStore):
    def __init__(self, state: Optional[SharedSessionState] = None, lockout_threshold: int = 5) -> None:
        self.state = state or SharedSessionState()
        self.lockout_threshold = lockout_threshold

    async def register_session(self, user_id, jti, max_sessions=2, ttl_seconds=86400) -> None:
        sessions = self.state.sessions.setdefault(str(user_id), [])
        if jti not in sessions:
            sessions.append(jti)
        del sessions[:-max_sessions]

    async def is_session_active(self, user_id, jti) -> bool:
        return bool(jti) and jti in self.state.sessions.get(str(user_id), [])

    async def invalidate_session(self, user_id, jti) -> None:
        sessions = self.state.sessions.get(str(user_id), [])
        if jti in sessions:
            sessions.remove(jti)

    async def invalidate_all_user_sessions(self, user_id, except_jti=None) -> None:
        sessions = self.state.sessions.get(str(user_id), [])
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


