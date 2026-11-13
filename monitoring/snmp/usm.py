"""SNMPv3 user-based security (USM): keys, message authentication and privacy.

Standards followed:
- RFC 3414: password-to-key (1 MB of the repeated password hashed), key localisation to the
  device's engine ID, HMAC-MD5-96 and HMAC-SHA-96, and DES-CBC privacy.
- RFC 7860: HMAC-SHA-2 (224/256/384/512, truncated to 16/24/32/48 bytes), same key method.
- RFC 3826: AES-128-CFB privacy, IV = engine boots + engine time + 64-bit salt.

Beyond the RFCs, because devices use them (docs/architecture/snmp-vendor-research.md):
- AES-192 and AES-256 CFB, same IV as AES-128. A 16 or 20 byte MD5/SHA-1 localised key is too
  short for them, and vendors stretch it in two incompatible ways, so both are offered:
  * "aes192" / "aes256": draft-blumenthal-aes-usm-04, Kul' = Kul || H(Kul) || ... (net-snmp
    AES-256, FortiGate aes256). Checked against the draft's appendix A.1 sample.
  * "aes192c" / "aes256c": the Reeder method from draft-reeder-snmpv3-usm-3desede-00: run the whole
    password-to-key again with the previous localised key as the password (Cisco, HPE Aruba
    AOS-S, FortiGate aes256cisco, net-snmp AES-256-C). Checked against the draft's appendix B.
- "3des": 3DES-EDE-CBC from the same Reeder draft (Junos, older Cisco): 32-byte Reeder key, the
  first 24 bytes are the 3DES key, the last 8 a pre-IV XORed with the salt as for DES. The draft
  suggests hashing the salt first ("SHOULD"); net-snmp and the other implementations we know of
  do not, so neither do we.

With a SHA-2 authentication protocol the localised key is long enough already and the two AES
methods give the same key (the stretching only runs when the key is short).

Keys are derived once per (password, engine ID) and cached by the caller; deriving hashes
1 MB (twice for the Reeder method), a few milliseconds, too slow to repeat on every request.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from app.core import libcrypto

# name -> (hash constructor, bytes of the digest sent on the wire)
AUTH_PROTOCOLS: Dict[str, Tuple[Callable, int]] = {
    "md5": (hashlib.md5, 12),
    "sha": (hashlib.sha1, 12),
    "sha224": (hashlib.sha224, 16),
    "sha256": (hashlib.sha256, 24),
    "sha384": (hashlib.sha384, 32),
    "sha512": (hashlib.sha512, 48),
}

# name -> (OpenSSL cipher, key bytes, key stretching method)
_PRIV: Dict[str, Tuple[str, int, str]] = {
    "des": ("des-cbc", 16, "none"),  # 8 key bytes + 8 pre-IV bytes
    "3des": ("des-ede3-cbc", 32, "reeder"),  # 24 key bytes + 8 pre-IV bytes
    "aes": ("aes-128-cfb", 16, "none"),
    "aes128": ("aes-128-cfb", 16, "none"),
    "aes192": ("aes-192-cfb", 24, "blumenthal"),
    "aes256": ("aes-256-cfb", 32, "blumenthal"),
    "aes192c": ("aes-192-cfb", 24, "reeder"),
    "aes256c": ("aes-256-cfb", 32, "reeder"),
}
PRIV_PROTOCOLS = tuple(_PRIV)
_DES_FAMILY = ("des", "3des")

# Report OIDs (RFC 3414 usmStats, RFC 3413 snmpTarget contexts, RFC 3412 MPD): the agent
# answers a Report carrying one of these instead of a Response; they say what was wrong.
USM_STATS = {
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 1, 0): "unsupported security level (no such user at this level, or the device cannot do SNMPv3 privacy at all)",
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0): "not in time window",
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0): "unknown user name",
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0): "unknown engine ID",
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 5, 0): "wrong digest (authentication password or protocol is wrong)",
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 6, 0): "decryption error (privacy password or protocol is wrong)",
    (1, 3, 6, 1, 6, 3, 12, 1, 4, 0): "unavailable context",
    (1, 3, 6, 1, 6, 3, 12, 1, 5, 0): "unknown context (check the context name)",
    (1, 3, 6, 1, 6, 3, 11, 2, 1, 3, 0): "unknown PDU handler",
}
NOT_IN_TIME_WINDOW = (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0)
UNKNOWN_ENGINE_ID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0)

_MEGABYTE = 1_048_576


class UsmError(Exception):
    """Bad credentials or a message that fails a security check."""


def password_to_key(password: bytes, auth: str) -> bytes:
    """RFC 3414 A.2: the hash of the password repeated to fill exactly 1 MB."""
    if len(password) < 8:
        # RFC 3414 section 11.2 sets 8 characters as the minimum; agents refuse shorter ones,
        # so say it here rather than as a confusing "wrong digest" from the device.
        raise UsmError("SNMPv3 passwords must be at least 8 characters")
    hash_fn = AUTH_PROTOCOLS[auth][0]
    h = hash_fn()
    repeated = password * (_MEGABYTE // len(password) + 1)
    h.update(repeated[:_MEGABYTE])
    return h.digest()


def localize_key(key: bytes, engine_id: bytes, auth: str) -> bytes:
    hash_fn = AUTH_PROTOCOLS[auth][0]
    return hash_fn(key + engine_id + key).digest()


def privacy_key(password: bytes, engine_id: bytes, auth: str, priv: str) -> bytes:
    """The localised privacy key, stretched to the cipher's needs (see the module docstring).

    Both stretching methods start from the standard localised key, so every protocol shares
    its first bytes; they differ only in how the missing bytes are made."""
    _, needed, method = _PRIV[priv]
    hash_fn = AUTH_PROTOCOLS[auth][0]
    key = localize_key(password_to_key(password, auth), engine_id, auth)
    block = key
    while len(key) < needed:
        if method == "blumenthal":
            key += hash_fn(key).digest()
        elif method == "reeder":
            block = localize_key(password_to_key(block, auth), engine_id, auth)
            key += block
        else:
            raise UsmError(f"{auth} gives too short a key for {priv}")
    return key[:needed]


@dataclass(frozen=True)
class LocalizedKeys:
    auth: Optional[str]
    auth_key: Optional[bytes]
    priv: Optional[str]
    priv_key: Optional[bytes]

    @property
    def digest_len(self) -> int:
        return AUTH_PROTOCOLS[self.auth][1] if self.auth else 0


def derive_keys(engine_id: bytes, auth: Optional[str], auth_password: Optional[bytes],
                priv: Optional[str], priv_password: Optional[bytes]) -> LocalizedKeys:
    """`priv_password=None` means "same as the authentication password", which Meraki and
    UniFi require (one password for both)."""
    if priv and not auth:
        raise UsmError("SNMPv3 privacy needs authentication too (authPriv)")
    if auth and auth not in AUTH_PROTOCOLS:
        raise UsmError(f"unknown authentication protocol {auth!r}")
    if priv and priv not in _PRIV:
        raise UsmError(f"unknown privacy protocol {priv!r}")
    auth_key = localize_key(password_to_key(auth_password or b"", auth), engine_id, auth) if auth else None
    priv_key = None
    if priv:
        # The privacy key is made with the authentication protocol's hash (RFC 3414 / 3826).
        password = priv_password if priv_password is not None else (auth_password or b"")
        priv_key = privacy_key(password, engine_id, auth, priv)
    return LocalizedKeys(auth, auth_key, priv, priv_key)


def sign(keys: LocalizedKeys, whole_message: bytes) -> bytes:
    """The digest of the whole message, sent with its authentication field set to zeros."""
    hash_fn, length = AUTH_PROTOCOLS[keys.auth]
    return hmac.new(keys.auth_key, whole_message, hash_fn).digest()[:length]


def verify(keys: LocalizedKeys, message_with_zeroed_digest: bytes, received: bytes) -> bool:
    return hmac.compare_digest(sign(keys, message_with_zeroed_digest), received)


class SaltCounter:
    """Salts must not repeat under one key; a random start and a counter does that."""

    def __init__(self) -> None:
        self._value = int.from_bytes(os.urandom(8), "big")

    def next(self) -> int:
        self._value = (self._value + 1) & 0xFFFFFFFFFFFFFFFF
        return self._value


def _des_iv(keys: LocalizedKeys, priv_params: bytes) -> bytes:
    pre_iv = keys.priv_key[-8:]
    return bytes(a ^ b for a, b in zip(pre_iv, priv_params))


def encrypt(keys: LocalizedKeys, plaintext: bytes, boots: int, time: int, salt: SaltCounter) -> Tuple[bytes, bytes]:
    """(ciphertext, msgPrivacyParameters)."""
    if keys.priv not in _PRIV:
        raise UsmError("no privacy protocol")
    cipher, needed, _ = _PRIV[keys.priv]
    if keys.priv in _DES_FAMILY:
        priv_params = boots.to_bytes(4, "big") + (salt.next() & 0xFFFFFFFF).to_bytes(4, "big")
        # CBC needs whole 8-byte blocks; the receiver reads the BER length and ignores the
        # padding, so its value does not matter.
        padded = plaintext + b"\x00" * (-len(plaintext) % 8)
        return libcrypto.cipher(cipher, keys.priv_key[:needed - 8], _des_iv(keys, priv_params), padded, True), priv_params
    priv_params = salt.next().to_bytes(8, "big")
    iv = boots.to_bytes(4, "big") + time.to_bytes(4, "big") + priv_params
    return libcrypto.cipher(cipher, keys.priv_key[:needed], iv, plaintext, True), priv_params


def decrypt(keys: LocalizedKeys, ciphertext: bytes, priv_params: bytes, boots: int, time: int) -> bytes:
    if keys.priv not in _PRIV:
        raise UsmError("no privacy protocol")
    if len(priv_params) != 8:
        raise UsmError("privacy parameters must be 8 bytes")
    cipher, needed, _ = _PRIV[keys.priv]
    try:
        if keys.priv in _DES_FAMILY:
            if len(ciphertext) % 8:
                raise UsmError("DES data is not a whole number of blocks")
            return libcrypto.cipher(cipher, keys.priv_key[:needed - 8], _des_iv(keys, priv_params), ciphertext, False)
        iv = boots.to_bytes(4, "big") + time.to_bytes(4, "big") + priv_params
        return libcrypto.cipher(cipher, keys.priv_key[:needed], iv, ciphertext, False)
    except libcrypto.CipherError as exc:
        raise UsmError(str(exc)) from exc
