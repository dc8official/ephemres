"""SNMP core (v3.3.6 phase 6): BER, USM keys and ciphers against published vectors, and the
client against a local fake agent over real UDP (v2c and v3, walks, retries, errors).

Interoperability with real devices is checked by the survey in the GNS3 lab, not here.
"""
import asyncio
import json

import pytest

from monitoring.snmp import ber, mib, usm
from monitoring.snmp.ber import BerError, parse_oid as O
from monitoring.snmp.client import (CommunityAuth, SnmpClient, SnmpErrorStatus, SnmpSecurityError, SnmpTimeout,
                                    UserAuth)
from monitoring.snmp.survey import _jsonable, render_text, run_survey
from app.core import libcrypto

from tests.backend.snmp_fake_agent import start_agent

H = bytes.fromhex


# --- BER -----------------------------------------------------------------------------------------

def test_known_encodings():
    assert ber.enc_oid(O("1.3.6.1.2.1.1.1.0")) == H("06082b06010201010100")
    assert ber.enc_oid(O("1.3.6.1.4.1.311")) == H("06072b060104018237")
    assert ber.enc_int(0) == H("020100")
    assert ber.enc_int(127) == H("02017f")
    assert ber.enc_int(128) == H("02020080")
    assert ber.enc_int(-1) == H("0201ff")
    assert ber.enc_int(-129) == H("0202ff7f")
    assert ber.enc_unsigned(2 ** 32 - 1, ber.COUNTER32) == H("410500ffffffff")
    assert ber.enc_octets(b"x" * 300)[:4] == H("0482012c")


def test_round_trips():
    for oid in [O("1.3.6.1.2.1.31.1.1.1.6.10101"), O("1.0.8802.1.1.2.1.4.1.1.9.0.3.1"), (2, 999, 1)]:
        tag, s, e = ber.read_tlv(ber.enc_oid(oid), 0)
        assert ber.dec_oid(ber.enc_oid(oid)[s:e]) == oid
    for v in [0, 1, 255, 2 ** 63 + 5, 2 ** 64 - 1]:
        raw = ber.enc_unsigned(v, ber.COUNTER64)
        _, s, e = ber.read_tlv(raw, 0)
        assert ber.dec_unsigned(raw[s:e]) == v
    pdu = ber.encode_pdu(ber.RESPONSE, 42, 0, 0, [(O("1.3.6.1.2.1.1.5.0"), ber.OCTET_STRING, b"abj-rtr"),
                                                   (O("1.3.6.1.2.1.4.20.1.1.10.0.0.1"), ber.IP_ADDRESS, "10.0.0.1"),
                                                   (O("1.3.6.1.2.1.1.9.0"), ber.NO_SUCH_INSTANCE, None)])
    back = ber.decode_pdu(pdu, 0, len(pdu))
    assert back.request_id == 42
    assert [v.value for v in back.varbinds] == [b"abj-rtr", "10.0.0.1", None]
    assert back.varbinds[2].is_exception


def test_counter_without_leading_zero_reads_unsigned():
    # Some agents send a Counter32 with the top bit set and no leading zero.
    _, s, e = ber.read_tlv(H("4104ffffffff"), 0)
    assert ber.decode_value(ber.COUNTER32, H("4104ffffffff")[s:e]) == 2 ** 32 - 1


@pytest.mark.parametrize("raw", [H("04"), H("0405abcd"), H("0480"), H("04850000000001"), H("1f0100")])
def test_malformed_packets_raise_ber_error(raw):
    with pytest.raises(BerError):
        ber.read_tlv(raw, 0)


def test_malformed_pdu_raises_ber_error():
    bad = ber.enc_seq(ber.enc_int(1), ber.enc_int(0), tag=ber.RESPONSE)
    with pytest.raises(BerError):
        ber.decode_pdu(bad, 0, len(bad))


# --- USM: RFC 3414 appendix A.3 and NIST SP 800-38A -----------------------------------------------

