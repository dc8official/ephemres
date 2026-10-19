from __future__ import annotations

import datetime
import socket
import struct
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from app.core.asgi import FastAPI, TestClient

from app.database import get_db
from app.routers.auth import get_current_user, require_admin
from app.routers.bandwidth import router as bandwidth_router
from app.schemas.bandwidth import (
    format_dscp_label,
    format_tcp_flags,
    derive_traffic_profile,
    BandwidthOverview,
    TrafficSeriesPoint,
    TopTalkerItem,
    TopConversationItem,
    QoSDistributionItem,
    TrafficZoneItem,
    ApplicationDistributionResponse,
)
from monitoring.flow.aggregator import (
    FlowAggregator,
    KNOWN_SERVICE_PORTS,
    normalize_service_port,
)
from monitoring.flow.ipfix_parser import (
    IPFIXParser,
    IPFIXFieldSpec,
    IPFIXTemplate,
    IE_OCTET_DELTA_COUNT,
    IE_PACKET_DELTA_COUNT,
    IE_PROTOCOL_IDENTIFIER,
    IE_IP_CLASS_OF_SERVICE,
    IE_TCP_CONTROL_BITS,
    IE_SOURCE_TRANSPORT_PORT,
    IE_SOURCE_IPV4_ADDRESS,
    IE_DESTINATION_TRANSPORT_PORT,
    IE_DESTINATION_IPV4_ADDRESS,
    IE_FLOW_START_DELTA_MICROSECONDS,
    IE_FLOW_END_DELTA_MICROSECONDS,
    IE_FLOW_START_SYS_UP_TIME,
    IE_FLOW_END_SYS_UP_TIME,
    IE_POST_OCTET_DELTA_COUNT,
    IE_POST_PACKET_DELTA_COUNT,
    IE_OCTET_TOTAL_COUNT,
    IE_PACKET_TOTAL_COUNT,
)
from monitoring.flow.v5_parser import RECORD_STRUCT, parse_v5_packet
from monitoring.flow.v9_parser import (
    V9Parser,
    FT_IN_BYTES,
    FT_IN_PKTS,
    FT_IPV4_DST_ADDR,
    FT_IPV4_SRC_ADDR,
    FT_L4_DST_PORT,
    FT_L4_SRC_PORT,
    FT_OUT_BYTES,
    FT_OUT_PKTS,
    FT_PROTOCOL,
    FT_SRC_TOS,
    FT_TCP_FLAGS,
)


# ==============================================================================
# 1. IPFIX Variable-Length Fields (RFC 7011) + ToS + Delta Microseconds
# ==============================================================================

