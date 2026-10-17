from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import os
import re
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from app.config import settings

_SALT = b"ephemres-alert-crypto-salt-v1"
_INFO = b"ephemres-aes-256-gcm-key-derivation"
_LEGACY_SALT = b"lnmp-alert-crypto-salt-v1"
_LEGACY_INFO = b"lnmp-aes-256-gcm-key-derivation"

# OpenSSL EVP AEAD control constants (<openssl/evp.h>)
EVP_CTRL_AEAD_SET_IVLEN = 0x09
EVP_CTRL_AEAD_GET_TAG = 0x10
EVP_CTRL_AEAD_SET_TAG = 0x11
GCM_IV_LEN = 12
GCM_TAG_LEN = 16


def _load_libcrypto() -> ctypes.CDLL:
    for candidate in ("libcrypto.so.3", "libcrypto.so.1.1", ctypes.util.find_library("crypto") or "libcrypto.so"):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("Could not load OpenSSL libcrypto shared library.")

    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_new.argtypes = []

    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

    lib.EVP_aes_256_gcm.restype = ctypes.c_void_p
    lib.EVP_aes_256_gcm.argtypes = []

    lib.EVP_CipherInit_ex.restype = ctypes.c_int
    lib.EVP_CipherInit_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
    ]

    lib.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int
    lib.EVP_CIPHER_CTX_ctrl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
    ]

    lib.EVP_CipherUpdate.restype = ctypes.c_int
    lib.EVP_CipherUpdate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.c_int,
    ]

    lib.EVP_CipherFinal_ex.restype = ctypes.c_int
    lib.EVP_CipherFinal_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
    ]

    return lib


_libcrypto = _load_libcrypto()


# ---------------------------------------------------------------------------
# Pure-Python RFC 5869 HKDF-SHA256
# ---------------------------------------------------------------------------
def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    if not salt:
        salt = b"\x00" * hashlib.sha256().digest_size
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    okm = b""
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


def _derive_key(secret_str: str, salt: bytes = _SALT, info: bytes = _INFO) -> bytes:
    master_secret = secret_str.encode("utf-8")
    prk = hkdf_extract(salt, master_secret)
    return hkdf_expand(prk, info, length=32)


def _get_aes_key() -> bytes:
    return _derive_key(settings.security.effective_crypto_key)


class DecryptionError(Exception):
    """Raised when an encrypted secret cannot be decrypted."""
    pass


# ---------------------------------------------------------------------------
# OpenSSL libcrypto.so.3 EVP AES-256-GCM Implementation
# ---------------------------------------------------------------------------
def _aes256_gcm_encrypt(
    key: bytes,
    nonce: bytes,
    plaintext: bytes,
    aad: Optional[bytes] = None,
) -> bytes:
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes.")
    if len(nonce) != GCM_IV_LEN:
        raise ValueError(f"AES-256-GCM nonce must be {GCM_IV_LEN} bytes.")

    ctx = _libcrypto.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed.")

    try:
        cipher = _libcrypto.EVP_aes_256_gcm()
        if _libcrypto.EVP_CipherInit_ex(ctx, cipher, None, None, None, 1) != 1:
            raise RuntimeError("EVP_CipherInit_ex (cipher init) failed.")
        if _libcrypto.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_SET_IVLEN, len(nonce), None) != 1:
            raise RuntimeError("EVP_CIPHER_CTX_ctrl (SET_IVLEN) failed.")
        if _libcrypto.EVP_CipherInit_ex(ctx, None, None, key, nonce, 1) != 1:
            raise RuntimeError("EVP_CipherInit_ex (key/IV init) failed.")

        if aad:
            aad_len = ctypes.c_int(0)
            if _libcrypto.EVP_CipherUpdate(ctx, None, ctypes.byref(aad_len), aad, len(aad)) != 1:
                raise RuntimeError("EVP_CipherUpdate (AAD) failed.")

        out_buf = ctypes.create_string_buffer(len(plaintext) + 16)
        out_len = ctypes.c_int(0)
        if _libcrypto.EVP_CipherUpdate(ctx, out_buf, ctypes.byref(out_len), plaintext, len(plaintext)) != 1:
            raise RuntimeError("EVP_CipherUpdate (plaintext) failed.")

        final_buf = ctypes.create_string_buffer(16)
        final_len = ctypes.c_int(0)
        if _libcrypto.EVP_CipherFinal_ex(ctx, final_buf, ctypes.byref(final_len)) != 1:
            raise RuntimeError("EVP_CipherFinal_ex failed.")

        tag_buf = ctypes.create_string_buffer(GCM_TAG_LEN)
        if _libcrypto.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_GET_TAG, GCM_TAG_LEN, tag_buf) != 1:
            raise RuntimeError("EVP_CIPHER_CTX_ctrl (GET_TAG) failed.")

        ciphertext = out_buf.raw[: out_len.value] + final_buf.raw[: final_len.value]
        return ciphertext + tag_buf.raw[:GCM_TAG_LEN]
    finally:
        _libcrypto.EVP_CIPHER_CTX_free(ctx)


