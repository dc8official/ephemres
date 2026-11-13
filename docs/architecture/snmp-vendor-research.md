# SNMP across vendors: documentation research and design rules (v3.3.6 phase 6)

Written 2026-11-12. Kenneth asked to design the SNMP poller for any device, not one vendor,
and to research how each family behaves, since real-device surveys cannot be run yet.

**How sure each fact is.** Every row says where it comes from:
- **doc:** the vendor's own documentation.
- **3rd:** a third-party guide, tool documentation, a forum or a STIG audit.
- **lab:** our own GNS3 survey on 2026-11-12 (IOSv 15.9, vIOS-L2, RouterOS CHR 7.8).

Anything marked 3rd should be confirmed on a real device before we rely on it. Sources are
listed at the end.

---

## 1. SNMPv3 security each family offers

| Family | Authentication | Privacy | Notes |
| :--- | :--- | :--- | :--- |
| Cisco IOS / IOS XE | MD5, SHA; SHA-2 (256/384/512) from IOS XE 17.10.1a (3rd) | DES, 3DES, AES-128; AES-192/256 as a Cisco extension (doc) | From 17.11.1a the weak algorithms (MD5, DES, 3DES) are no longer offered by default (doc). Routers without the security licence have no privacy at all (lab: IOSv rejects `v3 priv`) |
| Cisco NX-OS | MD5, SHA, SHA-2 (3rd) | AES-128 (`priv aes-128`) | AES pass phrase at least 8 characters (3rd) |
| Cisco Meraki MX, MS, MR (local polling) | SHA-1 or SHA-2 (doc) | DES or AES-128 (doc) | **One password for both** (doc). The Campus Gateway is v3 + AES-128 only, no v2c (doc) |
| Juniper Junos | MD5, SHA, SHA-224, SHA-256 (doc) | DES, 3DES, AES-128 (doc) | Older releases needed the jcrypto package for any privacy (doc, 13.x) |
| Aruba AOS-CX | SHA, SHA-224/256/384/512 (3rd) | DES, AES, AES-192, AES-256 (3rd) | SNMP is enabled per VRF (doc) |
| HPE Aruba AOS-S (2930F etc.) | MD5, SHA (3rd) | DES, AES-128; AES-256 **only with the Cisco key method** (3rd: net-snmp `-x AES-256-C` worked, `AES-256` failed) | |
| NVIDIA Cumulus Linux | MD5, SHA (net-snmp) (doc) | DES, AES (doc) | Plain net-snmp `snmpd`; passwords at least 8 characters (doc) |
| Ubiquiti UniFi (set in UniFi Network) | SHA, fixed (doc) | AES-128, fixed (doc) | **One password for both**, and the protocols cannot be changed (doc, 3rd) |
| Sophos Firewall (SFOS) | MD5, SHA-256, SHA-512: **no SHA-1** (3rd: Sophos Ansible collection) | AES, DES | Passwords at least 12 characters (3rd) |
| Fortinet FortiGate | MD5, SHA, SHA-2 (doc) | DES, AES-128, `aes256` **and** `aes256cisco` (doc) | Offers both AES-256 key methods by name. Only partial RFC 3414 support claimed (doc, 6.0) |
| Palo Alto PAN-OS | SHA-1 (doc, older releases) | AES-128 (doc) | Needs an SNMP view assigned to the user (doc) |
| MikroTik RouterOS | MD5, SHA1 (lab) | DES, AES (lab) | v3 users are communities with `security=private` (lab) |

### The AES-192/256 problem

RFC 3826 standardises only AES-128. For AES-192/256 the 128 or 160 bits of an MD5 or SHA-1
localised key are not enough, and two incompatible ways of making a longer key exist. Both are in
use:

- **Blumenthal** (draft-blumenthal-aes-usm-04): `Kul' = Kul || H(Kul)`, then hash the growing key
  again until long enough, and take the first 24 or 32 octets. net-snmp calls this `AES-192` /
  `AES-256`. FortiGate calls it `aes256`. The draft's own worked example disagrees with its
  formula on the number of rounds, so test vectors from net-snmp are needed before implementing.