def test_ipfix_rfc7011_variable_length_and_tos_and_deltas():
    parser = IPFIXParser()
    export_time = 1700000000
    obs_domain_id = 1
    template_id = 300

    # 1. Template Set (Set ID 2)
    # Fields:
    # 1. IE_SOURCE_IPV4_ADDRESS (8), len 4
    # 2. IE_DESTINATION_IPV4_ADDRESS (12), len 4
    # 3. IE_IP_CLASS_OF_SERVICE (5), len 1
    # 4. IE_TCP_CONTROL_BITS (6), len 1
    # 5. IE_FLOW_START_DELTA_MICROSECONDS (158), len 4
    # 6. IE_FLOW_END_DELTA_MICROSECONDS (159), len 4
    # 7. IE_OCTET_DELTA_COUNT (1), len 4
    # 8. Variable length field (IE 999), length 65535 (0xFFFF)
    tmpl_fields_bytes = (
        struct.pack("!HH", 8, 4)
        + struct.pack("!HH", 12, 4)
        + struct.pack("!HH", 5, 1)
        + struct.pack("!HH", 6, 1)
        + struct.pack("!HH", 158, 4)
        + struct.pack("!HH", 159, 4)
        + struct.pack("!HH", 1, 4)
        + struct.pack("!HH", 999, 65535)
    )
    tmpl_count = 8
    tmpl_record = struct.pack("!HH", template_id, tmpl_count) + tmpl_fields_bytes
    tmpl_set_len = 4 + len(tmpl_record)
    tmpl_set = struct.pack("!HH", 2, tmpl_set_len) + tmpl_record

    # 2. Data Set (Set ID 300) with 2 records:
    # Record A: Variable length < 255 (e.g. 10 bytes)
    var_a = b"0123456789"
    var_a_header = bytes([len(var_a)])  # 1-byte length prefix
    rec_a = (
        socket.inet_aton("192.168.1.10")
        + socket.inet_aton("10.0.0.1")
        + struct.pack("!B", 0xB8)  # ToS: 184 (DSCP 46, EF)
        + struct.pack("!B", 0x18)  # TCP flags: PSH, ACK
        + struct.pack("!I", 5_000_000)  # start 5.0s before export_time
        + struct.pack("!I", 1_000_000)  # end 1.0s before export_time
        + struct.pack("!I", 1500)  # octets
        + var_a_header
        + var_a
    )

    # Record B: Variable length >= 255 (e.g. 256 bytes -> 0xFF + uint16 256)
    var_b = b"A" * 256
    var_b_header = bytes([255]) + struct.pack("!H", len(var_b))
    rec_b = (
        socket.inet_aton("192.168.1.20")
        + socket.inet_aton("10.0.0.2")
        + struct.pack("!B", 0x28)  # ToS: 40 (DSCP 10, AF11)
        + struct.pack("!B", 0x02)  # TCP flags: SYN
        + struct.pack("!I", 2_000_000)  # start 2.0s before export_time
        + struct.pack("!I", 500_000)  # end 0.5s before export_time
        + struct.pack("!I", 3000)  # octets
        + var_b_header
        + var_b
    )

    data_records = rec_a + rec_b
    data_set_len = 4 + len(data_records)
    data_set = struct.pack("!HH", template_id, data_set_len) + data_records

    payload = tmpl_set + data_set
    total_len = 16 + len(payload)
    header = struct.pack("!HHIII", 10, total_len, export_time, 1, obs_domain_id)
    packet = header + payload

    flows = parser.parse_packet(packet, "10.254.0.1")
    assert len(flows) == 2

    # Verify flow A
    f_a = flows[0]
    assert f_a["src_ip"] == "192.168.1.10"
    assert f_a["dst_ip"] == "10.0.0.1"
    assert f_a["tos"] == 0xB8
    assert f_a["tcp_flags"] == 0x18
    assert f_a["bytes"] == 1500
    assert abs(f_a["start_time"] - (export_time - 5.0)) < 0.001
    assert abs(f_a["end_time"] - (export_time - 1.0)) < 0.001

    # Verify flow B
    f_b = flows[1]
    assert f_b["src_ip"] == "192.168.1.20"
    assert f_b["dst_ip"] == "10.0.0.2"
    assert f_b["tos"] == 0x28
    assert f_b["tcp_flags"] == 0x02
    assert f_b["bytes"] == 3000
    assert abs(f_b["start_time"] - (export_time - 2.0)) < 0.001
    assert abs(f_b["end_time"] - (export_time - 0.5)) < 0.001


def test_ipfix_sysuptime_duration_fallback_and_total_counts():
    parser = IPFIXParser()
    export_time = 1700000000
    obs_domain_id = 1
    template_id = 301

    # Template with sysUpTime ms (IE 21, 22) and total counters (IE 85, 86)
    tmpl_record = struct.pack("!HH", template_id, 6) + (
        struct.pack("!HH", IE_SOURCE_IPV4_ADDRESS, 4)
        + struct.pack("!HH", IE_DESTINATION_IPV4_ADDRESS, 4)
        + struct.pack("!HH", IE_FLOW_START_SYS_UP_TIME, 4)
        + struct.pack("!HH", IE_FLOW_END_SYS_UP_TIME, 4)
        + struct.pack("!HH", IE_OCTET_TOTAL_COUNT, 4)
        + struct.pack("!HH", IE_PACKET_TOTAL_COUNT, 4)
    )
    tmpl_set = struct.pack("!HH", 2, 4 + len(tmpl_record)) + tmpl_record

    rec = (
        socket.inet_aton("172.16.0.1")
        + socket.inet_aton("172.16.0.2")
        + struct.pack("!I", 10000)  # start: 10,000 ms
        + struct.pack("!I", 14500)  # end: 14,500 ms (duration 4.5s)
        + struct.pack("!I", 50000)  # total octets
        + struct.pack("!I", 45)  # total packets
    )
    data_set = struct.pack("!HH", template_id, 4 + len(rec)) + rec

    payload = tmpl_set + data_set
    packet = struct.pack("!HHIII", 10, 16 + len(payload), export_time, 2, obs_domain_id) + payload

    flows = parser.parse_packet(packet, "172.16.0.254")
    assert len(flows) == 1
    f = flows[0]
    assert f["bytes"] == 50000
    assert f["packets"] == 45
    assert abs(f["start_time"] - (export_time - 4.5)) < 0.001
    assert abs(f["end_time"] - float(export_time)) < 0.001


