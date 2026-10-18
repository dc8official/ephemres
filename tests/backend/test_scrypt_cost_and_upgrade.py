import base64
import hashlib
import secrets
import pytest
from app.services.auth_service import (
    hash_password,
    verify_password,
    needs_rehash,
    ScryptPasswordHasher,
)


def _generate_old_n16384_hash(plain: str) -> str:
    n, r, p = 16384, 8, 1
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        plain.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=32 * 1024 * 1024,
        dklen=64,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"$scrypt$n={n},r={r},p={p}${salt_b64}${hash_b64}"


def test_scrypt_cost_constants():
    assert ScryptPasswordHasher.N == 131072
    assert ScryptPasswordHasher.MAXMEM >= 128 * ScryptPasswordHasher.N * ScryptPasswordHasher.R


def test_scrypt_hash_verify_roundtrip():
    plain = "SuperSecurePassword999!"
    hashed = hash_password(plain)
    assert hashed.startswith(f"$scrypt$n={ScryptPasswordHasher.N},r=8,p=1$")

    assert verify_password(plain, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False
    assert needs_rehash(hashed) is False


def test_old_n16384_hash_verifies_and_needs_rehash():
    plain = "LegacyUserPassword123!"
    old_hash = _generate_old_n16384_hash(plain)

    # Old hash must still verify
    assert verify_password(plain, old_hash) is True
    assert verify_password("wrong", old_hash) is False

    # Old hash must be flagged for rehash
    assert needs_rehash(old_hash) is True