- **Reeder** (draft-reeder-snmpv3-usm-3desede-00, written for 3DES): run the whole
  password-to-key algorithm again, with the previous localised key as the password and the same
  engine ID, and concatenate. Cisco uses this for AES-192/256 (3rd; Cisco only says its method is
  "Cisco-specific"), and so do HPE Aruba AOS-S (3rd) and FortiGate `aes256cisco` (doc). net-snmp
  calls it `AES-256-C`.

So a poller that wants AES-256 on any device must offer **both**, as two privacy choices. The
same Reeder method is what 3DES needs (Junos, older Cisco).

## 2. What each family answers

| Family | 64-bit counters (ifHC*) | Neighbours | Notes |
| :--- | :--- | :--- | :--- |
| Cisco IOS / IOS XE | yes (lab) | LLDP-MIB with management addresses, CDP (lab) | `ifAlias` is the interface description (lab) |
| Cisco NX-OS | not checked (no source found) | CDP; LLDP-MIB not confirmed in the lists found | Check the platform's MIB support list |
| Cisco Meraki | yes (doc: example walk from an MR shows `ifHCInOctets`) | not documented | Only SNMPv2-MIB and "the majority of" IF-MIB are promised (doc). **MS and MR fail to respond to OIDs they do not support** instead of answering noSuchObject (doc) |
| Juniper Junos | yes | LLDP-MIB (doc), object list per platform | |
| Aruba AOS-CX | not confirmed in docs | LLDP-MIB (3rd) | |
| Cumulus Linux | yes, but older releases need `-y` removed from `SNMPDOPTS` (doc, 4.4) | LLDP-MIB only if `lldpd` runs with `-x` (AgentX) (doc) | Not every MIB file is installed by default (doc) |
| Ubiquiti UniFi switches | yes (3rd) | LLDP-MIB (3rd: LibreNMS reads neighbours) | Gen-2 switches answer more than gen-1 (3rd). SNMP traps not available (doc) |
| Sophos Firewall | 32 and 64-bit claimed (3rd); one community walk showed only 32-bit | not documented | Interfaces appear as `Port1`, `Port2`... (3rd) |
| Fortinet FortiGate | not confirmed in docs | not documented | Fortinet says MIB-II protocol statistics do not capture all traffic; its own MIB is "more accurate" (doc) |
| Palo Alto PAN-OS | not confirmed in docs | not documented | |
| MikroTik RouterOS | yes (lab) | LLDP-MIB **without management addresses**; no CDP MIB (lab) | `hrSystemUptime` answers (lab) |

**LLDP-V2-MIB.** IEEE 802.1AB-2009 defines a second LLDP MIB at `1.3.111.2.802.1.1.13`. Some
vendors run the 2009 protocol but keep exposing the 2005 MIB (`1.0.8802.1.1.2`, doc: H3C). No
source showed a vendor offering only V2, but a poller for any device should try V2 when V1 is
empty.

## 3. How families behave when polled

| Behaviour | Seen on | Source |
| :--- | :--- | :--- |
| Silence (no answer at all) for an OID or table the device does not support | Meraki MS and MR; RouterOS for `lldpRemManAddrTable` in a multi-column request | doc, lab |
| Silence for every v3 mistake (unknown user, wrong password, wrong protocol) | RouterOS | lab |
| "unsupported security level" for any authPriv request | IOS router without privacy support | lab |
| Extra or out-of-order varbinds in GETNEXT replies | RouterOS | lab |
| Interface counters refreshed only every 5 to 30 s | Juniper (5 s, up to 30 on some platforms), Meraki (about 25 to 30 s) | 3rd |
| Slow answers to big walks | Juniper MX (forum reports); Meraki's own examples use a 10 s timeout | 3rd, doc |
| SNMP bound to a management VRF or instance, so the agent must be told where to listen | Aruba AOS-CX (`snmp-server vrf mgmt`), Juniper (`mgmt_junos`, `routing-instance-access`), Cumulus (`listening-address ... vrf mgmt`), NX-OS (management VRF) | doc, 3rd |
| v2c community with a routing-instance prefix (`ri@community`) to read another instance | Juniper | 3rd |
| Poller's address must be allow-listed on the device | Meraki MX (Security & SD-WAN → Firewall → WAN appliance services), Sophos (device access per zone), most others via ACLs | doc |
| Interface numbers (ifIndex) may change after a reboot or when interfaces are added | Any. Cisco keeps them only with `snmp-server ifindex persist`; Junos has kept them by default since 2010; others not documented | doc, 3rd |