# ==============================================================================
# 2. NetFlow v5 & v9 ToS and TCP Flags Extraction
# ==============================================================================

def test_v5_parser_extracts_tos_and_tcp_flags():
    src_ip_int = struct.unpack("!I", socket.inet_aton("192.168.1.100"))[0]
    dst_ip_int = struct.unpack("!I", socket.inet_aton("10.0.0.50"))[0]

    record = RECORD_STRUCT.pack(
        src_ip_int,
        dst_ip_int,
        0,  # nexthop
        1,  # in_if
        2,  # out_if
        10,  # packets
        1500,  # bytes
        90000,  # first_ms
        95000,  # last_ms
        12345,  # src_port
        443,  # dst_port
        0,  # pad1
        0x12,  # tcp_flags (SYN + ACK)
        6,  # protocol TCP
        0xB8,  # tos (184 = DSCP 46 EF)
        0, 0, 24, 24, 0,  # as and masks
    )
    header = struct.pack("!HHIIIIBBH", 5, 1, 100000, 1700000000, 0, 1, 0, 0, 0)
    pkt = header + record

    flows = parse_v5_packet(pkt, "192.168.1.1")
    assert len(flows) == 1
    flow = flows[0]
    assert flow["tos"] == 0xB8
    assert flow["tcp_flags"] == 0x12
    assert flow["bytes"] == 1500
    assert flow["packets"] == 10


def test_v9_parser_extracts_tos_and_fallback_counters():
    parser = V9Parser()
    source_id = 1
    exporter_ip = "10.0.0.1"

    # Template with ToS, TCP flags, and FT_OUT_BYTES / FT_OUT_PKTS fallback
    tmpl_fields = (
        struct.pack("!HH", FT_IPV4_SRC_ADDR, 4)
        + struct.pack("!HH", FT_IPV4_DST_ADDR, 4)
        + struct.pack("!HH", FT_L4_SRC_PORT, 2)
        + struct.pack("!HH", FT_L4_DST_PORT, 2)
        + struct.pack("!HH", FT_PROTOCOL, 1)
        + struct.pack("!HH", FT_SRC_TOS, 1)
        + struct.pack("!HH", FT_TCP_FLAGS, 1)
        + struct.pack("!HH", FT_OUT_BYTES, 4)
        + struct.pack("!HH", FT_OUT_PKTS, 4)
    )
    tmpl_id = 260
    flowset_tmpl = struct.pack("!HH", 0, 4 + 4 + len(tmpl_fields)) + struct.pack("!HH", tmpl_id, 9) + tmpl_fields

    data_rec = (
        socket.inet_aton("10.1.1.1")
        + socket.inet_aton("10.2.2.2")
        + struct.pack("!HH", 54321, 80)
        + struct.pack("!BBB", 6, 0x28, 0x02)  # proto 6, tos 40 (AF11), SYN
        + struct.pack("!II", 12345, 12)  # out_bytes=12345, out_pkts=12
    )
    flowset_data = struct.pack("!HH", tmpl_id, 4 + len(data_rec)) + data_rec

    header = struct.pack("!HHIIII", 9, 2, 100000, 1700000000, 1, source_id)
    packet = header + flowset_tmpl + flowset_data

    flows = parser.parse_packet(packet, exporter_ip)
    assert len(flows) == 1
    f = flows[0]
    assert f["tos"] == 0x28
    assert f["tcp_flags"] == 0x02
    assert f["bytes"] == 12345
    assert f["packets"] == 12


# ==============================================================================
# 3. normalize_service_port
# ==============================================================================

