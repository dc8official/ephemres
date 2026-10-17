import os
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.main import app
from app.routers.alerts import _merge_channel_config, _mask_channel_config
from app.services.alert_dispatcher import AlertDispatcher
from app.services.topology import TopologyGraphManager
from monitoring.ping import PingResult
from monitoring.state_machine import EndpointState, StateMachine


def test_cors_middleware_includes_put():
    """Verify that CORSMiddleware has 'PUT' explicitly enabled in allow_methods."""
    cors_found = False
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            cors_found = True
            allow_methods = middleware.kwargs.get("allow_methods", [])
            assert "PUT" in allow_methods, f"PUT not found in CORSMiddleware allow_methods: {allow_methods}"
            assert "GET" in allow_methods
            assert "POST" in allow_methods
            assert "DELETE" in allow_methods
            assert "OPTIONS" in allow_methods
    assert cors_found, "CORSMiddleware not configured in FastAPI app"


@pytest.mark.anyio
async def test_state_machine_transition_payload_includes_hostname_and_ip():
    """Verify that StateMachine carries forward hostname and ip_address and emits them in transition_payload."""
    sm = StateMachine(confirmation_threshold=1)
    ep_id = uuid4()
    event_id = uuid4()

    state = EndpointState(
        endpoint_id=ep_id,
        active_event_id=event_id,
        confirmed_operational_state="UP",
        confirmed_detailed_state="UP",
        pending_detailed_state="DOWN",
        pending_cycle_count=0,
        hostname="router-01.corp",
        ip_address="192.168.10.1",
    )

    mock_db = AsyncMock()
    mock_row = MagicMock()
    mock_row.id = uuid4()
    mock_res = MagicMock()
    mock_res.fetchone.return_value = mock_row
    mock_db.execute.return_value = mock_res

    ping_result = PingResult(success_count=0, failed_count=5, avg_rtt_ms=None)

    published_payloads = []
    async def mock_broadcast(event_type, payload):
        if event_type == "STATE_TRANSITION":
            published_payloads.append(payload)

    with patch("app.services.topology.topology_manager.update_node_status", new_callable=AsyncMock), \
         patch("app.routers.events.broadcast_sse_event", side_effect=mock_broadcast), \
         patch("app.services.driver_manager.driver_manager.get_event_broker", return_value=None), \
         patch("app.services.rca_engine.run_differential_rca", new_callable=AsyncMock):

        next_state = await sm.process_cycle(state, ping_result, mock_db)
        assert next_state.hostname == "router-01.corp"
        assert next_state.ip_address == "192.168.10.1"
        assert next_state.confirmed_operational_state == "DOWN"

        import asyncio
        await asyncio.sleep(0.02)

        assert len(published_payloads) == 1
        payload = published_payloads[0]
        assert payload["hostname"] == "router-01.corp"
        assert payload["ip_address"] == "192.168.10.1"
        assert payload["endpoint_id"] == str(ep_id)
        assert payload["operational_state"] == "DOWN"


@pytest.mark.anyio
async def test_alert_dispatcher_metadata_enrichment_from_topology_manager():
    """Verify fallback enrichment in _dispatch_event using topology_manager.get_node."""
    dispatcher = AlertDispatcher()
    ep_id = uuid4()

    event = {
        "event_type": "STATE_TRANSITION",
        "endpoint_id": str(ep_id),
        "endpoint_name": "Unknown Endpoint",
        "ip_address": "0.0.0.0",
        "operational_state": "DOWN",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": {},
    }

    mock_topo = MagicMock(spec=TopologyGraphManager)
    mock_topo.get_node.return_value = {
        "id": str(ep_id),
        "label": "Core-Switch-A",
        "ip_address": "10.50.1.1",
    }

    mock_channel = MagicMock()
    mock_channel.id = uuid4()
    mock_channel.name = "Test Channel"
    mock_channel.channel_type = "GENERIC_WEBHOOK"
    mock_channel.is_enabled = True
    mock_channel.endpoint_ids = []
    mock_channel.subnet_filters = []
    mock_channel.severity_filters = ["DOWN"]
    mock_channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [mock_channel]
    mock_session.execute.return_value = mock_res

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    with patch("app.services.topology.topology_manager", mock_topo), \
         patch("app.services.alert_dispatcher.AsyncSessionLocal", return_value=mock_session_ctx), \
         patch.object(dispatcher, "_is_alerting_enabled", new_callable=AsyncMock, return_value=True), \
         patch.object(dispatcher, "_evaluate_and_send_channel", new_callable=AsyncMock) as mock_eval:

        await dispatcher._dispatch_event("STATE_TRANSITION", event)

        assert mock_eval.called
        call_kwargs = mock_eval.call_args[1]
        assert call_kwargs["endpoint_name"] == "Core-Switch-A"
        assert call_kwargs["ip_address"] == "10.50.1.1"


