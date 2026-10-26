import socket
import struct
import time
import unittest

from monitoring.flow.v5_parser import parse_v5_packet, HEADER_STRUCT, RECORD_STRUCT
from monitoring.flow.v9_parser import (
    V9Parser,
    FT_IN_BYTES,
    FT_IN_PKTS,
    FT_PROTOCOL,
    FT_L4_SRC_PORT,
    FT_IPV4_SRC_ADDR,
    FT_L4_DST_PORT,
    FT_IPV4_DST_ADDR,
    FT_IPV6_SRC_ADDR,
    FT_IPV6_DST_ADDR,
)
from monitoring.flow.ipfix_parser import (
    IPFIXParser,
    IE_OCTET_DELTA_COUNT,
    IE_PACKET_DELTA_COUNT,
    IE_PROTOCOL_IDENTIFIER,
    IE_SOURCE_TRANSPORT_PORT,
    IE_SOURCE_IPV4_ADDRESS,
    IE_DESTINATION_TRANSPORT_PORT,
    IE_DESTINATION_IPV4_ADDRESS,
    IE_SOURCE_IPV6_ADDRESS,
    IE_DESTINATION_IPV6_ADDRESS,
)


class TestNetflowV5Parser(unittest.TestCase):
    def _create_v5_packet(
        self,
        records=None,
        sys_uptime=100000,
        unix_secs=1700000000,
        unix_nsecs=0,
        sampling=0,
    ):
        if records is None:
            records = []
        count = len(records)
        header = HEADER_STRUCT.pack(
            5,  # version
            count,  # count
            sys_uptime,
            unix_secs,
            unix_nsecs,
            1,  # flow_seq
            0,  # engine_type
            0,  # engine_id
            sampling,  # raw_sampling
        )
        body = bytearray(header)
        for r in records:
            src_ip_int = struct.unpack("!I", socket.inet_aton(r.get("src_ip", "10.0.0.1")))[0]
            dst_ip_int = struct.unpack("!I", socket.inet_aton(r.get("dst_ip", "10.0.0.2")))[0]
            rec = RECORD_STRUCT.pack(
                src_ip_int,
                dst_ip_int,
                struct.unpack("!I", socket.inet_aton(r.get("next_hop", "0.0.0.0")))[0],
                r.get("input_if", 1),
                r.get("output_if", 2),
                r.get("packets", 10),
                r.get("bytes", 1500),
                r.get("first_ms", 90000),
                r.get("last_ms", 99000),
                r.get("src_port", 443),
                r.get("dst_port", 54321),
                0,  # pad1
                r.get("tcp_flags", 0x18),  # PSH, ACK
                r.get("protocol", 6),  # TCP
                0,  # tos
                0,  # src_as
                0,  # dst_as
                24,  # src_mask
                24,  # dst_mask
                0,  # pad2
            )
            body.extend(rec)
        return bytes(body)

    def test_parse_v5_valid_single_record(self):
        records = [
            {
                "src_ip": "192.168.1.10",
                "dst_ip": "10.10.10.50",
                "src_port": 80,
                "dst_port": 49152,
                "protocol": 6,
                "bytes": 5000,
                "packets": 4,
                "input_if": 1,
                "output_if": 2,
            }
        ]
        pkt = self._create_v5_packet(records=records)
        flows = parse_v5_packet(pkt, "192.168.1.1")
        self.assertEqual(len(flows), 1)
        f = flows[0]
        self.assertEqual(f["version"], 5)
        self.assertEqual(f["exporter_ip"], "192.168.1.1")
        self.assertEqual(f["src_ip"], "192.168.1.10")
        self.assertEqual(f["dst_ip"], "10.10.10.50")
        self.assertEqual(f["src_port"], 80)
        self.assertEqual(f["dst_port"], 49152)
        self.assertEqual(f["protocol"], 6)
        self.assertEqual(f["bytes"], 5000)
        self.assertEqual(f["packets"], 4)

    def test_parse_v5_sampling_multiplier(self):
        # Sampling rate 1:100 encoded with interval mask 0x3FFF
        raw_sampling = 0x4000 | 100  # Mode bits + interval
        records = [{"bytes": 200, "packets": 2}]
        pkt = self._create_v5_packet(records=records, sampling=raw_sampling)
        flows = parse_v5_packet(pkt, "192.168.1.1")
        self.assertEqual(len(flows), 1)
        # Scaled bytes = 200 * 100 = 20000, scaled packets = 2 * 100 = 200
        self.assertEqual(flows[0]["bytes"], 20000)
        self.assertEqual(flows[0]["packets"], 200)

    def test_parse_v5_rollover_compensation(self):
        # sys_uptime has rolled over past 2^32 ms (e.g. current uptime 5000 ms, first_ms was 4294960000 ms)
        sys_uptime = 5000
        first_ms = 4294960000
        last_ms = 4294965000
        records = [{"first_ms": first_ms, "last_ms": last_ms}]
        pkt = self._create_v5_packet(records=records, sys_uptime=sys_uptime, unix_secs=1700000000)
        flows = parse_v5_packet(pkt, "192.168.1.1")
        self.assertEqual(len(flows), 1)
        # diff = 5000 - 4294960000 + 4294967296 = 12296 ms (~12.296s ago)
        self.assertAlmostEqual(flows[0]["start_time"], 1700000000 - 12.296, places=2)

    def test_parse_v5_truncated_packet(self):
        # Less than 24 bytes header
        flows = parse_v5_packet(b"\x00\x05\x00\x01\x00\x00", "192.168.1.1")
        self.assertEqual(flows, [])

        # Version mismatch
        hdr = HEADER_STRUCT.pack(4, 1, 100, 100, 0, 1, 0, 0, 0)
        flows = parse_v5_packet(hdr, "192.168.1.1")
        self.assertEqual(flows, [])


