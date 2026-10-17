from __future__ import annotations

import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.core.schema import ValidationError

from app.models.alert_channel import AlertChannel
from app.schemas.alerts import AlertChannelCreate, AlertChannelUpdate
from app.services.alert_dispatcher import AlertDispatcher
from app.services.event_broker import PostgresEventBroker
from app.core.http_client import SSRFSafeBackend


@pytest.mark.anyio
async def test_ssrf_validator_pins_resolved_ip():
    """
    Verifies that SSRFSafeBackend resolves the host and invokes connect_tcp
    with the resolved target_ip rather than the host string, eliminating TOCTOU DNS rebinding.
    """
    mock_inner = AsyncMock()
    mock_inner.connect_tcp = AsyncMock(return_value="mock_stream")
    backend = SSRFSafeBackend(mock_inner, allow_private=False)

    fake_addr_info = [
        (2, 1, 6, "", ("93.184.216.34", 443))
    ]
    with patch("socket.getaddrinfo", return_value=fake_addr_info):
        stream = await backend.connect_tcp("example.com", 443)
        assert stream == "mock_stream"
        mock_inner.connect_tcp.assert_called_once()
        call_args = mock_inner.connect_tcp.call_args
        assert call_args[0][0] == "93.184.216.34"
        assert call_args[0][1] == 443


@pytest.mark.anyio
async def test_smtp_private_relay_and_metadata_isolation():
    """
    Verifies that AlertDispatcher._sync_send_smtp allows private RFC1918 and loopback
    relays while strictly blocking cloud metadata (169.254.169.254).
    """
    dispatcher = AlertDispatcher()

    # Cloud metadata IP must be blocked
    cfg_meta = {
        "smtp_host": "169.254.169.254",
        "smtp_port": 25,
        "from_address": "test@corp.com",
        "to_addresses": ["admin@corp.com"],
    }
    with pytest.raises(ValueError, match="SSRF|metadata|forbidden address"):
        dispatcher._sync_send_smtp(cfg_meta, MagicMock())

    # RFC1918 and loopback should be allowed
    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value = MagicMock()
        cfg_private = {
            "smtp_host": "10.0.0.25",
            "smtp_port": 25,
            "from_address": "test@corp.com",
            "to_addresses": ["admin@corp.com"],
        }
        dispatcher._sync_send_smtp(cfg_private, MagicMock())
        mock_smtp.assert_called_with("10.0.0.25", 25, timeout=10.0)


@pytest.mark.anyio
async def test_alert_rate_limiter_none_endpoint_isolation():
    """
    Verifies that AlertDispatcher._evaluate_and_send_channel keys non-endpoint events
    by event type and endpoint name rather than a shared 'None' key.
    """
    dispatcher = AlertDispatcher()
    channel = AlertChannel(
        id=uuid4(),
        name="Test Rate Limit",
        channel_type="GENERIC_WEBHOOK",
        is_enabled=True,
        config="{}",
        endpoint_ids=[],
        subnet_filters=[],
        severity_filters=["WARNING"],
    )

    mock_db = AsyncMock()
    now = datetime.datetime.now(datetime.timezone.utc)

    with patch.object(dispatcher, "_parse_channel_config", return_value={"url": "https://example.com"}), \
         patch.object(dispatcher, "_send_to_provider", new_callable=AsyncMock) as mock_send, \
         patch.object(dispatcher, "_record_delivery_log", new_callable=AsyncMock):
        mock_send.return_value = ("DELIVERED", 200, "OK")

        await dispatcher._evaluate_and_send_channel(
            db=mock_db,
            channel=channel,
            endpoint_id=None,
            endpoint_name="core-switch",
            ip_address="192.168.1.1",
            event_type="SYSTEM_ALERT",
            severity="WARNING",
            timestamp=now,
            details={"title": "High CPU"},
            is_flapping=False,
        )
        assert mock_send.call_count == 1
        expected_key1 = (str(channel.id), "event:SYSTEM_ALERT:core-switch", "WARNING")
        assert expected_key1 in dispatcher._rate_limits

        # Second event has different event_type, should NOT be throttled
        await dispatcher._evaluate_and_send_channel(
            db=mock_db,
            channel=channel,
            endpoint_id=None,
            endpoint_name="core-switch",
            ip_address="192.168.1.1",
            event_type="BACKUP_FAILURE",
            severity="WARNING",
            timestamp=now,
            details={"title": "Backup failed"},
            is_flapping=False,
        )
        assert mock_send.call_count == 2
        expected_key2 = (str(channel.id), "event:BACKUP_FAILURE:core-switch", "WARNING")
        assert expected_key2 in dispatcher._rate_limits


