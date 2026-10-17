import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest

from app.services.alert_dispatcher import AlertDispatcher


@pytest.mark.anyio
async def test_alert_dispatcher_flapping_detection():
    dispatcher = AlertDispatcher()
    now = datetime.now(timezone.utc)
    ep_key = "test-node-1"

    assert dispatcher._check_flapping(ep_key, now) is False
    assert dispatcher._check_flapping(ep_key, now + timedelta(seconds=30)) is False
    assert dispatcher._check_flapping(ep_key, now + timedelta(seconds=60)) is False
    # 4th transition within 10 minutes triggers flapping detection
    assert dispatcher._check_flapping(ep_key, now + timedelta(seconds=90)) is True


@pytest.mark.anyio
async def test_alert_dispatcher_send_test_alert_mock_success():
    dispatcher = AlertDispatcher()
    test_channel = {
        "channel_type": "GENERIC_WEBHOOK",
        "name": "Integration Test Webhook",
        "config": {
            "webhook_url": "https://8.8.8.8/webhook",
        },
    }

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"

    with patch("app.services.ssrf_validator.SSRFSafeHttpClient.post", return_value=mock_resp):
        res = await dispatcher.send_test_alert(test_channel)
        assert res["success"] is True
        assert res["status_code"] == 200


@pytest.mark.anyio
async def test_alert_dispatcher_send_test_alert_ssrf_blocked():
    dispatcher = AlertDispatcher()
    test_channel = {
        "channel_type": "TEAMS",
        "name": "Internal Exploitation Webhook",
        "config": {
            "webhook_url": "http://127.0.0.1:8000/internal",
        },
    }
    res = await dispatcher.send_test_alert(test_channel)
    assert res["success"] is False
    assert res["status_code"] == 400
    assert "loopback" in res["message"].lower() or "ssrf" in res["message"].lower()


@pytest.mark.anyio
async def test_subnet_filter_matching():
    from unittest.mock import MagicMock
    from uuid import uuid4

    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "Subnet Filter Channel"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = []
    channel.severity_filters = ["DOWN"]
    channel.subnet_filters = ["192.168.1.0/24", "10.0.0.0/8"]
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    dispatcher._send_to_provider = AsyncMock(return_value=("DELIVERED", 200, "OK"))
    dispatcher._record_delivery_log = AsyncMock()

    # Case A: Inside subnet -> dispatched
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Node-Inside",
        ip_address="192.168.1.50",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_called_once()

    # Case B: Outside subnet -> suppressed early
    dispatcher._send_to_provider.reset_mock()
    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Node-Outside",
        ip_address="172.16.0.1",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )
    dispatcher._send_to_provider.assert_not_called()


@pytest.mark.anyio
async def test_flapping_suppression():
    from unittest.mock import MagicMock
    from uuid import uuid4

    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "Flapping Test Channel"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = []
    channel.severity_filters = ["DOWN"]
    channel.subnet_filters = []
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    dispatcher._send_to_provider = AsyncMock(return_value=("DELIVERED", 200, "OK"))
    dispatcher._record_delivery_log = AsyncMock()

    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=uuid4(),
        endpoint_name="Flapping-Node",
        ip_address="192.168.1.10",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=True,
    )
    dispatcher._send_to_provider.assert_not_called()
    dispatcher._record_delivery_log.assert_called_once()
    kwargs = dispatcher._record_delivery_log.call_args[1]
    assert kwargs["status"] == "THROTTLED"
    assert "flapping" in kwargs["response_message"].lower()


