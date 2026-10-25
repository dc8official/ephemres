from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID

from app.core.orm import delete, func, select
from app.core.orm import AsyncSession

from app.database import AsyncSessionLocal
from app.models.user_session import UserSession

logger = logging.getLogger(__name__)


class SessionStore(ABC):
    """Abstract interface for user session management and concurrency control."""

    @abstractmethod
    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
    ) -> None:
        pass

    @abstractmethod
    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        pass

    @abstractmethod
    async def invalidate_session(self, user_id: str, jti: Optional[str]) -> None:
        pass

    @abstractmethod
    async def invalidate_all_user_sessions(self, user_id: str, except_jti: Optional[str] = None) -> None:
        pass

    @abstractmethod
    async def record_failed_attempt(
        self, client_ip: str, username: str, max_attempts: Optional[int] = None, lockout_seconds: int = 900
    ) -> None:
        pass

    @abstractmethod
    async def is_account_locked(self, client_ip: str, username: str) -> bool:
        pass

    @abstractmethod
    async def clear_failed_attempts(self, client_ip: str, username: str) -> None:
        pass


class PostgresSessionStore(SessionStore):
    """
    PostgreSQL-native session driver using user_sessions table
    with indexed TTL auto-cleanup and FIFO eviction.
    """

    def __init__(self, session_factory=AsyncSessionLocal) -> None:
        self.session_factory = session_factory

    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
    ) -> None:
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        async with self.session_factory() as db:
            try:
                # 1. Clean up globally expired sessions for this user
                await db.execute(
                    delete(UserSession).where(
                        UserSession.user_id == u_uuid,
                        UserSession.expires_at <= now,
                    )
                )

                # 2. Insert new session
                new_session = UserSession(
                    user_id=u_uuid,
                    jti=jti,
                    created_at=now,
                    expires_at=expires_at,
                )
                db.add(new_session)
                await db.flush()

                # 3. FIFO session eviction if count > max_sessions
                stmt = (
                    select(UserSession.id)
                    .where(
                        UserSession.user_id == u_uuid,
                        UserSession.expires_at > now,
                    )
                    .order_by(UserSession.created_at.desc())
                )
                res = await db.execute(stmt)
                active_ids = []
                if hasattr(res, "scalars") and hasattr(res.scalars(), "all"):
                    scalars_res = res.scalars().all()
                    if hasattr(scalars_res, "__iter__"):
                        active_ids = list(scalars_res)
                elif hasattr(res, "fetchall"):
                    rows = res.fetchall()
                    if hasattr(rows, "__iter__"):
                        active_ids = [
                            row[0] if isinstance(row, (tuple, list)) else getattr(row, "id", row)
                            for row in rows
                        ]

                if len(active_ids) > max_sessions:
                    excess_ids = active_ids[max_sessions:]
                    await db.execute(
                        delete(UserSession).where(UserSession.id.in_(excess_ids))
                    )

                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.register_session error: %s", e)

    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        if not jti:
            return False
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return False

        now = datetime.now(timezone.utc)
        async with self.session_factory() as db:
            try:
                stmt = select(UserSession.id).where(
                    UserSession.user_id == u_uuid,
                    UserSession.jti == jti,
                    UserSession.expires_at > now,
                ).limit(1)
                res = await db.execute(stmt)
                row = res.scalar_one_or_none() if hasattr(res, "scalar_one_or_none") else (res.fetchone() if hasattr(res, "fetchone") else None)
                return row is not None
            except Exception as e:
                logger.error("PostgresSessionStore.is_session_active error: %s", e)
                return False

    async def invalidate_session(self, user_id: str, jti: Optional[str]) -> None:
        if not jti:
            return
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        async with self.session_factory() as db:
            try:
                await db.execute(
                    delete(UserSession).where(
                        UserSession.user_id == u_uuid,
                        UserSession.jti == jti,
                    )
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.invalidate_session error: %s", e)

    async def invalidate_all_user_sessions(self, user_id: str, except_jti: Optional[str] = None) -> None:
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        async with self.session_factory() as db:
            try:
                stmt = delete(UserSession).where(UserSession.user_id == u_uuid)
                if except_jti:
                    stmt = stmt.where(UserSession.jti != except_jti)
                await db.execute(stmt)
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.invalidate_all_user_sessions error: %s", e)

    async def record_failed_attempt(
        self, client_ip: str, username: str, max_attempts: Optional[int] = None, lockout_seconds: int = 900
    ) -> None:
        from app.services.auth_service import record_failed_attempt as sync_record
        sync_record(client_ip, username)

    async def is_account_locked(self, client_ip: str, username: str) -> bool:
        from app.services.auth_service import is_account_locked as sync_is_locked
        return sync_is_locked(client_ip, username)

    async def clear_failed_attempts(self, client_ip: str, username: str) -> None:
        from app.services.auth_service import clear_failed_attempts as sync_clear
        sync_clear(client_ip, username)


class FastStoreSessionStore(SessionStore):
    """
    FastStore-accelerated session driver using FastStoreClient with key TTL auto-expiration.
    """

    def __init__(self, store_client: Any = None, redis_client: Any = None) -> None:
        self.store = store_client if store_client is not None else redis_client

    @property
    def redis(self) -> Any:
        return self.store

    def _get_key(self, user_id: str) -> str:
        return f"user_sessions:{user_id}"

    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
    ) -> None:
        key = self._get_key(user_id)
        try:
            raw = await self.store.get(key)
            sessions: List[str] = json.loads(raw) if raw else []
            sessions.append(jti)
            if len(sessions) > max_sessions:
                sessions = sessions[-max_sessions:]
            await self.store.set(key, json.dumps(sessions), ex=ttl_seconds)
        except Exception as e:
            logger.error("FastStoreSessionStore.register_session error: %s", e)
            raise

    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        if not jti:
            return False
        key = self._get_key(user_id)
        try:
            raw = await self.store.get(key)
            if not raw:
                return False
            sessions: List[str] = json.loads(raw)
            return jti in sessions
        except Exception as e:
            logger.error("FastStoreSessionStore.is_session_active error: %s", e)
            return False

    async def invalidate_session(self, user_id: str, jti: Optional[str]) -> None:
        if not jti:
            return
        key = self._get_key(user_id)
        try:
            raw = await self.store.get(key)
            if raw:
                sessions: List[str] = [s for s in json.loads(raw) if s != jti]
                if sessions:
                    ttl = await self.store.ttl(key)
                    ex = max(ttl, 60) if ttl > 0 else 86400
                    await self.store.set(key, json.dumps(sessions), ex=ex)
                else:
                    await self.store.delete(key)
        except Exception as e:
            logger.error("FastStoreSessionStore.invalidate_session error: %s", e)
            raise

    async def invalidate_all_user_sessions(self, user_id: str, except_jti: Optional[str] = None) -> None:
        key = self._get_key(user_id)
        try:
            if except_jti:
                raw = await self.store.get(key)
                if raw:
                    sessions: List[str] = [s for s in json.loads(raw) if s == except_jti]
                    if sessions:
                        ttl = await self.store.ttl(key)
                        ex = max(ttl, 60) if ttl > 0 else 86400
                        await self.store.set(key, json.dumps(sessions), ex=ex)
                    else:
                        await self.store.delete(key)
            else:
                await self.store.delete(key)
        except Exception as e:
            logger.error("FastStoreSessionStore.invalidate_all_user_sessions error: %s", e)
            raise

    def _get_lockout_keys(self, client_ip: str, username: str) -> tuple[str, str]:
        ip = client_ip.strip() if client_ip else "127.0.0.1"
        user = username.strip().lower() if username else ""
        return f"auth:failed:{ip}:{user}", f"auth:locked:{ip}:{user}"

    async def record_failed_attempt(
        self, client_ip: str, username: str, max_attempts: Optional[int] = None, lockout_seconds: int = 900
    ) -> None:
        if max_attempts is None:
            from app.services.runtime_settings import get_runtime_settings

            max_attempts = get_runtime_settings().lockout_threshold
        failed_key, locked_key = self._get_lockout_keys(client_ip, username)
        try:
            count = await self.store.incr(failed_key)
            if count == 1:
                await self.store.expire(failed_key, 1800)
            if count >= max_attempts:
                await self.store.set(locked_key, "1", ex=lockout_seconds)
        except Exception as e:
            logger.error("FastStoreSessionStore.record_failed_attempt error: %s", e)

    async def is_account_locked(self, client_ip: str, username: str) -> bool:
        _, locked_key = self._get_lockout_keys(client_ip, username)
        try:
            res = await self.store.exists(locked_key)
            return bool(res)
        except Exception as e:
            logger.error("FastStoreSessionStore.is_account_locked error: %s", e)
            return False

    async def clear_failed_attempts(self, client_ip: str, username: str) -> None:
        failed_key, locked_key = self._get_lockout_keys(client_ip, username)
        try:
            await self.store.delete(failed_key, locked_key)
        except Exception as e:
            logger.error("FastStoreSessionStore.clear_failed_attempts error: %s", e)


