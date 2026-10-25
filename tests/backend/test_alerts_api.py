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


def test_alert_delivery_log_delivered_at_alias():
    from app.models.alert_delivery_log import AlertDeliveryLog
    from datetime import datetime, timezone
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    log = AlertDeliveryLog(
        id=uuid4(),
        channel_id=uuid4(),
        channel_name="Test",
        endpoint_name="router1",
        event_type="STATE_TRANSITION",
        status="SUCCESS",
        timestamp=now,
    )
    assert log.timestamp == now
    assert log.delivered_at == now


@pytest.mark.anyio
async def test_get_alert_history_compilation():
    from app.models.alert_delivery_log import AlertDeliveryLog
    from app.core.orm import select, CompileContext

    stmt = select(AlertDeliveryLog).order_by(AlertDeliveryLog.timestamp.desc()).offset(0).limit(50)
    ctx = CompileContext()
    sql = stmt.compile(ctx)
    assert "ORDER BY" in sql
    assert "timestamp" in sql
    assert "DESC" in sql