@pytest.mark.anyio
async def test_alert_dispatcher_metadata_enrichment_from_db_fallback():
    """Verify fallback enrichment in _dispatch_event using DB query when topology node is missing."""
    dispatcher = AlertDispatcher()
    ep_id = uuid4()

    event = {
        "event_type": "STATE_TRANSITION",
        "endpoint_id": str(ep_id),
        "endpoint_name": None,
        "ip_address": None,
        "operational_state": "DOWN",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": {},
    }

    mock_topo = MagicMock(spec=TopologyGraphManager)
    mock_topo.get_node.return_value = None

    mock_ep_row = MagicMock()
    mock_ep_row.hostname = "Database-Server-Prod"
    mock_ep_row.ip_address = "10.100.4.20"

    mock_channel = MagicMock()
    mock_channel.id = uuid4()
    mock_channel.name = "Test Channel"
    mock_channel.channel_type = "GENERIC_WEBHOOK"
    mock_channel.is_enabled = True
    mock_channel.endpoint_ids = []
    mock_channel.subnet_filters = []
    mock_channel.severity_filters = ["DOWN"]
    mock_channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    mock_session = AsyncMock()
    async def mock_execute(stmt, params=None):
        m_res = MagicMock()
        stmt_str = str(stmt)
        if "endpoints" in stmt_str:
            m_res.fetchone.return_value = mock_ep_row
        else:
            m_res.scalars.return_value.all.return_value = [mock_channel]
        return m_res

    mock_session.execute.side_effect = mock_execute

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    with patch("app.services.topology.topology_manager", mock_topo), \
         patch("app.services.alert_dispatcher.AsyncSessionLocal", return_value=mock_session_ctx), \
         patch.object(dispatcher, "_is_alerting_enabled", new_callable=AsyncMock, return_value=True), \
         patch.object(dispatcher, "_evaluate_and_send_channel", new_callable=AsyncMock) as mock_eval:

        await dispatcher._dispatch_event("STATE_TRANSITION", event)

        assert mock_eval.called
        call_kwargs = mock_eval.call_args[1]
        assert call_kwargs["endpoint_name"] == "Database-Server-Prod"
        assert call_kwargs["ip_address"] == "10.100.4.20"


@pytest.mark.anyio
async def test_severity_filter_equivalence_up_and_recovered():
    """Verify that severity_filters=['RECOVERED'] matches severity='UP' and vice-versa."""
    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "Recovery Alerts Channel"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = []
    channel.subnet_filters = []
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    dispatcher._send_to_provider = AsyncMock(return_value=("DELIVERED", 200, "OK"))
    dispatcher._record_delivery_log = AsyncMock()

    # Case 1: Channel expects RECOVERED, event sends UP -> must match and send
    channel.severity_filters = ["RECOVERED"]
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Host-A",
        ip_address="8.8.8.8",
        event_type="STATE_TRANSITION",
        severity="UP",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_called_once()

    # Case 2: Channel expects UP, event sends RECOVERED -> must match and send
    dispatcher._send_to_provider.reset_mock()
    channel.severity_filters = ["UP"]
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Host-A",
        ip_address="8.8.8.8",
        event_type="STATE_TRANSITION",
        severity="RECOVERED",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_called_once()

    # Case 3: Channel expects DOWN, event sends UP -> must NOT send
    dispatcher._send_to_provider.reset_mock()
    channel.severity_filters = ["DOWN"]
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Host-A",
        ip_address="8.8.8.8",
        event_type="STATE_TRANSITION",
        severity="UP",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_not_called()

    # Case 4: Channel has default filter ['DOWN', 'RECOVERED'], event sends UP -> must deliver!
    dispatcher._send_to_provider.reset_mock()
    channel.severity_filters = ["DOWN", "RECOVERED"]
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Host-A",
        ip_address="8.8.8.8",
        event_type="STATE_TRANSITION",
        severity="UP",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_called_once()