ENGINE_A3 = H("000000000000000000000002")


def test_rfc3414_md5_key_vectors():
    ku = usm.password_to_key(b"maplesyrup", "md5")
    assert ku == H("9faf3283884e92834ebc9847d8edd963")
    assert usm.localize_key(ku, ENGINE_A3, "md5") == H("526f5eed9fcce26f8964c2930787d82b")


def test_rfc3414_sha_key_vectors():
    ku = usm.password_to_key(b"maplesyrup", "sha")
    assert ku == H("9fb5cc0381497b3793528939ff788d5d79145211")
    assert usm.localize_key(ku, ENGINE_A3, "sha") == H("6695febc9288e36282235fc7151f128497b38f3f")


def test_short_passwords_are_refused_before_asking_the_device():
    with pytest.raises(usm.UsmError):
        usm.password_to_key(b"short", "sha")


def test_aes128_cfb_nist_vector():
    key = H("2b7e151628aed2a6abf7158809cf4f3c")
    iv = H("000102030405060708090a0b0c0d0e0f")
    plain = H("6bc1bee22e409f96e93d7e117393172a")
    assert libcrypto.cipher("aes-128-cfb", key, iv, plain, True) == H("3b3fd92eb72dad20333449f8e83cfb4a")


def test_privacy_round_trips():
    for priv in ("aes", "des"):
        if priv == "des" and not libcrypto.des_available():
            continue
        keys = usm.derive_keys(ENGINE_A3, "sha", b"authpassword", priv, b"privpassword")
        plain = ber.enc_seq(ber.enc_octets(b"x" * 37))
        enc, params = usm.encrypt(keys, plain, 3, 1234, usm.SaltCounter())
        assert enc != plain and len(params) == 8
        back = usm.decrypt(keys, enc, params, 3, 1234)
        assert back[: len(plain)] == plain


# --- client against the fake agent ---------------------------------------------------------------

def interface_mib(count=3):
    m = {
        mib.SYSTEM["sysDescr"]: (ber.OCTET_STRING, b"Lab router"),
        mib.SYSTEM["sysObjectID"]: (ber.OBJECT_IDENTIFIER, O("1.3.6.1.4.1.9.1.1")),
        mib.SYSTEM["sysUpTime"]: (ber.TIMETICKS, 8_640_000),
        mib.SYSTEM["sysName"]: (ber.OCTET_STRING, b"abj-rtr"),
        mib.SYSTEM["snmpEngineBoots"]: (ber.INTEGER, 7),
    }
    for i in range(1, count + 1):
        m[mib.IF_TABLE["ifDescr"] + (i,)] = (ber.OCTET_STRING, f"GigabitEthernet0/{i}".encode())
        m[mib.IF_TABLE["ifSpeed"] + (i,)] = (ber.GAUGE32, 1_000_000_000)
        m[mib.IF_TABLE["ifAdminStatus"] + (i,)] = (ber.INTEGER, 1)
        m[mib.IF_TABLE["ifOperStatus"] + (i,)] = (ber.INTEGER, 1 if i < count else 2)
        m[mib.IF_TABLE["ifInErrors"] + (i,)] = (ber.COUNTER32, 0)
        m[mib.IF_TABLE["ifOutDiscards"] + (i,)] = (ber.COUNTER32, 0)
        m[mib.IF_X_TABLE["ifName"] + (i,)] = (ber.OCTET_STRING, f"Gi0/{i}".encode())
        m[mib.IF_X_TABLE["ifHCInOctets"] + (i,)] = (ber.COUNTER64, 2 ** 40 + i)
        m[mib.IF_X_TABLE["ifHCOutOctets"] + (i,)] = (ber.COUNTER64, 2 ** 33 + i)
        m[mib.IF_X_TABLE["ifHighSpeed"] + (i,)] = (ber.GAUGE32, 1000)
    m[mib.IP_ADDR["ipAdEntIfIndex"] + (10, 55, 0, 1)] = (ber.INTEGER, 1)
    m[mib.LLDP_LOC_PORT["lldpLocPortDesc"] + (1,)] = (ber.OCTET_STRING, b"Gi0/1")
    rem = (0, 1, 1)
    m[mib.LLDP_REMOTE["lldpRemSysName"] + rem] = (ber.OCTET_STRING, b"abj-sw1")
    m[mib.LLDP_REMOTE["lldpRemPortId"] + rem] = (ber.OCTET_STRING, b"Gi1/0/48")
    m[mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE + rem + (1, 4, 10, 55, 0, 2)] = (ber.INTEGER, 2)
    m[mib.CDP_CACHE["cdpCacheDeviceId"] + (2, 1)] = (ber.OCTET_STRING, b"PE-A")
    m[mib.CDP_CACHE["cdpCacheDevicePort"] + (2, 1)] = (ber.OCTET_STRING, b"Gi0/0/3")
    m[mib.CDP_CACHE["cdpCacheAddressType"] + (2, 1)] = (ber.INTEGER, 1)
    m[mib.CDP_CACHE["cdpCacheAddress"] + (2, 1)] = (ber.OCTET_STRING, bytes([192, 0, 2, 1]))
    return m