def test_normalize_service_port_rules():
    # ICMP protocol 1 and 58 collapse to 0
    assert normalize_service_port(src_port=8, dst_port=0, protocol=1) == 0
    assert normalize_service_port(src_port=0, dst_port=8, protocol=1) == 0
    assert normalize_service_port(src_port=128, dst_port=0, protocol=58) == 0

    # Common service ports preservation
    for port in (3389, 4500, 51820, 5432, 8080):
        assert port in KNOWN_SERVICE_PORTS
        # Client ephemeral port -> service port
        assert normalize_service_port(src_port=54321, dst_port=port, protocol=6) == port
        # Service port -> client ephemeral port
        assert normalize_service_port(src_port=port, dst_port=54321, protocol=6) == port

    # Privileged ports < 1024
    assert normalize_service_port(src_port=50000, dst_port=443, protocol=6) == 443
    assert normalize_service_port(src_port=443, dst_port=50000, protocol=6) == 443

    # Client ephemeral port scatter prevention: src < 10000 and dst >= 10000
    assert normalize_service_port(src_port=8080, dst_port=49152, protocol=6) == 8080
    assert normalize_service_port(src_port=5000, dst_port=50000, protocol=6) == 5000


# ==============================================================================
# 4. FlowAggregator Accumulating tcp_flags (Bitwise OR) and tos (Max)
# ==============================================================================

@pytest.mark.anyio
async def test_flow_aggregator_tcp_flags_or_and_tos_max():
    mock_correlator = MagicMock()
    exp_id = uuid4()
    src_ep_id = uuid4()
    dst_ep_id = uuid4()
    mock_correlator.correlate.return_value = (exp_id, src_ep_id, dst_ep_id)

    mock_db_session = AsyncMock()
    mock_factory = MagicMock(return_value=mock_db_session)
    mock_db_session.__aenter__.return_value = mock_db_session

    agg = FlowAggregator(
        store_client=None,
        correlator=mock_correlator,
        db_session_factory=mock_factory,
    )

    t0 = 1700000000.0  # within same 1-minute bucket (1699999980 to 1700000040)
    # Flow 1: SYN flag (0x02), ToS 32
    agg.accumulate_flow(
        exporter_ip="10.0.0.1",
        src_ip="192.168.1.10",
        dst_ip="10.0.0.20",
        src_port=50000,
        dst_port=443,
        protocol=6,
        flow_bytes=1000,
        flow_packets=2,
        start_time_unix=t0,
        tcp_flags=0x02,
        tos=32,
    )

    # Flow 2: ACK flag (0x10), ToS 184 (0xB8), PSH flag (0x08)
    agg.accumulate_flow(
        exporter_ip="10.0.0.1",
        src_ip="192.168.1.10",
        dst_ip="10.0.0.20",
        src_port=50000,
        dst_port=443,
        protocol=6,
        flow_bytes=500,
        flow_packets=1,
        start_time_unix=t0 + 10.0,
        tcp_flags=0x18,  # ACK + PSH
        tos=184,
    )

    # Verify accumulator entry
    assert len(agg._accumulator) == 1
    key = list(agg._accumulator.keys())[0]
    entry = agg._accumulator[key]
    assert entry[0] == 1500  # bytes
    assert entry[1] == 3     # packets
    assert entry[2] == 2     # flow_count
    assert entry[3] == (0x02 | 0x18)  # bitwise OR = 0x1A
    assert entry[4] == 184   # max(32, 184)

    # Flush to database and check SQL params
    await agg.flush_to_timescaledb()
    assert mock_db_session.execute.called
    call_args = mock_db_session.execute.call_args[0]
    sql_text = str(call_args[0])
    chunk_rows = call_args[1]

    assert "tcp_flags = (COALESCE(flow_minute_rollups.tcp_flags, 0) | EXCLUDED.tcp_flags)" in sql_text
    assert "tos = GREATEST(COALESCE(flow_minute_rollups.tos, 0), EXCLUDED.tos)" in sql_text
    assert len(chunk_rows) == 1
    assert chunk_rows[0]["tcp_flags"] == 0x1A
    assert chunk_rows[0]["tos"] == 184

    # Accumulator should be cleared after successful commit
    assert len(agg._accumulator) == 0


# ==============================================================================
# 5. /top-talkers Ingress/Egress Accounting & Metadata Badges
# ==============================================================================

test_app = FastAPI()
test_app.include_router(bandwidth_router, prefix="/api/v1")


@pytest.fixture
def mock_admin_user():
    admin = MagicMock()
    admin.id = uuid4()
    admin.username = "admin"
    admin.role = "admin"
    admin.is_active = True
    return admin