def test_merge_channel_config_preserves_masked_credentials():
    """Verify _merge_channel_config preserves secrets when incoming payload has bullets."""
    existing_cfg = {
        "webhook_url": "https://discord.com/api/webhooks/123/super_secret_token",
        "password": "ExistingStrongPassword!2026",
        "smtp_host": "smtp.company.com",
        "smtp_port": 587,
        "headers": {
            "Authorization": "Bearer super-secret-api-key",
            "X-Api-Key": "my-secret-key-12345",
            "Content-Type": "application/json",
        },
    }

    incoming_cfg = {
        "webhook_url": "https://discord.com/api/webhooks/123/••••••••",
        "password": "••••••••••••",
        "smtp_host": "smtp.company.com",
        "smtp_port": 587,
        "headers": {
            "Authorization": "•••••••••••••••••••••••••••••",
            "X-Api-Key": "Bearer new-token-unmasked",
            "Content-Type": "application/json",
        },
    }

    merged = _merge_channel_config(existing_cfg, incoming_cfg)

    # Webhook URL preserved
    assert merged["webhook_url"] == "https://discord.com/api/webhooks/123/super_secret_token"
    # Password preserved
    assert merged["password"] == "ExistingStrongPassword!2026"
    # Authorization header preserved
    assert merged["headers"]["Authorization"] == "Bearer super-secret-api-key"
    # X-Api-Key updated
    assert merged["headers"]["X-Api-Key"] == "Bearer new-token-unmasked"
    assert merged["headers"]["Content-Type"] == "application/json"


@pytest.mark.anyio
async def test_smtp_host_ssrf_rejection_in_sync_send():
    """Verify that _sync_send_smtp rejects cloud metadata IPs with SSRF error while allowing private/loopback relays."""
    dispatcher = AlertDispatcher()

    # Cloud metadata IP must be blocked
    cfg_metadata = {
        "smtp_host": "169.254.169.254",
        "smtp_port": 25,
        "from_address": "alerts@corp.com",
        "to_addresses": ["admin@corp.com"],
    }
    with pytest.raises(ValueError, match="SSRF|metadata|forbidden address"):
        dispatcher._sync_send_smtp(cfg_metadata, MagicMock())

    # Loopback IP and RFC1918 internal relay must be permitted by SSRF check
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        cfg_loopback = {
            "smtp_host": "127.0.0.1",
            "smtp_port": 25,
            "from_address": "alerts@corp.com",
            "to_addresses": ["admin@corp.com"],
        }
        dispatcher._sync_send_smtp(cfg_loopback, MagicMock())
        mock_smtp.assert_called_with("127.0.0.1", 25, timeout=10.0)

        cfg_private = {
            "smtp_host": "192.168.1.50",
            "smtp_port": 25,
            "from_address": "alerts@corp.com",
            "to_addresses": ["admin@corp.com"],
        }
        dispatcher._sync_send_smtp(cfg_private, MagicMock())
        mock_smtp.assert_called_with("192.168.1.50", 25, timeout=10.0)


def test_console_url_resolution():
    """Verify _resolve_portal_url handles DOMAIN_NAME and fallback to host:port."""
    dispatcher = AlertDispatcher()

    # Case 1: DOMAIN_NAME set without scheme -> https:// default
    with patch.dict(os.environ, {"DOMAIN_NAME": "monitor.mycompany.org"}):
        url = dispatcher._resolve_portal_url()
        assert url == "https://monitor.mycompany.org"

    # Case 2: DOMAIN_NAME set with http:// scheme and trailing slash
    with patch.dict(os.environ, {"DOMAIN_NAME": "http://internal-portal.local:8080/"}):
        url = dispatcher._resolve_portal_url()
        assert url == "http://internal-portal.local:8080"

    # Case 3: DOMAIN_NAME empty -> fallback to settings.api.host:settings.api.port
    with patch.dict(os.environ, {"DOMAIN_NAME": ""}):
        url = dispatcher._resolve_portal_url()
        assert "http://" in url
        assert ":" in url


