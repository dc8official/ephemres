"""OpenSSL libcrypto through ctypes, with no app configuration.

Moved out of `core/crypto.py` in v3.3.6 so the SNMP code (`monitoring/snmp`) can use the same
binding from a standalone command (`python -m monitoring.snmp.survey`) that must not need the
app's config or database. `core/crypto.py` keeps its AES-256-GCM secret storage and imports
the loaded library from here.

`cipher()` covers the non-AEAD ciphers SNMPv3 privacy needs: AES-128-CFB (RFC 3826), AES-192 and
AES-256 CFB (the vendor extensions), DES-CBC (RFC 3414) and 3DES-EDE-CBC. OpenSSL 3 keeps DES in its "legacy" provider, which may not be installed;
`des_available()` says whether it works so callers can report it instead of failing late.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Optional


def load_libcrypto() -> ctypes.CDLL:
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
    # The SNMP ciphers are optional: a build without one (OpenSSL compiled with no-des, say)
    # must not stop secret storage, which only needs AES-256-GCM above.
    for name in ("EVP_aes_128_cfb128", "EVP_aes_192_cfb128", "EVP_aes_256_cfb128", "EVP_des_cbc", "EVP_des_ede3_cbc"):
        if hasattr(lib, name):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_void_p
            fn.argtypes = []

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

    lib.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
    lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]

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

    # OpenSSL 3 only. try_load with retain_fallbacks=1: loading a provider by name would
    # otherwise stop the default provider loading automatically, and AES-GCM needs it.
    if hasattr(lib, "OSSL_PROVIDER_try_load"):
        lib.OSSL_PROVIDER_try_load.restype = ctypes.c_void_p
        lib.OSSL_PROVIDER_try_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

    return lib


libcrypto = load_libcrypto()

_CIPHERS = {
    name: getattr(libcrypto, symbol)
    for name, symbol in (("aes-128-cfb", "EVP_aes_128_cfb128"), ("aes-192-cfb", "EVP_aes_192_cfb128"),
                         ("aes-256-cfb", "EVP_aes_256_cfb128"), ("des-cbc", "EVP_des_cbc"),
                         ("des-ede3-cbc", "EVP_des_ede3_cbc"))
    if hasattr(libcrypto, symbol)
}
_legacy_tried = False


class CipherError(Exception):
    """OpenSSL refused the cipher, key or data."""


def _load_legacy_provider() -> None:
    global _legacy_tried
    if _legacy_tried:
        return
    _legacy_tried = True
    if hasattr(libcrypto, "OSSL_PROVIDER_try_load"):
        libcrypto.OSSL_PROVIDER_try_load(None, b"legacy", 1)


def cipher(name: str, key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    """One-shot encrypt or decrypt without padding (SNMP pads DES itself; CFB needs none)."""
    if name not in _CIPHERS:
        raise CipherError(f"{name} is not available in this OpenSSL")
    if name == "des-cbc":
        _load_legacy_provider()
    ctx = libcrypto.EVP_CIPHER_CTX_new()
    if not ctx:
        raise CipherError("EVP_CIPHER_CTX_new failed")
    try:
        if libcrypto.EVP_CipherInit_ex(ctx, _CIPHERS[name](), None, key, iv, 1 if encrypt else 0) != 1:
            raise CipherError(f"{name} is not available in this OpenSSL")
        libcrypto.EVP_CIPHER_CTX_set_padding(ctx, 0)
        out = ctypes.create_string_buffer(len(data) + 32)
        out_len = ctypes.c_int(0)
        if libcrypto.EVP_CipherUpdate(ctx, out, ctypes.byref(out_len), data, len(data)) != 1:
            raise CipherError(f"{name} update failed")
        tail = ctypes.create_string_buffer(32)
        tail_len = ctypes.c_int(0)
        if libcrypto.EVP_CipherFinal_ex(ctx, tail, ctypes.byref(tail_len)) != 1:
            raise CipherError(f"{name} final failed (data not a whole number of blocks?)")
        return out.raw[: out_len.value] + tail.raw[: tail_len.value]
    finally:
        libcrypto.EVP_CIPHER_CTX_free(ctx)


_des_ok: Optional[bool] = None


def des_available() -> bool:
    global _des_ok
    if _des_ok is None:
        try:
            cipher("des-cbc", b"\x01" * 8, b"\x00" * 8, b"\x00" * 8, True)
            _des_ok = True
        except CipherError:
            _des_ok = False
    return _des_ok
