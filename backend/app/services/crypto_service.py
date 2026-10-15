from __future__ import annotations

import base64
import os
import re
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings

_SALT = b"ephemres-alert-crypto-salt-v1"
_INFO = b"ephemres-aes-256-gcm-key-derivation"
_LEGACY_SALT = b"lnmp-alert-crypto-salt-v1"
_LEGACY_INFO = b"lnmp-aes-256-gcm-key-derivation"


def _derive_key(secret_str: str, salt: bytes = _SALT, info: bytes = _INFO) -> bytes:
    master_secret = secret_str.encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    )
    return hkdf.derive(master_secret)


def _get_aes_key() -> bytes:
    return _derive_key(settings.security.effective_crypto_key)


def encrypt_secret(plaintext: str) -> str:
    """
    Encrypts sensitive credentials (webhook secrets, SMTP passwords)
    using AES-256-GCM. Produces: ENC:v1:<nonce_b64>:<ciphertext_b64>
    """
    if not plaintext:
        return ""
    if plaintext.startswith("ENC:v1:"):
        return plaintext

    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ct_b64 = base64.b64encode(ciphertext).decode("ascii")
    return f"ENC:v1:{nonce_b64}:{ct_b64}"


class DecryptionError(Exception):
    """Raised when an encrypted secret cannot be decrypted."""
    pass


def decrypt_secret(ciphertext: str) -> str:
    """
    Decrypts an AES-256-GCM encrypted payload.
    Falls back to plaintext for unencrypted strings (backward compatibility).
    Supports key rotation across effective_crypto_key and fallback_crypto_keys.
    Raises DecryptionError if an encrypted payload cannot be decrypted.
    """
    if not ciphertext:
        return ""
    if not ciphertext.startswith("ENC:v1:"):
        return ciphertext

    parts = ciphertext.split(":")
    if len(parts) != 4:
        raise DecryptionError("Malformed encrypted secret: invalid segment count.")

    try:
        nonce = base64.b64decode(parts[2])
        raw_ct = base64.b64decode(parts[3])
    except Exception as exc:
        raise DecryptionError(f"Malformed base64 in encrypted secret: {exc}") from exc

    # Candidate keys for decryption (effective key first, then fallback keys)
    candidate_keys = [settings.security.effective_crypto_key]
    for fb in settings.security.fallback_crypto_keys:
        if fb and fb not in candidate_keys:
            candidate_keys.append(fb)

    last_exc = None
    for key_str in candidate_keys:
        for s, i in [(_SALT, _INFO), (_LEGACY_SALT, _LEGACY_INFO)]:
            try:
                key = _derive_key(key_str, salt=s, info=i)
                aesgcm = AESGCM(key)
                decrypted = aesgcm.decrypt(nonce, raw_ct, None)
                return decrypted.decode("utf-8")
            except Exception as exc:
                last_exc = exc
                continue

    raise DecryptionError(f"Failed to decrypt secret payload: {last_exc}") from last_exc


def mask_secret(secret: str) -> str:
    """
    Masks webhook URLs (e.g. https://.../invoke?sig=••••••••) and passwords (••••••••••••).
    """
    if not secret:
        return ""

    if secret.startswith("ENC:v1:"):
        try:
            secret = decrypt_secret(secret)
        except DecryptionError:
            return "••••••••"

    if secret.startswith("http://") or secret.startswith("https://"):
        try:
            parsed = urlparse(secret)
            # Mask query parameters
            query_params = parse_qsl(parsed.query, keep_blank_values=True)
            sensitive_keys = {
                "sig", "token", "key", "secret", "webhook", "auth",
                "api_key", "password", "access_token", "apikey", "bearer", "code",
            }
            if query_params:
                masked_params = []
                for k, v in query_params:
                    if k.lower() in sensitive_keys:
                        masked_params.append((k, "••••••••"))
                    else:
                        masked_params.append((k, v))
                new_query = unquote(urlencode(masked_params))
            else:
                new_query = parsed.query

            path = parsed.path
            # Mask webhook path tokens (e.g. Discord: /api/webhooks/<id>/<token>)
            if "/api/webhooks/" in path:
                segments = path.strip("/").split("/")
                if len(segments) >= 3:
                    # segments[0] = api, segments[1] = webhooks, segments[2] = id, segments[3] = token
                    segments[-1] = "••••••••"
                    path = "/" + "/".join(segments)
            # Mask Slack webhook path (e.g. /services/T.../B.../token)
            elif "/services/T" in path:
                segments = path.strip("/").split("/")
                if len(segments) >= 3:
                    segments[-1] = "••••••••"
                    path = "/" + "/".join(segments)
            # Mask Microsoft Teams incoming webhook path (e.g. /IncomingWebhook/<token1>/<token2>)
            elif re.search(r"/IncomingWebhook/([^/]+)/([^/?]+)", path, flags=re.IGNORECASE):
                path = re.sub(
                    r"(/IncomingWebhook/)([^/]+)/([^/?]+)",
                    r"\1••••••••••••/••••••••••••",
                    path,
                    flags=re.IGNORECASE,
                )

            # Mask embedded basic auth credentials in netloc (e.g. https://user:pass@host/path)
            netloc = parsed.netloc
            if "@" in netloc:
                user_info, host = netloc.split("@", 1)
                if ":" in user_info:
                    user, _ = user_info.split(":", 1)
                    netloc = f"{user}:••••••••@{host}"
                else:
                    netloc = f"••••••••@{host}"

            masked_url = urlunparse((
                parsed.scheme,
                netloc,
                path,
                parsed.params,
                new_query,
                parsed.fragment,
            ))
            return masked_url
        except Exception:
            return "https://••••••••"

    return "••••••••••••"