RedisSessionStore = FastStoreSessionStore


async def migrate_sessions_pg_to_faststore(session_factory, store_client) -> int:
    """
    Warm-migrates active unexpired user sessions from PostgreSQL to FastStore.
    Preserves existing session lifetimes so active users remain authenticated across driver switches.
    """
    now = datetime.now(timezone.utc)
    count = 0
    try:
        async with session_factory() as db:
            stmt = (
                select(UserSession)
                .where(UserSession.expires_at > now)
                .order_by(UserSession.created_at.asc())
            )
            res = await db.execute(stmt)
            sessions = res.scalars().all()

            user_map: dict[str, list[UserSession]] = {}
            for s in sessions:
                u_id = str(s.user_id)
                user_map.setdefault(u_id, []).append(s)

            for u_id, sess_list in user_map.items():
                key = f"user_sessions:{u_id}"
                jtis = [s.jti for s in sess_list if s.jti]
                if not jtis:
                    continue
                max_exp = max(s.expires_at for s in sess_list)
                ttl = int((max_exp - now).total_seconds())
                if ttl > 0:
                    await store_client.set(key, json.dumps(jtis), ex=ttl)
                    count += len(jtis)
        logger.info(
            "Warm session migration: Migrated %d active session(s) from PostgreSQL to FastStore.",
            count,
        )
    except Exception as exc:
        logger.warning(
            "Warm session migration (PostgreSQL -> FastStore) encountered an error: %s",
            exc,
        )
    return count