def run(coro):
    return asyncio.run(coro)


def test_v2c_get_and_column_walk_with_bulk():
    async def go():
        agent = await start_agent(mib=interface_mib(30))
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, max_repetitions=10) as c:
            got = await c.get([mib.SYSTEM["sysName"], mib.SYSTEM["sysContact"]])
            assert got[0].value == b"abj-rtr" and got[1].tag == ber.NO_SUCH_OBJECT
            cols = [mib.IF_X_TABLE["ifName"], mib.IF_X_TABLE["ifHCInOctets"], mib.IF_TABLE["ifDescr"]]
            res = await c.walk_columns(cols)
            assert [vb.value for vb in res[cols[0]]] == [f"Gi0/{i}".encode() for i in range(1, 31)]
            assert res[cols[1]][-1].value == 2 ** 40 + 30
            # 30 rows x 3 columns at 10 rows a request: 3 full requests and one that ends them.
            assert c.stats.requests - 1 <= 4
        agent.transport.close()

    run(go())


def test_walk_halves_repetitions_on_too_big_and_getnext_mode_matches():
    async def go():
        agent = await start_agent(mib=interface_mib(12), too_big_above=4)
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, max_repetitions=20) as c:
            bulk = await c.walk(mib.IF_X_TABLE["ifName"])
            assert len(bulk) == 12 and c.max_repetitions == 2
            nxt = await c.walk(mib.IF_X_TABLE["ifName"], use_bulk=False)
            assert nxt == bulk
        agent.transport.close()

    run(go())


def test_walk_stops_at_end_of_mib_view():
    async def go():
        agent = await start_agent(mib=interface_mib(2))
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port) as c:
            # CDP is the last subtree in this MIB, so the walk must stop on endOfMibView.
            assert len(await c.walk(mib.CDP_CACHE["cdpCacheAddress"])) == 1
            assert await c.walk(O("1.3.6.1.4.1.99999")) == []
        agent.transport.close()

    run(go())


def test_retry_after_a_lost_packet_and_timeout_on_wrong_community():
    async def go():
        agent = await start_agent(mib=interface_mib(1), drop_first=1)
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.3, retries=1) as c:
            assert (await c.get([mib.SYSTEM["sysName"]]))[0].value == b"abj-rtr"
            assert c.stats.timeouts == 1 and c.stats.sends == 2
        async with SnmpClient("127.0.0.1", CommunityAuth(b"wrong"), port=agent.port, timeout=0.2, retries=1) as c:
            with pytest.raises(SnmpTimeout, match="after 2 tries"):
                await c.get([mib.SYSTEM["sysName"]])
        agent.transport.close()

    run(go())


