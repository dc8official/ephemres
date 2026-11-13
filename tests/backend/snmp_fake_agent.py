"""A small SNMP agent for the client tests, on 127.0.0.1 over real UDP.

It parses requests and builds replies with its own code (only the BER primitives and the USM
key/cipher functions are shared with the client, and those have RFC vectors of their own), so
a mistake in the client's message layout is not mirrored here and passed off as correct.
Real-device interoperability is the GNS3 survey, not this.

Knobs for the awkward cases: drop the first N requests, answer tooBig above a GETBULK size,
start the engine clock far from the client's guess, refuse a user, and silently drop requests
that touch one subtree (RouterOS 7.8 does that with lldpRemManAddrTable in a multi-column
GETBULK; `drop_alone` drops it even when asked for on its own), ignore GETBULK altogether
(`no_bulk`), and serve only one SNMPv3 context name (`context`).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple

from monitoring.snmp import ber, usm
from monitoring.snmp.ber import Oid

OID_ENGINE_UNKNOWN = (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0)
OID_NOT_IN_WINDOW = (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0)
OID_UNKNOWN_USER = (1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0)
OID_WRONG_DIGEST = (1, 3, 6, 1, 6, 3, 15, 1, 1, 5, 0)
OID_UNKNOWN_CONTEXT = (1, 3, 6, 1, 6, 3, 12, 1, 5, 0)


class FakeAgent(asyncio.DatagramProtocol):
    def __init__(self, mib: Dict[Oid, Tuple[int, Any]], community: bytes = b"public",
                 user: Optional[bytes] = None, auth: Optional[str] = None, auth_password: Optional[bytes] = None,
                 priv: Optional[str] = None, priv_password: Optional[bytes] = None,
                 engine_id: bytes = bytes.fromhex("80001f8880aabbccddeeff0011"), boots: int = 7, engine_time: int = 5000,
                 drop_first: int = 0, too_big_above: Optional[int] = None,
                 drop_with: Optional[Oid] = None, drop_alone: bool = False, no_bulk: bool = False,
                 context: bytes = b""):
        self.mib = dict(mib)
        self.order: List[Oid] = sorted(self.mib)
        self.community = community
        self.user, self.auth, self.priv = user, auth, priv
        self.engine_id, self.boots, self.engine_time = engine_id, boots, engine_time
        self.keys = usm.derive_keys(engine_id, auth, auth_password, priv, priv_password) if auth else None
        self.drop_first = drop_first
        self.too_big_above = too_big_above
        self.drop_with, self.drop_alone = drop_with, drop_alone
        self.no_bulk, self.context = no_bulk, context
        self.received = 0
        self.requests: List[Tuple[int, int]] = []  # (pdu tag, max-repetitions) per answered request
        self.salt = usm.SaltCounter()
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: Any) -> None:
        self.transport = transport

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    # --- MIB view --------------------------------------------------------------------------

    def _next(self, oid: Oid) -> Tuple[Oid, int, Any]:
        for candidate in self.order:
            if candidate > oid:
                return (candidate, *self.mib[candidate])
        return oid, ber.END_OF_MIB_VIEW, None

    def _answer(self, tag: int, a: int, b: int, oids: List[Oid]) -> Tuple[int, List[Tuple[Oid, int, Any]]]:
        self.requests.append((tag, b if tag == ber.GET_BULK else 0))
        if tag == ber.GET:
            return 0, [(o, *self.mib[o]) if o in self.mib else (o, ber.NO_SUCH_OBJECT, None) for o in oids]
        if tag == ber.GET_NEXT:
            return 0, [self._next(o) for o in oids]
        if tag == ber.GET_BULK:
            if self.too_big_above is not None and b > self.too_big_above:
                return 1, []
            out = [self._next(o) for o in oids[:a]]
            cursor = list(oids[a:])
            for _ in range(b):
                row = [self._next(o) for o in cursor]
                out.extend(row)
                cursor = [r[0] for r in row]
                if all(r[1] == ber.END_OF_MIB_VIEW for r in row):
                    break
            return 0, out
        return 5, []

    # --- wire ------------------------------------------------------------------------------

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.received += 1
        if self.received <= self.drop_first:
            return
        _, s, e = ber.read_tlv(data, 0)
        parts = ber.children(data, s, e)
        version = ber.dec_int(data[parts[0][1]:parts[0][2]])
        reply = self._v2c(data, parts) if version == 1 else self._v3(data, parts)
        if reply is not None:
            self.transport.sendto(reply, addr)

    def _v2c(self, data: bytes, parts: list) -> Optional[bytes]:
        if data[parts[1][1]:parts[1][2]] != self.community:
            return None  # a wrong community gets silence, as on real devices
        pdu = ber.decode_pdu(data, parts[1][2], parts[2][2])
        if self._dropped(pdu):
            return None
        status, vbs = self._answer(pdu.tag, pdu.error_status, pdu.error_index, [v.oid for v in pdu.varbinds])
        if status:
            vbs = [(v.oid, ber.NULL, None) for v in pdu.varbinds]
        return ber.enc_seq(ber.enc_int(1), ber.enc_octets(self.community),
                           ber.encode_pdu(ber.RESPONSE, pdu.request_id, status, 1 if status else 0, vbs))

    def _dropped(self, pdu: ber.Pdu) -> bool:
        if self.no_bulk and pdu.tag == ber.GET_BULK:
            return True
        if self.drop_with is None:
            return False
        n = len(self.drop_with)
        touches = any(v.oid[:n] == self.drop_with for v in pdu.varbinds)
        return touches and (len(pdu.varbinds) > 1 or self.drop_alone)

    def _v3(self, data: bytes, parts: list) -> Optional[bytes]:
        g = ber.children(data, parts[1][1], parts[1][2])
        msg_id = ber.dec_int(data[g[0][1]:g[0][2]])
        flags = data[g[2][1]]
        _, ss, se = ber.read_tlv(data, parts[2][1])
        sp = ber.children(data, ss, se)
        f = [data[x[1]:x[2]] for x in sp]
        engine_id, user, priv_params = f[0], f[3], f[5]
        boots, mtime = ber.dec_int(f[1]), ber.dec_int(f[2])
        if not engine_id:
            return self._report(msg_id, OID_ENGINE_UNKNOWN, request_id=0)
        if user != self.user:
            return self._report(msg_id, OID_UNKNOWN_USER, request_id=0)
        if flags & 1:
            a_s, a_e = sp[4][1], sp[4][2]
            zeroed = data[:a_s] + b"\x00" * (a_e - a_s) + data[a_e:]
            if not usm.verify(self.keys, zeroed, data[a_s:a_e]):
                return self._report(msg_id, OID_WRONG_DIGEST, request_id=0)
            if boots != self.boots or abs(mtime - self.engine_time) > 150:
                return self._report(msg_id, OID_NOT_IN_WINDOW, request_id=0, authenticated=True)
        stag, s_s, s_e = parts[3]
        if flags & 2:
            # A wrong privacy password decrypts to garbage; stay silent, as the Cisco switch in
            # the GNS3 survey did, rather than send a decryptionError report.
            try:
                plain = usm.decrypt(self.keys, data[s_s:s_e], priv_params, boots, mtime)
                _, ps, pe = ber.read_tlv(plain, 0)
                buf, sc = plain, ber.children(plain, ps, pe)
                ber.decode_pdu(buf, sc[1][2], sc[2][2])
            except (ber.BerError, usm.UsmError, IndexError):
                return None
        else:
            buf, sc = data, ber.children(data, s_s, s_e)
        pdu = ber.decode_pdu(buf, sc[1][2], sc[2][2])
        if buf[sc[1][1]:sc[1][2]] != self.context:
            return self._report(msg_id, OID_UNKNOWN_CONTEXT, request_id=pdu.request_id)
        if self._dropped(pdu):
            return None
        status, vbs = self._answer(pdu.tag, pdu.error_status, pdu.error_index, [v.oid for v in pdu.varbinds])
        if status:
            vbs = [(v.oid, ber.NULL, None) for v in pdu.varbinds]
        body = ber.encode_pdu(ber.RESPONSE, pdu.request_id, status, 1 if status else 0, vbs)
        return self._message(msg_id, flags & 3, body)

    def _report(self, msg_id: int, oid: Oid, request_id: int, authenticated: bool = False) -> bytes:
        body = ber.encode_pdu(ber.REPORT, request_id, 0, 0, [(oid, ber.COUNTER32, 1)])
        return self._message(msg_id, 1 if authenticated else 0, body)

    def _message(self, msg_id: int, flags: int, pdu: bytes) -> bytes:
        scoped = ber.enc_seq(ber.enc_octets(self.engine_id), ber.enc_octets(b""), pdu)
        priv_params = b""
        if flags & 2:
            enc, priv_params = usm.encrypt(self.keys, scoped, self.boots, self.engine_time, self.salt)
            scoped = ber.enc_octets(enc)
        digest_len = self.keys.digest_len if flags & 1 else 0
        # Build with a marker in the digest field, then find it: a different method from the
        # client's offset arithmetic on purpose.
        marker = os.urandom(digest_len) if digest_len else b""
        sec = ber.enc_seq(ber.enc_octets(self.engine_id), ber.enc_int(self.boots), ber.enc_int(self.engine_time),
                          ber.enc_octets(self.user or b""), ber.enc_octets(marker), ber.enc_octets(priv_params))
        msg = ber.enc_seq(ber.enc_int(3),
                          ber.enc_seq(ber.enc_int(msg_id), ber.enc_int(1500), ber.enc_octets(bytes([flags])), ber.enc_int(3)),
                          ber.enc_octets(sec), scoped)
        if not digest_len:
            return msg
        at = msg.index(marker)
        zeroed = msg[:at] + b"\x00" * digest_len + msg[at + digest_len:]
        return zeroed[:at] + usm.sign(self.keys, zeroed) + zeroed[at + digest_len:]


async def start_agent(**kwargs: Any) -> FakeAgent:
    loop = asyncio.get_running_loop()
    _, agent = await loop.create_datagram_endpoint(lambda: FakeAgent(**kwargs), local_addr=("127.0.0.1", 0))
    return agent