async def migrate_sessions_faststore_to_pg(store_client, session_factory) -> int:
    """
    Warm-migrates active user sessions from FastStore into PostgreSQL user_sessions.
    Ensures active sessions remain valid during FastStore-to-PostgreSQL fallback or driver switch.
    """
    now = datetime.now(timezone.utc)
    count = 0
    try:
        async with session_factory() as db:
            async for key in store_client.scan_iter(match="user_sessions:*"):
                key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                parts = key_str.split(":", 1)
                if len(parts) != 2:
                    continue
                user_id_str = parts[1]
                try:
                    u_uuid = UUID(user_id_str)
                except (ValueError, TypeError):
                    continue

                raw = await store_client.get(key)
                if not raw:
                    continue
                jtis = json.loads(raw)
                if not isinstance(jtis, list):
                    continue

                ttl = await store_client.ttl(key)
                ttl_sec = ttl if ttl > 0 else 86400
                exp = now + timedelta(seconds=ttl_sec)

                for jti in jtis:
                    if not jti:
                        continue
                    stmt = select(UserSession.id).where(
                        UserSession.user_id == u_uuid,
                        UserSession.jti == jti,
                        UserSession.expires_at > now,
                    ).limit(1)
                    res = await db.execute(stmt)
                    if res.scalar_one_or_none() is None:
                        new_sess = UserSession(
                            user_id=u_uuid,
                            jti=jti,
                            created_at=now,
                            expires_at=exp,
                        )
                        db.add(new_sess)
                        count += 1
            await db.commit()
        logger.info(
            "Warm session migration: Migrated %d active session(s) from FastStore to PostgreSQL.",
            count,
        )
    except Exception as exc:
        logger.warning(
            "Warm session migration (FastStore -> PostgreSQL) encountered an error: %s",
            exc,
        )
    return count


migrate_sessions_pg_to_redis = migrate_sessions_pg_to_faststore
migrate_sessions_redis_to_pg = migrate_sessions_faststore_to_pg