@pytest.mark.anyio
async def test_subnet_filter_inet_prefix_handling():
    """
    Verifies that _evaluate_and_send_channel cleanly handles endpoint_ip with CIDR prefix
    (e.g., '192.168.1.50/32' or '10.0.0.1/24') without raising ValueError.
    """
    dispatcher = AlertDispatcher()
    channel = AlertChannel(
        id=uuid4(),
        name="Subnet Channel",
        channel_type="GENERIC_WEBHOOK",
        is_enabled=True,
        config="{}",
        endpoint_ids=[],
        subnet_filters=["192.168.1.0/24"],
        severity_filters=["DOWN"],
    )

    mock_db = AsyncMock()
    now = datetime.datetime.now(datetime.timezone.utc)

    with patch.object(dispatcher, "_parse_channel_config", return_value={"url": "https://example.com"}), \
         patch.object(dispatcher, "_send_to_provider", new_callable=AsyncMock) as mock_send, \
         patch.object(dispatcher, "_record_delivery_log", new_callable=AsyncMock):
        mock_send.return_value = ("DELIVERED", 200, "OK")

        await dispatcher._evaluate_and_send_channel(
            db=mock_db,
            channel=channel,
            endpoint_id=uuid4(),
            endpoint_name="host-1",
            ip_address="192.168.1.50/32",
            event_type="TEST",
            severity="DOWN",
            timestamp=now,
            details={"title": "Host Down"},
            is_flapping=False,
        )
        assert mock_send.call_count == 1


def test_alert_channel_type_enum_validation():
    """
    Verifies that AlertChannelCreate / AlertChannelUpdate validates channel_type against allowed types.
    """
    # Valid channel types
    for ct in ["TEAMS", "DISCORD", "SLACK", "EMAIL_SMTP", "GENERIC_WEBHOOK"]:
        acc = AlertChannelCreate(
            name="Valid",
            channel_type=ct,
            config={"url": "https://example.com"} if ct != "EMAIL_SMTP" else {
                "smtp_host": "mail.example.com",
                "smtp_port": 25,
                "from_address": "a@b.com",
                "to_addresses": ["c@d.com"],
            },
        )
        assert acc.channel_type == ct

    # Invalid channel types
    with pytest.raises(ValidationError):
        AlertChannelCreate(
            name="Invalid",
            channel_type="PAGERDUTY",
            config={"key": "123"},
        )
    with pytest.raises(ValidationError):
        AlertChannelUpdate(
            channel_type="UNKNOWN_SERVICE",
        )


@pytest.mark.anyio
async def test_postgres_event_broker_payload_limit_protection():
    """
    Verifies that PostgresEventBroker.publish truncates payload if encoded size exceeds 7500 bytes.
    """
    mock_db_factory = MagicMock()
    mock_session = AsyncMock()
    mock_db_factory.return_value.__aenter__.return_value = mock_session
    mock_db_factory.return_value.__aexit__.return_value = None

    broker = PostgresEventBroker(mock_db_factory)

    large_payload = {
        "event_id": str(uuid4()),
        "event_type": "LARGE_EVENT",
        "symptom_endpoint_ids": [str(uuid4()) for _ in range(300)],
    }

    await broker.publish("test_channel", large_payload)

    mock_session.execute.assert_called_once()
    sql_params = mock_session.execute.call_args[0][1]

    payload_str = sql_params["payload"]
    assert len(payload_str.encode("utf-8")) <= 7500
    parsed = json.loads(payload_str)
    assert parsed.get("truncated") is True
    assert parsed.get("symptom_count") == 300
    assert len(parsed.get("symptom_endpoint_ids")) < 300
