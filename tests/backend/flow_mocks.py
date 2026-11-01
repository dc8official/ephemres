"""Mock query results for routes that read raw SQL rows through `.mappings()`."""
from unittest.mock import MagicMock


class _Mappings(list):
    def first(self):
        return self[0] if self else None

    def all(self):
        return list(self)


def mapping_result(rows):
    """A result whose `.mappings()` yields `rows` (dicts) and supports `.first()`."""
    result = MagicMock()
    result.mappings.return_value = _Mappings(rows)
    result.all.return_value = [tuple(r.values()) for r in rows]
    return result


def conversation_row(src, dst, protocol, port, total_bytes, flows, packets=0, flags=0, tos=0, anomaly=None):
    """A row as produced by app.services.conversations (anomaly_hint is computed in SQL)."""
    return {
        "src_ip": src,
        "dst_ip": dst,
        "protocol": protocol,
        "dst_port": port,
        "total_bytes": total_bytes,
        "total_flows": flows,
        "total_packets": packets,
        "tcp_flags_mask": flags,
        "max_tos": tos,
        "anomaly_hint": anomaly,
        "dscp": (tos & 255) >> 2,
        "avg_packet_size": total_bytes // packets if packets else 0,
    }


def talker_row(ip, endpoint_id, ingress, egress, flows, packets=0):
    return {
        "ip": ip,
        "endpoint_id": endpoint_id,
        "ingress_bytes": ingress,
        "egress_bytes": egress,
        "total_bytes": ingress + egress,
        "flow_count": flows,
        "total_packets": packets,
    }


def top_talkers_results(talkers, hostnames, conversations, conversation_hostnames):
    """Result sequence for GET /bandwidth/top-talkers, in query order: talkers, endpoint
    hostnames (only when a talker has an endpoint), conversation counts, conversation page,
    conversation hostnames (only when the page is non-empty)."""
    results = [mapping_result(talkers)]
    if any(t["endpoint_id"] for t in talkers):
        host = MagicMock()
        host.all.return_value = hostnames
        results.append(host)
    results.append(mapping_result([{"all_count": len(conversations), "anomalies": 0, "qos": 0, "bulk": 0}]))
    results.append(mapping_result(conversations))
    if conversations:
        results.append(mapping_result([{"ip": ip, "hostname": h} for ip, h in conversation_hostnames]))
    return results