def test_cache_pruning():
    dispatcher = AlertDispatcher()
    now = datetime.now(timezone.utc)

    # Setup rate limits
    old_key = ("chan1", "ep1", "DOWN")
    recent_key = ("chan2", "ep2", "DOWN")
    dispatcher._rate_limits[old_key] = now - timedelta(hours=2)
    dispatcher._rate_limits[recent_key] = now - timedelta(minutes=10)

    # Setup flapping tracker
    old_flap_ep = "old-flapper"
    active_flap_ep = "active-flapper"
    empty_flap_ep = "empty-flapper"
    dispatcher._flapping_tracker[old_flap_ep] = [now - timedelta(minutes=20), now - timedelta(minutes=15)]
    dispatcher._flapping_tracker[active_flap_ep] = [now - timedelta(minutes=15), now - timedelta(minutes=2)]
    dispatcher._flapping_tracker[empty_flap_ep] = []

    # Prune
    dispatcher._prune_memory_caches(now)

    # Verify rate limits
    assert old_key not in dispatcher._rate_limits
    assert recent_key in dispatcher._rate_limits

    # Verify flapping tracker
    assert old_flap_ep not in dispatcher._flapping_tracker
    assert empty_flap_ep not in dispatcher._flapping_tracker
    assert active_flap_ep in dispatcher._flapping_tracker
    assert len(dispatcher._flapping_tracker[active_flap_ep]) == 1
    assert dispatcher._flapping_tracker[active_flap_ep][0] == now - timedelta(minutes=2)


@pytest.mark.anyio
async def test_leader_election_passive_standby():
    from unittest.mock import MagicMock

    dispatcher = AlertDispatcher()
    mock_conn = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar.return_value = False  # Lock not acquired
    mock_conn.execute.return_value = mock_res
    mock_conn.close = AsyncMock()

    with patch("app.services.alert_dispatcher.async_engine") as mock_engine:
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect = AsyncMock(return_value=mock_conn)

        await dispatcher.start()
        assert dispatcher._is_leader is False
        assert dispatcher._running is True
        task_names = [t.get_name() for t in dispatcher._tasks]
        assert "alert_broker_state_trans" not in task_names
        assert "alert_broker_rca" not in task_names

        await dispatcher.stop()


@pytest.mark.anyio
async def test_leader_election_active_leader():
    from unittest.mock import MagicMock

    dispatcher = AlertDispatcher()
    mock_conn = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar.return_value = True  # Lock acquired
    mock_conn.execute = AsyncMock(return_value=mock_res)
    mock_conn.close = AsyncMock()

    async def fake_listen(channel):
        await asyncio.sleep(3600)

    with patch("app.services.alert_dispatcher.async_engine") as mock_engine, \
         patch.object(dispatcher, "_listen_broker", side_effect=fake_listen):
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect = AsyncMock(return_value=mock_conn)

        await dispatcher.start()
        assert dispatcher._is_leader is True
        assert dispatcher._running is True
        task_names = [t.get_name() for t in dispatcher._tasks]
        assert "alert_broker_state_trans" in task_names
        assert "alert_broker_rca" in task_names

        await dispatcher.stop()
        assert any("pg_advisory_unlock" in str(call.args[0]) for call in mock_conn.execute.call_args_list)


@pytest.mark.anyio
async def test_standby_worker_failover_promotion():
    from unittest.mock import MagicMock

    dispatcher = AlertDispatcher()
    mock_conn1 = AsyncMock()
    mock_res1 = MagicMock()
    mock_res1.scalar.return_value = False  # Initial start fails to acquire lock
    mock_conn1.execute = AsyncMock(return_value=mock_res1)
    mock_conn1.close = AsyncMock()

    mock_conn2 = AsyncMock()
    mock_res2 = MagicMock()
    mock_res2.scalar.return_value = True  # Retry in election loop acquires lock
    mock_conn2.execute = AsyncMock(return_value=mock_res2)
    mock_conn2.close = AsyncMock()

    async def fake_listen(channel):
        await asyncio.sleep(3600)

    # Patch sleep in election loop to run immediately
    with patch("app.services.alert_dispatcher.async_engine") as mock_engine, \
         patch("app.services.alert_dispatcher.asyncio.sleep", return_value=None), \
         patch.object(dispatcher, "_listen_broker", side_effect=fake_listen):
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect = AsyncMock(side_effect=[mock_conn1, mock_conn2])

        await dispatcher.start()
        assert dispatcher._is_leader is False

        # Wait briefly for election loop task to iterate
        if dispatcher._election_task:
            await asyncio.wait_for(dispatcher._election_task, timeout=1.0)

        assert dispatcher._is_leader is True
        task_names = [t.get_name() for t in dispatcher._tasks]
        assert "alert_broker_state_trans" in task_names
        assert "alert_broker_rca" in task_names

        await dispatcher.stop()
        assert dispatcher._is_leader is False


