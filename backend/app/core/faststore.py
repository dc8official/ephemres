from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse
import weakref


logger = logging.getLogger(__name__)

_WARNED_PARAMS = False


class FastStoreError(Exception):
    pass


RedisError = FastStoreError


class ConnectionError(FastStoreError):
    pass


class TimeoutError(FastStoreError):
    pass


class ResponseError(FastStoreError):
    pass


_DB_LOCK = threading.RLock()
_PUBSUB_WAITERS: Dict[str, weakref.WeakSet[asyncio.Queue]] = {}


def _get_faststore_dir() -> str:
    env_dir = os.environ.get("EPHEMRES_FASTSTORE_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir
    if os.path.isdir("/run/ephemres") and os.access("/run/ephemres", os.W_OK):
        return "/run/ephemres"
    if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        return "/dev/shm"
    return "/tmp"


def _get_db_path(db: int = 0) -> str:
    base_dir = _get_faststore_dir()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(base_dir, f"ephemres_faststore_u{uid}_db{int(db)}.sqlite3")


class _SharedSQLiteStore:
    _instances: Dict[int, "_SharedSQLiteStore"] = {}

    @classmethod
    def get(cls, db: int = 0) -> "_SharedSQLiteStore":
        with _DB_LOCK:
            if db not in cls._instances:
                cls._instances[db] = cls(db)
            return cls._instances[db]

    def __init__(self, db: int = 0) -> None:
        self.db_num = int(db)
        self.db_path = _get_db_path(self.db_num)

        for ext in ("", "-wal", "-shm"):
            p = f"{self.db_path}{ext}"
            if os.path.exists(p):
                st = os.stat(p)
                if hasattr(os, "getuid") and st.st_uid != os.getuid():
                    try:
                        os.remove(p)
                    except OSError:
                        raise FastStoreError(
                            f"FastStore file '{p}' is owned by UID {st.st_uid}, but current UID is {os.getuid()}."
                        )
                if ext == "" and (st.st_mode & 0o022):
                    raise FastStoreError(
                        f"FastStore database file '{p}' has insecure permissions ({oct(st.st_mode)}): group or other write allowed."
                    )

        try:
            self._open_connection()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning(
                "FastStore database at '%s' encountered initialization error (%s). Purging stale database and companion WAL/SHM files and retrying...",
                self.db_path,
                exc,
            )
            self._purge_companion_files()
            self._open_connection()

    def _purge_companion_files(self) -> None:
        if hasattr(self, "_conn") and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        for ext in ("", "-wal", "-shm"):
            p = f"{self.db_path}{ext}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _open_connection(self) -> None:
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,
            )
        finally:
            os.umask(old_umask)

        if os.path.exists(self.db_path):
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

        self._init_schema()

        for ext in ("-wal", "-shm"):
            extra_path = f"{self.db_path}{ext}"
            if os.path.exists(extra_path):
                try:
                    os.chmod(extra_path, 0o600)
                except OSError:
                    pass

    def _init_schema(self) -> None:
        with _DB_LOCK:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA wal_autocheckpoint=1000;")
            cur.execute("PRAGMA journal_size_limit=16777216;")
            cur.execute("PRAGMA synchronous=OFF;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute("PRAGMA busy_timeout=10000;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    k TEXT PRIMARY KEY,
                    v BLOB,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS key_expiry (
                    k TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS set_store (
                    k TEXT,
                    member BLOB,
                    PRIMARY KEY (k, member)
                );
                CREATE TABLE IF NOT EXISTS zset_store (
                    k TEXT,
                    member BLOB,
                    score REAL,
                    PRIMARY KEY (k, member)
                );
                CREATE TABLE IF NOT EXISTS stream_entries (
                    stream TEXT,
                    id_ms INTEGER,
                    id_seq INTEGER,
                    entry_id TEXT,
                    fields_json TEXT,
                    PRIMARY KEY (stream, id_ms, id_seq)
                );
                CREATE TABLE IF NOT EXISTS stream_groups (
                    stream TEXT,
                    group_name TEXT,
                    last_ms INTEGER,
                    last_seq INTEGER,
                    PRIMARY KEY (stream, group_name)
                );
                CREATE TABLE IF NOT EXISTS stream_pel (
                    stream TEXT,
                    group_name TEXT,
                    entry_id TEXT,
                    consumer TEXT,
                    delivered_at REAL,
                    PRIMARY KEY (stream, group_name, entry_id)
                );
                CREATE TABLE IF NOT EXISTS pubsub_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT,
                    payload BLOB,
                    sender_pid INTEGER,
                    created_at REAL
                );
                """
            )
            cur.close()

    def execute_sync(self, fn, *args, **kwargs):
        with _DB_LOCK:
            return fn(self._conn, *args, **kwargs)

    async def execute(self, fn, *args, **kwargs):
        return await asyncio.to_thread(self.execute_sync, fn, *args, **kwargs)


def _to_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _encode_val(v: Any) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, (int, float)):
        return str(v).encode("utf-8")
    return str(v).encode("utf-8")


def _decode_val(b: Optional[bytes], decode_responses: bool) -> Any:
    if b is None:
        return None
    if isinstance(b, str):
        return b if decode_responses else b.encode("utf-8")
    return b.decode("utf-8", errors="replace") if decode_responses else b


def _parse_stream_id(id_str: Union[str, bytes]) -> Tuple[int, int]:
    s = id_str.decode("utf-8") if isinstance(id_str, bytes) else str(id_str).strip()
    if s in ("0", "0-0"):
        return (0, 0)
    if s == "$":
        return (int(time.time() * 1000), 999999)
    if "-" in s:
        p1, p2 = s.split("-", 1)
        return (int(p1), int(p2))
    return (int(s), 0)


def _parse_score_bound(val: Union[str, float, int], is_min: bool) -> Tuple[float, bool]:
    if isinstance(val, (int, float)):
        return (float(val), False)
    s = str(val).strip().lower()
    exclusive = False
    if s.startswith("("):
        exclusive = True
        s = s[1:].strip()
    if s in ("-inf", "-infinity"):
        return (float("-inf"), False)
    if s in ("+inf", "inf", "+infinity", "infinity"):
        return (float("inf"), False)
    return (float(s), exclusive)


class PubSub:
    def __init__(self, client: "FastStoreClient") -> None:
        self._client = client
        self._channels: Set[str] = set()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._last_log_id: int = self._get_latest_log_id()
        self._poll_task: Optional[asyncio.Task] = None

    def _get_latest_log_id(self) -> int:
        def _op(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM pubsub_log").fetchone()
            return int(row[0]) if row else 0

        return self._client._store.execute_sync(_op)

    def _ensure_poller(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._poll_task = loop.create_task(self._cross_process_poll_loop())
            except RuntimeError:
                pass

    async def _cross_process_poll_loop(self) -> None:
        my_pid = os.getpid()
        while not self._closed:
            try:
                if self._channels:
                    rows = await self._client._store.execute(
                        lambda conn: conn.execute(
                            "SELECT id, channel, payload, sender_pid FROM pubsub_log WHERE id > ? ORDER BY id ASC LIMIT 200",
                            (self._last_log_id,),
                        ).fetchall()
                    )
                    for row_id, ch, payload, sender_pid in rows:
                        self._last_log_id = max(self._last_log_id, int(row_id))
                        # Skip if published by this process (already queued in-memory)
                        if sender_pid == my_pid:
                            continue
                        if ch in self._channels:
                            msg = {
                                "type": "message",
                                "pattern": None,
                                "channel": ch if self._client.decode_responses else ch.encode("utf-8"),
                                "data": _decode_val(payload, self._client.decode_responses),
                            }
                            self._queue.put_nowait(msg)
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)

    async def subscribe(self, *channels: Union[str, bytes]) -> None:
        for ch in channels:
            c_str = _to_str(ch)
            self._channels.add(c_str)
            with _DB_LOCK:
                _PUBSUB_WAITERS.setdefault(c_str, weakref.WeakSet()).add(self._queue)
        self._ensure_poller()

    async def unsubscribe(self, *channels: Union[str, bytes]) -> None:
        targets = [_to_str(c) for c in channels] if channels else list(self._channels)
        for c_str in targets:
            self._channels.discard(c_str)
            with _DB_LOCK:
                if c_str in _PUBSUB_WAITERS:
                    _PUBSUB_WAITERS[c_str].discard(self._queue)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 0.0
    ) -> Optional[Dict[str, Any]]:
        self._ensure_poller()
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return self._queue.get_nowait()
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return None

    async def listen(self) -> AsyncIterator[Dict[str, Any]]:
        self._ensure_poller()
        while not self._closed:
            msg = await self._queue.get()
            if msg is None:
                break
            yield msg

    async def close(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await self.unsubscribe()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._queue.put_nowait(None)


class Pipeline:
    def __init__(self, client: "FastStoreClient", transaction: bool = True) -> None:
        self._client = client
        self._transaction = transaction
        self._ops: List[Tuple[str, tuple, dict]] = []

    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self._ops.clear()

    def _enqueue(self, method_name: str, *args, **kwargs) -> "Pipeline":
        self._ops.append((method_name, args, kwargs))
        return self

    def __getattr__(self, name: str):
        def _wrapper(*args, **kwargs):
            return self._enqueue(name, *args, **kwargs)

        return _wrapper

    def get(self, *a, **kw) -> "Pipeline": return self._enqueue("get", *a, **kw)
    def set(self, *a, **kw) -> "Pipeline": return self._enqueue("set", *a, **kw)
    def setex(self, *a, **kw) -> "Pipeline": return self._enqueue("setex", *a, **kw)
    def delete(self, *a, **kw) -> "Pipeline": return self._enqueue("delete", *a, **kw)
    def expire(self, *a, **kw) -> "Pipeline": return self._enqueue("expire", *a, **kw)
    def incr(self, *a, **kw) -> "Pipeline": return self._enqueue("incr", *a, **kw)
    def sadd(self, *a, **kw) -> "Pipeline": return self._enqueue("sadd", *a, **kw)
    def srem(self, *a, **kw) -> "Pipeline": return self._enqueue("srem", *a, **kw)
    def smembers(self, *a, **kw) -> "Pipeline": return self._enqueue("smembers", *a, **kw)
    def zadd(self, *a, **kw) -> "Pipeline": return self._enqueue("zadd", *a, **kw)
    def zrem(self, *a, **kw) -> "Pipeline": return self._enqueue("zrem", *a, **kw)
    def zrange(self, *a, **kw) -> "Pipeline": return self._enqueue("zrange", *a, **kw)
    def zrevrange(self, *a, **kw) -> "Pipeline": return self._enqueue("zrevrange", *a, **kw)
    def zremrangebyscore(self, *a, **kw) -> "Pipeline": return self._enqueue("zremrangebyscore", *a, **kw)
    def zcard(self, *a, **kw) -> "Pipeline": return self._enqueue("zcard", *a, **kw)
    def xadd(self, *a, **kw) -> "Pipeline": return self._enqueue("xadd", *a, **kw)
    def xack(self, *a, **kw) -> "Pipeline": return self._enqueue("xack", *a, **kw)
    def xtrim(self, *a, **kw) -> "Pipeline": return self._enqueue("xtrim", *a, **kw)
    def publish(self, *a, **kw) -> "Pipeline": return self._enqueue("publish", *a, **kw)

    async def execute(self) -> List[Any]:
        results = []
        ops = list(self._ops)
        self._ops.clear()
        if not ops:
            return results

        if self._transaction:
            def _tx_op(conn: sqlite3.Connection) -> List[Any]:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    res_list = []
                    for name, args, kwargs in ops:
                        op_fn = getattr(self._client, f"_{name}_op", None)
                        if op_fn is None:
                            raise FastStoreError(f"Operation '{name}' does not support pipeline transactions")
                        res_list.append(op_fn(conn, *args, **kwargs))
                    conn.execute("COMMIT")
                    return res_list
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise

            return await self._client._store.execute(_tx_op)
        else:
            def _nontx_op(conn: sqlite3.Connection) -> List[Any]:
                res_list = []
                for name, args, kwargs in ops:
                    op_fn = getattr(self._client, f"_{name}_op", None)
                    if op_fn is not None:
                        res_list.append(op_fn(conn, *args, **kwargs))
                    else:
                        raise FastStoreError(f"Operation '{name}' does not support pipeline")
                return res_list

            return await self._client._store.execute(_nontx_op)


class FastStoreClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        decode_responses: bool = False,
        socket_connect_timeout: Optional[float] = None,
        socket_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        global _WARNED_PARAMS
        if not _WARNED_PARAMS and (password is not None or host != "127.0.0.1" or port != 6379):
            _WARNED_PARAMS = True
            logger.warning(
                "FastStore is an in-process SQLite store; host, port, and password parameters are ignored."
            )
        self.host = str(host)
        self.port = int(port)
        self.db = int(db)
        self.password = password
        self.decode_responses = bool(decode_responses)
        self._store = _SharedSQLiteStore.get(self.db)

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "FastStoreClient":
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        db = 0
        if parsed.path and parsed.path.strip("/"):
            try:
                db = int(parsed.path.strip("/").split("/")[0])
            except ValueError:
                db = 0
        merged_kwargs = dict(kwargs)
        if "decode_responses" not in merged_kwargs:
            merged_kwargs["decode_responses"] = kwargs.get("decode_responses", False)
        if parsed.password:
            merged_kwargs["password"] = parsed.password
        return cls(host=host, port=port, db=db, **merged_kwargs)

    async def __aenter__(self) -> "FastStoreClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    async def close(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def _ping_op(self, conn: sqlite3.Connection) -> bool:
        res = conn.execute("SELECT 1").fetchone()
        return bool(res and res[0] == 1)

    async def ping(self) -> bool:
        return await self._store.execute(self._ping_op)

    def pipeline(self, transaction: bool = True) -> Pipeline:
        return Pipeline(self, transaction=transaction)

    def pubsub(self) -> PubSub:
        return PubSub(self)

    def _flushdb_op(self, conn: sqlite3.Connection) -> bool:
        cur = conn.cursor()
        cur.execute("DELETE FROM kv_store;")
        cur.execute("DELETE FROM set_store;")
        cur.execute("DELETE FROM zset_store;")
        cur.execute("DELETE FROM stream_entries;")
        cur.execute("DELETE FROM stream_groups;")
        cur.execute("DELETE FROM stream_pel;")
        cur.execute("DELETE FROM pubsub_log;")
        cur.execute("DELETE FROM key_expiry;")
        cur.close()
        return True

    async def flushdb(self) -> bool:
        return await self._store.execute(self._flushdb_op)

    async def info(self, section: Optional[str] = None) -> Dict[str, Any]:
        used_mem = 0
        try:
            if os.path.exists(self._store.db_path):
                used_mem = os.path.getsize(self._store.db_path)
        except OSError:
            pass

        return {
            "redis_version": "7.2.0",
            "redis_mode": "standalone",
            "used_memory": used_mem,
            "used_memory_human": f"{used_mem / (1024 * 1024):.2f}M",
            "maxmemory": 268435456,
            "connected_clients": 1,
        }

    # -----------------------------------------------------------------------
    # KV Store & Expiration
    # -----------------------------------------------------------------------
    def _evict_expired_sync(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        conn.execute("DELETE FROM kv_store WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        rows = conn.execute("SELECT k FROM key_expiry WHERE expires_at <= ?", (now,)).fetchall()
        if rows:
            for r in rows:
                k_exp = r[0]
                conn.execute("DELETE FROM kv_store WHERE k = ?", (k_exp,))
                conn.execute("DELETE FROM set_store WHERE k = ?", (k_exp,))
                conn.execute("DELETE FROM zset_store WHERE k = ?", (k_exp,))
                conn.execute("DELETE FROM stream_entries WHERE stream = ?", (k_exp,))
                conn.execute("DELETE FROM stream_groups WHERE stream = ?", (k_exp,))
                conn.execute("DELETE FROM stream_pel WHERE stream = ?", (k_exp,))
                conn.execute("DELETE FROM key_expiry WHERE k = ?", (k_exp,))

    def _get_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> Any:
        k_str = _to_str(name)
        now = time.time()
        self._evict_expired_sync(conn)
        row = conn.execute("SELECT v, expires_at FROM kv_store WHERE k = ?", (k_str,)).fetchone()
        if row is None:
            return None
        val, expires_at = row
        if expires_at is not None and expires_at <= now:
            conn.execute("DELETE FROM kv_store WHERE k = ?", (k_str,))
            return None
        return _decode_val(val, self.decode_responses)

    async def get(self, name: Union[str, bytes]) -> Any:
        return await self._store.execute(self._get_op, name)

    def _set_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        value: Any,
        ex: Optional[Union[int, float]] = None,
        px: Optional[Union[int, float]] = None,
        nx: bool = False,
        xx: bool = False,
    ) -> Optional[bool]:
        k_str = _to_str(name)
        v_bytes = _encode_val(value)
        now = time.time()
        expires_at = None
        if ex is not None:
            expires_at = now + float(ex)
        elif px is not None:
            expires_at = now + (float(px) / 1000.0)

        self._evict_expired_sync(conn)
        existing = conn.execute("SELECT expires_at FROM kv_store WHERE k = ?", (k_str,)).fetchone()
        exists = existing is not None and (existing[0] is None or existing[0] > now)
        if nx and exists:
            return None
        if xx and not exists:
            return None
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (k, v, expires_at) VALUES (?, ?, ?)",
            (k_str, v_bytes, expires_at),
        )
        if expires_at is not None:
            conn.execute("INSERT OR REPLACE INTO key_expiry (k, expires_at) VALUES (?, ?)", (k_str, expires_at))
        else:
            conn.execute("DELETE FROM key_expiry WHERE k = ?", (k_str,))
        return True

    async def set(
        self,
        name: Union[str, bytes],
        value: Any,
        ex: Optional[Union[int, float]] = None,
        px: Optional[Union[int, float]] = None,
        nx: bool = False,
        xx: bool = False,
    ) -> Optional[bool]:
        return await self._store.execute(self._set_op, name, value, ex=ex, px=px, nx=nx, xx=xx)

    def _setex_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        time_sec: Union[int, float],
        value: Any,
    ) -> bool:
        res = self._set_op(conn, name, value, ex=time_sec)
        return bool(res)

    async def setex(
        self,
        name: Union[str, bytes],
        time_sec: Union[int, float],
        value: Any,
    ) -> bool:
        return await self._store.execute(self._setex_op, name, time_sec, value)

    def _delete_op(self, conn: sqlite3.Connection, *names: Union[str, bytes]) -> int:
        if not names:
            return 0
        deleted_count = 0
        for name in names:
            k_str = _to_str(name)
            c1 = conn.execute("DELETE FROM kv_store WHERE k = ?", (k_str,)).rowcount
            c2 = conn.execute("DELETE FROM set_store WHERE k = ?", (k_str,)).rowcount
            c3 = conn.execute("DELETE FROM zset_store WHERE k = ?", (k_str,)).rowcount
            c4 = conn.execute("DELETE FROM stream_entries WHERE stream = ?", (k_str,)).rowcount
            conn.execute("DELETE FROM stream_groups WHERE stream = ?", (k_str,))
            conn.execute("DELETE FROM stream_pel WHERE stream = ?", (k_str,))
            conn.execute("DELETE FROM key_expiry WHERE k = ?", (k_str,))
            if c1 > 0 or c2 > 0 or c3 > 0 or c4 > 0:
                deleted_count += 1
        return deleted_count

    async def delete(self, *names: Union[str, bytes]) -> int:
        if not names:
            return 0
        return await self._store.execute(self._delete_op, *names)

    def _exists_op(self, conn: sqlite3.Connection, *names: Union[str, bytes]) -> int:
        if not names:
            return 0
        now = time.time()
        self._evict_expired_sync(conn)
        count = 0
        for name in names:
            k_str = _to_str(name)
            if conn.execute(
                "SELECT 1 FROM kv_store WHERE k = ? AND (expires_at IS NULL OR expires_at > ?)",
                (k_str, now),
            ).fetchone():
                count += 1
            elif conn.execute("SELECT 1 FROM set_store WHERE k = ? LIMIT 1", (k_str,)).fetchone():
                count += 1
            elif conn.execute("SELECT 1 FROM zset_store WHERE k = ? LIMIT 1", (k_str,)).fetchone():
                count += 1
            elif conn.execute("SELECT 1 FROM stream_entries WHERE stream = ? LIMIT 1", (k_str,)).fetchone():
                count += 1
        return count

    async def exists(self, *names: Union[str, bytes]) -> int:
        if not names:
            return 0
        return await self._store.execute(self._exists_op, *names)

    def _incr_op(self, conn: sqlite3.Connection, name: Union[str, bytes], amount: int = 1) -> int:
        k_str = _to_str(name)
        now = time.time()
        self._evict_expired_sync(conn)
        row = conn.execute("SELECT v, expires_at FROM kv_store WHERE k = ?", (k_str,)).fetchone()
        if row is None or (row[1] is not None and row[1] <= now):
            new_val = int(amount)
            expires_at = None
        else:
            try:
                curr = int(row[0].decode("utf-8"))
            except Exception:
                raise ResponseError("value is not an integer or out of range")
            new_val = curr + int(amount)
            expires_at = row[1]
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (k, v, expires_at) VALUES (?, ?, ?)",
            (k_str, str(new_val).encode("utf-8"), expires_at),
        )
        return new_val

    async def incr(self, name: Union[str, bytes], amount: int = 1) -> int:
        return await self._store.execute(self._incr_op, name, amount)

    def _expire_op(self, conn: sqlite3.Connection, name: Union[str, bytes], time_sec: Union[int, float]) -> bool:
        k_str = _to_str(name)
        now = time.time()
        new_exp = now + float(time_sec)
        self._evict_expired_sync(conn)
        found = False
        row = conn.execute("SELECT v, expires_at FROM kv_store WHERE k = ?", (k_str,)).fetchone()
        if row is not None and (row[1] is None or row[1] > now):
            conn.execute("UPDATE kv_store SET expires_at = ? WHERE k = ?", (new_exp, k_str))
            found = True
        elif (
            conn.execute("SELECT 1 FROM set_store WHERE k = ? LIMIT 1", (k_str,)).fetchone()
            or conn.execute("SELECT 1 FROM zset_store WHERE k = ? LIMIT 1", (k_str,)).fetchone()
            or conn.execute("SELECT 1 FROM stream_entries WHERE stream = ? LIMIT 1", (k_str,)).fetchone()
        ):
            found = True

        if found:
            conn.execute(
                "INSERT OR REPLACE INTO key_expiry (k, expires_at) VALUES (?, ?)",
                (k_str, new_exp),
            )
            return True
        return False

    async def expire(self, name: Union[str, bytes], time_sec: Union[int, float]) -> bool:
        return await self._store.execute(self._expire_op, name, time_sec)

    def _ttl_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> int:
        k_str = _to_str(name)
        now = time.time()
        self._evict_expired_sync(conn)
        exp_row = conn.execute("SELECT expires_at FROM key_expiry WHERE k = ?", (k_str,)).fetchone()
        if exp_row is not None:
            expires_at = exp_row[0]
            if expires_at <= now:
                return -2
            return max(0, int(expires_at - now))

        row = conn.execute("SELECT expires_at FROM kv_store WHERE k = ?", (k_str,)).fetchone()
        if row is None:
            if (
                conn.execute("SELECT 1 FROM set_store WHERE k = ? LIMIT 1", (k_str,)).fetchone()
                or conn.execute("SELECT 1 FROM zset_store WHERE k = ? LIMIT 1", (k_str,)).fetchone()
                or conn.execute("SELECT 1 FROM stream_entries WHERE stream = ? LIMIT 1", (k_str,)).fetchone()
            ):
                return -1
            return -2
        expires_at = row[0]
        if expires_at is None:
            return -1
        if expires_at <= now:
            conn.execute("DELETE FROM kv_store WHERE k = ?", (k_str,))
            return -2
        return max(0, int(expires_at - now))

    async def ttl(self, name: Union[str, bytes]) -> int:
        return await self._store.execute(self._ttl_op, name)

    def _keys_op(self, conn: sqlite3.Connection, pattern: Union[str, bytes] = "*") -> List[Any]:
        pat = _to_str(pattern)
        self._evict_expired_sync(conn)
        all_keys: Set[str] = set()
        for r in conn.execute("SELECT k FROM kv_store").fetchall():
            all_keys.add(r[0])
        for r in conn.execute("SELECT DISTINCT k FROM set_store").fetchall():
            all_keys.add(r[0])
        for r in conn.execute("SELECT DISTINCT k FROM zset_store").fetchall():
            all_keys.add(r[0])
        for r in conn.execute("SELECT DISTINCT stream FROM stream_entries").fetchall():
            all_keys.add(r[0])

        matched = [k for k in sorted(all_keys) if fnmatch.fnmatch(k, pat)]
        return [k if self.decode_responses else k.encode("utf-8") for k in matched]

    async def keys(self, pattern: Union[str, bytes] = "*") -> List[Any]:
        return await self._store.execute(self._keys_op, pattern)

    async def scan_iter(
        self, match: Union[str, bytes] = "*", count: Optional[int] = None
    ) -> AsyncIterator[Any]:
        all_matched = await self.keys(match)
        for k in all_matched:
            yield k

    # -----------------------------------------------------------------------
    # Sets Store
    # -----------------------------------------------------------------------
    def _sadd_op(self, conn: sqlite3.Connection, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        k_str = _to_str(name)
        self._evict_expired_sync(conn)
        added = 0
        for v in values:
            m_bytes = _encode_val(v)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO set_store (k, member) VALUES (?, ?)",
                    (k_str, m_bytes),
                )
                added += cur.rowcount
                cur.close()
            except sqlite3.IntegrityError:
                pass
        return added

    async def sadd(self, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        return await self._store.execute(self._sadd_op, name, *values)

    def _srem_op(self, conn: sqlite3.Connection, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        k_str = _to_str(name)
        self._evict_expired_sync(conn)
        removed = 0
        for v in values:
            m_bytes = _encode_val(v)
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM set_store WHERE k = ? AND member = ?",
                (k_str, m_bytes),
            )
            removed += cur.rowcount
            cur.close()
        return removed

    async def srem(self, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        return await self._store.execute(self._srem_op, name, *values)

    def _smembers_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> Set[Any]:
        k_str = _to_str(name)
        self._evict_expired_sync(conn)
        rows = conn.execute("SELECT member FROM set_store WHERE k = ?", (k_str,)).fetchall()
        return {
            row[0].decode("utf-8", errors="replace") if self.decode_responses else row[0]
            for row in rows
        }

    async def smembers(self, name: Union[str, bytes]) -> Set[Any]:
        return await self._store.execute(self._smembers_op, name)

    def _scard_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> int:
        k_str = _to_str(name)
        self._evict_expired_sync(conn)
        row = conn.execute("SELECT COUNT(*) FROM set_store WHERE k = ?", (k_str,)).fetchone()
        return int(row[0]) if row else 0

    async def scard(self, name: Union[str, bytes]) -> int:
        return await self._store.execute(self._scard_op, name)

    def _sismember_op(self, conn: sqlite3.Connection, name: Union[str, bytes], value: Any) -> bool:
        k_str = _to_str(name)
        m_bytes = _encode_val(value)
        self._evict_expired_sync(conn)
        row = conn.execute(
            "SELECT 1 FROM set_store WHERE k = ? AND member = ?",
            (k_str, m_bytes),
        ).fetchone()
        return row is not None

    async def sismember(self, name: Union[str, bytes], value: Any) -> bool:
        return await self._store.execute(self._sismember_op, name, value)

    # -----------------------------------------------------------------------
    # Sorted Sets Store
    # -----------------------------------------------------------------------
    def _zadd_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        mapping: Dict[Any, float],
        nx: bool = False,
        xx: bool = False,
    ) -> int:
        if not mapping:
            return 0
        k_str = _to_str(name)
        added = 0
        for member, score in mapping.items():
            m_bytes = _encode_val(member)
            s_float = float(score)
            existing = conn.execute(
                "SELECT score FROM zset_store WHERE k = ? AND member = ?",
                (k_str, m_bytes),
            ).fetchone()
            if nx and existing is not None:
                continue
            if xx and existing is None:
                continue
            if existing is None:
                added += 1
            conn.execute(
                "INSERT OR REPLACE INTO zset_store (k, member, score) VALUES (?, ?, ?)",
                (k_str, m_bytes, s_float),
            )
        return added

    async def zadd(
        self,
        name: Union[str, bytes],
        mapping: Dict[Any, float],
        nx: bool = False,
        xx: bool = False,
    ) -> int:
        if not mapping:
            return 0
        return await self._store.execute(self._zadd_op, name, mapping, nx=nx, xx=xx)

    def _zrem_op(self, conn: sqlite3.Connection, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        k_str = _to_str(name)
        removed = 0
        for v in values:
            m_bytes = _encode_val(v)
            c = conn.execute(
                "DELETE FROM zset_store WHERE k = ? AND member = ?",
                (k_str, m_bytes),
            ).rowcount
            removed += c
        return removed

    async def zrem(self, name: Union[str, bytes], *values: Any) -> int:
        if not values:
            return 0
        return await self._store.execute(self._zrem_op, name, *values)

    def _zcard_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> int:
        k_str = _to_str(name)
        row = conn.execute("SELECT COUNT(*) FROM zset_store WHERE k = ?", (k_str,)).fetchone()
        return int(row[0]) if row else 0

    async def zcard(self, name: Union[str, bytes]) -> int:
        return await self._store.execute(self._zcard_op, name)

    def _zscore_op(self, conn: sqlite3.Connection, name: Union[str, bytes], value: Any) -> Optional[float]:
        k_str = _to_str(name)
        m_bytes = _encode_val(value)
        row = conn.execute(
            "SELECT score FROM zset_store WHERE k = ? AND member = ?",
            (k_str, m_bytes),
        ).fetchone()
        return float(row[0]) if row else None

    async def zscore(self, name: Union[str, bytes], value: Any) -> Optional[float]:
        return await self._store.execute(self._zscore_op, name, value)

    def _zrange_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        start: int,
        end: int,
        desc: bool = False,
        withscores: bool = False,
    ) -> List[Any]:
        k_str = _to_str(name)
        order = "DESC" if desc else "ASC"
        rows = conn.execute(
            f"SELECT member, score FROM zset_store WHERE k = ? ORDER BY score {order}, member {order}",
            (k_str,),
        ).fetchall()
        n = len(rows)
        s_idx = int(start)
        e_idx = int(end)
        if s_idx < 0:
            s_idx = max(0, n + s_idx)
        if e_idx < 0:
            e_idx = n + e_idx
        slice_rows = rows[s_idx : e_idx + 1] if s_idx <= e_idx and s_idx < n else []
        res = []
        for m_bytes, score in slice_rows:
            m = m_bytes.decode("utf-8", errors="replace") if self.decode_responses else m_bytes
            if withscores:
                res.append((m, float(score)))
            else:
                res.append(m)
        return res

    async def zrange(
        self,
        name: Union[str, bytes],
        start: int,
        end: int,
        desc: bool = False,
        withscores: bool = False,
    ) -> List[Any]:
        return await self._store.execute(self._zrange_op, name, start, end, desc=desc, withscores=withscores)

    def _zrevrange_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        start: int,
        end: int,
        withscores: bool = False,
    ) -> List[Any]:
        return self._zrange_op(conn, name, start, end, desc=True, withscores=withscores)

    async def zrevrange(
        self,
        name: Union[str, bytes],
        start: int,
        end: int,
        withscores: bool = False,
    ) -> List[Any]:
        return await self._store.execute(self._zrevrange_op, name, start, end, withscores=withscores)

    def _zremrangebyscore_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        min: Union[str, float, int],
        max: Union[str, float, int],
    ) -> int:
        k_str = _to_str(name)
        min_val, min_ex = _parse_score_bound(min, is_min=True)
        max_val, max_ex = _parse_score_bound(max, is_min=False)
        min_op = ">" if min_ex else ">="
        max_op = "<" if max_ex else "<="
        c = conn.execute(
            f"DELETE FROM zset_store WHERE k = ? AND score {min_op} ? AND score {max_op} ?",
            (k_str, min_val, max_val),
        ).rowcount
        return c

    async def zremrangebyscore(
        self,
        name: Union[str, bytes],
        min: Union[str, float, int],
        max: Union[str, float, int],
    ) -> int:
        return await self._store.execute(self._zremrangebyscore_op, name, min, max)

    def _zrevrangebyscore_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        max: Union[str, float, int],
        min: Union[str, float, int],
        withscores: bool = False,
        start: Optional[int] = None,
        num: Optional[int] = None,
    ) -> List[Any]:
        k_str = _to_str(name)
        max_val, max_ex = _parse_score_bound(max, is_min=False)
        min_val, min_ex = _parse_score_bound(min, is_min=True)
        max_op = "<" if max_ex else "<="
        min_op = ">" if min_ex else ">="
        sql = f"SELECT member, score FROM zset_store WHERE k = ? AND score {max_op} ? AND score {min_op} ? ORDER BY score DESC, member DESC"
        if start is not None and num is not None:
            sql += f" LIMIT {int(num)} OFFSET {int(start)}"
        rows = conn.execute(sql, (k_str, max_val, min_val)).fetchall()
        res = []
        for m_bytes, score in rows:
            m = m_bytes.decode("utf-8", errors="replace") if self.decode_responses else m_bytes
            if withscores:
                res.append((m, float(score)))
            else:
                res.append(m)
        return res

    async def zrevrangebyscore(
        self,
        name: Union[str, bytes],
        max: Union[str, float, int],
        min: Union[str, float, int],
        withscores: bool = False,
        start: Optional[int] = None,
        num: Optional[int] = None,
    ) -> List[Any]:
        return await self._store.execute(
            self._zrevrangebyscore_op, name, max, min, withscores=withscores, start=start, num=num
        )

    def _zrangebyscore_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        min: Union[str, float, int],
        max: Union[str, float, int],
        withscores: bool = False,
        start: Optional[int] = None,
        num: Optional[int] = None,
    ) -> List[Any]:
        k_str = _to_str(name)
        min_val, min_ex = _parse_score_bound(min, is_min=True)
        max_val, max_ex = _parse_score_bound(max, is_min=False)
        min_op = ">" if min_ex else ">="
        max_op = "<" if max_ex else "<="
        sql = f"SELECT member, score FROM zset_store WHERE k = ? AND score {min_op} ? AND score {max_op} ? ORDER BY score ASC, member ASC"
        if start is not None and num is not None:
            sql += f" LIMIT {int(num)} OFFSET {int(start)}"
        rows = conn.execute(sql, (k_str, min_val, max_val)).fetchall()
        res = []
        for m_bytes, score in rows:
            m = m_bytes.decode("utf-8", errors="replace") if self.decode_responses else m_bytes
            if withscores:
                res.append((m, float(score)))
            else:
                res.append(m)
        return res

    async def zrangebyscore(
        self,
        name: Union[str, bytes],
        min: Union[str, float, int],
        max: Union[str, float, int],
        withscores: bool = False,
        start: Optional[int] = None,
        num: Optional[int] = None,
    ) -> List[Any]:
        return await self._store.execute(
            self._zrangebyscore_op, name, min, max, withscores=withscores, start=start, num=num
        )

    def _zremrangebyrank_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        start: int,
        stop: int,
    ) -> int:
        k_str = _to_str(name)
        rows = conn.execute(
            "SELECT member FROM zset_store WHERE k = ? ORDER BY score ASC, member ASC",
            (k_str,),
        ).fetchall()
        n = len(rows)
        s_idx = int(start)
        e_idx = int(stop)
        if s_idx < 0:
            s_idx = max(0, n + s_idx)
        if e_idx < 0:
            e_idx = n + e_idx
        to_del = rows[s_idx : e_idx + 1] if s_idx <= e_idx and s_idx < n else []
        count = 0
        for r in to_del:
            conn.execute("DELETE FROM zset_store WHERE k = ? AND member = ?", (k_str, r[0]))
            count += 1
        return count

    async def zremrangebyrank(
        self,
        name: Union[str, bytes],
        start: int,
        stop: int,
    ) -> int:
        return await self._store.execute(self._zremrangebyrank_op, name, start, stop)

    # -----------------------------------------------------------------------
    # Stream Operations
    # -----------------------------------------------------------------------
    def _xadd_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        fields: Dict[Any, Any],
        id: Union[str, bytes] = "*",
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> Any:
        s_name = _to_str(name)
        id_str = _to_str(id) if id is not None else "*"
        if id_str == "*":
            now_ms = int(time.time() * 1000)
            last_row = conn.execute(
                "SELECT id_ms, id_seq FROM stream_entries WHERE stream = ? ORDER BY id_ms DESC, id_seq DESC LIMIT 1",
                (s_name,),
            ).fetchone()
            if last_row:
                last_ms, last_seq = int(last_row[0]), int(last_row[1])
                if now_ms <= last_ms:
                    id_ms = last_ms
                    id_seq = last_seq + 1
                else:
                    id_ms = now_ms
                    id_seq = 0
            else:
                id_ms = now_ms
                id_seq = 0
            entry_id = f"{id_ms}-{id_seq}"
        else:
            id_ms, id_seq = _parse_stream_id(id_str)
            entry_id = f"{id_ms}-{id_seq}"

        serialized_fields = {}
        for k, v in fields.items():
            k_s = _to_str(k)
            v_s = _to_str(v)
            serialized_fields[k_s] = v_s
        fields_json = json.dumps(serialized_fields)

        conn.execute(
            "INSERT OR REPLACE INTO stream_entries (stream, id_ms, id_seq, entry_id, fields_json) VALUES (?, ?, ?, ?, ?)",
            (s_name, id_ms, id_seq, entry_id, fields_json),
        )

        if maxlen is not None and int(maxlen) >= 0:
            do_trim = True
            if approximate:
                s_counter = getattr(self._store, "_xadd_counter", 0) + 1
                self._store._xadd_counter = s_counter
                do_trim = (s_counter % 100 == 0)

            if do_trim:
                conn.execute(
                    """
                    DELETE FROM stream_entries
                    WHERE stream = ? AND rowid NOT IN (
                        SELECT rowid FROM stream_entries WHERE stream = ? ORDER BY id_ms DESC, id_seq DESC LIMIT ?
                    )
                    """,
                    (s_name, s_name, int(maxlen)),
                )
        return entry_id if self.decode_responses else entry_id.encode("utf-8")

    async def xadd(
        self,
        name: Union[str, bytes],
        fields: Dict[Any, Any],
        id: Union[str, bytes] = "*",
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> Any:
        return await self._store.execute(
            self._xadd_op, name, fields, id=id, maxlen=maxlen, approximate=approximate
        )

    def _xgroup_create_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        groupname: Union[str, bytes],
        id: Union[str, bytes] = "$",
        mkstream: bool = False,
    ) -> bool:
        s_name = _to_str(name)
        grp_name = _to_str(groupname)
        id_str = _to_str(id)

        existing = conn.execute(
            "SELECT 1 FROM stream_groups WHERE stream = ? AND group_name = ?",
            (s_name, grp_name),
        ).fetchone()
        if existing:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")

        if id_str == "$":
            last = conn.execute(
                "SELECT id_ms, id_seq FROM stream_entries WHERE stream = ? ORDER BY id_ms DESC, id_seq DESC LIMIT 1",
                (s_name,),
            ).fetchone()
            last_ms = int(last[0]) if last else 0
            last_seq = int(last[1]) if last else 0
        else:
            last_ms, last_seq = _parse_stream_id(id_str)

        conn.execute(
            "INSERT OR REPLACE INTO stream_groups (stream, group_name, last_ms, last_seq) VALUES (?, ?, ?, ?)",
            (s_name, grp_name, last_ms, last_seq),
        )
        return True

    async def xgroup_create(
        self,
        name: Union[str, bytes],
        groupname: Union[str, bytes],
        id: Union[str, bytes] = "$",
        mkstream: bool = False,
    ) -> bool:
        return await self._store.execute(self._xgroup_create_op, name, groupname, id=id, mkstream=mkstream)

    def _xreadgroup_query_op(
        self,
        conn: sqlite3.Connection,
        groupname: Union[str, bytes],
        consumername: Union[str, bytes],
        streams: Dict[Any, Any],
        limit_count: int,
    ) -> List[Any]:
        grp_str = _to_str(groupname)
        c_str = _to_str(consumername)
        out = []
        now = time.time()
        for s_raw, id_raw in streams.items():
            s_name = _to_str(s_raw)
            id_target = _to_str(id_raw).strip()
            entries = []

            if id_target == ">":
                grp_row = conn.execute(
                    "SELECT last_ms, last_seq FROM stream_groups WHERE stream = ? AND group_name = ?",
                    (s_name, grp_str),
                ).fetchone()
                if not grp_row:
                    continue
                last_ms, last_seq = int(grp_row[0]), int(grp_row[1])
                rows = conn.execute(
                    """
                    SELECT id_ms, id_seq, entry_id, fields_json FROM stream_entries
                    WHERE stream = ? AND (id_ms > ? OR (id_ms = ? AND id_seq > ?))
                    ORDER BY id_ms ASC, id_seq ASC LIMIT ?
                    """,
                    (s_name, last_ms, last_ms, last_seq, limit_count),
                ).fetchall()
                for r_ms, r_seq, e_id, f_json in rows:
                    f_dict = json.loads(f_json)
                    if not self.decode_responses:
                        f_dict = {k.encode("utf-8"): v.encode("utf-8") for k, v in f_dict.items()}
                    ret_id = e_id if self.decode_responses else e_id.encode("utf-8")
                    entries.append((ret_id, f_dict))
                    conn.execute(
                        "INSERT OR REPLACE INTO stream_pel (stream, group_name, entry_id, consumer, delivered_at) VALUES (?, ?, ?, ?, ?)",
                        (s_name, grp_str, e_id, c_str, now),
                    )
                    conn.execute(
                        "UPDATE stream_groups SET last_ms = ?, last_seq = ? WHERE stream = ? AND group_name = ?",
                        (int(r_ms), int(r_seq), s_name, grp_str),
                    )
            else:
                pel_rows = conn.execute(
                    "SELECT entry_id FROM stream_pel WHERE stream = ? AND group_name = ? AND consumer = ? LIMIT ?",
                    (s_name, grp_str, c_str, limit_count),
                ).fetchall()
                for p_row in pel_rows:
                    e_id = p_row[0]
                    e_row = conn.execute(
                        "SELECT fields_json FROM stream_entries WHERE stream = ? AND entry_id = ?",
                        (s_name, e_id),
                    ).fetchone()
                    if e_row:
                        f_dict = json.loads(e_row[0])
                        if not self.decode_responses:
                            f_dict = {k.encode("utf-8"): v.encode("utf-8") for k, v in f_dict.items()}
                        ret_id = e_id if self.decode_responses else e_id.encode("utf-8")
                        entries.append((ret_id, f_dict))

            if entries:
                ret_stream = s_name if self.decode_responses else s_name.encode("utf-8")
                out.append([ret_stream, entries])
        return out

    def _xreadgroup_op(
        self,
        conn: sqlite3.Connection,
        groupname: Union[str, bytes],
        consumername: Union[str, bytes],
        streams: Dict[Any, Any],
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> List[Any]:
        limit_count = int(count) if count is not None else 1000
        return self._xreadgroup_query_op(conn, groupname, consumername, streams, limit_count)

    async def xreadgroup(
        self,
        groupname: Union[str, bytes],
        consumername: Union[str, bytes],
        streams: Dict[Any, Any],
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> List[Any]:
        limit_count = int(count) if count is not None else 1000
        deadline = time.time() + (float(block) / 1000.0) if block is not None and int(block) > 0 else 0
        while True:
            res = await self._store.execute(
                self._xreadgroup_query_op, groupname, consumername, streams, limit_count
            )
            if res:
                return res
            if not deadline or time.time() >= deadline:
                return res
            sleep_time = min(0.05, max(0.005, deadline - time.time()))
            await asyncio.sleep(sleep_time)

    def _xack_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        groupname: Union[str, bytes],
        *ids: Union[str, bytes],
    ) -> int:
        if not ids:
            return 0
        s_name = _to_str(name)
        grp_name = _to_str(groupname)
        acked = 0
        for e_id in ids:
            id_str = _to_str(e_id)
            c = conn.execute(
                "DELETE FROM stream_pel WHERE stream = ? AND group_name = ? AND entry_id = ?",
                (s_name, grp_name, id_str),
            ).rowcount
            acked += c
        return acked

    async def xack(
        self,
        name: Union[str, bytes],
        groupname: Union[str, bytes],
        *ids: Union[str, bytes],
    ) -> int:
        if not ids:
            return 0
        return await self._store.execute(self._xack_op, name, groupname, *ids)

    def _xtrim_op(
        self,
        conn: sqlite3.Connection,
        name: Union[str, bytes],
        maxlen: Optional[int] = None,
        approximate: bool = True,
        minid: Optional[Union[str, bytes]] = None,
        limit: Optional[int] = None,
    ) -> int:
        s_name = _to_str(name)
        trimmed = 0
        if minid is not None:
            min_ms, min_seq = _parse_stream_id(minid)
            c = conn.execute(
                """
                DELETE FROM stream_entries
                WHERE stream = ? AND (id_ms < ? OR (id_ms = ? AND id_seq < ?))
                """,
                (s_name, min_ms, min_ms, min_seq),
            ).rowcount
            trimmed += c
        if maxlen is not None and int(maxlen) >= 0:
            c = conn.execute(
                """
                DELETE FROM stream_entries
                WHERE stream = ? AND rowid NOT IN (
                    SELECT rowid FROM stream_entries WHERE stream = ? ORDER BY id_ms DESC, id_seq DESC LIMIT ?
                )
                """,
                (s_name, s_name, int(maxlen)),
            ).rowcount
            trimmed += c
        return trimmed

    async def xtrim(
        self,
        name: Union[str, bytes],
        maxlen: Optional[int] = None,
        approximate: bool = True,
        minid: Optional[Union[str, bytes]] = None,
        limit: Optional[int] = None,
    ) -> int:
        return await self._store.execute(
            self._xtrim_op, name, maxlen=maxlen, approximate=approximate, minid=minid, limit=limit
        )

    def _xlen_op(self, conn: sqlite3.Connection, name: Union[str, bytes]) -> int:
        s_name = _to_str(name)
        row = conn.execute(
            "SELECT COUNT(*) FROM stream_entries WHERE stream = ?",
            (s_name,),
        ).fetchone()
        return int(row[0]) if row else 0

    async def xlen(self, name: Union[str, bytes]) -> int:
        return await self._store.execute(self._xlen_op, name)

    def _xdel_op(self, conn: sqlite3.Connection, name: Union[str, bytes], *ids: Union[str, bytes]) -> int:
        if not ids:
            return 0
        s_name = _to_str(name)
        deleted = 0
        for e_id in ids:
            id_str = _to_str(e_id)
            c = conn.execute(
                "DELETE FROM stream_entries WHERE stream = ? AND entry_id = ?",
                (s_name, id_str),
            ).rowcount
            deleted += c
        return deleted

    async def xdel(self, name: Union[str, bytes], *ids: Union[str, bytes]) -> int:
        if not ids:
            return 0
        return await self._store.execute(self._xdel_op, name, *ids)

    # -----------------------------------------------------------------------
    # Pub/Sub publish
    # -----------------------------------------------------------------------
    def _publish_op(
        self,
        conn: sqlite3.Connection,
        channel: Union[str, bytes],
        message: Any,
    ) -> int:
        c_str = _to_str(channel)
        payload = _encode_val(message)
        now = time.time()
        my_pid = os.getpid()

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pubsub_log (channel, payload, sender_pid, created_at) VALUES (?, ?, ?, ?)",
            (c_str, payload, my_pid, now),
        )
        cur.execute("DELETE FROM pubsub_log WHERE id < (SELECT MAX(id) - 10000 FROM pubsub_log)")
        cur.close()
        return 1

    async def publish(self, channel: Union[str, bytes], message: Any) -> int:
        c_str = _to_str(channel)
        payload = _encode_val(message)
        await self._store.execute(self._publish_op, channel, message)

        # In-process immediate dispatch to active subscribers
        recipients = 0
        with _DB_LOCK:
            waiters = list(_PUBSUB_WAITERS.get(c_str, []))
        for q in waiters:
            msg = {
                "type": "message",
                "pattern": None,
                "channel": c_str if self.decode_responses else c_str.encode("utf-8"),
                "data": _decode_val(payload, self.decode_responses),
            }
            q.put_nowait(msg)
            recipients += 1
        return max(recipients, 1)


def from_url(url: str, **kwargs: Any) -> FastStoreClient:
    return FastStoreClient.from_url(url, **kwargs)


connect_faststore = from_url
Redis = FastStoreClient
