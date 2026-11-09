"""Sign-in building blocks for the API, written against the standard library only.

- Passwords: scrypt hashes (hashlib.scrypt, from OpenSSL).
- Session tokens: HS256 JSON Web Tokens (RFC 7519), signed with the server's secret and sent
  as an HttpOnly cookie. Each token carries a session ID (jti) that must also be in the session
  store, so a session can be ended before its token expires.
- The client's real IP behind the nginx reverse proxy, used to key sign-in lockouts.

Sessions and lockouts live only in the shared session store (services/session_store.py): each
API worker is a separate process, so nothing here keeps its own copy.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from app.config import settings
from app.services.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom OpenSSL hashlib.scrypt Password Hasher (Zero External Dependencies)
# ---------------------------------------------------------------------------
class ScryptPasswordHasher:
    """
    Zero-dependency password hasher backed by OpenSSL hashlib.scrypt.
    Format: $scrypt$n=131072,r=8,p=1$<salt_b64>$<hash_b64> (older n=16384 hashes verify and are re-hashed on login)
    """

    # Cost: scrypt needs 128 * N * r bytes, so 128 MiB per check. MAXMEM is OpenSSL's ceiling,
    # set with room above that. DKLEN is the stored hash length in bytes.
    N = 2**17
    R = 8
    P = 1
    MAXMEM = 256 * 1024 * 1024
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
        # Older releases hashed with argon2, a third-party library the zero-dependency rule
        # removed. Those accounts can't be verified any more and are reset by the script.
        if hashed.startswith("$argon2"):
            logger.warning("Legacy argon2 hash for login attempt; run app.reset_legacy_passwords")
            return False
        parts = hashed.split("$")
        if len(parts) != 5 or parts[1] != "scrypt":
            return False
        # The cost and length come from the stored hash, not the class constants, so hashes
        # made with older settings still verify (and needs_rehash() upgrades them at sign-in).
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
            # Constant-time comparison: the time taken must not show how many bytes matched.
            return hmac.compare_digest(derived, expected_dk)
        except Exception:
            return False


_ph = ScryptPasswordHasher()

# Each scrypt run at N=2**17, r=8 needs 128 MiB. Sign-in floods (any username, since unknown
# ones spend a dummy check) would otherwise run one per thread at once and could exhaust
# memory, so at most SCRYPT_CONCURRENCY run at once per worker.
SCRYPT_CONCURRENCY = max(2, min(4, os.cpu_count() or 2))
_SCRYPT_SLOTS = threading.BoundedSemaphore(SCRYPT_CONCURRENCY)

# The async helpers run scrypt on these threads of its own, never in asyncio's default pool.
# A flood then waits in this pool's queue. In the default pool the waiting requests used to
# occupy its threads (each blocked on _SCRYPT_SLOTS), and everything else that uses it (every
# FastStore command, PDF rendering, SMTP) stalled behind them: 3.6 s for FastStore in a 32-way
# sign-in flood (scale review S3). Started lazily: scripts that never sign in make no threads.
_scrypt_pool: Optional[ThreadPoolExecutor] = None


def _run_scrypt(fn: Any, *args: Any) -> "asyncio.Future[Any]":
    global _scrypt_pool
    if _scrypt_pool is None:
        _scrypt_pool = ThreadPoolExecutor(max_workers=SCRYPT_CONCURRENCY, thread_name_prefix="scrypt")
    return asyncio.get_running_loop().run_in_executor(_scrypt_pool, fn, *args)


def _hash_bounded(password: str) -> str:
    with _SCRYPT_SLOTS:
        return _ph.hash(password)


def _verify_bounded(hashed: str, plain: str) -> bool:
    with _SCRYPT_SLOTS:
        return _ph.verify(hashed, plain)


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
    # Accept only the canonical spelling of each segment: characters outside the alphabet or
    # stray low bits would decode to the same bytes, and are refused. Trailing = is tolerated.
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

    # The algorithm is pinned to HS256 whatever the token says, which shuts out "alg": "none"
    # and algorithm-swap tricks (RFC 8725 2.1 and 3.1). The signature is checked before the
    # payload is even parsed.

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


# Default trusted reverse proxy CIDR networks (strictly loopback): nginx runs on the same host.
DEFAULT_TRUSTED_PROXIES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def generate_readable_password() -> str:
    """Generate high-entropy password (96 bits of entropy via secrets.token_urlsafe(16))."""
    return secrets.token_urlsafe(16)


def hash_password(password: str) -> str:
    """Synchronous OpenSSL scrypt password hashing."""
    return _hash_bounded(password)


async def hash_password_async(password: str) -> str:
    """Non-blocking OpenSSL scrypt password hashing on scrypt's own threads (_run_scrypt)."""
    return await _run_scrypt(_hash_bounded, password)