def test_a_column_the_device_drops_costs_only_itself():
    """RouterOS 7.8 (GNS3 survey) drops any multi-column GETBULK that includes
    lldpRemManAddrTable; the other columns must still come back."""
    man = mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE
    name_col = mib.LLDP_REMOTE["lldpRemSysName"]

    async def go():
        agent = await start_agent(mib=interface_mib(2), drop_with=man)
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0) as c:
            res = await c.walk_columns([name_col, man])
            assert [v.value for v in res[name_col]] == [b"abj-sw1"] and len(res[man]) == 1
        agent.drop_alone = True
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0) as c:
            errors = {}
            res = await c.walk_columns([name_col, man], errors=errors)
            assert [v.value for v in res[name_col]] == [b"abj-sw1"] and res[man] == []
            assert list(errors) == [man] and "no reply" in errors[man]
            with pytest.raises(SnmpTimeout):
                await c.walk(man)
        report = await run_survey("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0, interval=0)
        agent.transport.close()
        return report

    report = run(go())
    assert report["ok"] and report["unanswered_columns"].keys() == {"lldpRemManAddrIfSubtype"}
    assert report["lldp"]["neighbours"][0]["remote_name"] == "abj-sw1"
    assert report["cdp"]["answered"]


V3 = dict(user=b"ephemres", auth_password=b"authpass123", priv_password=b"privpass123")


@pytest.mark.parametrize("auth,priv", [("sha", "aes"), ("md5", "des"), ("sha256", None), ("sha512", "aes"), (None, None)])
def test_v3_levels_and_protocols(auth, priv):
    if priv == "des" and not libcrypto.des_available():
        pytest.skip("DES not available in this OpenSSL")

    async def go():
        agent = await start_agent(mib=interface_mib(5), user=V3["user"], auth=auth,
                                  auth_password=V3["auth_password"] if auth else None,
                                  priv=priv, priv_password=V3["priv_password"] if priv else None)
        creds = UserAuth(V3["user"], auth, V3["auth_password"] if auth else None, priv, V3["priv_password"] if priv else None)
        async with SnmpClient("127.0.0.1", creds, port=agent.port, max_repetitions=3) as c:
            assert (await c.get([mib.SYSTEM["sysName"]]))[0].value == b"abj-rtr"
            assert c.engine_id == agent.engine_id and c.engine_boots == agent.boots
            assert len(await c.walk(mib.IF_X_TABLE["ifHCOutOctets"])) == 5
        agent.transport.close()

    run(go())


def test_v3_wrong_password_and_unknown_user_say_so():
    async def go():
        agent = await start_agent(mib=interface_mib(1), user=b"ephemres", auth="sha", auth_password=b"authpass123",
                                  priv="aes", priv_password=b"privpass123")
        wrong = UserAuth(b"ephemres", "sha", b"not-the-password", "aes", b"privpass123")
        async with SnmpClient("127.0.0.1", wrong, port=agent.port) as c:
            with pytest.raises(SnmpSecurityError, match="wrong digest"):
                await c.get([mib.SYSTEM["sysName"]])
        stranger = UserAuth(b"someone", "sha", b"authpass123", "aes", b"privpass123")
        async with SnmpClient("127.0.0.1", stranger, port=agent.port) as c:
            with pytest.raises(SnmpSecurityError, match="unknown user"):
                await c.get([mib.SYSTEM["sysName"]])
        bad_priv = UserAuth(b"ephemres", "sha", b"authpass123", "aes", b"not-the-priv-pw")
        async with SnmpClient("127.0.0.1", bad_priv, port=agent.port, timeout=0.2, retries=0) as c:
            with pytest.raises(SnmpTimeout, match="answered SNMPv3 discovery"):
                await c.get([mib.SYSTEM["sysName"]])
        short = UserAuth(b"ephemres", "sha", b"short", None, None)
        async with SnmpClient("127.0.0.1", short, port=agent.port) as c:
            with pytest.raises(SnmpSecurityError, match="at least 8"):
                await c.get([mib.SYSTEM["sysName"]])
        agent.transport.close()

    run(go())


def test_v3_follows_a_reboot_through_not_in_time_window():
    async def go():
        agent = await start_agent(mib=interface_mib(1), user=b"ephemres", auth="sha", auth_password=b"authpass123",
                                  priv="aes", priv_password=b"privpass123")
        creds = UserAuth(b"ephemres", "sha", b"authpass123", "aes", b"privpass123")
        async with SnmpClient("127.0.0.1", creds, port=agent.port) as c:
            await c.get([mib.SYSTEM["sysName"]])
            agent.boots, agent.engine_time = agent.boots + 1, 12  # the device rebooted
            assert (await c.get([mib.SYSTEM["sysName"]]))[0].value == b"abj-rtr"
            assert c.engine_boots == agent.boots
        agent.transport.close()

    run(go())


def test_survey_report_end_to_end_without_secrets():
    async def go():
        agent = await start_agent(mib=interface_mib(4), user=b"ephemres", auth="sha", auth_password=b"authpass123",
                                  priv="aes", priv_password=b"privpass123")
        creds = UserAuth(b"ephemres", "sha", b"authpass123", "aes", b"privpass123")
        report = await run_survey("127.0.0.1", creds, port=agent.port, interval=0.2)
        agent.transport.close()
        return report

    report = run(go())
    assert report["ok"] and report["system"]["sysName"] == b"abj-rtr"
    assert len(report["interfaces"]) == 4 and report["counters"]["counter"].startswith("64-bit")
    assert report["addresses"] == [{"address": "10.55.0.1", "ifIndex": 1, "netmask": None}]
    assert report["lldp"]["neighbours"][0]["remote_name"] == "abj-sw1"
    assert report["lldp"]["neighbours"][0]["remote_addresses"] == ["10.55.0.2"]
    assert report["lldp"]["neighbours"][0]["local_port"] == "Gi0/1"
    assert report["cdp"]["neighbours"][0]["remote_address"] == "192.0.2.1"
    text = render_text(report)
    assert "64-bit counters on 4 of 4 interfaces" in text and "CDP: 1 neighbours" in text
    dumped = json.dumps(_jsonable(report))
    assert "authpass123" not in dumped and "privpass123" not in dumped
    assert '"sysObjectID": "1.3.6.1.4.1.9.1.1"' in dumped


def test_survey_stops_early_when_the_device_does_not_answer():
    async def go():
        agent = await start_agent(mib=interface_mib(1), community=b"other")
        report = await run_survey("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0)
        agent.transport.close()
        return report

    report = run(go())
    assert report["ok"] is False and "no reply" in report["error"]
    assert "interfaces" not in report and report["totals"]["requests"] == 1


# --- v3.3.6 additions from the vendor research ----------------------------------------------------

def test_reeder_key_extension_matches_the_3des_draft_appendix_b():
    """draft-reeder-snmpv3-usm-3desede-00 appendix B: the whole password-to-key run again on
    the first localised key. The Cisco-style AES-192/256 keys and 3DES use it."""
    md5 = H("526f5eed9fcce26f8964c2930787d82b79eff44a90650ee0a3a40abfac5acc12")
    sha = H("6695febc9288e36282235fc7151f128497b38f3f9b8b6d78936ba6e7d19dfd9cd2d5065547743fb5")
    assert usm.privacy_key(b"maplesyrup", ENGINE_A3, "md5", "3des") == md5
    assert usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha", "aes256c") == sha[:32]
    assert usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha", "aes192c") == sha[:24]


def test_blumenthal_key_extension_matches_the_aes_draft_appendix_a():
    """draft-blumenthal-aes-usm-04 appendix A.1: Kul || SHA1(Kul), first 32 octets."""
    key = usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha", "aes256")
    assert key == H("6695febc9288e36282235fc7151f128497b38f3f505e07eb9af25568fa1f5dbe")


def test_no_stretching_when_the_auth_hash_is_long_enough():
    assert usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha256", "aes256") == \
        usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha256", "aes256c")
    assert usm.privacy_key(b"maplesyrup", ENGINE_A3, "sha", "aes") == H("6695febc9288e36282235fc7151f1284")


@pytest.mark.parametrize("name,key,ct", [
    ("aes-192-cfb", "8e73b0f7da0e6452c810f32b809079e562f8ead2522c6b7b", "cdc80d6fddf18cab34c25909c99a4174"),
    ("aes-256-cfb", "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4", "dc7e84bfda79164b7ecd8486985d3860"),
])
def test_aes192_and_256_cfb_nist_vectors(name, key, ct):
    iv = H("000102030405060708090a0b0c0d0e0f")
    assert libcrypto.cipher(name, H(key), iv, H("6bc1bee22e409f96e93d7e117393172a"), True) == H(ct)


@pytest.mark.parametrize("auth,priv", [("sha", "aes256c"), ("sha", "aes192"), ("md5", "aes256"),
                                       ("sha", "3des"), ("sha256", "aes256"), ("sha224", "aes192c")])
def test_v3_extended_privacy_protocols_end_to_end(auth, priv):
    async def go():
        agent = await start_agent(mib=interface_mib(3), user=b"ephemres", auth=auth, auth_password=b"authpass123",
                                  priv=priv, priv_password=b"privpass123")
        creds = UserAuth(b"ephemres", auth, b"authpass123", priv, b"privpass123")
        async with SnmpClient("127.0.0.1", creds, port=agent.port) as c:
            assert len(await c.walk(mib.IF_X_TABLE["ifName"])) == 3
        agent.transport.close()

    run(go())


def test_one_password_for_both_and_a_context_name():
    async def go():
        agent = await start_agent(mib=interface_mib(1), user=b"ephemres", auth="sha", auth_password=b"onepassword1",
                                  priv="aes", priv_password=b"onepassword1", context=b"vrf-mgmt")
        same = UserAuth(b"ephemres", "sha", b"onepassword1", "aes", None, context=b"vrf-mgmt")
        async with SnmpClient("127.0.0.1", same, port=agent.port) as c:
            assert (await c.get([mib.SYSTEM["sysName"]]))[0].value == b"abj-rtr"
        no_context = UserAuth(b"ephemres", "sha", b"onepassword1", "aes", None)
        async with SnmpClient("127.0.0.1", no_context, port=agent.port) as c:
            with pytest.raises(SnmpSecurityError, match="unknown context"):
                await c.get([mib.SYSTEM["sysName"]])
        agent.transport.close()

    run(go())


def test_getnext_fallback_when_getbulk_is_ignored():
    async def go():
        agent = await start_agent(mib=interface_mib(4), no_bulk=True)
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0) as c:
            res = await c.walk_columns([mib.IF_X_TABLE["ifName"], mib.IF_TABLE["ifDescr"]])
            assert len(res[mib.IF_X_TABLE["ifName"]]) == 4 and len(res[mib.IF_TABLE["ifDescr"]]) == 4
            assert c.use_bulk is False
            timeouts = c.stats.timeouts
            await c.walk(mib.IF_X_TABLE["ifHighSpeed"])
            assert c.stats.timeouts == timeouts  # learned: no more GETBULK, no more waiting
        agent.transport.close()

    run(go())


