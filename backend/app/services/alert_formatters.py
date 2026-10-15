from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
import html
import re
from typing import Any, Dict, Optional


def _escape_slack_mrkdwn(text: Any) -> str:
    """Escapes Slack special characters to prevent broadcast injection."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_timestamp(ts: Any) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts) if ts else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sanitize_header(value: str) -> str:
    """CRLF sanitization for SMTP email headers to prevent header injection."""
    if not value:
        return ""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def build_polyglot_payload(
    endpoint_name: str,
    ip_address: str,
    event_type: str,
    severity: str,
    timestamp: Any = None,
    details: Optional[Dict[str, Any]] = None,
    portal_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds a polyglot webhook payload compatible with Microsoft Teams (Adaptive Card v1.4),
    Generic Webhook parsers, PagerDuty, and ticket ingestion webhooks.
    """
    ts_str = _format_timestamp(timestamp)
    url = portal_url or "http://localhost:8000"
    det = details or {}

    sev_upper = severity.upper()
    if sev_upper in ("DOWN", "CRITICAL"):
        card_style = "attention"
        theme_color = "#ef4444"
        status_emoji = "🚨"
    elif sev_upper in ("RECOVERED", "UP"):
        card_style = "good"
        theme_color = "#22c55e"
        status_emoji = "✅"
    else:
        card_style = "warning"
        theme_color = "#f59e0b"
        status_emoji = "⚠️"

    title_text = f"{status_emoji} Ephemres Alert: {endpoint_name} is {sev_upper}"
    plain_text = f"{status_emoji} [Ephemres Alert] {endpoint_name} ({ip_address}) - {sev_upper} | Event: {event_type} | Time: {ts_str}"

    adaptive_card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "Container",
                "style": card_style,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": title_text,
                        "weight": "Bolder",
                        "size": "Medium",
                        "wrap": True,
                    }
                ],
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Endpoint:", "value": endpoint_name},
                    {"title": "IP Address:", "value": ip_address},
                    {"title": "Severity:", "value": sev_upper},
                    {"title": "Event Type:", "value": event_type},
                    {"title": "Timestamp:", "value": ts_str},
                ],
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Open Ephemres Console",
                "url": url,
            }
        ],
    }

    if det:
        extra_facts = []
        for k, v in det.items():
            if v is not None and k not in ("endpoint_id", "id"):
                extra_facts.append({"title": f"{k}:", "value": str(v)})
        if extra_facts:
            adaptive_card["body"].append({
                "type": "FactSet",
                "facts": extra_facts[:6],
            })

    safe_name = html.escape(str(endpoint_name))
    safe_ip = html.escape(str(ip_address))
    safe_event = html.escape(str(event_type))
    safe_sev = html.escape(str(sev_upper))
    safe_title = f"{status_emoji} Ephemres Alert: {safe_name} is {safe_sev}"

    html_content = f"""
    <div style="font-family: Arial, sans-serif; border-left: 4px solid {theme_color}; padding: 12px 16px; background-color: #1e1e24; color: #f4f4f5; border-radius: 4px;">
        <h3 style="margin: 0 0 8px 0; color: {theme_color};">{safe_title}</h3>
        <p style="margin: 4px 0;"><strong>Endpoint:</strong> {safe_name} (<code>{safe_ip}</code>)</p>
        <p style="margin: 4px 0;"><strong>Severity:</strong> <span style="font-weight:bold; color:{theme_color};">{safe_sev}</span></p>
        <p style="margin: 4px 0;"><strong>Event:</strong> {safe_event}</p>
        <p style="margin: 4px 0;"><strong>Timestamp:</strong> {ts_str}</p>
        <div style="margin-top: 12px;">
            <a href="{url}" style="display:inline-block; padding:6px 14px; background-color:#3b82f6; color:#ffffff; text-decoration:none; border-radius:4px; font-weight:bold;">View in Console</a>
        </div>
    </div>
    """.strip()

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": adaptive_card,
            }
        ],
        "text": plain_text,
        "message": plain_text,
        "html_content": html_content,
        "endpoint": endpoint_name,
        "ip_address": ip_address,
        "event_type": event_type,
        "severity": sev_upper,
        "timestamp": ts_str,
        "details": det,
        "url": url,
    }


