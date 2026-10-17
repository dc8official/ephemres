import pytest
from app.routers.alerts import _mask_channel_config, _decrypt_config_dict
from app.schemas.alerts import AlertChannelCreate, AlertChannelResponse
from app.core.crypto import encrypt_secret
import json


def test_mask_channel_config_webhook_and_password():
    raw_cfg = {
        "webhook_url": "https://discord.com/api/webhooks/12345/secret_token_abc",
        "password": "SuperSecretPassword123!",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "headers": {
            "Authorization": "Bearer sensitive_bearer_token",
            "Content-Type": "application/json",
        },
    }
    masked = _mask_channel_config("DISCORD", raw_cfg)
    assert "secret_token_abc" not in masked["webhook_url"]
    assert "••••••••" in masked["webhook_url"]
    assert masked["password"] == "••••••••••••"
    assert masked["smtp_host"] == "smtp.office365.com"
    assert "sensitive_bearer_token" not in masked["headers"]["Authorization"]
    assert masked["headers"]["Content-Type"] == "application/json"


def test_decrypt_config_dict():
    cfg_data = {"webhook_url": "https://8.8.8.8/hook", "active": True}
    encrypted_blob = encrypt_secret(json.dumps(cfg_data))

    decrypted = _decrypt_config_dict(encrypted_blob)
    assert decrypted["webhook_url"] == "https://8.8.8.8/hook"
    assert decrypted["active"] is True


def test_alert_channel_create_schema():
    payload = AlertChannelCreate(
        name="Global NOC Alerts",
        channel_type="SLACK",
        is_enabled=True,
        config={"webhook_url": "https://hooks.slack.com/services/T00/B00/X00"},
        severity_filters=["DOWN", "RECOVERED"],
    )
    assert payload.name == "Global NOC Alerts"
    assert payload.channel_type == "SLACK"
