import time
import pytest
from app.services.auth_service import (
    jwt_encode,
    jwt_decode,
    create_access_token,
    decode_access_token,
    JWTError,
)

SECRET = "super-secret-test-key-32-chars-long!"


def test_jwt_decode_missing_exp_rejected():
    payload = {"sub": "user1", "username": "alice"}
    token = jwt_encode(payload, SECRET)

    with pytest.raises(JWTError, match="Missing 'exp' claim."):
        jwt_decode(token, SECRET)


def test_jwt_decode_invalid_exp():
    payload = {"sub": "user1", "exp": "not-a-number"}
    token = jwt_encode(payload, SECRET)

    with pytest.raises(JWTError, match="Invalid 'exp' claim in JWT."):
        jwt_decode(token, SECRET)


def test_jwt_decode_expired():
    payload = {"sub": "user1", "exp": time.time() - 10}
    token = jwt_encode(payload, SECRET)

    with pytest.raises(JWTError, match="JWT token has expired."):
        jwt_decode(token, SECRET)


def test_create_access_token_includes_iat_and_exp():
    token = create_access_token("uid123", "alice", "admin")
    payload = decode_access_token(token)

    assert payload is not None
    assert "iat" in payload
    assert "exp" in payload
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["exp"], int)
    assert payload["exp"] > payload["iat"]
    assert payload["sub"] == "uid123"
