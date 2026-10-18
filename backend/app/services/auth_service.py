from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom OpenSSL hashlib.scrypt Password Hasher (Zero External Dependencies)
# ---------------------------------------------------------------------------
class ScryptPasswordHasher:
    """
    Zero-dependency password hasher backed by OpenSSL hashlib.scrypt.
    Format: $scrypt$n=16384,r=8,p=1$<salt_b64>$<hash_b64>
    """

    N = 16384
    R = 8
    P = 1
    MAXMEM = 32 * 1024 * 1024
    DKLEN = 64

    def hash(self, password: str) -> str:
        if not isinstance(password, str):
            raise TypeError("Password must be a string")
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=self.N,
            r=self.R,
            p=self.P,
            maxmem=self.MAXMEM,
            dklen=self.DKLEN,
        )
        salt_b64 = base64.b64encode(salt).decode("ascii")
        hash_b64 = base64.b64encode(derived).decode("ascii")
        return f"$scrypt$n={self.N},r={self.R},p={self.P}${salt_b64}${hash_b64}"

    def verify(self, hashed: str, plain: str) -> bool:
        if not hashed or not isinstance(hashed, str) or not isinstance(plain, str):
            return False
        if hashed.startswith("$argon2"):
            logger.warning("Legacy argon2 hash for login attempt; run app.reset_legacy_passwords")
            return False
        parts = hashed.split("$")
        if len(parts) != 5 or parts[1] != "scrypt":
            return False
        params_str, salt_b64, hash_b64 = parts[2], parts[3], parts[4]
        try:
            params: Dict[str, int] = {}
            for item in params_str.split(","):
                k, v = item.split("=", 1)
                params[k.strip()] = int(v.strip())
            n = params["n"]
            r = params["r"]
            p = params["p"]
            salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
            expected_dk = base64.b64decode(hash_b64.encode("ascii"), validate=True)
            derived = hashlib.scrypt(
                plain.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=self.MAXMEM,
                dklen=len(expected_dk),
            )
            return hmac.compare_digest(derived, expected_dk)
        except Exception:
            return False


_ph = ScryptPasswordHasher()


# ---------------------------------------------------------------------------
# Custom RFC 7519 HS256 JWT Codec (Zero External Dependencies)
# ---------------------------------------------------------------------------
class JWTError(Exception):
    """Raised when JWT encoding, decoding, signature verification, or claim validation fails."""
    pass


ALGORITHM = "HS256"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    if not segment or not isinstance(segment, str):
        raise JWTError("Empty or invalid JWT segment.")
    try:
        raw = segment.encode("ascii")
    except UnicodeEncodeError as exc:
        raise JWTError("Invalid non-ASCII characters in JWT segment.") from exc
    pad_len = (-len(raw)) % 4
    padded = raw + (b"=" * pad_len)
    try:
        decoded = base64.urlsafe_b64decode(padded)
    except Exception as exc:
        raise JWTError(f"Invalid base64url encoding: {exc}") from exc
    if _b64url_encode(decoded) != segment.rstrip("="):
        raise JWTError("Non-canonical base64url encoding in JWT segment.")
    return decoded