@pytest.fixture
def api_client(mock_admin_user):
    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_get_current_user():
        return mock_admin_user

    async def override_require_admin():
        return mock_admin_user

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[require_admin] = override_require_admin

    with TestClient(test_app) as client:
        yield client, mock_session

    test_app.dependency_overrides.clear()


def test_top_talkers_true_ingress_egress_and_metadata(api_client):
    client, mock_session = api_client
    ep1_id = uuid4()
    ep2_id = uuid4()

    # 1. Egress query results:
    # IP 10.0.0.1 sent 1,000,000 bytes (100 flows)
    # IP 10.0.0.2 sent 200,000 bytes (20 flows)
    m_egress = MagicMock()
    m_egress.all.return_value = [
        ("10.0.0.1", ep1_id, 1_000_000, 100),
        ("10.0.0.2", ep2_id, 200_000, 20),
    ]

    # 2. Ingress query results:
    # IP 10.0.0.1 received 500,000 bytes (50 flows)
    # IP 10.0.0.2 received 5,000,000 bytes (500 flows)
    m_ingress = MagicMock()
    m_ingress.all.return_value = [
        ("10.0.0.1", ep1_id, 500_000, 50),
        ("10.0.0.2", ep2_id, 5_000_000, 500),
    ]

    # 3. Hostnames query result:
    m_host = MagicMock()
    m_host.all.return_value = [
        (ep1_id, "server-alpha"),
        (ep2_id, "server-beta"),
    ]

    # 4. Top conversations query result:
    # Row 1: SYN only (anomaly), ToS 184 (DSCP 46, EF)
    # Row 2: RST only (anomaly), ToS 0 (DSCP 0, Best Effort)
    # Row 3: SYN + ACK, ToS 40 (DSCP 10, AF11)
    m_conv = MagicMock()
    m_conv.all.return_value = [
        ("10.0.0.1", "10.0.0.2", 6, 443, 800_000, 80, 0x02, 184),
        ("10.0.0.2", "10.0.0.1", 6, 80, 400_000, 40, 0x04, 0),
        ("10.0.0.1", "10.0.0.2", 6, 22, 200_000, 20, 0x12, 40),
    ]

    mock_session.execute.side_effect = [m_egress, m_ingress, m_host, m_conv]

    res = client.get("/api/v1/bandwidth/top-talkers?window=1h&limit=10")
    assert res.status_code == 200
    data = res.json()["data"]

    endpoints = data["top_endpoints"]
    assert len(endpoints) == 2

    # server-beta: total = 200,000 eg + 5,000,000 in = 5,200,000 (ranked #1!)
    assert endpoints[0]["ip_address"] == "10.0.0.2"
    assert endpoints[0]["hostname"] == "server-beta"
    assert endpoints[0]["egress_bytes"] == 200_000
    assert endpoints[0]["ingress_bytes"] == 5_000_000
    assert endpoints[0]["total_bytes"] == 5_200_000
    assert endpoints[0]["flow_count"] == 520

    # server-alpha: total = 1,000,000 eg + 500,000 in = 1,500,000 (ranked #2!)
    assert endpoints[1]["ip_address"] == "10.0.0.1"
    assert endpoints[1]["hostname"] == "server-alpha"
    assert endpoints[1]["egress_bytes"] == 1_000_000
    assert endpoints[1]["ingress_bytes"] == 500_000
    assert endpoints[1]["total_bytes"] == 1_500_000
    assert endpoints[1]["flow_count"] == 150

    # Check top conversations
    conversations = data["top_conversations"]
    assert len(conversations) == 3

    # Conv 1: SYN only, DSCP 46 EF
    c1 = conversations[0]
    assert c1["src_ip"] == "10.0.0.1"
    assert c1["dst_ip"] == "10.0.0.2"
    assert c1["protocol"] == "TCP"
    assert c1["dst_port"] == 443
    assert c1["tcp_flags"] == 0x02
    assert c1["tcp_flags_labels"] == ["SYN"]
    assert c1["tos"] == 184
    assert c1["dscp"] == 46
    assert c1["dscp_label"] == "EF (VoIP)"
    assert c1["anomaly_hint"] == "SYN_ONLY"

    # Conv 2: RST only, DSCP 0 CS0
    c2 = conversations[1]
    assert c2["tcp_flags"] == 0x04
    assert c2["tcp_flags_labels"] == ["RST"]
    assert c2["tos"] == 0
    assert c2["dscp"] == 0
    assert c2["dscp_label"] == "Best Effort (CS0)"
    assert c2["anomaly_hint"] == "TCP_RST"

    # Conv 3: SYN+ACK, DSCP 10 AF11
    c3 = conversations[2]
    assert c3["tcp_flags"] == 0x12
    assert c3["tcp_flags_labels"] == ["SYN", "ACK"]
    assert c3["tos"] == 40
    assert c3["dscp"] == 10
    assert c3["dscp_label"] == "AF11"
    assert c3["anomaly_hint"] is None