class TestNetflowV9Parser(unittest.TestCase):
    def setUp(self):
        self.parser = V9Parser(template_ttl=60.0, orphan_ttl=5.0)

    def _build_v9_template_packet(self, source_id, template_id, fields):
        # Header (20 bytes): version=9, count=1, sys_uptime, unix_secs, seq, source_id
        hdr = struct.pack("!HHIIII", 9, 1, 100000, 1700000000, 1, source_id)
        # Flowset Header: flowset_id=0, length
        # Template payload: template_id, field_count, [(type, len)...]
        tmpl_bytes = struct.pack("!HH", template_id, len(fields))
        for ftype, flen in fields:
            tmpl_bytes += struct.pack("!HH", ftype, flen)
        flowset_len = 4 + len(tmpl_bytes)
        flowset_hdr = struct.pack("!HH", 0, flowset_len)
        return hdr + flowset_hdr + tmpl_bytes

    def _build_v9_data_packet(self, source_id, template_id, records_data):
        hdr = struct.pack("!HHIIII", 9, len(records_data), 100000, 1700000000, 2, source_id)
        data_bytes = b"".join(records_data)
        flowset_len = 4 + len(data_bytes)
        # Pad to 4-byte boundary if needed
        pad_len = (4 - (flowset_len % 4)) % 4
        flowset_len += pad_len
        flowset_hdr = struct.pack("!HH", template_id, flowset_len)
        return hdr + flowset_hdr + data_bytes + (b"\x00" * pad_len)

    def test_v9_template_and_data_parsing(self):
        source_id = 1
        template_id = 256
        fields = [
            (FT_IPV4_SRC_ADDR, 4),
            (FT_IPV4_DST_ADDR, 4),
            (FT_L4_SRC_PORT, 2),
            (FT_L4_DST_PORT, 2),
            (FT_PROTOCOL, 1),
            (FT_IN_BYTES, 4),
            (FT_IN_PKTS, 4),
        ]
        tmpl_pkt = self._build_v9_template_packet(source_id, template_id, fields)
        flows = self.parser.parse_packet(tmpl_pkt, "192.168.1.1")
        self.assertEqual(flows, [])  # Template packets return empty list

        # Verify template cached
        self.assertIn(("192.168.1.1", source_id, template_id), self.parser._templates)

        # Now send data packet
        rec_data = struct.pack(
            "!4s4sHHBII",
            socket.inet_aton("172.16.0.5"),
            socket.inet_aton("172.16.0.10"),
            12345,
            443,
            6,
            15000,
            12,
        )
        data_pkt = self._build_v9_data_packet(source_id, template_id, [rec_data])
        flows = self.parser.parse_packet(data_pkt, "192.168.1.1")
        self.assertEqual(len(flows), 1)
        f = flows[0]
        self.assertEqual(f["version"], 9)
        self.assertEqual(f["src_ip"], "172.16.0.5")
        self.assertEqual(f["dst_ip"], "172.16.0.10")
        self.assertEqual(f["src_port"], 12345)
        self.assertEqual(f["dst_port"], 443)
        self.assertEqual(f["protocol"], 6)
        self.assertEqual(f["bytes"], 15000)
        self.assertEqual(f["packets"], 12)

    def test_v9_orphan_buffer_resolution(self):
        source_id = 2
        template_id = 257
        fields = [
            (FT_IPV4_SRC_ADDR, 4),
            (FT_IPV4_DST_ADDR, 4),
            (FT_IN_BYTES, 4),
            (FT_IN_PKTS, 4),
        ]
        rec_data = struct.pack(
            "!4s4sII",
            socket.inet_aton("10.1.1.1"),
            socket.inet_aton("10.2.2.2"),
            8000,
            5,
        )
        # Send data BEFORE template
        data_pkt = self._build_v9_data_packet(source_id, template_id, [rec_data])
        flows = self.parser.parse_packet(data_pkt, "192.168.1.2")
        self.assertEqual(len(flows), 0)  # Buffered as orphan

        # Send template now -> orphan buffer should resolve!
        tmpl_pkt = self._build_v9_template_packet(source_id, template_id, fields)
        flows = self.parser.parse_packet(tmpl_pkt, "192.168.1.2")
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["src_ip"], "10.1.1.1")
        self.assertEqual(flows[0]["dst_ip"], "10.2.2.2")
        self.assertEqual(flows[0]["bytes"], 8000)

    def test_v9_ipv6_parsing(self):
        source_id = 3
        template_id = 258
        fields = [
            (FT_IPV6_SRC_ADDR, 16),
            (FT_IPV6_DST_ADDR, 16),
            (FT_IN_BYTES, 4),
            (FT_IN_PKTS, 4),
        ]
        tmpl_pkt = self._build_v9_template_packet(source_id, template_id, fields)
        self.parser.parse_packet(tmpl_pkt, "192.168.1.3")

        src_v6 = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
        dst_v6 = socket.inet_pton(socket.AF_INET6, "2001:db8::2")
        rec_data = struct.pack("!16s16sII", src_v6, dst_v6, 4096, 3)
        data_pkt = self._build_v9_data_packet(source_id, template_id, [rec_data])
        flows = self.parser.parse_packet(data_pkt, "192.168.1.3")
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["src_ip"], "2001:db8::1")
        self.assertEqual(flows[0]["dst_ip"], "2001:db8::2")