## 4. Design rules for the poller (for any device)

These replace "design for Meraki and Sophos" as the basis of the poller's design. The real-device
surveys become confirmation, not a blocker.

1. **Credentials hold every combination the table in section 1 needs:**
   - Version: v2c, or v3 at any of the three levels (noAuthNoPriv, authNoPriv, authPriv).
   - Authentication: MD5, SHA, SHA-224/256/384/512.
   - Privacy: DES, 3DES, AES-128, AES-192, AES-256 and "AES-192/256 (Cisco)".
   - Separate authentication and privacy passwords, with "same as authentication" as an
     explicit choice (Meraki and UniFi use one).
   - Optional context name, port and timeout.

   authNoPriv is a normal choice, not an error. The form never assumes a vendor.
2. **Vendor-neutral by default.** Logic depends on what the device answers, never on its
   sysObjectID. The enterprise number in sysObjectID (Cisco 9, Juniper 2636, MikroTik 14988,
   Fortinet 12356, Palo Alto 25461, Ubiquiti 41112, HP 11, Aruba 14823, Meraki 29671,
   Cumulus 40310, Sophos 2604) is shown to the user and may pick a setup hint, nothing more.
3. **A per-device capability profile, learned and remembered.** The first contact runs what the
   survey does now and stores:
   - which groups answered (system, IF-MIB, ifXTable, IP-MIB, LLDP-MIB or LLDP-V2-MIB, CDP);
   - whether 64-bit counters exist;
   - the columns that must be walked alone;
   - whether GETBULK works and the max-repetitions that worked;
   - the reply time.

   Regular polls ask only for what the profile says works, so a device that drops a column
   costs the timeout once, not on every poll. The profile is refreshed after a reboot, when a
   poll starts failing, and once a day.
4. **Silence is normal, not proof the device is down.** The device is down only when the
   system group stops answering. A missing column or table is a capability, recorded in the
   profile. v3 silence after a successful engine discovery is reported as "credentials do
   not match".
5. **Interfaces are known by name as well as number.** Keep `ifIndex` and `ifName`/`ifDescr`
   together. After a reboot (sysUpTime or engine boots going back), re-read the interface
   table and re-match by name before using any counter. Manual names and speeds in
   `flow_interface_aliases` stay and win over SNMP.
6. **Counters:**
   - Use 64-bit counters wherever they exist.
   - Accept 32-bit counters only when the poll interval is shorter than their wrap time at
     the link's speed.
   - Drop a delta when a counter goes backwards, when the device rebooted, or when
     `ifCounterDiscontinuityTime` changed.
   - Some devices refresh counters only every 5 to 30 s, so the poll interval is 60 s or
     more and rates are averaged over at least one interval. No per-second graphs from SNMP.
7. **Neighbours from any source, matched carefully:**
   - Sources: LLDP-MIB, then LLDP-V2-MIB, then CDP.
   - Match a neighbour to a known endpoint by, in order:
     1. any of its management addresses;
     2. its chassis MAC against the `ifPhysAddress` of known devices;
     3. its system name.

     RouterOS gives no addresses, and CDP gives only one.
   - Match ports on `ifName` or `ifDescr`. CDP names ports long and LLDP short.
   - LLDP's local port number is not the ifIndex: map it through `lldpLocPortTable`.
