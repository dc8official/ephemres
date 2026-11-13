"""The OIDs Ephemres reads: the fixed scope agreed for v3.3.6 (build plan 8.1).

Standard MIBs only, plus CISCO-CDP-MIB because CDP is what Cisco and Meraki gear speaks when
LLDP is off. No vendor CPU or memory tables, no MIB files: these numbers are the whole MIB
knowledge the product has, so adding a column is a deliberate change here.
"""
from __future__ import annotations

from typing import Dict

from monitoring.snmp.ber import Oid, parse_oid as O

# SNMPv2-MIB system group, plus two more clocks: sysUpTime is the agent's uptime (it resets when
# the SNMP process restarts), hrSystemUptime is the host's, and snmpEngineBoots counts restarts
# of the SNMP engine. Telling a reboot from an agent restart needs whichever the device answers.
SYSTEM: Dict[str, Oid] = {
    "sysDescr": O("1.3.6.1.2.1.1.1.0"),
    "sysObjectID": O("1.3.6.1.2.1.1.2.0"),
    "sysUpTime": O("1.3.6.1.2.1.1.3.0"),
    "sysContact": O("1.3.6.1.2.1.1.4.0"),
    "sysName": O("1.3.6.1.2.1.1.5.0"),
    "sysLocation": O("1.3.6.1.2.1.1.6.0"),
    "hrSystemUptime": O("1.3.6.1.2.1.25.1.1.0"),
    "snmpEngineBoots": O("1.3.6.1.6.3.10.2.1.2.0"),
    "snmpEngineTime": O("1.3.6.1.6.3.10.2.1.3.0"),
    "ifNumber": O("1.3.6.1.2.1.2.1.0"),
}

# IF-MIB ifTable (indexed by ifIndex).
IF_TABLE: Dict[str, Oid] = {
    "ifDescr": O("1.3.6.1.2.1.2.2.1.2"),
    "ifType": O("1.3.6.1.2.1.2.2.1.3"),
    "ifMtu": O("1.3.6.1.2.1.2.2.1.4"),
    "ifSpeed": O("1.3.6.1.2.1.2.2.1.5"),
    "ifPhysAddress": O("1.3.6.1.2.1.2.2.1.6"),
    "ifAdminStatus": O("1.3.6.1.2.1.2.2.1.7"),
    "ifOperStatus": O("1.3.6.1.2.1.2.2.1.8"),
    "ifLastChange": O("1.3.6.1.2.1.2.2.1.9"),
    "ifInOctets": O("1.3.6.1.2.1.2.2.1.10"),
    "ifInDiscards": O("1.3.6.1.2.1.2.2.1.13"),
    "ifInErrors": O("1.3.6.1.2.1.2.2.1.14"),
    "ifOutOctets": O("1.3.6.1.2.1.2.2.1.16"),
    "ifOutDiscards": O("1.3.6.1.2.1.2.2.1.19"),
    "ifOutErrors": O("1.3.6.1.2.1.2.2.1.20"),
}

# IF-MIB ifXTable: names, 64-bit counters (32-bit octet counters wrap in under 6 minutes at
# 100 Mb/s, so these are the ones to poll) and speed in Mb/s (ifSpeed tops out at 4.29 Gb/s).
IF_X_TABLE: Dict[str, Oid] = {
    "ifName": O("1.3.6.1.2.1.31.1.1.1.1"),
    "ifHCInOctets": O("1.3.6.1.2.1.31.1.1.1.6"),
    "ifHCOutOctets": O("1.3.6.1.2.1.31.1.1.1.10"),
    "ifHighSpeed": O("1.3.6.1.2.1.31.1.1.1.15"),
    "ifAlias": O("1.3.6.1.2.1.31.1.1.1.18"),
    # Changes when the device resets this interface's counters (module swap, clear counters):
    # a delta across a change is meaningless and must be dropped.
    "ifCounterDiscontinuityTime": O("1.3.6.1.2.1.31.1.1.1.19"),
}