def lldp_v2_mib():
    m = interface_mib(2)
    for key in [k for k in m if k[:len(mib.LLDP_REMOTE["lldpRemSysName"]) - 1] == mib.LLDP_REMOTE["lldpRemSysName"][:-1]
                or k[:len(mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE)] == mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE
                or k[:len(mib.LLDP_LOC_PORT["lldpLocPortDesc"])] == mib.LLDP_LOC_PORT["lldpLocPortDesc"]]:
        del m[key]
    rem = (0, 2, 1, 7)  # timeMark, local ifIndex 2, dest address index 1, remote index 7
    m[mib.LLDP_V2_REMOTE["lldpV2RemSysName"] + rem] = (ber.OCTET_STRING, b"abj-sw2")
    m[mib.LLDP_V2_REMOTE["lldpV2RemPortId"] + rem] = (ber.OCTET_STRING, b"ge-0/0/1")
    m[mib.LLDP_V2_REM_MAN_ADDR_IF_SUBTYPE + rem + (1, 4, 10, 55, 0, 3)] = (ber.INTEGER, 2)
    return m


def test_survey_falls_back_to_lldp_v2_and_writes_a_profile():
    async def go():
        agent = await start_agent(mib=lldp_v2_mib())
        report = await run_survey("127.0.0.1", CommunityAuth(b"public"), port=agent.port, interval=0)
        agent.transport.close()
        return report

    report = run(go())
    lldp = report["lldp"]
    assert lldp["mib"] == "LLDP-V2-MIB"
    nb = lldp["neighbours"][0]
    assert (nb["remote_name"], nb["local_ifIndex"], nb["remote_addresses"]) == ("abj-sw2", 2, ["10.55.0.3"])
    assert "Gi0/2 -> abj-sw2 port ge-0/0/1 10.55.0.3" in render_text(report)
    prof = report["profile"]
    assert prof["vendor"] == "Cisco" and prof["answers"]["lldp"] == "LLDP-V2-MIB" and prof["answers"]["hc_counters"]
    assert prof["use_bulk"] is True and prof["walk_alone"] == [] and prof["average_reply_ms"] is not None