@pytest.mark.anyio
async def test_rca_cascade_suppression_child_event():
    from unittest.mock import MagicMock
    from uuid import uuid4

    dispatcher = AlertDispatcher()
    now = datetime.now(timezone.utc)
    parent_id = str(uuid4())
    child_id = str(uuid4())
    incident_id = str(uuid4())

    # 1. RCA Incident event arrives
    rca_data = {
        "event_type": "RCA_INCIDENT",
        "incident_id": incident_id,
        "root_cause_endpoint_id": parent_id,
        "symptom_endpoint_ids": [child_id],
    }
    await dispatcher._dispatch_event("RCA_INCIDENT", rca_data)
    assert child_id in dispatcher._suppressed_children

    # 2. Child state transition arrives
    dispatcher._is_alerting_enabled = AsyncMock(return_value=True)
    dispatcher._record_delivery_log = AsyncMock()
    dispatcher._evaluate_and_send_channel = AsyncMock()

    mock_channel = MagicMock()
    mock_channel.id = uuid4()
    mock_channel.name = "Mock Channel"

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [mock_channel]
    mock_session.execute.return_value = mock_res
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with patch("app.services.alert_dispatcher.AsyncSessionLocal", return_value=mock_session):
        child_event = {
            "endpoint_id": child_id,
            "hostname": "child-switch-01",
            "ip_address": "10.0.1.5",
            "operational_state": "DOWN",
        }
        await dispatcher._dispatch_event("STATE_TRANSITION", child_event)

        # evaluate_and_send_channel should NOT be called (suppressed)
        dispatcher._evaluate_and_send_channel.assert_not_called()
        # Throttled delivery log recorded with reason
        dispatcher._record_delivery_log.assert_called()
        kwargs = dispatcher._record_delivery_log.call_args[1]
        assert kwargs["status"] == "THROTTLED"
        assert "Suppressed by upstream root cause" in kwargs["response_message"]


@pytest.mark.anyio
async def test_rate_limit_timing_on_provider_error():
    from unittest.mock import MagicMock
    from uuid import uuid4

    dispatcher = AlertDispatcher()
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = "Rate Limit Error Test"
    channel.channel_type = "GENERIC_WEBHOOK"
    channel.endpoint_ids = []
    channel.severity_filters = []
    channel.subnet_filters = []
    channel.config = '{"webhook_url": "https://8.8.8.8/hook"}'

    ep_id = uuid4()
    rl_key = (str(channel.id), str(ep_id), "DOWN")

    # Simulate network failure in _send_to_provider
    dispatcher._send_to_provider = AsyncMock(side_effect=Exception("Connection timed out"))
    dispatcher._record_delivery_log = AsyncMock()

    await dispatcher._evaluate_and_send_channel(
        db=None,
        channel=channel,
        endpoint_id=ep_id,
        endpoint_name="Unstable-Gateway",
        ip_address="192.168.1.1",
        event_type="STATE_TRANSITION",
        severity="DOWN",
        timestamp=datetime.now(timezone.utc),
        details={},
        is_flapping=False,
    )

    # Delivery failed, so rate limit timestamp must NOT be recorded
    assert rl_key not in dispatcher._rate_limits


@pytest.mark.anyio
async def test_smtp_to_emails_comma_separated_parsing():
    dispatcher = AlertDispatcher()
    config = {
        "to_emails": "admin@corp.net, ops@corp.net; alerts@corp.net",
        "smtp_host": "smtp.corp.net",
    }
    sent_msgs = []

    def mock_send(cfg, msg):
        sent_msgs.append(msg["To"])

    with patch.object(dispatcher, "_sync_send_smtp", side_effect=mock_send):
        status, code, msg = await dispatcher._send_to_provider(
            channel_type="EMAIL_SMTP",
            config=config,
            endpoint_name="core-switch-01",
            ip_address="10.0.0.1",
            event_type="STATE_TRANSITION",
            severity="DOWN",
            timestamp=datetime.now(timezone.utc),
            details={},
        )
        assert status == "DELIVERED"
        assert code == 250
        assert len(sent_msgs) == 3
        assert "admin@corp.net" in sent_msgs
        assert "ops@corp.net" in sent_msgs
        assert "alerts@corp.net" in sent_msgs