8. **Gentle on devices:**
   - One request at a time per device.
   - GETBULK with adaptive max-repetitions, falling back to GETNEXT.
   - Default timeout of a few seconds with one retry; a profile can raise it for slow devices.
   - Never more than one full walk per device per interval.
9. **Setup help per family, not per-family code.** The SNMP guide documents where each family
   enables SNMP, binds it to a management VRF and allow-lists the poller, since most "no reply"
   cases are device-side configuration (section 3).

## 5. What this means for the code already written

Updated 2026-11-12, after building the additions:
- **Privacy:** AES-192/256 in both key methods (`aes192`/`aes256` Blumenthal,
  `aes192c`/`aes256c` Reeder) and 3DES.
  - The keys reproduce the drafts' published samples: AES draft A.1, 3DES draft B.1 and B.2.
  - The ciphers reproduce the NIST SP 800-38A vectors.
  - On a Cisco vIOS-L2 (GNS3), `aes256c`, `aes192c` (SHA and MD5) and `3des` worked. `aes256`
    and `aes192` got no reply, as expected for Cisco. So Cisco uses the Reeder method, and does
    not hash the 3DES salt.
- **Passwords and context:** one password for both (`priv_password=None`, `--priv-same`, or
  Enter at the prompt; checked on the vIOS-L2), and an SNMPv3 context name (`--context`).
- **What is read:** the LLDP-V2-MIB fallback, and `ifCounterDiscontinuityTime`.
- **Per-device profile:** `monitoring/snmp/profile.py`, written into every survey report. A
  client started from it skips the walks a device drops (no timeout) and stays on GETNEXT where
  GETBULK goes unanswered.
- **Still open:**
  - Blumenthal AES-192/256 has only been checked against the draft's sample, because no lab
    device offers it. FortiGate `aes256` or net-snmp would confirm it.
  - LLDP-V2-MIB has only been checked against the fake agent. The test devices answer LLDP-MIB.

## Sources