def test_a_profile_carries_learned_quirks_to_the_next_client():
    from monitoring.snmp.profile import DeviceProfile
    man = mib.LLDP_REM_MAN_ADDR_IF_SUBTYPE

    async def go():
        agent = await start_agent(mib=interface_mib(2), drop_with=man)
        report = await run_survey("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0, interval=0)
        prof = DeviceProfile.from_dict(json.loads(json.dumps(report["profile"])))
        assert ber.format_oid(man) in prof.walk_alone
        cols = {**mib.LLDP_REMOTE, "man": man}
        async with SnmpClient("127.0.0.1", CommunityAuth(b"public"), port=agent.port, timeout=0.2, retries=0,
                              **prof.client_options()) as c:
            res = await c.walk_columns(list(cols.values()))
            assert res[mib.LLDP_REMOTE["lldpRemSysName"]][0].value == b"abj-sw1" and len(res[man]) == 1
            assert c.stats.timeouts == 0  # the quirk cost nothing this time
        agent.transport.close()

    run(go())


def test_survey_command_line_takes_one_password_for_both(monkeypatch, capsys):
    from monitoring.snmp import survey
    seen = {}

    async def fake_run(host, creds, **kw):
        seen["creds"] = creds
        return {"target": host, "snmp": "v3", "surveyed_at": "now", "ok": False, "error": "x", "findings": []}

    monkeypatch.setattr(survey, "run_survey", fake_run)
    monkeypatch.setenv("EPHEMRES_SNMP_AUTH_PASSWORD", "onepassword1")
    monkeypatch.delenv("EPHEMRES_SNMP_PRIV_PASSWORD", raising=False)
    monkeypatch.setattr(survey.getpass, "getpass", lambda prompt: pytest.fail("must not prompt"))
    assert survey.main(["10.0.0.1", "--version", "3", "--user", "u", "--auth", "sha", "--priv", "aes256c",
                        "--priv-same", "--context", "vrf-mgmt"]) == 2
    c = seen["creds"]
    assert (c.priv, c.priv_password, c.context) == ("aes256c", None, b"vrf-mgmt")
    # At the prompt, Enter means "same as authentication" too.
    monkeypatch.setattr(survey.getpass, "getpass", lambda prompt: "")
    survey.main(["10.0.0.1", "--version", "3", "--user", "u", "--auth", "sha", "--priv", "aes"])
    assert seen["creds"].priv_password is None
    capsys.readouterr()