def verify_password(plain: str, hashed: str) -> bool:
    """Synchronous OpenSSL scrypt password verification."""
    try:
        return _verify_bounded(hashed, plain)
    except Exception:
        return False


async def verify_password_async(plain: str, hashed: str) -> bool:
    """Non-blocking OpenSSL scrypt password verification on scrypt's own threads (_run_scrypt)."""
    return await _run_scrypt(verify_password, plain, hashed)


_dummy_password_hash: Optional[str] = None


async def burn_password_check_async(plain: str) -> None:
    """Spends the same scrypt work as a real check, for unknown or disabled usernames.

    Without it those sign-ins answer in a few milliseconds while real accounts take one scrypt
    verification, which tells an attacker which usernames exist.
    """
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = await hash_password_async(secrets.token_urlsafe(16))
    await verify_password_async(plain or "", _dummy_password_hash)


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

    # The peer is a trusted proxy, so X-Forwarded-For can be believed, but only partly: each
    # proxy appends the address it received from, and the client can put anything it likes
    # at the start. Walking from the right, skip our own proxies; the first other address is
    # the client as our outermost proxy saw it.
    if hasattr(request, "headers") and hasattr(request.headers, "get"):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded and isinstance(forwarded, str):
            # Parse right-to-left through the forwarded chain
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            for candidate in reversed(parts):
                if not is_ip_in_networks(candidate, trusted_nets):
                    return candidate
            if parts:
                return peer_ip

        real_ip = request.headers.get("X-Real-IP")
        if real_ip and isinstance(real_ip, str) and real_ip.strip():
            return real_ip.strip()

    return peer_ip


async def register_session_async(user_id: str, jti: str, max_sessions: int = 2) -> None:
    """Records a new session (jti) for the user in the shared session store; past
    `max_sessions` the store ends the user's oldest one.

    A store failure is only logged. That fails closed: the token is issued, but every request
    with it gets 401 because its jti is not in the store."""
    u_id = str(user_id)
    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.register_session(u_id, jti, max_sessions=max_sessions)
    except Exception:
        logger.exception("Failed to register session for user %s, jti %s in store", u_id, jti)


async def is_session_active(user_id: str, jti: Optional[str]) -> bool:
    """
    Validates a session JTI against the configured backing session store, which every API
    worker shares. The store is the only authority, because a logout, password change or
    reset handled by another worker only reaches the store. A store error propagates: nothing is allowed through, and the caller can
    say "try again" instead of signing the user out of a session that is still valid.
    Tokens without a jti are rejected.
    """
    if not jti:
        return False
    from app.services.driver_manager import driver_manager

    store = driver_manager.get_session_store()
    return bool(await store.is_session_active(str(user_id), jti))


async def invalidate_session_async(user_id: str, jti: Optional[str]) -> None:
    """Ends one session (sign-out) in the shared session store."""
    if not jti:
        return
    u_id = str(user_id)
    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.invalidate_session(u_id, jti)
    except Exception:
        logger.exception("Failed to invalidate session for user %s, jti %s in store", u_id, jti)


async def invalidate_all_user_sessions_async(user_id: str, except_jti: Optional[str] = None) -> None:
    """Ends every session of a user in the shared session store, except `except_jti` when given
    (the session making a password change stays signed in)."""
    u_id = str(user_id)
    try:
        from app.services.driver_manager import driver_manager
        store = driver_manager.get_session_store()
        await store.invalidate_all_user_sessions(u_id, except_jti=except_jti)
    except Exception:
        logger.exception("Failed to invalidate all sessions for user %s in store", u_id)


def create_access_token(
    user_id: str,
    username: str,
    role_name: str,
    jti: Optional[str] = None,
) -> str:
    session_id = jti or str(secrets.token_hex(16))

    now_utc = datetime.now(timezone.utc)
    # username and role are for display only: the API reads the role from the database on
    # every request (routers/auth.get_current_user), so a changed role applies at once.
    payload = {
        "sub": user_id,
        "username": username,
        "role": role_name,
        "jti": session_id,
        "iat": now_utc,
        "exp": now_utc + timedelta(minutes=get_runtime_settings().session_timeout_minutes),
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


# Lockouts count per (client IP, username), never per username alone: a per-username lockout
# would let anyone lock the real admin out by failing on purpose. Usernames are lowercased.
def _get_lockout_key(client_ip: str, username: str) -> str:
    ip = client_ip.strip() if client_ip else "127.0.0.1"
    user = username.strip().lower() if username else ""
    return f"{ip}:{user}"