def test_schema_helpers_flags_and_dscp():
    # TCP flags formatting
    assert format_tcp_flags(0) == []
    assert format_tcp_flags(0x02) == ["SYN"]
    assert format_tcp_flags(0x12) == ["SYN", "ACK"]
    assert format_tcp_flags(0x18) == ["PSH", "ACK"]
    assert format_tcp_flags(0xFF) == ["FIN", "SYN", "RST", "PSH", "ACK", "URG", "ECE", "CWR"]

    # DSCP label formatting
    assert format_dscp_label(0) == "Best Effort (CS0)"
    assert format_dscp_label(184) == "EF (VoIP)"
    assert format_dscp_label(40) == "AF11"
    assert format_dscp_label(136) == "AF41 (Video)"
    assert format_dscp_label(4) == "DSCP 1"


def test_derive_traffic_profile_classification():
    # 1. Anomaly priority
    assert derive_traffic_profile("SYN_ONLY", 60, 1.0, 0, 443) == "SYN Scan / Probe"
    assert derive_traffic_profile("TCP_RST", 1400, 20.0, 0, 80) == "Reset / Refused"

    # 2. Bulk Transfer (avg_packet_size >= 1100 and packets_per_flow >= 10)
    assert derive_traffic_profile(None, 1450, 12.0, 0, 443) == "Bulk Transfer"
    assert derive_traffic_profile(None, 1100, 10.0, 0, 80) == "Bulk Transfer"

    # 3. Micro-Flow / Control (avg_packet_size <= 120 and packets_per_flow <= 3)
    assert derive_traffic_profile(None, 80, 2.0, 0, 80) == "Micro-Flow / Control"
    assert derive_traffic_profile(None, 120, 3.0, 0, 443) == "Micro-Flow / Control"

    # 4. Real-Time / VoIP (dscp == 46 or dst_port in (5060, 5061, 16384))
    assert derive_traffic_profile(None, 500, 5.0, 46, 12345) == "Real-Time / VoIP"
    assert derive_traffic_profile(None, 200, 5.0, 0, 5060) == "Real-Time / VoIP"
    assert derive_traffic_profile(None, 200, 5.0, 0, 5061) == "Real-Time / VoIP"
    assert derive_traffic_profile(None, 200, 5.0, 0, 16384) == "Real-Time / VoIP"

    # 5. Default: Interactive
    assert derive_traffic_profile(None, 500, 6.0, 0, 443) == "Interactive"
    assert derive_traffic_profile(None, 1200, 5.0, 0, 443) == "Interactive"  # high bytes but low pkts/fl
    assert derive_traffic_profile(None, 200, 20.0, 0, 443) == "Interactive"  # many pkts but medium size