def build_discord_payload(
    endpoint_name: str,
    ip_address: str,
    event_type: str,
    severity: str,
    timestamp: Any = None,
    details: Optional[Dict[str, Any]] = None,
    portal_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds a Discord Webhook payload using Rich Embeds.
    """
    ts_str = _format_timestamp(timestamp)
    url = portal_url or "http://localhost:8000"
    sev_upper = severity.upper()

    if sev_upper in ("DOWN", "CRITICAL"):
        color = 15158332  # Red
        emoji = "🚨"
    elif sev_upper in ("RECOVERED", "UP"):
        color = 3066993   # Green
        emoji = "✅"
    else:
        color = 16753920  # Amber
        emoji = "⚠️"

    fields = [
        {"name": "Hostname", "value": endpoint_name, "inline": True},
        {"name": "IP Address", "value": f"`{ip_address}`", "inline": True},
        {"name": "Severity", "value": f"**{sev_upper}**", "inline": True},
        {"name": "Event Type", "value": event_type, "inline": True},
        {"name": "Timestamp", "value": ts_str, "inline": False},
    ]

    if details:
        if details.get("avg_rtt_ms") is not None:
            fields.append({"name": "Latency (RTT)", "value": f"{details['avg_rtt_ms']} ms", "inline": True})
        if details.get("packet_loss_pct") is not None:
            fields.append({"name": "Packet Loss", "value": f"{details['packet_loss_pct']}%", "inline": True})
        if details.get("root_cause"):
            fields.append({"name": "Root Cause", "value": str(details["root_cause"]), "inline": False})

    return {
        "content": f"{emoji} **Ephemres Alert:** `{endpoint_name}` transitioned to **{sev_upper}**",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{emoji} {endpoint_name} is {sev_upper}",
                "description": f"Operational State Transition detected by Ephemres Monitoring Engine.",
                "url": url,
                "color": color,
                "fields": fields,
                "footer": {"text": "Ephemres Enterprise Observability Engine v3.3.1n"},
            }
        ],
    }


def build_slack_payload(
    endpoint_name: str,
    ip_address: str,
    event_type: str,
    severity: str,
    timestamp: Any = None,
    details: Optional[Dict[str, Any]] = None,
    portal_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds a Slack Webhook payload using Slack Block Kit.
    """
    ts_str = _format_timestamp(timestamp)
    url = portal_url or "http://localhost:8000"
    sev_upper = severity.upper()

    emoji = "🚨" if sev_upper in ("DOWN", "CRITICAL") else ("✅" if sev_upper in ("RECOVERED", "UP") else "⚠️")

    safe_name = _escape_slack_mrkdwn(endpoint_name)
    safe_ip = _escape_slack_mrkdwn(ip_address)
    safe_event = _escape_slack_mrkdwn(event_type)
    safe_sev = _escape_slack_mrkdwn(sev_upper)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Ephemres Alert: {safe_name} is {safe_sev}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Endpoint:*\n{safe_name}"},
                {"type": "mrkdwn", "text": f"*IP Address:*\n`{safe_ip}`"},
                {"type": "mrkdwn", "text": f"*Severity:*\n*{safe_sev}*"},
                {"type": "mrkdwn", "text": f"*Event Type:*\n{safe_event}"},
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Timestamp (UTC): *{ts_str}* | Ephemres Observability Platform v3.3.1n",
                }
            ],
        },
    ]

    if url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open in Console"},
                    "url": url,
                    "style": "primary",
                }
            ],
        })

    return {
        "text": f"{emoji} Ephemres Alert: {safe_name} is {safe_sev}",
        "blocks": blocks,
    }


def build_email_mime(
    to_email: str,
    from_email: str,
    endpoint_name: str,
    ip_address: str,
    event_type: str,
    severity: str,
    timestamp: Any = None,
    details: Optional[Dict[str, Any]] = None,
    portal_url: Optional[str] = None,
) -> EmailMessage:
    """
    Builds an EmailMessage instance formatted with multipart/alternative (plain text & HTML),
    with CRLF header sanitization to protect against header injection attacks.
    """
    ts_str = _format_timestamp(timestamp)
    url = portal_url or "http://localhost:8000"
    sev_upper = severity.upper()

    clean_to = _sanitize_header(to_email)
    clean_from = _sanitize_header(from_email)
    clean_subject = _sanitize_header(f"[{sev_upper}] Ephemres Alert: {endpoint_name} ({ip_address})")

    theme_color = "#ef4444" if sev_upper in ("DOWN", "CRITICAL") else ("#22c55e" if sev_upper in ("RECOVERED", "UP") else "#f59e0b")

    safe_name = html.escape(str(endpoint_name))
    safe_ip = html.escape(str(ip_address))
    safe_event = html.escape(str(event_type))
    safe_sev = html.escape(str(sev_upper))

    plain_body = f"""
Ephemres Enterprise Network Monitoring Alert
============================================
Endpoint:   {endpoint_name}
IP Address: {ip_address}
Severity:   {sev_upper}
Event:      {event_type}
Timestamp:  {ts_str}

Console:    {url}
--------------------------------------------
Sent by Ephemres Enterprise Platform v3.3.1n
""".strip()

    html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; background-color: #0f0f12; color: #f4f4f5; margin: 0; padding: 24px;">
  <div style="max-width: 600px; margin: 0 auto; background-color: #18181b; border: 1px solid #27272a; border-radius: 8px; overflow: hidden;">
    <div style="background-color: {theme_color}; color: #ffffff; padding: 16px 20px;">
      <h2 style="margin: 0; font-size: 18px;">Ephemres Alert: {safe_name} is {safe_sev}</h2>
    </div>
    <div style="padding: 20px;">
      <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
        <tr><td style="padding: 6px 0; color: #a1a1aa; width: 120px;">Endpoint:</td><td style="font-weight: bold; color: #ffffff;">{safe_name}</td></tr>
        <tr><td style="padding: 6px 0; color: #a1a1aa;">IP Address:</td><td><code>{safe_ip}</code></td></tr>
        <tr><td style="padding: 6px 0; color: #a1a1aa;">Severity:</td><td style="font-weight: bold; color: {theme_color};">{safe_sev}</td></tr>
        <tr><td style="padding: 6px 0; color: #a1a1aa;">Event:</td><td>{safe_event}</td></tr>
        <tr><td style="padding: 6px 0; color: #a1a1aa;">Timestamp:</td><td>{ts_str}</td></tr>
      </table>
      <div style="text-align: center; margin-top: 24px;">
        <a href="{url}" style="display: inline-block; background-color: #3b82f6; color: #ffffff; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold;">Open in Ephemres Console</a>
      </div>
    </div>
    <div style="background-color: #121215; padding: 12px 20px; text-align: center; font-size: 12px; color: #71717a;">
      Ephemres Enterprise Observability Engine v3.3.1n • Autonomous Network Reliability
    </div>
  </div>
</body>
</html>
""".strip()

    msg = EmailMessage()
    msg["Subject"] = clean_subject
    msg["From"] = clean_from
    msg["To"] = clean_to
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    return msg
