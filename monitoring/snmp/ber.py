"""BER encoding for SNMP (X.690, the subset RFC 3416 uses), hand-written for the zero-dependency rule.

Only what SNMP needs: single-byte tags, definite lengths, INTEGER, OCTET STRING, NULL, OBJECT
IDENTIFIER, SEQUENCE, the SMI application types (IpAddress, Counter32, Gauge32, TimeTicks,
Opaque, Counter64), the three exception values and the PDU tags.

Decoding is defensive because it reads packets from the network: every length is checked
against the buffer, indefinite lengths are refused, and anything malformed raises BerError
rather than IndexError, so one broken device reply cannot take the poller down.

OIDs are tuples of ints inside the code (they compare and sort correctly as tuples, which a
walk relies on); `parse_oid` / `format_oid` convert to and from the dotted text people read.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence, Tuple

INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OBJECT_IDENTIFIER = 0x06
SEQUENCE = 0x30

IP_ADDRESS = 0x40
COUNTER32 = 0x41
GAUGE32 = 0x42
TIMETICKS = 0x43
OPAQUE = 0x44
COUNTER64 = 0x46

NO_SUCH_OBJECT = 0x80
NO_SUCH_INSTANCE = 0x81
END_OF_MIB_VIEW = 0x82
EXCEPTIONS = (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW)

GET = 0xA0
GET_NEXT = 0xA1
RESPONSE = 0xA2
SET = 0xA3
GET_BULK = 0xA5
INFORM = 0xA6
TRAP_V2 = 0xA7
REPORT = 0xA8
PDU_TAGS = (GET, GET_NEXT, RESPONSE, SET, GET_BULK, INFORM, TRAP_V2, REPORT)

_UNSIGNED = (COUNTER32, GAUGE32, TIMETICKS, COUNTER64)

TYPE_NAMES = {
    INTEGER: "INTEGER", OCTET_STRING: "OCTET STRING", NULL: "NULL", OBJECT_IDENTIFIER: "OID",
    IP_ADDRESS: "IpAddress", COUNTER32: "Counter32", GAUGE32: "Gauge32", TIMETICKS: "TimeTicks",
    OPAQUE: "Opaque", COUNTER64: "Counter64", NO_SUCH_OBJECT: "noSuchObject",
    NO_SUCH_INSTANCE: "noSuchInstance", END_OF_MIB_VIEW: "endOfMibView",
}

Oid = Tuple[int, ...]


class BerError(ValueError):
    """A packet that is not valid BER, or not the SNMP shape we expect."""


# --- OIDs as text ------------------------------------------------------------------------------

def parse_oid(text: str) -> Oid:
    parts = text.strip().lstrip(".").split(".")
    try:
        oid = tuple(int(p) for p in parts)
    except ValueError:
        raise BerError(f"not an OID: {text!r}") from None
    if len(oid) < 2 or any(p < 0 for p in oid):
        raise BerError(f"not an OID: {text!r}")
    return oid


def format_oid(oid: Sequence[int]) -> str:
    return ".".join(str(p) for p in oid)


# --- encoding ----------------------------------------------------------------------------------

def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _length(len(payload)) + payload


def enc_int(value: int, tag: int = INTEGER) -> bytes:
    """Signed two's complement, shortest form (INTEGER: request ids, error status, versions)."""
    length = max(1, (value + (value < 0)).bit_length() // 8 + 1)
    return tlv(tag, value.to_bytes(length, "big", signed=True))


def enc_unsigned(value: int, tag: int) -> bytes:
    """Counter32 / Gauge32 / TimeTicks / Counter64: unsigned, with a leading zero byte when the
    top bit is set, so a receiver reading it as signed BER still gets a positive number."""
    if value < 0:
        raise BerError("unsigned SNMP types cannot be negative")
    length = value.bit_length() // 8 + 1
    return tlv(tag, value.to_bytes(length, "big"))


def enc_octets(value: bytes, tag: int = OCTET_STRING) -> bytes:
    return tlv(tag, bytes(value))


def enc_null(tag: int = NULL) -> bytes:
    return bytes([tag, 0])


def enc_oid(oid: Sequence[int]) -> bytes:
    if len(oid) < 2 or oid[0] > 2 or (oid[0] < 2 and oid[1] >= 40):
        raise BerError(f"cannot encode OID {format_oid(oid)}")
    out = bytearray()
    for arc in [oid[0] * 40 + oid[1], *oid[2:]]:
        if arc < 0:
            raise BerError(f"cannot encode OID {format_oid(oid)}")
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out.extend(reversed(chunk))
    return tlv(OBJECT_IDENTIFIER, bytes(out))


def enc_seq(*parts: bytes, tag: int = SEQUENCE) -> bytes:
    return tlv(tag, b"".join(parts))


def enc_value(tag: int, value: Any) -> bytes:
    if tag == INTEGER:
        return enc_int(int(value))
    if tag in _UNSIGNED:
        return enc_unsigned(int(value), tag)
    if tag in (OCTET_STRING, OPAQUE):
        return enc_octets(value, tag)
    if tag == IP_ADDRESS:
        raw = bytes(int(p) for p in str(value).split(".")) if isinstance(value, str) else bytes(value)
        if len(raw) != 4:
            raise BerError("IpAddress needs four bytes")
        return enc_octets(raw, IP_ADDRESS)
    if tag == OBJECT_IDENTIFIER:
        return enc_oid(value)
    if tag in (NULL, *EXCEPTIONS):
        return enc_null(tag)
    raise BerError(f"cannot encode tag 0x{tag:02x}")


# --- decoding ----------------------------------------------------------------------------------

def read_tlv(data: bytes, pos: int, end: int | None = None) -> Tuple[int, int, int]:
    """(tag, value_start, value_end) of the element at `pos`; the next element starts at
    value_end. Positions are kept (not sliced copies) because SNMPv3 has to find the
    authentication field inside the received bytes to check the digest."""
    end = len(data) if end is None else end
    if pos + 2 > end:
        raise BerError("truncated element")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise BerError("multi-byte tags are not used by SNMP")
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0:
            raise BerError("indefinite length is not allowed")
        if count > 4 or pos + count > end:
            raise BerError("bad length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > end:
        raise BerError("element runs past the end of the packet")
    return tag, pos, pos + length


def children(data: bytes, start: int, end: int) -> List[Tuple[int, int, int]]:
    out = []
    pos = start
    while pos < end:
        tag, s, e = read_tlv(data, pos, end)
        out.append((tag, s, e))
        pos = e
    return out


def dec_int(raw: bytes) -> int:
    if not raw:
        raise BerError("empty INTEGER")
    return int.from_bytes(raw, "big", signed=True)


def dec_unsigned(raw: bytes) -> int:
    # Read as unsigned on purpose: some agents send a Counter32 with the top bit set and no
    # leading zero, which signed decoding would turn negative.
    if not raw:
        raise BerError("empty counter")
    return int.from_bytes(raw, "big")


def dec_oid(raw: bytes) -> Oid:
    if not raw:
        raise BerError("empty OID")
    arcs: List[int] = []
    value = 0
    for i, byte in enumerate(raw):
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
        elif i == len(raw) - 1:
            raise BerError("OID ends mid sub-identifier")
    first = arcs[0]
    head = (0, first) if first < 40 else (1, first - 40) if first < 80 else (2, first - 80)
    return head + tuple(arcs[1:])


def decode_value(tag: int, raw: bytes) -> Any:
    if tag == INTEGER:
        return dec_int(raw)
    if tag in _UNSIGNED:
        return dec_unsigned(raw)
    if tag in (OCTET_STRING, OPAQUE):
        return bytes(raw)
    if tag == IP_ADDRESS:
        return ".".join(str(b) for b in raw) if len(raw) == 4 else bytes(raw)
    if tag == OBJECT_IDENTIFIER:
        return dec_oid(raw)
    if tag in (NULL, *EXCEPTIONS):
        return None
    # Unknown application types are kept as bytes rather than refused: one odd column must
    # not lose the rest of a table.
    return bytes(raw)


# --- PDUs --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Varbind:
    oid: Oid
    tag: int
    value: Any

    @property
    def is_exception(self) -> bool:
        return self.tag in EXCEPTIONS


@dataclass
class Pdu:
    tag: int
    request_id: int
    error_status: int  # non-repeaters for GETBULK
    error_index: int  # max-repetitions for GETBULK
    varbinds: List[Varbind]


def encode_pdu(tag: int, request_id: int, a: int, b: int, varbinds: Iterable[Tuple[Oid, int, Any]]) -> bytes:
    vbs = b"".join(enc_seq(enc_oid(oid), enc_value(vtag, value)) for oid, vtag, value in varbinds)
    return enc_seq(enc_int(request_id), enc_int(a), enc_int(b), enc_seq(vbs), tag=tag)


def decode_pdu(data: bytes, start: int, end: int) -> Pdu:
    tag, s, e = read_tlv(data, start, end)
    if tag not in PDU_TAGS:
        raise BerError(f"not a PDU (tag 0x{tag:02x})")
    parts = children(data, s, e)
    if len(parts) != 4 or [p[0] for p in parts[:3]] != [INTEGER] * 3 or parts[3][0] != SEQUENCE:
        raise BerError("malformed PDU")
    rid, a, b = (dec_int(data[ps:pe]) for _, ps, pe in parts[:3])
    varbinds = []
    for vtag, vs, ve in children(data, parts[3][1], parts[3][2]):
        if vtag != SEQUENCE:
            raise BerError("malformed varbind")
        pair = children(data, vs, ve)
        if len(pair) != 2 or pair[0][0] != OBJECT_IDENTIFIER:
            raise BerError("malformed varbind")
        (_, os_, oe), (t, xs, xe) = pair
        varbinds.append(Varbind(dec_oid(data[os_:oe]), t, decode_value(t, data[xs:xe])))
    return Pdu(tag, rid, a, b, varbinds)