class TestIPFIXParser(unittest.TestCase):
    def setUp(self):
        self.parser = IPFIXParser(template_ttl=60.0, orphan_ttl=5.0)

    def _build_ipfix_template_packet(self, obs_domain_id, template_id, fields):
        # Header (16 bytes): version=10, length, export_time, seq, obs_domain_id
        # Set Header: set_id=2 (Template Set), set_length
        # Template payload: template_id, field_count, [(ie_id, len)...]
        tmpl_bytes = struct.pack("!HH", template_id, len(fields))
        for item in fields:
            if len(item) == 2:
                ie_id, flen = item
                tmpl_bytes += struct.pack("!HH", ie_id, flen)
            else:
                ie_id, flen, ent_num = item
                tmpl_bytes += struct.pack("!HHI", ie_id | 0x8000, flen, ent_num)

        set_len = 4 + len(tmpl_bytes)
        hdr_len = 16 + set_len
        hdr = struct.pack("!HHIII", 10, hdr_len, 1700000000, 1, obs_domain_id)
        set_hdr = struct.pack("!HH", 2, set_len)
        return hdr + set_hdr + tmpl_bytes

    def _build_ipfix_data_packet(self, obs_domain_id, template_id, records_data):
        data_bytes = b"".join(records_data)
        set_len = 4 + len(data_bytes)
        hdr_len = 16 + set_len
        hdr = struct.pack("!HHIII", 10, hdr_len, 1700000000, 2, obs_domain_id)
        set_hdr = struct.pack("!HH", template_id, set_len)
        return hdr + set_hdr + data_bytes

    def test_ipfix_template_and_data_parsing(self):
        obs_domain_id = 100
        template_id = 260
        fields = [
            (IE_SOURCE_IPV4_ADDRESS, 4),
            (IE_DESTINATION_IPV4_ADDRESS, 4),
            (IE_SOURCE_TRANSPORT_PORT, 2),
            (IE_DESTINATION_TRANSPORT_PORT, 2),
            (IE_PROTOCOL_IDENTIFIER, 1),
            (IE_OCTET_DELTA_COUNT, 8),  # 64-bit counter
            (IE_PACKET_DELTA_COUNT, 8),  # 64-bit counter
        ]
        tmpl_pkt = self._build_ipfix_template_packet(obs_domain_id, template_id, fields)
        flows = self.parser.parse_packet(tmpl_pkt, "192.168.10.1")
        self.assertEqual(flows, [])

        # Send data
        rec_data = struct.pack(
            "!4s4sHHBQQ",
            socket.inet_aton("10.20.30.40"),
            socket.inet_aton("10.20.30.50"),
            53,
            59123,
            17,  # UDP
            1048576,  # 1MB
            1024,
        )
        data_pkt = self._build_ipfix_data_packet(obs_domain_id, template_id, [rec_data])
        flows = self.parser.parse_packet(data_pkt, "192.168.10.1")
        self.assertEqual(len(flows), 1)
        f = flows[0]
        self.assertEqual(f["version"], 10)
        self.assertEqual(f["src_ip"], "10.20.30.40")
        self.assertEqual(f["dst_ip"], "10.20.30.50")
        self.assertEqual(f["src_port"], 53)
        self.assertEqual(f["dst_port"], 59123)
        self.assertEqual(f["protocol"], 17)
        self.assertEqual(f["bytes"], 1048576)
        self.assertEqual(f["packets"], 1024)

    def test_ipfix_enterprise_bit_handling(self):
        # Enterprise-specific element (bit 0x8000 set)
        obs_domain_id = 200
        template_id = 261
        fields = [
            (IE_SOURCE_IPV4_ADDRESS, 4),
            (IE_DESTINATION_IPV4_ADDRESS, 4),
            (IE_OCTET_DELTA_COUNT, 4),
            (999, 4, 12345),  # Custom vendor field with Enterprise #12345
        ]
        tmpl_pkt = self._build_ipfix_template_packet(obs_domain_id, template_id, fields)
        self.parser.parse_packet(tmpl_pkt, "192.168.10.2")

        rec_data = struct.pack(
            "!4s4sII",
            socket.inet_aton("1.2.3.4"),
            socket.inet_aton("5.6.7.8"),
            500,
            0xDEADBEEF,  # Custom vendor value
        )
        data_pkt = self._build_ipfix_data_packet(obs_domain_id, template_id, [rec_data])
        flows = self.parser.parse_packet(data_pkt, "192.168.10.2")
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["src_ip"], "1.2.3.4")
        self.assertEqual(flows[0]["bytes"], 500)


if __name__ == "__main__":
    unittest.main()
