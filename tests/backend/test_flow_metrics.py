"""Parity tests for app.services.flow_metrics.

The `_legacy_*` functions are verbatim copies of the inline code that bandwidth.py and
report_compiler.py used before the extraction (v3.3.2.v). Each helper must reproduce them over
a grid of inputs, so the refactor cannot change what the dashboard or the PDF report shows.
"""
import ipaddress
import itertools

import pytest

from app.schemas.bandwidth import format_dscp_label
from app.services import flow_metrics as fm


# --- legacy reference implementations -------------------------------------------------------

def _legacy_bandwidth_alias(alias_data, if_idx):
    if isinstance(alias_data, dict):
        name = str(alias_data.get("name", f"Interface {if_idx}"))
        speed_mbps = int(alias_data.get("speed_mbps", 1000))
    elif isinstance(alias_data, str):
        name = alias_data
        speed_mbps = 1000
    else:
        name = f"Interface {if_idx}"
        speed_mbps = 1000
    return name, max(1, speed_mbps)


def _legacy_report_alias(alias_entry, if_idx):
    if isinstance(alias_entry, str):
        return alias_entry, 1000
    if isinstance(alias_entry, dict):
        return alias_entry.get("name", f"Interface #{if_idx}"), int(alias_entry.get("speed_mbps", 1000) or 1000)
    return f"Interface #{if_idx}", 1000


def _legacy_bandwidth_util(in_bps, out_bps, speed_mbps):
    capacity_bps = speed_mbps * 1_000_000.0
    peak_bps = max(in_bps, out_bps)
    return round(min(100.0, (peak_bps / capacity_bps) * 100.0), 2)


def _legacy_report_util(peak_rate_bps, speed_mbps):
    speed_bps = speed_mbps * 1e6
    return min(100.0, (peak_rate_bps / speed_bps * 100.0) if speed_bps > 0 else 0.0)


def _legacy_dscp(rows):
    dscp_buckets = {}
    total = 0
    for q_row in rows:
        if len(q_row) < 2:
            continue
        tos_val = int(q_row[0] or 0)
        q_bytes = int(q_row[1] or 0)
        q_pkts = int(q_row[2] or 0) if len(q_row) > 2 else 0
        q_flows = int(q_row[3] or 0) if len(q_row) > 3 else 0
        dscp_val = (tos_val & 0xFF) >> 2
        total += q_bytes
        if dscp_val not in dscp_buckets:
            dscp_buckets[dscp_val] = {
                "dscp": dscp_val,
                "dscp_label": format_dscp_label(tos_val),
                "total_bytes": 0,
                "total_packets": 0,
                "flow_count": 0,
            }
        dscp_buckets[dscp_val]["total_bytes"] += q_bytes
        dscp_buckets[dscp_val]["total_packets"] += q_pkts
        dscp_buckets[dscp_val]["flow_count"] += q_flows
    out = []
    for item in sorted(dscp_buckets.values(), key=lambda x: x["total_bytes"], reverse=True):
        pct = (item["total_bytes"] / total * 100.0) if total > 0 else 0.0
        out.append({**item, "percentage": pct})
    return out


def _legacy_is_internal(ip_val, ep_val):
    if ep_val is not None:
        return True
    try:
        clean_ip = str(ip_val or "").split("/")[0].strip()
        if not clean_ip:
            return False
        return ipaddress.ip_address(clean_ip).is_private
    except Exception:
        return False


def _legacy_zone(src_int, dst_int):
    if src_int and dst_int:
        return "East-West (Internal)"
    elif src_int and not dst_int:
        return "North-South Egress"
    elif not src_int and dst_int:
        return "North-South Ingress"
    return "External / Transit"


def _legacy_conversation_hint(flags):
    if (flags & 0x02) and not (flags & 0x10):
        return "SYN_ONLY"
    elif (flags & 0x04) and not (flags & 0x01):
        return "TCP_RST"
    return None


def _legacy_overview_is_anomaly(a_flags):
    return bool(((a_flags & 0x02) and not (a_flags & 0x10)) or ((a_flags & 0x04) and not (a_flags & 0x01)))


# --- parity tests ---------------------------------------------------------------------------

ALIASES = [
    None,
    {},
    "WAN-Uplink",
    {"name": "Gi0/1"},
    {"name": "Gi0/1", "speed_mbps": 100},
    {"speed_mbps": 10000},
    {"name": "LAN", "speed_mbps": 1000},
    {"name": "Core", "speed_mbps": "40000"},
]


@pytest.mark.parametrize("alias", ALIASES)
def test_interface_alias_matches_both_legacy_callers(alias):
    name, speed = fm.interface_alias(alias, "Interface 7")
    assert (name, max(1, speed)) == _legacy_bandwidth_alias(alias, 7)

    report_alias = alias if alias is not None else {}
    assert fm.interface_alias(report_alias, "Interface #7") == _legacy_report_alias(report_alias, 7)


def test_interface_alias_tolerates_null_speed():
    # The old bandwidth.py code raised TypeError on {"speed_mbps": None}; the report used 1 Gbps.
    assert fm.interface_alias({"name": "X", "speed_mbps": None}, "d") == ("X", 1000)


