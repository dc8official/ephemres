"""An asyncio SNMP v2c / v3 client: GET, GETNEXT, GETBULK and table walks.

One client talks to one device over its own connected UDP socket and sends one request at a
time; a poller polls many devices by running many clients concurrently, so one slow device
only ever delays itself.

Replies are matched on the request id (v2c) or message id (v3); anything else that arrives is
counted and dropped, including late replies to an earlier retry. Each retry is a new message
with a new id (RFC 3412 requires a fresh msgID; for v2c it keeps the two cases alike) and both
ids are accepted, so a slow first reply is still used.

SNMPv3: the first request discovers the device's engine ID, boots and time (RFC 3414 section 4),
the keys are localised to that engine once, and the engine clock is followed from then on.
A "not in time window" report (the device rebooted, or our estimate drifted) resyncs the clock
and the request is sent once more. Other reports become SnmpSecurityError with a plain reason
("unknown user name", "wrong digest ...") because that is what someone setting up a device
needs to see.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from monitoring.snmp import ber, usm
from monitoring.snmp.ber import Oid, Pdu, Varbind

ERROR_STATUS = {
    0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr",
    6: "noAccess", 7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue",
    11: "noCreation", 12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
    15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName",
}
TOO_BIG = 1

# The largest UDP payload; the device answers within the smaller of this and its own limit.
MAX_MESSAGE_SIZE = 65507
FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 0x01, 0x02, 0x04


@dataclass(frozen=True)
class CommunityAuth:
    """SNMP v2c."""
    community: bytes = field(repr=False)


@dataclass(frozen=True)
class UserAuth:
    """SNMP v3. `auth` is one of usm.AUTH_PROTOCOLS or None, `priv` one of usm.PRIV_PROTOCOLS
    or None. `priv_password=None` with a `priv` means "same as the authentication password"
    (Meraki and UniFi use one password for both). `context` is the SNMPv3 context name, empty
    for the default context; a few devices keep per-VRF data in named contexts."""
    user: bytes
    auth: Optional[str] = None
    auth_password: Optional[bytes] = field(default=None, repr=False)
    priv: Optional[str] = None
    priv_password: Optional[bytes] = field(default=None, repr=False)
    context: bytes = b""

    @property
    def level(self) -> str:
        return "authPriv" if self.priv else "authNoPriv" if self.auth else "noAuthNoPriv"


Credentials = Union[CommunityAuth, UserAuth]


class SnmpError(Exception):
    """Base for everything the client raises about a device."""


class SnmpTimeout(SnmpError):
    pass


class SnmpSecurityError(SnmpError):
    """The device answered with a USM report: our v3 settings do not match its own."""


class SnmpErrorStatus(SnmpError):
    def __init__(self, status: int, index: int):
        self.status, self.index = status, index
        super().__init__(f"device answered {ERROR_STATUS.get(status, status)} (varbind {index})")


@dataclass
class ClientStats:
    requests: int = 0
    sends: int = 0
    timeouts: int = 0
    dropped: int = 0  # replies that were late, for another request, or malformed
    auth_failures: int = 0  # v3 replies whose digest did not check out
    bytes_out: int = 0
    bytes_in: int = 0
    replies: int = 0
    reply_seconds: float = 0.0  # summed time from send to the accepted reply

    @property
    def average_reply_ms(self) -> Optional[float]:
        return round(self.reply_seconds / self.replies * 1000, 1) if self.replies else None


@dataclass
class _Report:
    pdu: Pdu
    authenticated: bool


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        # On a connected UDP socket an ICMP port-unreachable arrives here: nothing listens.
        self.queue.put_nowait(exc)


def in_subtree(oid: Oid, base: Oid) -> bool:
    return len(oid) > len(base) and oid[: len(base)] == base


class SnmpClient:
    def __init__(self, host: str, credentials: Credentials, port: int = 161, timeout: float = 2.0,
                 retries: int = 1, max_repetitions: int = 20, use_bulk: bool = True,
                 walk_alone: Iterable[Oid] = ()) -> None:
        self.host, self.port = host, port
        self.credentials = credentials
        self.timeout, self.retries = timeout, retries
        self.max_repetitions = max_repetitions
        # What a device profile (profile.py) knows about this device; learned again as it goes.
        # use_bulk turns False when GETBULK goes unanswered but GETNEXT works; walk_alone holds
        # the columns that were in a multi-column request the device ignored.
        self.use_bulk = use_bulk
        self.walk_alone: Set[Oid] = set(walk_alone)
        self.stats = ClientStats()
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._proto: Optional[_Protocol] = None
        self._lock = asyncio.Lock()
        self._next = int.from_bytes(os.urandom(4), "big") & 0x3FFFFFFF
        # v3 engine state
        self.engine_id: Optional[bytes] = None
        self.engine_boots = 0
        self._engine_time = 0
        self._engine_time_at = 0.0
        self._keys: Optional[usm.LocalizedKeys] = None
        self._salt = usm.SaltCounter()

    # --- lifecycle -----------------------------------------------------------------------------

    async def open(self) -> "SnmpClient":
        loop = asyncio.get_running_loop()
        self._transport, self._proto = await loop.create_datagram_endpoint(
            _Protocol, remote_addr=(self.host, self.port))
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def __aenter__(self) -> "SnmpClient":
        return await self.open()

    async def __aexit__(self, *exc: Any) -> None:
        self.close()

    # --- operations ----------------------------------------------------------------------------

    async def get(self, oids: Sequence[Oid]) -> List[Varbind]:
        return (await self._request(ber.GET, 0, 0, oids)).varbinds

    async def get_next(self, oids: Sequence[Oid]) -> List[Varbind]:
        return (await self._request(ber.GET_NEXT, 0, 0, oids)).varbinds

    async def get_bulk(self, oids: Sequence[Oid], max_repetitions: int, non_repeaters: int = 0) -> List[Varbind]:
        return (await self._request(ber.GET_BULK, non_repeaters, max_repetitions, oids)).varbinds

    async def walk(self, base: Oid, use_bulk: Optional[bool] = None) -> List[Varbind]:
        return (await self.walk_columns([base], use_bulk))[base]

    async def walk_columns(self, columns: Sequence[Oid], use_bulk: Optional[bool] = None,
                           errors: Optional[Dict[Oid, str]] = None) -> Dict[Oid, List[Varbind]]:
        """Walks several table columns side by side, one GETBULK for all of them at a time.

        A table of 50 interfaces and 12 columns takes about 50 * 12 / (max-repetitions * 12)
        requests this way instead of one walk per column. On "tooBig" the repetitions are
        halved and kept lower for the rest of this client's life. A column stops when it leaves
        its subtree, reaches endOfMibView, or stops increasing (a broken agent looping).

        Some agents silently drop a request that includes one column they cannot serve
        (RouterOS 7.8 drops any GETBULK that includes lldpRemManAddrTable, found in the GNS3
        survey). So when a request for several columns gets no reply, the remaining columns are
        walked one at a time from where each had got to, and only the bad column is lost. A
        single column that gets no reply raises SnmpTimeout, or, when `errors` is given, is
        recorded there and the walk carries on with what it has.

        What is learned is kept on the client (and from there in the device profile): columns
        that had to be walked alone go to `walk_alone` and are asked for on their own from the
        start next time, so the timeout is paid once; and when a single-column GETBULK goes
        unanswered but GETNEXT works, `use_bulk` turns off for this device."""
        bulk = self.use_bulk if use_bulk is None else use_bulk
        current: Dict[Oid, Oid] = {c: c for c in columns}
        results: Dict[Oid, List[Varbind]] = {c: [] for c in columns}
        together = [c for c in columns if c not in self.walk_alone]
        if together:
            await self._walk(together, current, results, bulk, errors)
        for col in columns:
            if col in self.walk_alone:
                await self._walk([col], current, results, bulk, errors)
        return results

    async def _walk(self, active: List[Oid], current: Dict[Oid, Oid], results: Dict[Oid, List[Varbind]],
                    use_bulk: bool, errors: Optional[Dict[Oid, str]]) -> None:
        while active:
            asked = [current[c] for c in active]
            try:
                if use_bulk:
                    vbs = await self.get_bulk(asked, self.max_repetitions)
                else:
                    vbs = await self.get_next(asked)
            except SnmpErrorStatus as exc:
                if exc.status == TOO_BIG and use_bulk and self.max_repetitions > 1:
                    self.max_repetitions = max(1, self.max_repetitions // 2)
                    continue
                raise
            except SnmpTimeout as exc:
                if len(active) > 1:
                    self.walk_alone.update(active)
                    for col in active:
                        await self._walk([col], current, results, use_bulk, errors)
                    return
                if use_bulk:
                    # One column, still no answer: try GETNEXT before giving the column up, for
                    # agents whose GETBULK is broken or missing.
                    try:
                        vbs = await self.get_next(asked)
                    except SnmpTimeout:
                        pass
                    else:
                        self.use_bulk = use_bulk = False
                        if self._take(vbs, active, current, results):
                            continue
                        return
                if errors is None:
                    raise
                errors[active[0]] = str(exc)
                return
            if not self._take(vbs, active, current, results):
                break

    @staticmethod
    def _take(vbs: List[Varbind], active: List[Oid], current: Dict[Oid, Oid],
              results: Dict[Oid, List[Varbind]]) -> bool:
        """Files one reply's varbinds under their columns and drops finished columns from
        `active` (in place). False when nothing moved forward, so the walk stops."""
        done: Set[Oid] = set()
        progressed = False
        for i, vb in enumerate(vbs):
            col = active[i % len(active)]
            if col in done:
                continue
            if vb.tag == ber.END_OF_MIB_VIEW or not in_subtree(vb.oid, col) or vb.oid <= current[col]:
                done.add(col)
                continue
            if vb.tag in ber.EXCEPTIONS:
                continue
            results[col].append(vb)
            current[col] = vb.oid
            progressed = True
        active[:] = [c for c in active if c not in done]
        return progressed

    # --- request / reply -----------------------------------------------------------------------

    def _new_id(self) -> int:
        self._next = (self._next + 1) & 0x7FFFFFFF or 1
        return self._next

    async def _request(self, tag: int, a: int, b: int, oids: Sequence[Oid]) -> Pdu:
        if self._transport is None:
            raise SnmpError("client is not open")
        varbinds = [(oid, ber.NULL, None) for oid in oids]
        async with self._lock:
            self.stats.requests += 1
            if isinstance(self.credentials, CommunityAuth):
                pdu = await self._exchange(
                    lambda mid: self._v2c_message(tag, mid, a, b, varbinds), self._v2c_parse)
            else:
                pdu = await self._v3_request(tag, a, b, varbinds)
        if pdu.error_status:
            raise SnmpErrorStatus(pdu.error_status, pdu.error_index)
        return pdu

    async def _exchange(self, build: Callable[[int], bytes], parse: Callable[[bytes], Optional[Tuple[int, Any]]]) -> Any:
        loop = asyncio.get_running_loop()
        ids: Set[int] = set()
        unreachable = False
        for _ in range(self.retries + 1):
            mid = self._new_id()
            ids.add(mid)
            payload = build(mid)
            self._transport.sendto(payload)
            self.stats.sends += 1
            self.stats.bytes_out += len(payload)
            sent_at = loop.time()
            deadline = sent_at + self.timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    item = await asyncio.wait_for(self._proto.queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if isinstance(item, Exception):
                    unreachable = isinstance(item, ConnectionRefusedError)
                    continue
                self.stats.bytes_in += len(item)
                try:
                    parsed = parse(item)
                except (ber.BerError, usm.UsmError):
                    parsed = None
                if parsed is None or parsed[0] not in ids:
                    self.stats.dropped += 1
                    continue
                self.stats.replies += 1
                self.stats.reply_seconds += loop.time() - sent_at
                return parsed[1]
            self.stats.timeouts += 1
        hint = ""
        if unreachable:
            hint = " (ICMP port unreachable: no SNMP agent on that port)"
        elif self.stats.auth_failures:
            hint = f" ({self.stats.auth_failures} replies failed the v3 authentication check)"
        raise SnmpTimeout(f"no reply from {self.host}:{self.port} after {self.retries + 1} tries of {self.timeout:g}s{hint}")

    # --- v2c -----------------------------------------------------------------------------------

    def _v2c_message(self, tag: int, mid: int, a: int, b: int, varbinds: list) -> bytes:
        return ber.enc_seq(ber.enc_int(1), ber.enc_octets(self.credentials.community),
                           ber.encode_pdu(tag, mid, a, b, varbinds))

    def _v2c_parse(self, data: bytes) -> Optional[Tuple[int, Pdu]]:
        tag, s, e = ber.read_tlv(data, 0)
        if tag != ber.SEQUENCE:
            return None
        parts = ber.children(data, s, e)
        if len(parts) != 3 or ber.dec_int(data[parts[0][1]:parts[0][2]]) != 1:
            return None
        if data[parts[1][1]:parts[1][2]] != self.credentials.community:
            return None
        # children() gives value spans; an element itself starts where its previous sibling ends.
        pdu = ber.decode_pdu(data, parts[1][2], parts[2][2])
        if pdu.tag != ber.RESPONSE:
            return None
        return pdu.request_id, pdu

    # --- v3 ------------------------------------------------------------------------------------

    def _current_engine_time(self) -> int:
        return min(self._engine_time + int(time.monotonic() - self._engine_time_at), 0x7FFFFFFF)

    def _set_engine_clock(self, boots: int, engine_time: int) -> None:
        self.engine_boots, self._engine_time, self._engine_time_at = boots, engine_time, time.monotonic()

    async def discover(self) -> None:
        """RFC 3414 section 4: an empty, unauthenticated, reportable request; the device's
        Report carries its engine ID, boots and time in the header."""
        creds: UserAuth = self.credentials
        result = await self._exchange(
            lambda mid: self._v3_message(mid, ber.encode_pdu(ber.GET, mid, 0, 0, []), FLAG_REPORTABLE,
                                         b"", 0, 0, b"", None),
            self._v3_parse_discovery)
        engine_id, boots, engine_time = result
        if not engine_id:
            raise SnmpError("device did not report its SNMPv3 engine ID")
        self.engine_id = engine_id
        self._set_engine_clock(boots, engine_time)
        if creds.auth:
            try:
                self._keys = usm.derive_keys(engine_id, creds.auth, creds.auth_password, creds.priv, creds.priv_password)
            except usm.UsmError as exc:
                raise SnmpSecurityError(str(exc)) from exc
        else:
            self._keys = None

    async def _v3_request(self, tag: int, a: int, b: int, varbinds: list) -> Pdu:
        if self.engine_id is None:
            await self.discover()
        creds: UserAuth = self.credentials
        flags = FLAG_REPORTABLE | (FLAG_AUTH if creds.auth else 0) | (FLAG_PRIV if creds.priv else 0)
        for attempt in range(2):
            try:
                result = await self._exchange(
                    lambda mid: self._v3_message(mid, ber.encode_pdu(tag, mid, a, b, varbinds), flags, self.engine_id,
                                                 self.engine_boots, self._current_engine_time(), creds.user, self._keys,
                                                 creds.context),
                    self._v3_parse)
            except SnmpTimeout as exc:
                # The device answered discovery, so it is there and speaks v3. Silence now is
                # nearly always mismatched v3 settings: in the GNS3 survey RouterOS ignored every
                # v3 mistake and Cisco ignored a wrong privacy protocol instead of reporting it.
                raise SnmpTimeout(f"{exc}; it answered SNMPv3 discovery, so the user name, protocols or "
                                  f"passwords probably do not match (some devices stay silent rather than report it)") from None
            if isinstance(result, Pdu):
                return result
            report: _Report = result
            oid = report.pdu.varbinds[0].oid if report.pdu.varbinds else None
            if oid == usm.NOT_IN_TIME_WINDOW and attempt == 0:
                continue  # _v3_parse already took the device's clock from the report
            if oid == usm.UNKNOWN_ENGINE_ID and attempt == 0:
                await self.discover()  # the device's engine ID changed (replaced or reset)
                continue
            reason = usm.USM_STATS.get(oid, f"report {ber.format_oid(oid) if oid else '(empty)'}")
            raise SnmpSecurityError(f"SNMPv3 {creds.level} as {creds.user.decode(errors='replace')!r}: {reason}")
        raise SnmpSecurityError("SNMPv3: device kept answering 'not in time window'")

    def _v3_message(self, mid: int, pdu: bytes, flags: int, engine_id: bytes, boots: int, engine_time: int,
                    user: bytes, keys: Optional[usm.LocalizedKeys], context: bytes = b"") -> bytes:
        scoped = ber.enc_seq(ber.enc_octets(engine_id), ber.enc_octets(context), pdu)
        priv_params = b""
        if flags & FLAG_PRIV:
            encrypted, priv_params = usm.encrypt(keys, scoped, boots, engine_time, self._salt)
            scoped_field = ber.enc_octets(encrypted)
        else:
            scoped_field = scoped
        digest_len = keys.digest_len if (flags & FLAG_AUTH) else 0
        before_digest = (ber.enc_octets(engine_id) + ber.enc_int(boots) + ber.enc_int(engine_time)
                         + ber.enc_octets(user))
        sec_inner = before_digest + ber.enc_octets(b"\x00" * digest_len) + ber.enc_octets(priv_params)
        sec_seq = ber.enc_seq(sec_inner)
        sec_field = ber.enc_octets(sec_seq)
        global_data = ber.enc_seq(ber.enc_int(mid), ber.enc_int(MAX_MESSAGE_SIZE),
                                  ber.enc_octets(bytes([flags])), ber.enc_int(3))
        version = ber.enc_int(3)
        body = version + global_data + sec_field + scoped_field
        message = ber.enc_seq(body)
        if not digest_len:
            return message
        # Where the zeroed digest sits: walk the same layout the bytes were built from.
        offset = (len(message) - len(body)) + len(version) + len(global_data) \
            + (len(sec_field) - len(sec_seq)) + (len(sec_seq) - len(sec_inner)) + len(before_digest) + 2
        assert message[offset:offset + digest_len] == b"\x00" * digest_len
        digest = usm.sign(keys, message)
        return message[:offset] + digest + message[offset + digest_len:]

    def _v3_split(self, data: bytes) -> Dict[str, Any]:
        tag, s, e = ber.read_tlv(data, 0)
        if tag != ber.SEQUENCE:
            raise ber.BerError("not an SNMP message")
        parts = ber.children(data, s, e)
        if len(parts) != 4 or ber.dec_int(data[parts[0][1]:parts[0][2]]) != 3:
            raise ber.BerError("not an SNMPv3 message")
        g = ber.children(data, parts[1][1], parts[1][2])
        if len(g) != 4:
            raise ber.BerError("bad SNMPv3 header")
        msg_id = ber.dec_int(data[g[0][1]:g[0][2]])
        flag_bytes = data[g[2][1]:g[2][2]]
        if len(flag_bytes) != 1 or ber.dec_int(data[g[3][1]:g[3][2]]) != 3:
            raise ber.BerError("bad SNMPv3 header")
        if parts[2][0] != ber.OCTET_STRING:
            raise ber.BerError("bad security parameters")
        stag, ss, se = ber.read_tlv(data, parts[2][1], parts[2][2])
        sp = ber.children(data, ss, se)
        if stag != ber.SEQUENCE or len(sp) != 6:
            raise ber.BerError("bad security parameters")
        return {
            "msg_id": msg_id,
            "flags": flag_bytes[0],
            "engine_id": data[sp[0][1]:sp[0][2]],
            "boots": ber.dec_int(data[sp[1][1]:sp[1][2]]),
            "time": ber.dec_int(data[sp[2][1]:sp[2][2]]),
            "user": data[sp[3][1]:sp[3][2]],
            "auth_span": (sp[4][1], sp[4][2]),
            "priv_params": data[sp[5][1]:sp[5][2]],
            "scoped": parts[3],
        }

    def _v3_parse_discovery(self, data: bytes) -> Optional[Tuple[int, Tuple[bytes, int, int]]]:
        m = self._v3_split(data)
        return m["msg_id"], (m["engine_id"], m["boots"], m["time"])

    def _v3_parse(self, data: bytes) -> Optional[Tuple[int, Union[Pdu, _Report]]]:
        m = self._v3_split(data)
        flags = m["flags"]
        authenticated = False
        if flags & FLAG_AUTH:
            if self._keys is None or m["engine_id"] != self.engine_id:
                return None
            a_s, a_e = m["auth_span"]
            if a_e - a_s != self._keys.digest_len:
                self.stats.auth_failures += 1
                return None
            zeroed = data[:a_s] + b"\x00" * (a_e - a_s) + data[a_e:]
            if not usm.verify(self._keys, zeroed, data[a_s:a_e]):
                self.stats.auth_failures += 1
                return None
            authenticated = True
            # RFC 3414 3.2 step 7b: follow the device's clock from authenticated messages.
            if m["boots"] > self.engine_boots or (m["boots"] == self.engine_boots and m["time"] > self._current_engine_time()):
                self._set_engine_clock(m["boots"], m["time"])
        stag, ss, se = m["scoped"]
        if flags & FLAG_PRIV:
            if not authenticated or stag != ber.OCTET_STRING:
                return None
            plain = usm.decrypt(self._keys, data[ss:se], m["priv_params"], m["boots"], m["time"])
            _, ps, pe = ber.read_tlv(plain, 0)
            scoped_bytes, sc_s, sc_e = plain, ps, pe
        else:
            if stag != ber.SEQUENCE:
                return None
            scoped_bytes, sc_s, sc_e = data, ss, se
        sc = ber.children(scoped_bytes, sc_s, sc_e)
        if len(sc) != 3:
            raise ber.BerError("bad scoped PDU")
        pdu = ber.decode_pdu(scoped_bytes, sc[1][2], sc[2][2])
        if pdu.tag == ber.REPORT:
            if pdu.varbinds and pdu.varbinds[0].oid == usm.NOT_IN_TIME_WINDOW and authenticated:
                self._set_engine_clock(m["boots"], m["time"])
            return m["msg_id"], _Report(pdu, authenticated)
        if pdu.tag != ber.RESPONSE:
            return None
        if self.credentials.auth and not authenticated:
            return None  # never accept an unauthenticated answer to an authenticated question
        return m["msg_id"], pdu

