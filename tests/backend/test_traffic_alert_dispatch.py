"""Traffic alerts in the dispatcher and the message formats (v3.3.6 build plan 7.4), without a
database: which channels get them, the per-link rate limit, and how the messages read.
The rule itself (high, normal, silent, resumed) is tested in tests/integration/test_traffic_alerts.py.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.services.alert_dispatcher import AlertDispatcher
from app.services.alert_formatters import build_discord_payload, build_email_mime, build_polyglot_payload, build_slack_payload
from app.services.traffic_alerts import TrafficEvent, WatchedLink, link_rule


def _channel(filters):
    return SimpleNamespace(id=uuid4(), name="ch", channel_type="GENERIC_WEBHOOK", config="x",
                           endpoint_ids=[], subnet_filters=[], severity_filters=filters)


EXPORTER = uuid4()


def _event(severity="TRAFFIC_HIGH", idx=1, direction="in"):
    link = WatchedLink(EXPORTER, "abj-rtr", "10.55.0.1", idx, f"WAN{idx}", 10, 80, 15)
    return TrafficEvent(severity, link, f"abj-rtr WAN{idx}: inbound traffic above 80% for 15 min",
                        {"Link": f"WAN{idx} (10 Mbps)", "Peak": "90%"}, direction)


def _send(dispatcher, channels, event):
    sent = []

    async def fake_send(**kw):
        sent.append(kw)
        return "DELIVERED", 200, "ok"

    with patch.object(dispatcher, "_is_alerting_enabled", AsyncMock(return_value=True)), \
         patch.object(dispatcher, "_load_enabled_channels", AsyncMock(return_value=channels)), \
         patch.object(dispatcher, "_send_to_provider", side_effect=fake_send), \
         patch.object(dispatcher, "_parse_channel_config", return_value={}), \
         patch.object(dispatcher, "_record_delivery_log", AsyncMock()):
        asyncio.run(dispatcher._dispatch_traffic_event(event))
    return sent


def test_only_channels_that_ticked_traffic_get_it():
    d = AlertDispatcher()
    ticked = _channel(["DOWN", "TRAFFIC_HIGH"])
    sent = _send(d, [_channel(["DOWN", "RECOVERED"]), _channel([]), ticked], _event())
    assert len(sent) == 1  # an empty filter ("all") predates traffic alerts and means state changes
    assert sent[0]["severity"] == "TRAFFIC_HIGH"
    assert sent[0]["details"]["summary"].startswith("abj-rtr WAN1")
    assert sent[0]["ip_address"] == "10.55.0.1"


def test_rate_limit_is_per_link_and_direction():
    d = AlertDispatcher()
    ch = _channel(["TRAFFIC_HIGH"])
    assert len(_send(d, [ch], _event(idx=1))) == 1
    assert len(_send(d, [ch], _event(idx=2))) == 1  # another link of the same exporter
    assert len(_send(d, [ch], _event(idx=1, direction="out"))) == 1
    assert len(_send(d, [ch], _event(idx=1))) == 0  # same link and direction within 5 minutes


def test_off_master_switch_sends_nothing():
    d = AlertDispatcher()
    with patch.object(d, "_is_alerting_enabled", AsyncMock(return_value=False)), \
         patch.object(d, "_load_enabled_channels", AsyncMock()) as load:
        asyncio.run(d._dispatch_traffic_event(_event()))
    load.assert_not_awaited()


def test_link_rule_default_custom_off():
    assert link_rule({"role": "wan"}, 80, 15) == (80, 15)
    assert link_rule({"alert": {"mode": "custom", "threshold_pct": 95}}, 80, 15) == (95, 15)
    assert link_rule({"alert": {"mode": "off"}}, 80, 15) is None
    assert link_rule("legacy name", 80, 15) == (80, 15)


def test_messages_use_the_summary_and_facts():
    details = {"summary": "abj-rtr WAN1: inbound traffic above 80% for 15 min", "Link": "WAN1 (10 Mbps)", "Peak": "90%"}
    args = dict(endpoint_name="abj-rtr WAN1", ip_address="10.55.0.1", event_type="TRAFFIC_ALERT",
                severity="TRAFFIC_HIGH", details=details)
    teams = build_polyglot_payload(**args)
    assert "inbound traffic above 80%" in teams["attachments"][0]["content"]["body"][0]["items"][0]["text"]
    discord = build_discord_payload(**args)
    assert "inbound traffic above 80%" in discord["embeds"][0]["title"]
    assert {"name": "Peak", "value": "90%", "inline": True} in discord["embeds"][0]["fields"]
    slack = build_slack_payload(**args)
    assert "inbound traffic above 80%" in slack["text"]
    mail = build_email_mime(to_email="a@example.test", from_email="b@example.test", **args)
    assert "inbound traffic above 80%" in mail["Subject"]
    assert "Peak:" in mail.get_body(("plain",)).get_content()

    # Back to normal is green; a state change still reads "<endpoint> is DOWN".
    normal = build_discord_payload(**{**args, "severity": "TRAFFIC_NORMAL"})
    assert normal["embeds"][0]["color"] == 3066993
    down = build_discord_payload(endpoint_name="sw1", ip_address="10.0.0.2", event_type="STATE_TRANSITION",
                                 severity="DOWN", details={})
    assert down["embeds"][0]["title"].endswith("sw1 is DOWN")