def _aes256_gcm_decrypt(
    key: bytes,
    nonce: bytes,
    data: bytes,
    aad: Optional[bytes] = None,
) -> bytes:
    if len(key) != 32:
        raise DecryptionError("AES-256-GCM key must be 32 bytes.")
    if len(nonce) != GCM_IV_LEN:
        raise DecryptionError(f"AES-256-GCM nonce must be {GCM_IV_LEN} bytes.")
    if len(data) < GCM_TAG_LEN:
        raise DecryptionError("Ciphertext too short to contain GCM authentication tag.")

    ciphertext = data[:-GCM_TAG_LEN]
    tag = data[-GCM_TAG_LEN:]

    ctx = _libcrypto.EVP_CIPHER_CTX_new()
    if not ctx:
        raise DecryptionError("EVP_CIPHER_CTX_new failed.")

    try:
        cipher = _libcrypto.EVP_aes_256_gcm()
        if _libcrypto.EVP_CipherInit_ex(ctx, cipher, None, None, None, 0) != 1:
            raise DecryptionError("EVP_CipherInit_ex (cipher init) failed.")
        if _libcrypto.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_SET_IVLEN, len(nonce), None) != 1:
            raise DecryptionError("EVP_CIPHER_CTX_ctrl (SET_IVLEN) failed.")
        if _libcrypto.EVP_CipherInit_ex(ctx, None, None, key, nonce, 0) != 1:
            raise DecryptionError("EVP_CipherInit_ex (key/IV init) failed.")

        if aad:
            aad_len = ctypes.c_int(0)
            if _libcrypto.EVP_CipherUpdate(ctx, None, ctypes.byref(aad_len), aad, len(aad)) != 1:
                raise DecryptionError("EVP_CipherUpdate (AAD) failed.")

        out_buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        out_len = ctypes.c_int(0)
        if _libcrypto.EVP_CipherUpdate(ctx, out_buf, ctypes.byref(out_len), ciphertext, len(ciphertext)) != 1:
            raise DecryptionError("EVP_CipherUpdate (ciphertext) failed.")

        tag_buf = ctypes.create_string_buffer(tag, GCM_TAG_LEN)
        if _libcrypto.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_SET_TAG, GCM_TAG_LEN, tag_buf) != 1:
            raise DecryptionError("EVP_CIPHER_CTX_ctrl (SET_TAG) failed.")

        final_buf = ctypes.create_string_buffer(16)
        final_len = ctypes.c_int(0)
        if _libcrypto.EVP_CipherFinal_ex(ctx, final_buf, ctypes.byref(final_len)) != 1:
            raise DecryptionError("GCM authentication tag verification failed.")

        return out_buf.raw[: out_len.value] + final_buf.raw[: final_len.value]
    finally:
        _libcrypto.EVP_CIPHER_CTX_free(ctx)


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
    nonce = os.urandom(GCM_IV_LEN)
    ciphertext = _aes256_gcm_encrypt(key, nonce, plaintext.encode("utf-8"), None)
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ct_b64 = base64.b64encode(ciphertext).decode("ascii")
    return f"ENC:v1:{nonce_b64}:{ct_b64}"


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
        nonce = base64.b64decode(parts[2], validate=True)
        raw_ct = base64.b64decode(parts[3], validate=True)
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
                decrypted = _aes256_gcm_decrypt(key, nonce, raw_ct, None)
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
