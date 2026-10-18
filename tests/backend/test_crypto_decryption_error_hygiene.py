import logging
import pytest
from app.core.crypto import decrypt_secret, DecryptionError


def test_decryption_error_does_not_leak_underlying_exception(caplog):
    caplog.set_level(logging.DEBUG, logger="ephemres.crypto")

    # 1. Malformed base64
    malformed_b64 = "ENC:v1:not-valid-base64:@@@@@@@@"
    with pytest.raises(DecryptionError) as exc_info:
        decrypt_secret(malformed_b64)

    assert str(exc_info.value) == "Malformed base64 in encrypted secret."
    assert "Incorrect padding" not in str(exc_info.value)
    assert any("Malformed base64 in encrypted secret:" in r.message for r in caplog.records)

    caplog.clear()

    # 2. Corrupted ciphertext (valid base64, but fails AES-GCM tag verification)
    import base64
    fake_nonce = base64.b64encode(b"0" * 12).decode("ascii")
    fake_ct = base64.b64encode(b"corrupted-ciphertext-without-valid-tag").decode("ascii")
    corrupted_secret = f"ENC:v1:{fake_nonce}:{fake_ct}"

    with pytest.raises(DecryptionError) as exc_info:
        decrypt_secret(corrupted_secret)

    assert str(exc_info.value) == "Failed to decrypt secret payload."
    assert "tag verification failed" not in str(exc_info.value)
    assert any("Failed to decrypt secret payload:" in r.message for r in caplog.records)