# Traffic counters read every poll by ephemres-snmpd (step 2): the 64-bit set where the device has
# ifXTable, else the 32-bit set (wraps in under 6 minutes at 100 Mb/s, so only good on slow
# links or short polls). Packets are unicast + multicast + broadcast where answered.
HC_COUNTERS: Dict[str, Oid] = {
    "in_octets": O("1.3.6.1.2.1.31.1.1.1.6"),
    "out_octets": O("1.3.6.1.2.1.31.1.1.1.10"),
    "in_ucast": O("1.3.6.1.2.1.31.1.1.1.7"),
    "in_mcast": O("1.3.6.1.2.1.31.1.1.1.8"),
    "in_bcast": O("1.3.6.1.2.1.31.1.1.1.9"),
    "out_ucast": O("1.3.6.1.2.1.31.1.1.1.11"),
    "out_mcast": O("1.3.6.1.2.1.31.1.1.1.12"),
    "out_bcast": O("1.3.6.1.2.1.31.1.1.1.13"),
}
LOW_COUNTERS: Dict[str, Oid] = {
    "in_octets": O("1.3.6.1.2.1.2.2.1.10"),
    "out_octets": O("1.3.6.1.2.1.2.2.1.16"),
    "in_ucast": O("1.3.6.1.2.1.2.2.1.11"),
    "out_ucast": O("1.3.6.1.2.1.2.2.1.17"),
}
# Always Counter32 (IF-MIB has no 64-bit errors or discards).
ERROR_COUNTERS: Dict[str, Oid] = {
    "in_errors": O("1.3.6.1.2.1.2.2.1.14"),
    "out_errors": O("1.3.6.1.2.1.2.2.1.20"),
    "in_discards": O("1.3.6.1.2.1.2.2.1.13"),
    "out_discards": O("1.3.6.1.2.1.2.2.1.19"),
}

# IP-MIB ipAddrTable (IPv4, indexed by the address): which of the device's addresses sits on
# which interface. Used to recognise a device by any of its addresses, as flows already do.
IP_ADDR: Dict[str, Oid] = {
    "ipAdEntIfIndex": O("1.3.6.1.2.1.4.20.1.2"),
    "ipAdEntNetMask": O("1.3.6.1.2.1.4.20.1.3"),
}

# LLDP-MIB (IEEE 802.1AB-2005).
LLDP_LOCAL: Dict[str, Oid] = {
    "lldpLocChassisIdSubtype": O("1.0.8802.1.1.2.1.3.1.0"),
    "lldpLocChassisId": O("1.0.8802.1.1.2.1.3.2.0"),
    "lldpLocSysName": O("1.0.8802.1.1.2.1.3.3.0"),
}
# lldpLocPortTable, indexed by lldpLocPortNum (not always the ifIndex).
LLDP_LOC_PORT: Dict[str, Oid] = {
    "lldpLocPortIdSubtype": O("1.0.8802.1.1.2.1.3.7.1.2"),
    "lldpLocPortId": O("1.0.8802.1.1.2.1.3.7.1.3"),
    "lldpLocPortDesc": O("1.0.8802.1.1.2.1.3.7.1.4"),
}
# lldpRemTable, indexed by timeMark.localPortNum.remIndex.
LLDP_REMOTE: Dict[str, Oid] = {
    "lldpRemChassisIdSubtype": O("1.0.8802.1.1.2.1.4.1.1.4"),
    "lldpRemChassisId": O("1.0.8802.1.1.2.1.4.1.1.5"),
    "lldpRemPortIdSubtype": O("1.0.8802.1.1.2.1.4.1.1.6"),
    "lldpRemPortId": O("1.0.8802.1.1.2.1.4.1.1.7"),
    "lldpRemPortDesc": O("1.0.8802.1.1.2.1.4.1.1.8"),
    "lldpRemSysName": O("1.0.8802.1.1.2.1.4.1.1.9"),
    "lldpRemSysDesc": O("1.0.8802.1.1.2.1.4.1.1.10"),
}
# lldpRemManAddrTable: the neighbour's management address is in the index
# (timeMark.localPortNum.remIndex.addrSubtype.addrLen.addr...), the value is just the
# interface numbering subtype, so one column is enough to list them.
LLDP_REM_MAN_ADDR_IF_SUBTYPE = O("1.0.8802.1.1.2.1.4.2.1.3")