def test_flow_workbench_enriched_schemas():
    overview = BandwidthOverview(
        total_ingress_bps=1000.0,
        total_egress_bps=2000.0,
        total_ingress_pps=15.5,
        total_egress_pps=25.0,
        avg_packet_size=512,
        active_exporters_count=2,
        unmatched_exporters_count=0,
        total_flows_count=100,
        anomaly_flows_count=4,
    )
    assert overview.total_ingress_pps == 15.5
    assert overview.total_egress_pps == 25.0
    assert overview.avg_packet_size == 512
    assert overview.anomaly_flows_count == 4

    point = TrafficSeriesPoint(
        timestamp="2026-10-18T20:00:00Z",
        ingress_bps=5000.0,
        egress_bps=8000.0,
        ingress_pps=12.5,
        egress_pps=20.0,
        flows_per_sec=3.2,
    )
    assert point.ingress_pps == 12.5
    assert point.egress_pps == 20.0
    assert point.flows_per_sec == 3.2

    talker = TopTalkerItem(
        ip_address="192.168.1.50",
        ingress_bytes=60_000,
        egress_bytes=40_000,
        total_bytes=100_000,
        flow_count=10,
        total_packets=200,
        avg_packet_size=500,
    )
    assert talker.total_packets == 200
    assert talker.avg_packet_size == 500

    conv = TopConversationItem(
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        protocol="TCP",
        dst_port=443,
        total_bytes=50_000,
        flow_count=5,
        total_packets=100,
        avg_packet_size=500,
        packets_per_flow=20.0,
        traffic_profile="Interactive",
    )
    assert conv.total_packets == 100
    assert conv.avg_packet_size == 500
    assert conv.packets_per_flow == 20.0
    assert conv.traffic_profile == "Interactive"

    qos_item = QoSDistributionItem(
        dscp=46,
        dscp_label="EF (VoIP)",
        total_bytes=10_000,
        total_packets=50,
        flow_count=5,
        percentage=75.5,
    )
    assert qos_item.dscp == 46
    assert qos_item.percentage == 75.5

    zone_item = TrafficZoneItem(
        zone="East-West (Internal)",
        total_bytes=25_000,
        flow_count=12,
        percentage=60.0,
    )
    assert zone_item.zone == "East-West (Internal)"
    assert zone_item.percentage == 60.0

    app_resp = ApplicationDistributionResponse(
        applications=[],
        qos_classes=[qos_item],
        traffic_zones=[zone_item],
    )
    assert len(app_resp.qos_classes) == 1
    assert len(app_resp.traffic_zones) == 1


def test_applications_qos_and_traffic_zones_integration(api_client):
    client, mock_session = api_client

    # 1. Applications query result: (protocol, dst_port, total_bytes)
    m_apps = MagicMock()
    m_apps.all.return_value = [
        (6, 443, 600_000),
        (17, 53, 400_000),
    ]

    # 2. QoS query result: (tos, total_bytes, total_packets, flow_count)
    # ToS 184 = DSCP 46 (EF)
    # ToS 0 = DSCP 0 (CS0)
    m_qos = MagicMock()
    m_qos.all.return_value = [
        (184, 750_000, 750, 75),
        (0, 250_000, 250, 25),
    ]

    # 3. Zones query result: (src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, total_bytes, flow_count)
    ep1_id = uuid4()
    ep2_id = uuid4()
    m_zones = MagicMock()
    m_zones.all.return_value = [
        # Internal to internal -> East-West (Internal)
        ("192.168.1.10", "192.168.1.20", ep1_id, ep2_id, 500_000, 50),
        # Internal to public -> North-South Egress
        ("192.168.1.10", "8.8.8.8", ep1_id, None, 300_000, 30),
        # Public to internal -> North-South Ingress
        ("1.1.1.1", "192.168.1.20", None, ep2_id, 200_000, 20),
    ]

    mock_session.execute.side_effect = [m_apps, m_qos, m_zones]

    exp_id = uuid4()
    res = client.get(f"/api/v1/bandwidth/applications?window=1h&exporter_id={exp_id}")
    assert res.status_code == 200
    data = res.json()["data"]

    # Applications check
    apps = data["applications"]
    assert len(apps) == 2
    assert apps[0]["service_label"] == "HTTPS"
    assert apps[0]["total_bytes"] == 600_000

    # QoS check
    qos = data["qos_classes"]
    assert len(qos) == 2
    assert qos[0]["dscp"] == 46
    assert qos[0]["dscp_label"] == "EF (VoIP)"
    assert qos[0]["total_bytes"] == 750_000
    assert qos[0]["percentage"] == 75.0
    assert qos[1]["dscp"] == 0
    assert qos[1]["percentage"] == 25.0

    # Zones check
    zones = data["traffic_zones"]
    assert len(zones) == 4
    zone_names = [z["zone"] for z in zones]
    assert "East-West (Internal)" in zone_names
    assert "North-South Egress" in zone_names
    assert "North-South Ingress" in zone_names
    assert "External / Transit" in zone_names

    ew_zone = next(z for z in zones if z["zone"] == "East-West (Internal)")
    assert ew_zone["total_bytes"] == 500_000
    assert ew_zone["percentage"] == 50.0