def jwt_encode(payload: Dict[str, Any], key: str, algorithm: str = "HS256") -> str:
    """Encodes a payload dictionary into an RFC 7519 HS256 JWT string."""
    if algorithm != "HS256":
        raise JWTError(f"Unsupported JWT algorithm '{algorithm}'. Only HS256 is supported.")
    if not key or not isinstance(key, (str, bytes)):
        raise JWTError("Signing key must be a non-empty string or bytes.")

    serializable_payload: Dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, datetime):
            serializable_payload[k] = int(v.timestamp())
        else:
            serializable_payload[k] = v

    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(serializable_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    signature = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def jwt_decode(
    token: str,
    key: str,
    algorithms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Decodes and validates an RFC 7519 HS256 JWT string."""
    allowed_algs = algorithms if algorithms is not None else ["HS256"]
    if not allowed_algs or "HS256" not in allowed_algs or any(a != "HS256" for a in allowed_algs):
        raise JWTError("Unsupported or insecure JWT algorithm specified.")
    if not token or not isinstance(token, str):
        raise JWTError("Token must be a non-empty string.")

    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("Invalid JWT structure: expected 3 dot-separated segments.")

    header_b64, payload_b64, sig_b64 = parts

    try:
        header_raw = _b64url_decode(header_b64)
        header = json.loads(header_raw.decode("utf-8"))
    except Exception as exc:
        raise JWTError(f"Malformed JWT header: {exc}") from exc

    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise JWTError(f"Invalid or rejected JWT header algorithm: {header.get('alg') if isinstance(header, dict) else header}")

    actual_sig = _b64url_decode(sig_b64)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    expected_sig = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()

    if not hmac.compare_digest(actual_sig, expected_sig):
        raise JWTError("JWT signature verification failed.")

    try:
        payload_raw = _b64url_decode(payload_b64)
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise JWTError(f"Malformed JWT payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise JWTError("JWT payload must be a JSON object.")

    now = time.time()

    if "exp" not in payload:
        raise JWTError("Missing 'exp' claim.")

    try:
        exp_ts = float(payload["exp"])
    except (ValueError, TypeError) as exc:
        raise JWTError("Invalid 'exp' claim in JWT.") from exc
    if now >= exp_ts:
        raise JWTError("JWT token has expired.")

    if "nbf" in payload:
        try:
            nbf_ts = float(payload["nbf"])
        except (ValueError, TypeError) as exc:
            raise JWTError("Invalid 'nbf' claim in JWT.") from exc
        if now < nbf_ts:
            raise JWTError("JWT token is not yet valid ('nbf' claim).")

    return payload


class _JWTNamespace:
    encode = staticmethod(jwt_encode)
    decode = staticmethod(jwt_decode)


jwt = _JWTNamespace()


# Default trusted reverse proxy CIDR networks (strictly loopback)
DEFAULT_TRUSTED_PROXIES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

# IP-Scoped Failed Login Tracker:
# Key format: "<client_ip>:<username>" -> {"count": int, "last_attempt": datetime, "locked_until": datetime}
_failed_attempts: dict[str, dict] = {}

# In-Memory Concurrent Session Tracker:
# Key format: "<user_id>" -> [jti_1, jti_2] (ordered list of active session JWT IDs, FIFO rotated)
_active_user_sessions: dict[str, list[str]] = {}


def generate_readable_password() -> str:
    """Generate high-entropy password (96 bits of entropy via secrets.token_urlsafe(16))."""
    return secrets.token_urlsafe(16)


def hash_password(password: str) -> str:
    """Synchronous OpenSSL scrypt password hashing."""
    return _ph.hash(password)


async def hash_password_async(password: str) -> str:
    """Non-blocking OpenSSL scrypt password hashing delegated to asyncio worker thread."""
    return await asyncio.to_thread(_ph.hash, password)


def verify_password(plain: str, hashed: str) -> bool:
    """Synchronous OpenSSL scrypt password verification."""
    try:
        return _ph.verify(hashed, plain)
    except Exception:
        return False


async def verify_password_async(plain: str, hashed: str) -> bool:
    """Non-blocking OpenSSL scrypt password verification delegated to asyncio worker thread."""
    return await asyncio.to_thread(verify_password, plain, hashed)


def needs_rehash(hashed: str) -> bool:
    """Returns True if the password hash is not an OpenSSL scrypt hash matching current cost parameters."""
    if not hashed or not isinstance(hashed, str):
        return True
    if not hashed.startswith("$scrypt$"):
        return True
    parts = hashed.split("$")
    if len(parts) != 5 or parts[1] != "scrypt":
        return True
    try:
        params: Dict[str, int] = {}
        for item in parts[2].split(","):
            k, v = item.split("=", 1)
            params[k.strip()] = int(v.strip())
        if (
            params.get("n") != ScryptPasswordHasher.N
            or params.get("r") != ScryptPasswordHasher.R
            or params.get("p") != ScryptPasswordHasher.P
        ):
            return True
        return False
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Trusted Reverse Proxy Header Security
# ---------------------------------------------------------------------------
def is_ip_in_networks(
    ip_str: str,
    networks: Optional[Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None,
) -> bool:
    """Checks if an IP string belongs to any of the specified trusted networks."""
    try:
        ip_obj = ipaddress.ip_address(ip_str.strip())
        target_nets = networks or DEFAULT_TRUSTED_PROXIES
        return any(ip_obj in net for net in target_nets)
    except ValueError:
        return False


def get_trusted_client_ip(
    request: Any,
    trusted_cidrs: Optional[Sequence[str]] = None,
) -> str:
    """
    Extracts the validated client IP address:
    - If the direct peer connection (request.client.host) is from a trusted proxy CIDR,
      inspects X-Forwarded-For to find the true source IP.
    - If the direct connection is NOT from a trusted proxy, ignores X-Forwarded-For
      to prevent header spoofing attacks.
    """
    peer_ip = "127.0.0.1"
    if getattr(request, "client", None) and getattr(request.client, "host", None):
        peer_ip = str(request.client.host).strip()

    trusted_nets = (
        [ipaddress.ip_network(c.strip()) for c in trusted_cidrs if c.strip()]
        if trusted_cidrs
        else DEFAULT_TRUSTED_PROXIES
    )

    if not is_ip_in_networks(peer_ip, trusted_nets):
        return peer_ip

    if hasattr(request, "headers") and hasattr(request.headers, "get"):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded and isinstance(forwarded, str):
            # Parse right-to-left through the forwarded chain
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            for candidate in reversed(parts):
                if not is_ip_in_networks(candidate, trusted_nets):
                    return candidate
            if parts:
                return parts[0]

        real_ip = request.headers.get("X-Real-IP")
        if real_ip and isinstance(real_ip, str) and real_ip.strip():
            return real_ip.strip()

    return peer_ip


# ---------------------------------------------------------------------------
# Concurrent Session Management (Token-based FIFO rotation)
# ---------------------------------------------------------------------------
_background_session_tasks: Set[asyncio.Task] = set()


def _handle_session_task_done(task: asyncio.Task) -> None:
    _background_session_tasks.discard(task)
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.exception("Background session task failed: %s", exc)


async def register_session_async(user_id: str, jti: str, max_sessions: int = 2) -> None:
    """Registers a new session JTI for the user, updating in-memory cache and awaiting backing store."""
    u_id = str(user_id)
    active_list = _active_user_sessions.get(u_id, [])
    if jti not in active_list:
        active_list.append(jti)
        if len(active_list) > max_sessions:
            active_list = active_list[-max_sessions:]
        _active_user_sessions[u_id] = active_list

    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.register_session(u_id, jti, max_sessions=max_sessions)
    except Exception:
        logger.exception("Failed to register session for user %s, jti %s in store", u_id, jti)


def register_session(user_id: str, jti: str, max_sessions: int = 2) -> None:
    """Registers a new session JTI for the user (synchronous backward compatibility)."""
    u_id = str(user_id)
    active_list = _active_user_sessions.get(u_id, [])
    if jti not in active_list:
        active_list.append(jti)
        if len(active_list) > max_sessions:
            active_list = active_list[-max_sessions:]
        _active_user_sessions[u_id] = active_list

    try:
        from app.services.driver_manager import driver_manager
        loop = asyncio.get_running_loop()
        store = driver_manager.get_session_store()
        task = loop.create_task(store.register_session(u_id, jti, max_sessions=max_sessions))
        _background_session_tasks.add(task)
        task.add_done_callback(_handle_session_task_done)
    except Exception:
        pass


class AwaitableBool:
    """
    A boolean wrapper that supports both synchronous truthiness evaluation
    and asynchronous await expressions. Ensures backwards compatibility across
    sync test suites (e.g. test_auth_security.py) and async callers.
    """
    __slots__ = ("_val",)

    def __init__(self, val: bool) -> None:
        self._val = bool(val)

    def __bool__(self) -> bool:
        return self._val

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AwaitableBool):
            return self._val == other._val
        return self._val == bool(other)

    def __repr__(self) -> str:
        return repr(self._val)

    def __await__(self):
        async def _coro():
            return self._val
        return _coro().__await__()


def is_session_active(user_id: str, jti: Optional[str]) -> AwaitableBool:
    """
    Validates if a given JTI session token is active for the user.
    Option B (Strict Security Wipe): Invalidate legacy tokens without jti immediately.
    """
    if not jti:
        return AwaitableBool(False)
    u_id = str(user_id)
    if u_id not in _active_user_sessions:
        return AwaitableBool(False)
    return AwaitableBool(jti in _active_user_sessions[u_id])


async def invalidate_session_async(user_id: str, jti: Optional[str]) -> None:
    """Invalidates a single active session JTI on logout, awaiting backing store."""
    if not jti:
        return
    u_id = str(user_id)
    if u_id in _active_user_sessions:
        _active_user_sessions[u_id] = [
            s for s in _active_user_sessions[u_id] if s != jti
        ]
        if not _active_user_sessions[u_id]:
            _active_user_sessions.pop(u_id, None)

    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.invalidate_session(u_id, jti)
    except Exception:
        logger.exception("Failed to invalidate session for user %s, jti %s in store", u_id, jti)


def invalidate_session(user_id: str, jti: Optional[str]) -> None:
    """Invalidates a single active session JTI on logout (synchronous backward compatibility)."""
    if not jti:
        return
    u_id = str(user_id)
    if u_id in _active_user_sessions:
        _active_user_sessions[u_id] = [
            s for s in _active_user_sessions[u_id] if s != jti
        ]
        if not _active_user_sessions[u_id]:
            _active_user_sessions.pop(u_id, None)

    try:
        from app.services.driver_manager import driver_manager
        loop = asyncio.get_running_loop()
        store = driver_manager.get_session_store()
        task = loop.create_task(store.invalidate_session(u_id, jti))
        _background_session_tasks.add(task)
        task.add_done_callback(_handle_session_task_done)
    except Exception:
        pass


async def invalidate_all_user_sessions_async(user_id: str) -> None:
    """Invalidates all sessions for a user, awaiting backing store."""
    u_id = str(user_id)
    _active_user_sessions.pop(u_id, None)

    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.invalidate_all_user_sessions(u_id)
    except Exception:
        logger.exception("Failed to invalidate all sessions for user %s in store", u_id)


def invalidate_all_user_sessions(user_id: str) -> None:
    """Invalidates all sessions for a user (synchronous backward compatibility)."""
    u_id = str(user_id)
    _active_user_sessions.pop(u_id, None)

    try:
        from app.services.driver_manager import driver_manager
        loop = asyncio.get_running_loop()
        store = driver_manager.get_session_store()
        task = loop.create_task(store.invalidate_all_user_sessions(u_id))
        _background_session_tasks.add(task)
        task.add_done_callback(_handle_session_task_done)
    except Exception:
        pass


def create_access_token(
    user_id: str,
    username: str,
    role_name: str,
    jti: Optional[str] = None,
) -> str:
    session_id = jti or str(secrets.token_hex(16))

    now_utc = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role_name,
        "jti": session_id,
        "iat": now_utc,
        "exp": now_utc + timedelta(minutes=settings.security.session_timeout_minutes),
    }
    return jwt_encode(
        payload,
        settings.security.effective_jwt_secret,
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt_decode(
            token,
            settings.security.effective_jwt_secret,
            algorithms=[ALGORITHM],
        )
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# IP-Scoped Account Lockout Memory Manager
# ---------------------------------------------------------------------------
MAX_FAILED_ATTEMPTS_ENTRIES = 2000


def _get_lockout_key(client_ip: str, username: str) -> str:
    ip = client_ip.strip() if client_ip else "127.0.0.1"
    user = username.strip().lower() if username else ""
    return f"{ip}:{user}"


def _prune_expired_attempts() -> None:
    """Prunes expired account lockout entries and enforces max memory allocation bounds."""
    now = datetime.now(timezone.utc)
    expired = [
        k
        for k, data in _failed_attempts.items()
        if (data.get("locked_until") and data["locked_until"] <= now)
        or (
            data.get("last_attempt")
            and now - data["last_attempt"] > timedelta(minutes=30)
        )
    ]
    for k in expired:
        _failed_attempts.pop(k, None)

    if len(_failed_attempts) > MAX_FAILED_ATTEMPTS_ENTRIES:
        sorted_keys = sorted(
            _failed_attempts.keys(),
            key=lambda k: _failed_attempts[k].get("last_attempt")
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        for k in sorted_keys[: len(_failed_attempts) - MAX_FAILED_ATTEMPTS_ENTRIES]:
            _failed_attempts.pop(k, None)


def is_account_locked(
    client_ip_or_username: str, username: Optional[str] = None
) -> bool:
    """Checks if the (client_ip, username) combination is currently locked out."""
    _prune_expired_attempts()
    if username is None:
        client_ip = "127.0.0.1"
        user = client_ip_or_username
    else:
        client_ip = client_ip_or_username
        user = username
    key = _get_lockout_key(client_ip, user)
    entry = _failed_attempts.get(key)
    if not entry:
        return False
    locked_until = entry.get("locked_until")
    if locked_until and locked_until > datetime.now(timezone.utc):
        return True
    if locked_until:
        _failed_attempts.pop(key, None)
    return False


def record_failed_attempt(
    client_ip_or_username: str, username: Optional[str] = None
) -> None:
    """Records a failed login attempt for the specific (client_ip, username) pair."""
    _prune_expired_attempts()
    if username is None:
        client_ip = "127.0.0.1"
        user = client_ip_or_username
    else:
        client_ip = client_ip_or_username
        user = username
    key = _get_lockout_key(client_ip, user)
    now = datetime.now(timezone.utc)
    entry = _failed_attempts.get(key, {"count": 0})
    entry["count"] = entry.get("count", 0) + 1
    entry["last_attempt"] = now
    if entry["count"] >= 5:
        entry["locked_until"] = now + timedelta(minutes=15)
    _failed_attempts[key] = entry


def clear_failed_attempts(
    client_ip_or_username: str, username: Optional[str] = None
) -> None:
    """Clears failed attempts for the specific (client_ip, username) upon successful authentication."""
    if username is None:
        client_ip = "127.0.0.1"
        user = client_ip_or_username
    else:
        client_ip = client_ip_or_username
        user = username
    key = _get_lockout_key(client_ip, user)
    _failed_attempts.pop(key, None)