# LLDP-V2-MIB (IEEE 802.1AB-2009, 1.3.111.2.802.1.1.13). Some devices answer only this one, so
# it is the fallback when LLDP-MIB is empty. Two differences from LLDP-MIB: the local port is
# a real ifIndex (lldpV2LocPortTable and the second index of the remote table), and the remote
# index has four parts: timeMark.localIfIndex.destAddressIndex.remIndex.
LLDP_V2_LOCAL: Dict[str, Oid] = {
    "lldpV2LocChassisIdSubtype": O("1.3.111.2.802.1.1.13.1.3.1.0"),
    "lldpV2LocChassisId": O("1.3.111.2.802.1.1.13.1.3.2.0"),
    "lldpV2LocSysName": O("1.3.111.2.802.1.1.13.1.3.3.0"),
}
LLDP_V2_LOC_PORT: Dict[str, Oid] = {
    "lldpV2LocPortIdSubtype": O("1.3.111.2.802.1.1.13.1.3.7.1.2"),
    "lldpV2LocPortId": O("1.3.111.2.802.1.1.13.1.3.7.1.3"),
    "lldpV2LocPortDesc": O("1.3.111.2.802.1.1.13.1.3.7.1.4"),
}
LLDP_V2_REMOTE: Dict[str, Oid] = {
    "lldpV2RemChassisIdSubtype": O("1.3.111.2.802.1.1.13.1.4.1.1.5"),
    "lldpV2RemChassisId": O("1.3.111.2.802.1.1.13.1.4.1.1.6"),
    "lldpV2RemPortIdSubtype": O("1.3.111.2.802.1.1.13.1.4.1.1.7"),
    "lldpV2RemPortId": O("1.3.111.2.802.1.1.13.1.4.1.1.8"),
    "lldpV2RemPortDesc": O("1.3.111.2.802.1.1.13.1.4.1.1.9"),
    "lldpV2RemSysName": O("1.3.111.2.802.1.1.13.1.4.1.1.10"),
    "lldpV2RemSysDesc": O("1.3.111.2.802.1.1.13.1.4.1.1.11"),
}
# lldpV2RemManAddrTable: index = the four remote parts, then addrSubtype.addrLen.addr...
LLDP_V2_REM_MAN_ADDR_IF_SUBTYPE = O("1.3.111.2.802.1.1.13.1.4.2.1.3")

# CISCO-CDP-MIB cdpCacheTable, indexed by cdpCacheIfIndex.cdpCacheDeviceIndex.
CDP_CACHE: Dict[str, Oid] = {
    "cdpCacheAddressType": O("1.3.6.1.4.1.9.9.23.1.2.1.1.3"),
    "cdpCacheAddress": O("1.3.6.1.4.1.9.9.23.1.2.1.1.4"),
    "cdpCacheVersion": O("1.3.6.1.4.1.9.9.23.1.2.1.1.5"),
    "cdpCacheDeviceId": O("1.3.6.1.4.1.9.9.23.1.2.1.1.6"),
    "cdpCacheDevicePort": O("1.3.6.1.4.1.9.9.23.1.2.1.1.7"),
    "cdpCachePlatform": O("1.3.6.1.4.1.9.9.23.1.2.1.1.8"),
}

IF_ADMIN_STATUS = {1: "up", 2: "down", 3: "testing"}
IF_OPER_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

# sysObjectID starts 1.3.6.1.4.1.<enterprise>; the IANA number names the vendor. Shown to
# people and used for setup hints only: no polling logic depends on it (vendor research, rule 2).
ENTERPRISES: Dict[int, str] = {
    9: "Cisco", 11: "HP", 2604: "Sophos", 2636: "Juniper", 12356: "Fortinet", 14823: "Aruba",
    14988: "MikroTik", 25461: "Palo Alto", 29671: "Cisco Meraki", 40310: "Cumulus", 41112: "Ubiquiti",
    47196: "HPE Aruba",
}