@pytest.mark.parametrize(
    "in_bps,out_bps,speed",
    list(itertools.product([0.0, 1.5, 20_000.0, 6_666.67, 2.5e8, 1e12], [0.0, 4e5, 9.9e9], [1, 10, 100, 1000, 40000])),
)
def test_utilization_matches_legacy(in_bps, out_bps, speed):
    assert round(fm.utilization_pct(max(in_bps, out_bps), speed), 2) == _legacy_bandwidth_util(in_bps, out_bps, speed)
    assert fm.utilization_pct(in_bps, speed) == _legacy_report_util(in_bps, speed)


def test_utilization_without_speed_is_zero():
    assert fm.utilization_pct(1e6, 0) == 0.0


@pytest.mark.parametrize("byte_count,seconds", [(0, 60.0), (15_003_000, 300.0), (12_000_000, 60.0), (7, 3600.0)])
def test_bits_per_second(byte_count, seconds):
    assert fm.bits_per_second(byte_count, seconds) == (float(byte_count) * 8.0) / seconds


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(0, 19_203_000, 14_000, 90), (184, 3_000_000, 3_000, 5)],
        [(184, 10, 1, 1), (185, 5, 1, 1), (None, 7, 2, 2), (40, 0, 0, 0)],
        [(72,), (72, 100), (72, 100, 10), (72, 100, 10, 3), (0, 50, 5, 1)],
        [(0, 0, 0, 0)],
    ],
)
def test_dscp_breakdown_matches_legacy(rows):
    assert fm.dscp_breakdown(rows) == _legacy_dscp(rows)


@pytest.mark.parametrize(
    "ip,ep",
    [
        ("10.0.1.10", None),
        ("10.0.1.10/32", None),
        ("52.94.0.1", None),
        ("52.94.0.1", "ep"),
        ("", None),
        (None, None),
        ("not-an-ip", None),
        ("fd00::1", None),
        ("2001:4860::8888", None),
    ],
)
def test_is_internal_address_matches_legacy(ip, ep):
    assert fm.is_internal_address(ip, ep) == _legacy_is_internal(ip, ep)


@pytest.mark.parametrize("src_int,dst_int", list(itertools.product([True, False], repeat=2)))
def test_classify_zone_matches_legacy(src_int, dst_int):
    assert fm.classify_zone(src_int, dst_int) == _legacy_zone(src_int, dst_int)


def test_traffic_zone_order():
    assert fm.TRAFFIC_ZONES == (
        "East-West (Internal)",
        "North-South Egress",
        "North-South Ingress",
        "External / Transit",
    )


@pytest.mark.parametrize("flags", range(256))
def test_tcp_anomaly_matches_every_legacy_flag_check(flags):
    hint = fm.tcp_anomaly(flags)
    assert hint == _legacy_conversation_hint(flags)
    assert (hint is not None) == _legacy_overview_is_anomaly(flags)


def test_tcp_anomaly_handles_missing_flags():
    assert fm.tcp_anomaly(None) is None


@pytest.mark.parametrize("tos", [None, 0, 3, 184, 255, 256 + 184])
def test_dscp_from_tos(tos):
    assert fm.dscp_from_tos(tos) == ((tos or 0) & 0xFF) >> 2


# --- v3.4 Phase 2 additions ------------------------------------------------------------------

def test_percentile_with_idle_matches_percentile_cont_on_padded_series():
    import statistics

    values = [5.0, 1.0, 9.0, 3.0]
    padded = sorted([0.0] * 16 + values)  # 20 slots, 16 idle
    expected = statistics.quantiles(padded, n=100, method="inclusive")[94]
    assert fm.percentile_with_idle(sorted(values), 20) == pytest.approx(expected)


def test_percentile_with_idle_mostly_idle_link_is_zero():
    # 2 busy minutes out of 60: the 95th percentile minute is idle.
    assert fm.percentile_with_idle([800.0, 900.0], 60) == 0.0


def test_percentile_with_idle_fully_busy_equals_plain_percentile():
    values = [float(v) for v in range(1, 101)]
    assert fm.percentile_with_idle(values, 100) == pytest.approx(95.05)


def test_percentile_with_idle_edge_cases():
    assert fm.percentile_with_idle([], 0) == 0.0
    assert fm.percentile_with_idle([7.0], 1) == 7.0
    # More rows than slots (clock skew): never index past the data.
    assert fm.percentile_with_idle([1.0, 2.0, 3.0], 2) == pytest.approx(2.9)


@pytest.mark.parametrize(
    "alias,expected",
    [
        (None, None),
        ("WAN", None),
        ({"name": "WAN"}, None),
        ({"name": "WAN", "speed_mbps": 0}, None),
        ({"name": "WAN", "speed_mbps": None}, None),
        ({"name": "WAN", "speed_mbps": "bad"}, None),
        ({"name": "WAN", "speed_mbps": 100}, 100),
        ({"speed_mbps": "10000"}, 10000),
    ],
)
def test_configured_speed_mbps(alias, expected):
    assert fm.configured_speed_mbps(alias) == expected


@pytest.mark.parametrize(
    "alias,expected",
    [
        (None, "lan"),
        ("WAN-Uplink", "lan"),  # legacy string alias: the name alone never makes a WAN link
        ({"name": "ISP-A"}, "lan"),
        ({"name": "ISP-A", "role": "lan"}, "lan"),
        ({"name": "ISP-A", "role": "WAN"}, "lan"),
        ({"name": "ISP-A", "role": "core"}, "lan"),
        ({"name": "ISP-A", "role": "wan"}, "wan"),
    ],
)
def test_interface_role(alias, expected):
    assert fm.interface_role(alias) == expected