- Meraki: [SNMP Overview and Configuration](https://documentation.meraki.com/Platform_Management/Dashboard_Administration/Operate_and_Maintain/Monitoring_and_Reporting/SNMP_Overview_and_Configuration); [counters every ~30 s (community)](https://community.meraki.com/t5/Switching/SNMP-successfully-polling-only-every-30sec/m-p/140586)
- Sophos: [Ansible sfos_snmp_user (algorithm choices)](https://sophosfirewall-ansible.readthedocs.io/en/latest/sfos_snmp_user_module.html); [Avanet: SNMP hardware monitoring](https://www.avanet.com/en/kb/sophos-firewall-snmp-hardware-monitoring/); [Zabbix forum: SNMPv3 and Sophos](https://www.zabbix.com/forum/zabbix-help/479577-snmpv3-and-sophos-firewall); [Sophos community: interface traffic](https://community.sophos.com/sophos-xg-firewall/f/discussions/98743/snmp-query-interface-traffic)
- Juniper: [Configure SNMPv3](https://juniper.net/documentation/us/en/software/junos/network-mgmt/topics/topic-map/configure-snmpv3.html); [authentication and encryption types](https://www.juniper.net/documentation/us/en/software/junos/network-mgmt/topics/topic-map/configure-the-snmpv3-authentication-type-and-encryption-type.html); [SNMP for routing instances](https://www.juniper.net/documentation/us/en/software/junos/network-mgmt/topics/topic-map/configuring-snmp-for-routing-instances.html); [MIBs supported by Junos](https://www.juniper.net/documentation/us/en/software/junos/network-mgmt/topics/topic-map/snmp-mibs-supported-by-junos-os-and-junos-os-evolved.html); [juniper-nsp: counter refresh](https://puck.nether.net/pipermail/juniper-nsp/2014-May/028794.html); [Wikimedia: mgmt_junos community](https://phabricator.wikimedia.org/T269340)
- Aruba: [AOS-CX 10.16 Configuring SNMP](https://arubanetworking.hpe.com/techdocs/AOS-CX/10.16/HTML/snmp_mib/Content/Chp_SNMP/cnf-snm.htm); [CIS AOS-CX SNMPv3 audit](https://www.tenable.com/audits/items/CIS_HPE_Aruba_Networking_CX_Switch_v1.0.1_L2.audit:92c5143216289ef4b0e8a3972795a886); [Auvik: SNMP on CX 8100](https://support.auvik.com/hc/en-us/articles/38161689018260-How-to-enable-SNMP-on-Aruba-CX-8100-without-Aruba-Central); [2930F SNMPv3 anomaly (AES-256-C)](https://airheads.hpe.com/discussion/aruba-2930f-snmpv3-anomaly)
- Cumulus: [Configure SNMP](https://docs.nvidia.com/networking-ethernet-software/cumulus-linux-518/Monitoring-and-Troubleshooting/Simple-Network-Management-Protocol-SNMP/Configure-SNMP/); [Supported MIBs (4.4)](https://docs.nvidia.com/networking-ethernet-software/cumulus-linux-44/Monitoring-and-Troubleshooting/Simple-Network-Management-Protocol-SNMP/Supported-MIBs)
- UniFi: [SNMP Monitoring in UniFi Network](https://help.ui.com/hc/en-us/articles/33502980942615-SNMP-Monitoring-in-UniFi-Network); [Auvik: UniFi SNMP](https://support.auvik.com/hc/en-us/articles/360000842663); [LibreNMS: USW detection](https://community.librenms.org/t/ubiquiti-usw-48-poe-detected-as-generic-mips/26072)
- Cisco: [AES and 3DES for SNMPv3](https://www.cisco.com/en/US/docs/ios-xml/ios/snmp/configuration/15-2mt/nm-snmp-encryot-snmp-support.html); [field notice FN72509 (weak algorithms)](https://www.cisco.com/c/en/us/support/docs/field-notices/725/fn72509.html); [community: SHA-2 + AES-256](https://community.cisco.com/t5/switching/snmpv3-configuration/m-p/5316812); [community: AES-192/256 key localisation](https://community.cisco.com/t5/network-management/snmpv3-aes192-256-key-localization-not-done-via-aes-draft/td-p/2954763); [ifIndex persistence](https://www.cisco.com/c/en/us/support/docs/ip/simple-network-management-protocol-snmp/28420-ifIndex-Persistence.html); [NX-OS SNMP (Nexus 3000/9000)](https://developer.cisco.com/docs/cisco-nexus-3000-and-9000-series-nx-api-rest-sdk-user-guide-and-api-reference/latest/configuring-snmp)
- Fortinet: [SNMP examples (FortiOS 7.2.8)](https://docs.fortinet.com/document/fortigate/7.2.8/administration-guide/614237); [WhatsUp Gold: AES256 Cisco](https://community.fortinet.com/t5/FortiGate/Technical-Tip-Integration-between-FortiOS-and-WhatsUp-Gold-using/ta-p/192808); [Fortinet MIBs](https://help.fortinet.com/fos50hlp/50/FortiOS%205.0%20Help/monitoring.102.25.html)
- Palo Alto: [Configure SNMPv3 polling](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000ClHO)
- Standards: [draft-blumenthal-aes-usm-04](https://www.ietf.org/archive/id/draft-blumenthal-aes-usm-04.txt); [draft-reeder-snmpv3-usm-3desede-00](https://www.ietf.org/archive/id/draft-reeder-snmpv3-usm-3desede-00.txt); [LLDP-V2-MIB](https://mibs.observium.org/mib/LLDP-V2-MIB/); [H3C: LLDP 2005 MIB kept](https://www.h3c.com/en/d_202410/2282065_294551_0.htm); [Statseeker: ifIndex persistence](https://docs.statseeker.com/snmp-index-persistence/)
