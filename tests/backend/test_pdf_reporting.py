import io
import os
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
from app.models.system_setting import AppSetting
from app.routers.auth import get_current_user, require_admin, require_operator_or_admin
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

@pytest.mark.anyio
async def test_compile_availability_report_data():
    """Verify compile_availability_report_data compiles endpoint SLA, outages, and metrics."""
    mock_db = AsyncMock()

    ep_id = uuid4()
    now = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)
    start_dt = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)

    mock_ep = Endpoint(
        id=ep_id,
        hostname="edge-router-01",
        ip_address="192.168.10.1",
        device_type="ROUTER",
        monitoring_enabled=True,
        endpoint_status="ACTIVE",
        created_at=start_dt - timedelta(days=10),
        deleted_at=None,
    )

    mock_res_ep = MagicMock()
    mock_res_ep.scalars().all.return_value = [mock_ep]

    # Mock events for the endpoint: 1 UP event (86280s) and 1 DOWN event (120s)
    ev_up = EndpointEvent(
        id=uuid4(),
        endpoint_id=ep_id,
        operational_state="UP",
        detailed_state="UP",
        success_count=60,
        failed_count=0,
        start_time=start_dt,
        end_time=end_dt - timedelta(seconds=120),
        duration_seconds=86280,
        avg_rtt_ms=Decimal("1.50"),
        health_score=Decimal("100.00"),
    )
    ev_down = EndpointEvent(
        id=uuid4(),
        endpoint_id=ep_id,
        operational_state="DOWN",
        detailed_state="DOWN",
        success_count=0,
        failed_count=2,
        start_time=end_dt - timedelta(seconds=120),
        end_time=end_dt,
        duration_seconds=120,
        avg_rtt_ms=None,
        health_score=Decimal("0.00"),
    )
    mock_res_events = MagicMock()
    mock_res_events.scalars().all.return_value = [ev_up, ev_down]

    # Mock incidents list query
    mock_res_incidents = MagicMock()
    mock_res_incidents.all.return_value = [(ev_down, "edge-router-01", "192.168.10.1")]

    # Mock RCA query
    mock_res_rca = MagicMock()
    mock_res_rca.scalars().all.return_value = []

    mock_db.execute.side_effect = [
        mock_res_ep,
        mock_res_events,
        mock_res_incidents,
        mock_res_rca,
    ]

    with patch("app.services.report_compiler.get_service_gap_intervals", new_callable=AsyncMock) as mock_gaps:
        mock_gaps.return_value = []
        data = await compile_availability_report_data(
            db=mock_db,
            endpoint_ids=[ep_id],
            start_dt=start_dt,
            end_dt=end_dt,
            include_rca=True,
        )
        mock_gaps.assert_called_once_with(mock_db, start_dt, end_dt)

        assert len(data["endpoints"]) == 1
        assert data["summary"]["total_incidents"] == 1
        ep_data = data["endpoints"][0]
        assert ep_data["hostname"] == "edge-router-01"
        assert ep_data["incident_count"] == 1
        assert ep_data["uptime_percentage"] > 99.0
        assert "2m" in ep_data["formatted_downtime"] or "120s" in ep_data["formatted_downtime"]


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
    mock_res_anomalies = MagicMock()
    mock_res_anomalies.all.return_value = [
        (6, 2, 15, 750),  # SYN ONLY
        (6, 4, 8, 400),   # TCP RST
    ]

    # 6. Traffic Zones Query (src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, total_bytes, flow_count)
    mock_res_zones = MagicMock()
    mock_res_zones.all.return_value = [
        ("10.0.0.10", "10.0.0.20", ep_id, ep_id, 30000000, 150),
        ("10.0.0.10", "8.8.8.8", ep_id, None, 20000000, 100),
    ]

    mock_db.execute.side_effect = [
        mock_res_ep,
        mock_res_ifs,
        mock_res_convs,
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
    assert data["anomalies"]["total_count"] == 23
    assert data["anomalies"]["syn_scan_count"] == 15
    assert data["anomalies"]["rst_burst_count"] == 8
    assert data["anomalies"]["has_anomalies"] is True

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

def test_reports_pdf_rbac_viewer_rejected():
    """VIEWER role must receive 403 Forbidden when requesting PDF generation."""
    mock_viewer = {
        "id": str(uuid4()),
        "username": "guest_viewer",
        "role": "VIEWER",
        "is_active": True,
    }

    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_require_operator_or_admin():
        from app.core.asgi import HTTPException
        if mock_viewer["role"].upper() not in ("ADMIN", "OPERATOR"):
            raise HTTPException(status_code=403, detail="Operator or Administrator role required.")
        return mock_viewer

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[require_operator_or_admin] = override_require_operator_or_admin

    with TestClient(test_app) as client:
        payload = {
            "template_type": "availability",
            "endpoint_ids": [str(uuid4())],
            "start_date": "2026-09-01T00:00:00Z",
            "end_date": "2026-09-02T00:00:00Z",
            "include_rca": True,
        }
        res = client.post("/reports/pdf", json=payload)
        assert res.status_code == 403
        assert "Operator or Administrator role required" in res.json()["detail"]

    test_app.dependency_overrides.clear()


def test_reports_pdf_rbac_operator_allowed():
    """OPERATOR role is permitted to generate and download PDF reports."""
    mock_operator = {
        "id": str(uuid4()),
        "username": "noc_operator",
        "role": "OPERATOR",
        "is_active": True,
    }

    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_require_operator_or_admin():
        return mock_operator

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[require_operator_or_admin] = override_require_operator_or_admin

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
    mock_db.execute.side_effect = [m_routers, m_if, m_conv, m_qos, m_anomaly, m_zone]

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

