import io
import os
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.core.asgi import FastAPI
from app.core.asgi import TestClient

from app.database import get_db
from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.rca_incident import EndpointRCAIncident
from app.models.system_setting import AppSetting
from app.routers.auth import get_current_user, require_admin
from app.routers.reports import router as reports_router
from app.routers.settings import router as settings_router
from app.core.pdf import NativePdfReader as PdfReader, render_pdf_async, secure_url_fetcher
from app.services.report_compiler import (
    compile_availability_report_data,
    compile_bandwidth_report_data,
    compile_master_report_data,
    get_organization_branding,
)

test_app = FastAPI()
test_app.include_router(reports_router)
test_app.include_router(settings_router)


# =====================================================================
# 1. Air-Gapped URL Fetcher & SSRF / LFI Defense Tests
# =====================================================================

def test_secure_url_fetcher_blocks_external_protocols():
    """Ensure HTTP, HTTPS, FTP, and other external requests are immediately blocked with PermissionError."""
    with pytest.raises(PermissionError, match="External network access is prohibited"):
        secure_url_fetcher("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(PermissionError, match="External network access is prohibited"):
        secure_url_fetcher("https://evil-attacker.com/malicious.css")

    with pytest.raises(PermissionError, match="External network access is prohibited"):
        secure_url_fetcher("ftp://10.0.0.1/exploit.png")


def test_secure_url_fetcher_blocks_unauthorized_local_files():
    """Ensure arbitrary filesystem traversal (e.g. /etc/passwd) is rejected with PermissionError."""
    with pytest.raises(PermissionError, match="not within authorized asset roots"):
        secure_url_fetcher("file:///etc/passwd")

    with pytest.raises(PermissionError, match="not within authorized asset roots"):
        secure_url_fetcher("/etc/shadow")


def test_secure_url_fetcher_allows_data_uris():
    """Ensure inline Base64 data URIs (e.g. corporate logos) are decoded safely."""
    # 1x1 transparent PNG data URI
    tiny_png = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    result = secure_url_fetcher(tiny_png)
    assert result.content_type == "image/png"
    assert len(result.read()) > 0


# =====================================================================
# 2. PDF Rendering Engine & Immutability Tests
# =====================================================================

@pytest.mark.anyio
async def test_pdf_rendering_and_immutability():
    """
    Ensure render_pdf_async:
    1. Returns valid PDF bytes.
    2. Applies permission encryption (read-only, printing allowed, modification prohibited).
    3. Reader can decrypt with empty user password.
    4. Reader metadata contains expected document properties.
    """
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head><title>Test Report</title></head>
    <body>
        <h1>Fleet SLA Audit</h1>
        <p>Endpoint Core-Router-01 achieved 99.98% availability.</p>
    </body>
    </html>
    """

    pdf_bytes = await render_pdf_async(
        html_content=sample_html,
        title="Ephemres Security Audit",
    )

    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500

    # Inspect with pypdf
    stream = io.BytesIO(pdf_bytes)
    reader = PdfReader(stream)

    # Document must be encrypted for permission restriction
    assert reader.is_encrypted is True

    # Empty user password unlocks reading
    decrypt_result = reader.decrypt("")
    assert decrypt_result > 0

    assert len(reader.pages) >= 1
    page_text = reader.pages[0].extract_text()
    assert "Fleet SLA Audit" in page_text
    assert "99.98%" in page_text

    # Verify document metadata
    metadata = reader.metadata
    assert metadata is not None
    assert "/Producer" in metadata
    assert "Ephemres Reporting Engine" in metadata["/Producer"]


@pytest.mark.anyio
async def test_render_bandwidth_and_master_templates_pdf():
    """Verify template_b_bandwidth.html and template_c_master.html render to valid encrypted PDF."""
    from app.core.template import TemplateEnvironment
    templates_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "app", "templates", "reports"))
    env = TemplateEnvironment(search_path=templates_dir, autoescape=True)

    bw_context = {
        "org": {"company_name": "Acme Corp", "department": "NetOps"},
        "report_title": "Acme Corp — Network Bandwidth & Flow Telemetry Report",
        "report_type_label": "Network Bandwidth & Flow Telemetry Report",
        "period_label": "2026-09-01 to 2026-09-02 UTC",
        "generated_at": "2026-09-02 12:00",
        "theme_accent": "#059669",
        "scope_title": "2 Core Flow Exporters",
        "exporters": [
            {
                "hostname": "core-gw-01",
                "primary_ip": "10.0.0.1",
                "active_interfaces_count": 2,
                "formatted_ingress_bytes": "50 MB",
                "formatted_egress_bytes": "100 MB",
                "formatted_ingress_bps": "4.6 Mbps",
                "formatted_peak_bps": "10.0 Mbps",
            }
        ],
        "interfaces": [
            {
                "router_hostname": "core-gw-01",
                "interface_idx": 1,
                "interface_name": "WAN-01",
                "speed_mbps": 1000,
                "formatted_speed": "1 Gbps",
                "formatted_in_bps": "2.5 Mbps",
                "formatted_out_bps": "5.0 Mbps",
                "formatted_peak_bps": "8.0 Mbps",
                "utilization_pct": 25.0,
            }
        ],
        "conversations": [
            {
                "src_display": "10.0.0.10",
                "dst_display": "1.1.1.1",
                "port_label": "53 / DNS",
                "router_name": "core-gw-01",
                "formatted_bytes": "5 MB",
                "pct_of_transit": 100.0,
            }
        ],
        "qos_classes": [
            {
                "dscp": 46,
                "dscp_label": "EF (VoIP)",
                "formatted_bytes": "10 MB",
                "formatted_packets": "8,000",
                "formatted_flows": "50",
                "percentage": 20.0,
            },
            {
                "dscp": 0,
                "dscp_label": "Best Effort (CS0)",
                "formatted_bytes": "40 MB",
                "formatted_packets": "30,000",
                "formatted_flows": "200",
                "percentage": 80.0,
            },
        ],
        "anomalies": {
            "total_count": 23,
            "syn_scan_count": 15,
            "syn_scan_bytes": "750 B",
            "rst_burst_count": 8,
            "rst_burst_bytes": "400 B",
            "has_anomalies": True,
        },
        "traffic_zones": [
            {
                "zone": "East-West (Internal)",
                "description": "Internal Mesh / Host-to-Host",
                "formatted_bytes": "30 MB",
                "formatted_flows": "150",
                "percentage": 60.0,
            },
            {
                "zone": "North-South Egress",
                "description": "Internal to External Transit",
                "formatted_bytes": "20 MB",
                "formatted_flows": "100",
                "percentage": 40.0,
            },
        ],
    }

    bw_html = env.get_template("template_b_bandwidth.html").render(**bw_context)
    pdf_bytes = await render_pdf_async(bw_html, title="Bandwidth Report")
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000

    # Also test template C master
    master_context = dict(bw_context)
    master_context["endpoints"] = [
        {
            "hostname": "core-gw-01",
            "ip_address": "10.0.0.1",
            "device_type": "ROUTER",
            "uptime_percentage": 99.98,
            "incident_count": 0,
            "formatted_downtime": "0s",
            "packet_loss_pct": 0.0,
            "avg_rtt_ms": 1.2,
            "operational_state": "UP",
        }
    ]
    master_context["incidents"] = []
    master_context["summary"] = {"mean_uptime": "99.98"}

    master_html = env.get_template("template_c_master.html").render(**master_context)
    master_pdf = await render_pdf_async(master_html, title="Master Audit")
    assert isinstance(master_pdf, bytes)
    assert master_pdf.startswith(b"%PDF-")
    assert len(master_pdf) > 1000


# =====================================================================
# 3. Report Compiler Logic Tests
# =====================================================================

def _event(ep_id, state, start, end, ok, failed, rtt=None, split=False, since=None):
    return EndpointEvent(
        id=uuid4(), endpoint_id=ep_id, operational_state=state, detailed_state=state,
        success_count=ok, failed_count=failed, start_time=start, end_time=end,
        duration_seconds=int(((end or start) - start).total_seconds()),
        avg_rtt_ms=Decimal(str(rtt)) if rtt is not None else None,
        health_score=Decimal("100.00") if state == "UP" else Decimal("0.00"),
        is_split_event=split, state_since=since or start,
    )


@pytest.mark.anyio
async def test_compile_availability_report_data():
    """Outages, uptime, loss, RTT and root causes over a 3-day window.

    - A 30 h outage the engine stored as a 24 h row plus a split row is one incident, starting
      at its real start, with its own root cause.
    - A DATABASE_DISCONNECT gap (events stay open across it) is neither up nor down time.
    - Loss is failed probes over all probes in the window; RTT is weighted by successful probes.
    - Each root cause goes to the outage it was taken for.
    """
    from app.services.report_compiler import format_duration

    mock_db = AsyncMock()
    w1 = datetime.now(timezone.utc).replace(microsecond=0)
    w0 = w1 - timedelta(days=3)
    h = timedelta(hours=1)
    m = timedelta(minutes=1)
    gap = (w0 + 50 * h, w0 + 51 * h)

    server, switch, printer = uuid4(), uuid4(), uuid4()
    endpoints = [
        Endpoint(id=server, hostname="srv-pacs", ip_address="10.0.10.17", device_type="SERVER",
                 monitoring_enabled=True, endpoint_status="ACTIVE", created_at=w0 - timedelta(days=30)),
        Endpoint(id=switch, hostname="sw-ward-b", ip_address="10.0.1.2", device_type="SWITCH",
                 monitoring_enabled=True, endpoint_status="ACTIVE", created_at=w0 - timedelta(days=30)),
        Endpoint(id=printer, hostname="printer", ip_address="10.0.30.12", device_type="PRINTER",
                 monitoring_enabled=False, endpoint_status="ACTIVE", created_at=w0 - timedelta(days=30)),
    ]
    res_ep = MagicMock()
    res_ep.scalars().all.return_value = endpoints

    events = [
        # Server: UP from before the window, 30 h outage (24 h row + split row), UP open.
        _event(server, "UP", w0 - 24 * h, w0 + 10 * h, ok=34 * 60 * 3, failed=0, rtt=2.0),
        _event(server, "DOWN", w0 + 10 * h, w0 + 34 * h, ok=0, failed=24 * 60 * 3),
        _event(server, "DOWN", w0 + 34 * h, w0 + 40 * h, ok=0, failed=6 * 60 * 3, split=True, since=w0 + 10 * h),
        _event(server, "UP", w0 + 40 * h, None, ok=32 * 60 * 3, failed=0, rtt=2.0),
        # Switch: two short outages with different root causes; one UP row has slow probes.
        _event(switch, "UP", w0 - 24 * h, w0 + 5 * h, ok=29 * 60 * 3 - 90, failed=90, rtt=1.0),
        _event(switch, "DOWN", w0 + 5 * h, w0 + 5 * h + 5 * m, ok=0, failed=15),
        _event(switch, "UP", w0 + 5 * h + 5 * m, w0 + 20 * h, ok=60, failed=0, rtt=9.0),
        _event(switch, "DOWN", w0 + 20 * h, w0 + 20 * h + 7 * m, ok=0, failed=21),
        _event(switch, "UP", w0 + 20 * h + 7 * m, None, ok=52 * 60 * 3, failed=0, rtt=1.0),
    ]
    res_events = MagicMock()
    res_events.scalars().all.return_value = events

    def rca(ep_id, at, text):
        return EndpointRCAIncident(id=uuid4(), endpoint_id=ep_id, incident_timestamp=at, status_at_execution="DOWN",
                                   rca_summary=text, baseline_snapshot=[], failure_trace_snapshot=[])

    res_rca = MagicMock()
    res_rca.scalars().all.return_value = [
        rca(server, w0 + 10 * h + m, "server cause"),
        rca(switch, w0 + 5 * h + m, "first switch cause"),
        rca(switch, w0 + 20 * h + m, "second switch cause"),
    ]
    mock_db.execute.side_effect = [res_ep, res_events, res_rca]

    with patch("app.services.report_compiler.get_service_gap_intervals", new_callable=AsyncMock) as mock_gaps:
        mock_gaps.return_value = [gap]
        data = await compile_availability_report_data(mock_db, [server, switch, printer], w0, w1, include_rca=True)

    rows = {r["hostname"]: r for r in data["endpoints"]}
    srv, sw, prn = rows["srv-pacs"], rows["sw-ward-b"], rows["printer"]

    assert srv["incident_count"] == 1
    assert srv["downtime_seconds"] == 30 * 3600
    # Up: 72 h - 30 h down - 1 h gap; the gap is also out of the denominator.
    assert srv["uptime_percentage"] == round(41 / 71 * 100, 2)
    assert srv["packet_loss_pct"] == round(100 * 30 / (10 + 30 + 32), 2)  # probe-weighted, by time in window
    assert srv["avg_rtt_ms"] == 2.0

    assert sw["incident_count"] == 2
    assert sw["downtime_seconds"] == 12 * 60
    # RTT weighted by successful probes: the 9 ms period had only 60 of them.
    assert 1.0 < sw["avg_rtt_ms"] < 1.1
    assert sw["packet_loss_pct"] < 1.0  # 90 + 36 failed of ~13,000 probes, not a mean of 0 % rows

    assert prn["operational_state"] == "PASSIVE" and prn["incident_count"] == 0
    assert prn["packet_loss_pct"] is None and prn["avg_rtt_ms"] is None

    ledger = data["incidents"]
    assert data["incidents_total"] == 3 and len(ledger) == 3
    assert [i["hostname"] for i in ledger] == ["sw-ward-b", "srv-pacs", "sw-ward-b"]  # newest first
    srv_outage = ledger[1]
    assert srv_outage["start_time"] == (w0 + 10 * h).strftime("%Y-%m-%d %H:%M:%S")
    assert srv_outage["resolved_time"] == (w0 + 40 * h).strftime("%Y-%m-%d %H:%M:%S")
    assert srv_outage["duration"] == format_duration(30 * 3600) == "1d 6h"
    assert [i["rca_summary"] for i in ledger] == ["second switch cause", "server cause", "first switch cause"]
    assert data["summary"]["total_incidents"] == 3


def test_format_duration_keeps_the_unit_after_the_largest():
    from app.services.report_compiler import format_duration, format_period

    assert [format_duration(s) for s in (0, 45, 180, 2832, 18047, 75636, 106462)] == [
        "0s", "45s", "3m", "47m 12s", "5h", "21h", "1d 5h",
    ]
    assert format_duration(21 * 3600 + 36) == "21h"  # was "21h 36s"
    utc = timezone.utc
    assert format_period(datetime(2026, 11, 6, 9, 30, tzinfo=utc), datetime(2026, 11, 9, 9, 30, tzinfo=utc)) == \
        "2026-11-06 09:30 to 2026-11-09 09:30 UTC"
    assert format_period(datetime(2026, 11, 6, tzinfo=utc), datetime(2026, 11, 8, 23, 59, 59, tzinfo=utc)) == \
        "2026-11-06 to 2026-11-08 UTC"


@pytest.mark.anyio
async def test_compile_bandwidth_report_data():
    """Verify compile_bandwidth_report_data aggregates routers, interfaces, and top conversations."""
    mock_db = AsyncMock()

    ep_id = uuid4()
    mock_ep = Endpoint(
        id=ep_id,
        hostname="core-gw-01",
        ip_address="10.0.0.1",
        device_type="FIREWALL",
        endpoint_status="ACTIVE",
        flow_interface_aliases={
            "1": {"name": "WAN-Uplink-Primary", "speed_mbps": 1000},
            "2": {"name": "LAN-Access", "speed_mbps": 1000},
        },
        deleted_at=None,
    )

    # 1. Fetch endpoints
    mock_res_ep = MagicMock()
    mock_res_ep.scalars().all.return_value = [mock_ep]

    # 2. Interface rollups (exporter_id, interface_idx, tot_in_bytes, tot_out_bytes, max_in_bytes, max_out_bytes)
    mock_res_ifs = MagicMock()
    mock_res_ifs.all.return_value = [
        (ep_id, 1, 50000000, 100000000, 5000000, 10000000),
        (ep_id, 2, 50000000, 100000000, 2500000, 5000000),
    ]

    # 3. Top forensic conversations (src_ip, dst_ip, dst_port, protocol, exporter_id, total_bytes)
    mock_res_convs = MagicMock()
    mock_res_convs.all.return_value = [
        ("10.0.0.10", "1.1.1.1", 53, 17, ep_id, 5000000),
    ]

    # 4. QoS / DSCP Breakdown (tos, total_bytes, total_packets, flow_count)
    mock_res_qos = MagicMock()
    mock_res_qos.all.return_value = [
        (184, 10000000, 8000, 50),  # ToS 184 -> DSCP 46 (EF VoIP)
        (0, 40000000, 30000, 200),  # ToS 0 -> DSCP 0 (Best Effort CS0)
    ]

    # 5. Anomaly Query (protocol, tcp_flags, flow_count, total_bytes)
    # 5. Anomaly summary over conversations (shared rule): 15 SYN-only flows, 8 RST flows
    from tests.backend.flow_mocks import mapping_result
    mock_res_anomalies = mapping_result([{
        "flows": 23, "conversations": 2,
        "syn_flows": 15, "syn_bytes": 750, "syn_conversations": 1,
        "rst_flows": 8, "rst_bytes": 400, "rst_conversations": 1,
    }])

    # 6. Traffic Zones Query (src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, total_bytes, flow_count)
    mock_res_zones = MagicMock()
    mock_res_zones.all.return_value = [
        ("10.0.0.10", "10.0.0.20", ep_id, ep_id, 30000000, 150),
        ("10.0.0.10", "8.8.8.8", ep_id, None, 20000000, 100),
    ]

    # 3b. All traffic in the window: conversation shares are of this total (not of the top ten)
    mock_res_total = MagicMock()
    mock_res_total.scalar.return_value = 20000000

    mock_db.execute.side_effect = [
        mock_res_ep,
        mock_res_ifs,
        mock_res_convs,
        mock_res_total,
        mock_res_qos,
        mock_res_anomalies,
        mock_res_zones,
    ]

    start_dt = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)

    data = await compile_bandwidth_report_data(
        db=mock_db,
        exporter_ids=[ep_id],
        start_dt=start_dt,
        end_dt=end_dt,
    )

    assert len(data["exporters"]) == 1
    assert len(data["interfaces"]) == 2
    assert data["interfaces"][0]["interface_name"] == "WAN-Uplink-Primary"
    assert data["interfaces"][0]["formatted_speed"] == "1 Gbps"
    assert len(data["conversations"]) == 1
    assert data["conversations"][0]["port_label"] == "53 / DNS"

    # Verify enriched flow analytics
    assert "qos_classes" in data
    assert len(data["qos_classes"]) == 2
    assert any(q["dscp"] == 46 for q in data["qos_classes"])

    assert "anomalies" in data
    # Counts are conversations; flows are reported beside them (the report used to call flows "bursts").
    assert data["anomalies"]["total_count"] == 2
    assert data["anomalies"]["syn_scan_count"] == 1
    assert data["anomalies"]["syn_scan_flows"] == "15"
    assert data["anomalies"]["rst_burst_count"] == 1
    assert data["anomalies"]["rst_burst_flows"] == "8"
    assert data["anomalies"]["total_flows"] == "23"
    assert data["anomalies"]["has_anomalies"] is True
    # Shares are of all traffic in the window (20 MB), not of the top ten (5 MB here).
    assert data["conversations"][0]["pct_of_transit"] == 25.0

    assert "traffic_zones" in data
    assert len(data["traffic_zones"]) == 4
    east_west = next(z for z in data["traffic_zones"] if "East-West" in z["zone"])
    assert east_west["total_bytes"] == 30000000


@pytest.mark.anyio
async def test_compile_master_report_data():
    """Verify compile_master_report_data synthesizes availability, SLA, interfaces, and conversations."""
    mock_db = AsyncMock()
    ep_id = uuid4()
    start_dt = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)

    mock_avail = {
        "endpoints": [{"hostname": "node-1"}],
        "incidents": [],
        "summary": {"mean_uptime": "99.95"},
    }
    mock_bw = {
        "exporters": [{"hostname": "gw-1"}],
        "interfaces": [{"interface_name": "WAN-1"}],
        "conversations": [{"port_label": "443 / HTTPS"}],
    }

    with patch("app.services.report_compiler.compile_availability_report_data", new_callable=AsyncMock) as m_avail, \
         patch("app.services.report_compiler.compile_bandwidth_report_data", new_callable=AsyncMock) as m_bw:
        m_avail.return_value = mock_avail
        m_bw.return_value = mock_bw

        master = await compile_master_report_data(mock_db, [ep_id], start_dt, end_dt)

        assert "endpoints" in master
        assert "incidents" in master
        assert "summary" in master
        assert "exporters" in master
        assert "interfaces" in master
        assert "conversations" in master
        assert master["summary"]["mean_uptime"] == "99.95"
        assert master["interfaces"][0]["interface_name"] == "WAN-1"


# =====================================================================
# 4. RBAC Authorization & Security Access Control Tests
# =====================================================================

@pytest.mark.parametrize("role", ["ADMIN", "VIEWER", "OPERATOR"])
def test_reports_pdf_allowed_for_any_authenticated_role(role):
    """PDF reports are read-only: every authenticated role (incl. legacy OPERATOR from restores) may generate them."""
    mock_operator = {
        "id": str(uuid4()),
        "username": f"{role.lower()}_user",
        "role": role,
        "is_active": True,
    }

    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_get_current_user():
        return mock_operator

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user

    ep_id = uuid4()
    mock_ep = Endpoint(
        id=ep_id,
        hostname="branch-sw-01",
        ip_address="172.16.1.1",
        device_type="SWITCH",
        monitoring_enabled=True,
        endpoint_status="ACTIVE",
        created_at=datetime.now(timezone.utc),
        deleted_at=None,
    )

    # 1. Organization branding query
    mock_res_org = MagicMock()
    mock_res_org.scalars().all.return_value = []

    # 2. compile_availability_report_data ep query
    mock_res_ep = MagicMock()
    mock_res_ep.scalars().all.return_value = [mock_ep]

    # 3. events query
    mock_res_events = MagicMock()
    mock_res_events.scalars().all.return_value = []

    # 4. incidents query
    mock_res_incidents = MagicMock()
    mock_res_incidents.all.return_value = []

    # 5. rca query
    mock_res_rca = MagicMock()
    mock_res_rca.scalars().all.return_value = []

    mock_session.execute.side_effect = [
        mock_res_org,       # get_organization_branding
        mock_res_ep,        # compile_availability_report_data ep query
        mock_res_events,    # events_stmt
        mock_res_incidents, # incidents_stmt
        mock_res_rca,       # rca_stmt
    ]

    with patch("app.services.report_compiler.get_service_gap_intervals", new_callable=AsyncMock) as mock_gaps:
        mock_gaps.return_value = []
        with TestClient(test_app) as client:
            payload = {
                "template_type": "availability",
                "endpoint_ids": [str(ep_id)],
                "start_date": "2026-09-01T00:00:00Z",
                "end_date": "2026-09-02T00:00:00Z",
                "include_rca": False,
            }
            res = client.post("/reports/pdf", json=payload)
            assert res.status_code == 200
            assert res.headers["content-type"] == "application/pdf"
            assert res.content.startswith(b"%PDF-")

    test_app.dependency_overrides.clear()


# =====================================================================
# 5. Organization Branding API & Validation Tests
# =====================================================================

def test_organization_branding_rbac_and_crud():
    """
    Ensure:
    1. GET /settings/organization is readable by authenticated users.
    2. PATCH /settings/organization requires ADMIN role.
    3. Admins can update company name, department, report footer, and logo.
    4. Oversized logos (> 2MB) return 400 Bad Request.
    """
    mock_admin = {
        "id": str(uuid4()),
        "username": "sysadmin",
        "role": "ADMIN",
        "is_active": True,
    }

    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_get_current_user():
        return mock_admin

    async def override_require_admin():
        return mock_admin

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[require_admin] = override_require_admin

    # Mock existing settings
    mock_setting = AppSetting(
        setting_key="org:company_name",
        setting_value="Apex Global Telecom",
    )
    mock_res = MagicMock()
    mock_res.scalars().all.return_value = [mock_setting]
    mock_res.scalar_one_or_none.return_value = mock_setting
    mock_session.execute.return_value = mock_res

    with TestClient(test_app) as client:
        # GET branding
        get_res = client.get("/settings/organization")
        assert get_res.status_code == 200
        data = get_res.json()["data"]
        assert data["companyName"] == "Apex Global Telecom"

        # PATCH branding valid
        patch_res = client.patch(
            "/settings/organization",
            json={
                "company_name": "Zenith Cloud Networks",
                "department": "Infrastructure Security",
                "report_footer": "Internal Audit Only",
                "logo_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            }
        )
        assert patch_res.status_code == 200

        # PATCH branding oversized logo (> 2MB)
        huge_logo = "data:image/png;base64," + ("A" * 2_500_000)
        oversized_res = client.patch(
            "/settings/organization",
            json={"logo_data": huge_logo}
        )
        assert oversized_res.status_code == 400
        assert "exceeds maximum allowed size" in oversized_res.json()["detail"]

    test_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_report_compiler_traffic_zones_with_cidr_masks():
    """Verify compile_bandwidth_report_data strips CIDR subnet masks (/32, /24) when classifying traffic zones."""
    from datetime import datetime, timezone, timedelta
    from app.models.endpoint import Endpoint

    exp_id = uuid4()
    mock_router = MagicMock()
    mock_router.id = exp_id
    mock_router.hostname = "edge-router-01"
    mock_router.ip_address = "10.0.0.1/32"
    mock_router.flow_interface_aliases = {}

    m_routers = MagicMock()
    m_routers.scalars.return_value.all.return_value = [mock_router]

    m_if = MagicMock()
    m_if.all.return_value = []

    m_conv = MagicMock()
    m_conv.all.return_value = []

    m_qos = MagicMock()
    m_qos.all.return_value = []

    m_anomaly = MagicMock()
    m_anomaly.all.return_value = []

    # Traffic zones rows with CIDR netmasks: (src_ip, dst_ip, src_ep, dst_ep, bytes, flows)
    m_zone = MagicMock()
    m_zone.all.return_value = [
        ("192.168.10.1/32", "172.16.0.5/24", None, None, 50000, 10),    # East-West (both private with CIDR)
        ("192.168.10.1/32", "8.8.4.4/32", None, None, 150000, 30),      # North-South Egress
        ("8.8.4.4/32", "192.168.10.1/32", None, None, 200000, 40),      # North-South Ingress
        ("1.0.0.1/32", "8.8.8.8/32", None, None, 10000, 2),             # External / Transit
    ]

    mock_db = AsyncMock()
    m_total = MagicMock()
    m_total.scalar.return_value = 0

    mock_db.execute.side_effect = [m_routers, m_if, m_conv, m_total, m_qos, m_anomaly, m_zone]

    now = datetime.now(timezone.utc)
    res = await compile_bandwidth_report_data(
        mock_db,
        start_dt=now - timedelta(hours=1),
        end_dt=now,
        exporter_ids=[exp_id],
    )

    zones = {z["zone"]: z for z in res["traffic_zones"]}
    assert zones["East-West (Internal)"]["total_bytes"] == 50000
    assert zones["North-South Egress"]["total_bytes"] == 150000
    assert zones["North-South Ingress"]["total_bytes"] == 200000
    assert zones["External / Transit"]["total_bytes"] == 10000



@pytest.mark.parametrize("role", ["ADMIN", "VIEWER", "OPERATOR"])
def test_batch_csv_export_allowed_for_any_authenticated_role(role):
    """Telemetry CSV export is read-only, so no role is rejected before streaming starts."""
    from app.core.asgi import StreamingResponse
    from app.routers.reports import BatchExportRequest, batch_export_telemetry

    request = BatchExportRequest(
        endpoint_ids=[uuid4()],
        start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    response = asyncio.run(batch_export_telemetry(request=request, current_user={"id": str(uuid4()), "role": role}))
    assert isinstance(response, StreamingResponse)


# =====================================================================
# 5. Report content reaches the PDF
# =====================================================================

def test_template_inline_conditional_renders_chosen_branch():
    """`x if cond else y` must render, not silently become empty (anomaly counts were blank)."""
    from app.core.template import TemplateEnvironment
    env = TemplateEnvironment(search_path=".", autoescape=True)
    tpl = env.from_string("{{ a.n if a else 0 }}|{{ a.b if a else '0 B' }}")
    assert tpl.render(a={"n": 15, "b": "750 KB"}) == "15|750 KB"
    assert tpl.render(a={}) == "0|0 B"
    assert tpl.render(a=None) == "0|0 B"
    # Only the chosen branch is evaluated, and the result is still escaped.
    assert env.from_string("{{ x if x else y }}").render(x="<b>", y="unused") == "&lt;b&gt;"


@pytest.mark.parametrize("template_name", ["template_b_bandwidth.html", "template_c_master.html"])
def test_anomaly_counts_and_volumes_appear_in_pdf(template_name):
    from app.routers.reports import jinja_env
    from app.core.pdf import _render_pdf_sync

    context = {
        "org": {"company_name": "Acme Corp"},
        "report_type_label": "Report",
        "period_label": "2026-09-01 to 2026-09-02 UTC",
        "generated_at": "2026-09-02 12:00",
        "scope_title": "1 exporter",
        "endpoints": [], "incidents": [], "summary": {}, "exporters": [], "interfaces": [],
        "conversations": [], "qos_classes": [], "traffic_zones": [],
        "anomalies": {"total_count": 23, "syn_scan_count": 15, "syn_scan_bytes": "750 KB",
                      "rst_burst_count": 8, "rst_burst_bytes": "401 KB"},
    }
    html_out = jinja_env.get_template(template_name).render(**context)
    text_out = "\n".join(p.extract_text() for p in PdfReader(_render_pdf_sync(html_out, "t")).pages)
    for expected in ("15", "750 KB", "8", "401 KB"):
        assert expected in text_out.split("\n"), expected


@pytest.mark.parametrize("template_type,compiler", [("bandwidth", "compile_bandwidth_report_data"),
                                                   ("master", "compile_master_report_data")])
def test_pdf_endpoint_passes_wan_edge_and_coverage_notes(template_type, compiler):
    """The compilers return wan_edge and coverage_notes; the router must hand both to the template."""
    data = {
        "endpoints": [], "incidents": [], "summary": {}, "exporters": [], "interfaces": [],
        "conversations": [], "qos_classes": [], "anomalies": {}, "traffic_zones": [],
        "wan_edge": {"interfaces_count": 2, "formatted_in_bytes": "4.21 TB", "formatted_out_bytes": "1.07 TB",
                     "formatted_in_bps": "12.9 Mbps", "formatted_out_bps": "3.3 Mbps"},
        "coverage_notes": ["Router and interface totals cover 2026-08-01 onward: this data is kept 90 days."],
    }
    captured = {}

    async def fake_render(html_content, title=""):
        captured["html"] = html_content
        return b"%PDF-1.4 fake"

    async def override_get_db():
        yield AsyncMock()

    async def override_get_current_user():
        return {"id": str(uuid4()), "username": "viewer", "role": "VIEWER", "is_active": True}

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        with patch("app.routers.reports.get_organization_branding", new=AsyncMock(return_value={})), \
             patch(f"app.routers.reports.{compiler}", new=AsyncMock(return_value=data)), \
             patch("app.routers.reports.render_pdf_async", new=fake_render):
            with TestClient(test_app) as client:
                res = client.post("/reports/pdf", json={
                    "template_type": template_type,
                    "endpoint_ids": [str(uuid4())],
                    "start_date": "2026-09-01T00:00:00Z",
                    "end_date": "2026-09-02T00:00:00Z",
                })
        assert res.status_code == 200
    finally:
        test_app.dependency_overrides.clear()

    assert "kept 90 days" in captured["html"]
    if template_type == "bandwidth":  # the master template has no WAN edge table
        assert "4.21 TB" in captured["html"]