def test_smtp_socket_cleanup_on_error():
    from unittest.mock import MagicMock
    dispatcher = AlertDispatcher()
    config = {
        "smtp_host": "mail.corp.net",
        "smtp_port": 587,
        "use_tls": True,
        "use_ssl": False,
    }
    mock_msg = MagicMock()
    mock_server = MagicMock()
    mock_server.starttls.side_effect = Exception("TLS handshake failed")

    with patch("smtplib.SMTP", return_value=mock_server):
        with pytest.raises(Exception, match="TLS handshake failed"):
            dispatcher._sync_send_smtp(config, mock_msg)

        # Ensure server.quit() and server.close() were invoked in finally
        mock_server.quit.assert_called_once()
        mock_server.close.assert_called_once()


@pytest.mark.anyio
async def test_rca_engine_publishes_rca_incident_to_broker():
    from uuid import uuid4
    from unittest.mock import MagicMock, AsyncMock
    from app.services.rca_engine import run_differential_rca

    root_ep_id = uuid4()
    child_ep_id = uuid4()
    incident_uuid = uuid4()

    mock_ep_row = MagicMock(ip_address="10.10.10.1", enable_rca=True, is_l2_segment=False)
    mock_ep_res = MagicMock()
    mock_ep_res.fetchone.return_value = mock_ep_row

    mock_bl_row = MagicMock(hops=[{"hop": 1, "ip": "10.0.0.1"}, {"hop": 2, "ip": "10.10.10.1"}])
    mock_bl_res = MagicMock()
    mock_bl_res.fetchone.return_value = mock_bl_row

    mock_inc_row = MagicMock(id=incident_uuid)
    mock_inc_res = MagicMock()
    mock_inc_res.fetchone.return_value = mock_inc_row

    mock_sym_row = MagicMock(id=child_ep_id)
    mock_sym_res = MagicMock()
    mock_sym_res.fetchall.return_value = [mock_sym_row]

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [mock_ep_res, mock_bl_res, mock_inc_res, mock_sym_res]

    published_events = []
    mock_broker = AsyncMock()

    async def mock_pub(channel, payload):
        published_events.append((channel, payload))

    mock_broker.publish.side_effect = mock_pub

    with patch("app.services.rca_engine.run_throttled_traceroute", return_value={"hops": [{"hop": 1, "ip": "10.0.0.1"}]}), \
         patch("app.services.rca_engine.driver_manager.get_event_broker", return_value=mock_broker):
        res = await run_differential_rca(root_ep_id, db=mock_session)
        assert res is not None
        assert res["incident_id"] == str(incident_uuid)
        assert str(child_ep_id) in res["symptom_endpoint_ids"]

        assert len(published_events) == 1
        ch, payload = published_events[0]
        assert ch == "RCA_INCIDENT"
        assert payload["event_type"] == "RCA_INCIDENT"
        assert payload["incident_id"] == str(incident_uuid)
        assert payload["root_cause_endpoint_id"] == str(root_ep_id)
        assert str(child_ep_id) in payload["symptom_endpoint_ids"]


@pytest.mark.anyio
async def test_election_loop_connection_cleanup_on_exception():
    from unittest.mock import MagicMock

    dispatcher = AlertDispatcher()
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = Exception("Postgres query timeout")
    mock_conn.close = AsyncMock()

    with patch("app.services.alert_dispatcher.async_engine") as mock_engine, \
         patch("app.services.alert_dispatcher.asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect = AsyncMock(return_value=mock_conn)

        try:
            await dispatcher._standby_election_loop()
        except asyncio.CancelledError:
            pass

        # Verify conn.close was called even though execute raised an exception
        mock_conn.close.assert_called()