@pytest.mark.anyio
async def test_create_alert_channel_smtp_ssrf_rejected():
    from app.routers.alerts import create_alert_channel
    from app.schemas.alerts import AlertChannelCreate
    from app.core.asgi import HTTPException

    payload = AlertChannelCreate(
        name="Metadata SMTP Exfiltration",
        channel_type="EMAIL_SMTP",
        is_enabled=True,
        config={
            "smtp_host": "169.254.169.254",
            "smtp_port": 25,
            "from_address": "test@corp.com",
            "to_addresses": ["victim@corp.com"],
        },
        severity_filters=["DOWN"],
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_alert_channel(payload=payload, current_user={"username": "admin"}, db=AsyncMock())
    assert exc_info.value.status_code == 400
    assert "SSRF" in str(exc_info.value.detail) or "blocked" in str(exc_info.value.detail).lower()


@pytest.mark.anyio
async def test_update_alert_channel_preserves_masked_header():
    from app.routers.alerts import update_alert_channel, _decrypt_config_dict
    from app.schemas.alerts import AlertChannelUpdate
    from app.models.alert_channel import AlertChannel
    from app.core.crypto import encrypt_secret
    import json

    existing_config = {
        "webhook_url": "https://8.8.8.8/hook",
        "headers": {
            "Authorization": "Bearer real_secret_api_token",
            "Content-Type": "application/json",
        },
    }
    channel_id = uuid4()
    mock_channel = AlertChannel(
        id=channel_id,
        name="Webhook NOC",
        channel_type="GENERIC_WEBHOOK",
        is_enabled=True,
        config=encrypt_secret(json.dumps(existing_config)),
        endpoint_ids=[],
        subnet_filters=[],
        severity_filters=["DOWN"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_channel
    mock_db.execute.return_value = mock_res

    update_payload = AlertChannelUpdate(
        config={
            "webhook_url": "https://8.8.8.8/hook",
            "headers": {
                "Authorization": "Bearer ••••••••••••",
                "Content-Type": "application/json",
            },
        }
    )

    await update_alert_channel(
        channel_id=channel_id,
        payload=update_payload,
        current_user={"username": "admin"},
        db=mock_db,
    )
    saved_cfg = _decrypt_config_dict(mock_channel.config)
    assert saved_cfg["headers"]["Authorization"] == "Bearer real_secret_api_token"


@pytest.mark.anyio
async def test_alert_dispatcher_metadata_enrichment_enables_subnet_filtering():
    """Verify that placeholder IP 0.0.0.0 is enriched, allowing subnet filter to match and deliver."""
    dispatcher = AlertDispatcher()
    ep_id = uuid4()

    event = {
        "event_type": "STATE_TRANSITION",
        "endpoint_id": str(ep_id),
        "endpoint_name": "Unknown Endpoint",
        "ip_address": "0.0.0.0",
        "operational_state": "DOWN",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": {},
    }

    mock_topo = MagicMock(spec=TopologyGraphManager)
    mock_topo.get_node.return_value = {
        "id": str(ep_id),
        "label": "Gateway-Switch-01",
        "ip_address": "10.50.1.1",
    }

    # Channel only accepts endpoints in 10.50.0.0/16
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "10.50 Subnet Channel"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.is_enabled = True
    channel.endpoint_ids = []
    channel.subnet_filters = ["10.50.0.0/16"]
    channel.severity_filters = ["DOWN"]
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [channel]
    mock_session.execute.return_value = mock_res

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    with patch("app.services.topology.topology_manager", mock_topo), \
         patch("app.services.alert_dispatcher.AsyncSessionLocal", return_value=mock_session_ctx), \
         patch.object(dispatcher, "_is_alerting_enabled", new_callable=AsyncMock, return_value=True), \
         patch.object(dispatcher, "_send_to_provider", new_callable=AsyncMock, return_value=("DELIVERED", 200, "OK")) as mock_send, \
         patch.object(dispatcher, "_record_delivery_log", new_callable=AsyncMock):

        await dispatcher._dispatch_event("STATE_TRANSITION", event)

        # Because 0.0.0.0 was enriched to 10.50.1.1, subnet filter matched!
        assert mock_send.called
        assert mock_send.call_args[1]["endpoint_name"] == "Gateway-Switch-01"
        assert mock_send.call_args[1]["ip_address"] == "10.50.1.1"


def test_pydantic_subnet_filter_validation():
    """Verify that AlertChannelCreate rejects invalid CIDRs and strips valid CIDRs."""
    from app.schemas.alerts import AlertChannelCreate, AlertChannelUpdate
    from app.core.schema import ValidationError

    # Invalid CIDR format
    with pytest.raises(ValidationError) as exc_info:
        AlertChannelCreate(
            name="Bad Subnet",
            channel_type="GENERIC_WEBHOOK",
            config={"webhook_url": "https://8.8.8.8/hook"},
            subnet_filters=["not-a-valid-cidr"],
        )
    assert "Invalid CIDR subnet format" in str(exc_info.value)

    # Valid CIDRs with whitespace
    valid = AlertChannelCreate(
        name="Good Subnet",
        channel_type="GENERIC_WEBHOOK",
        config={"webhook_url": "https://8.8.8.8/hook"},
        subnet_filters=[" 10.0.0.0/16 ", " 192.168.1.0/24 "],
    )
    assert valid.subnet_filters == ["10.0.0.0/16", "192.168.1.0/24"]

    # Update schema validation
    with pytest.raises(ValidationError) as exc_info_up:
        AlertChannelUpdate(subnet_filters=["999.999.999.999/24"])
    assert "Invalid CIDR subnet format" in str(exc_info_up.value)


@pytest.mark.anyio
async def test_endpoint_filter_blocks_none_endpoint_id():
    """Verify that a channel restricted to specific endpoints does NOT receive alerts when endpoint_id is None."""
    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "Targeted Channel"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = [uuid4()]
    channel.subnet_filters = []
    channel.severity_filters = ["DOWN"]
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    dispatcher._send_to_provider = AsyncMock()

    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=None,
        endpoint_name="Unknown Node",
        ip_address="10.0.0.1",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_not_called()


@pytest.mark.anyio
async def test_delivery_log_handles_non_uuid_safely():
    """Verify that delivery logging safely handles non-UUID endpoint identifiers without raising ValueError."""
    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "General Webhook"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = []
    channel.subnet_filters = []
    channel.severity_filters = ["DOWN"]
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    dispatcher._send_to_provider = AsyncMock(return_value=("DELIVERED", 200, "OK"))

    # Non-UUID string passed as endpoint_id
    await dispatcher._evaluate_and_send_channel(
        db=mock_db,
        channel=channel,
        endpoint_id="non-uuid-string-switch-1",
        endpoint_name="Switch-01",
        ip_address="10.0.0.1",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    # Logging completed without crashing
    assert mock_db.add.called


@pytest.mark.anyio
async def test_test_alert_channel_preserves_masked_header():
    """Verify that test_alert_channel preserves decrypted secret when masked bullets are passed."""
    from app.routers.alerts import test_alert_channel
    from app.schemas.alerts import AlertTestRequest
    from app.models.alert_channel import AlertChannel
    from app.core.crypto import encrypt_secret
    import json

    existing_config = {
        "webhook_url": "https://8.8.8.8/hook",
        "headers": {
            "Authorization": "Bearer real_secret_test_token",
            "Content-Type": "application/json",
        },
    }
    channel_id = uuid4()
    mock_channel = AlertChannel(
        id=channel_id,
        name="Test Webhook Channel",
        channel_type="GENERIC_WEBHOOK",
        is_enabled=True,
        config=encrypt_secret(json.dumps(existing_config)),
        endpoint_ids=[],
        subnet_filters=[],
        severity_filters=["DOWN"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_channel
    mock_db.execute.return_value = mock_res

    test_payload = AlertTestRequest(
        channel_id=channel_id,
        config={
            "webhook_url": "https://8.8.8.8/hook",
            "headers": {
                "Authorization": "Bearer ••••••••••••",
            },
        },
    )

    with patch("app.routers.alerts.alert_dispatcher.send_test_alert", new_callable=AsyncMock) as mock_probe, \
         patch("app.routers.alerts.validate_outbound_url"):
        mock_probe.return_value = {"success": True, "status_code": 200, "message": "Delivered"}

        resp = await test_alert_channel(
            payload=test_payload,
            current_user={"username": "admin"},
            db=mock_db,
        )
        assert resp.data["success"] is True
        sent_channel = mock_probe.call_args[0][0]
        assert sent_channel["config"]["headers"]["Authorization"] == "Bearer real_secret_test_token"

