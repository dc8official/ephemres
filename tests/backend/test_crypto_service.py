import pytest
from app.core.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)


def test_encrypt_and_decrypt_roundtrip():
    secret = "https://discord.com/api/webhooks/123456789/my-secret-token"
    enc = encrypt_secret(secret)
    assert enc.startswith("ENC:v1:")
    dec = decrypt_secret(enc)
    assert dec == secret


def test_encrypt_empty_or_idempotent():
    assert encrypt_secret("") == ""
    secret = "my-plain-secret"
    enc = encrypt_secret(secret)
    enc_again = encrypt_secret(enc)
    assert enc_again == enc


def test_decrypt_plaintext_fallback():
    # Backward compatibility: unencrypted plaintext is returned as-is
    raw = "unencrypted_api_key_123"
    assert decrypt_secret(raw) == raw
    assert decrypt_secret("") == ""


def test_mask_secret_webhook_url():
    url_with_query = "https://teams.office.com/webhook/v1/invoke?sig=SUPER_SECRET_123&env=prod"
    masked = mask_secret(url_with_query)
    assert "SUPER_SECRET_123" not in masked
    assert "••••••••" in masked
    assert "env=prod" in masked

    discord_url = "https://discord.com/api/webhooks/1234567890/very-secret-discord-token"
    masked_discord = mask_secret(discord_url)
    assert "very-secret-discord-token" not in masked_discord
    assert "••••••••" in masked_discord

    teams_url = "https://outlook.office.com/webhook/tenant123/IncomingWebhook/secretTokenOne/secretTokenTwo"
    masked_teams = mask_secret(teams_url)
    assert "secretTokenOne" not in masked_teams
    assert "secretTokenTwo" not in masked_teams
    assert "••••••••••••" in masked_teams

    teams_query_url = "https://custom.webhook.io/alert?access_token=my_secret_token&bearer=jwt_secret&code=otp123"
    masked_query = mask_secret(teams_query_url)
    assert "my_secret_token" not in masked_query
    assert "jwt_secret" not in masked_query
    assert "otp123" not in masked_query
    assert "••••••••" in masked_query


def test_mask_secret_password():
    pwd = "MySuperSecretPassword123!"
    masked = mask_secret(pwd)
    assert "MySuperSecret" not in masked
    assert masked == "••••••••••••"


def test_mask_secret_basic_auth_url():
    url = "https://admin:secret123@host.com/hook"
    masked = mask_secret(url)
    assert masked == "https://admin:••••••••@host.com/hook"
    assert "secret123" not in masked


def test_mask_all_12_sensitive_query_keys():
    keys = ["sig", "token", "key", "secret", "webhook", "auth", "api_key", "password", "access_token", "apikey", "bearer", "code"]
    query_parts = "&".join(f"{k}=val_{k}" for k in keys)
    url = f"https://example.com/api?{query_parts}"
    masked = mask_secret(url)
    for k in keys:
        assert f"val_{k}" not in masked
        assert f"{k}=••••••••" in masked

