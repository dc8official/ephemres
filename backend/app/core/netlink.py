from __future__ import annotations

import fcntl
import ipaddress
import os
import socket
import struct
import time

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B

_cached_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None
_cache_timestamp: float = 0.0


def get_local_interface_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    Enumerates all local directly-connected IPv4 and IPv6 interface subnets
    using Linux kernel procfs (/proc/net/route, /proc/net/if_inet6) and
    ioctl (SIOCGIFADDR / SIOCGIFNETMASK) over /sys/class/net/*.
    Cached with a 30-second monotonic TTL. Zero external dependencies.
    """
    global _cached_networks, _cache_timestamp
    now = time.monotonic()
    if _cached_networks is not None and (now - _cache_timestamp) < 30.0:
        return list(_cached_networks)

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    seen: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

    def _add_net(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
        if net not in seen:
            seen.add(net)
            networks.append(net)

    # Always include IPv4 and IPv6 loopback networks
    _add_net(ipaddress.ip_network("127.0.0.0/8"))
    _add_net(ipaddress.ip_network("::1/128"))

    # 1. Parse /proc/net/route for directly-connected IPv4 interface subnets
    #    where Gateway == 00000000 and Mask != 00000000
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[1:]:
            fields = line.strip().split()
            if len(fields) >= 8:
                dest_hex = fields[1]
                gw_hex = fields[2]
                mask_hex = fields[7]
                if gw_hex == "00000000" and mask_hex != "00000000":
                    try:
                        dest_ip = socket.inet_ntoa(struct.pack("<I", int(dest_hex, 16)))
                        mask_ip = socket.inet_ntoa(struct.pack("<I", int(mask_hex, 16)))
                        net = ipaddress.ip_network(f"{dest_ip}/{mask_ip}", strict=False)
                        _add_net(net)
                    except Exception:
                        pass
    except Exception:
        pass

    # 2. Query SIOCGIFADDR and SIOCGIFNETMASK via fcntl.ioctl for /sys/class/net/* interfaces
    try:
        if os.path.isdir("/sys/class/net"):
            ifaces = os.listdir("/sys/class/net")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                fd = sock.fileno()
                for ifname in ifaces:
                    try:
                        ifreq = struct.pack("256s", ifname.encode("utf-8")[:15])
                        res_addr = fcntl.ioctl(fd, SIOCGIFADDR, ifreq)
                        ip_str = socket.inet_ntoa(res_addr[20:24])
                        try:
                            res_mask = fcntl.ioctl(fd, SIOCGIFNETMASK, ifreq)
                            mask_str = socket.inet_ntoa(res_mask[20:24])
                        except Exception:
                            mask_str = "255.0.0.0" if ip_str.startswith("127.") else "255.255.255.255"
                        net = ipaddress.ip_network(f"{ip_str}/{mask_str}", strict=False)
                        _add_net(net)
                    except Exception:
                        continue
    except Exception:
        pass

    # 3. Parse /proc/net/if_inet6 for IPv6 interface addresses and prefix lengths
    try:
        if os.path.isfile("/proc/net/if_inet6"):
            with open("/proc/net/if_inet6", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 6:
                        hex_addr = parts[0]
                        prefix_hex = parts[2]
                        try:
                            raw_bytes = bytes.fromhex(hex_addr)
                            ipv6_addr = ipaddress.IPv6Address(raw_bytes)
                            prefix_len = int(prefix_hex, 16)
                            net6 = ipaddress.ip_network(f"{ipv6_addr}/{prefix_len}", strict=False)
                            _add_net(net6)
                        except Exception:
                            pass
    except Exception:
        pass

    _cached_networks = list(networks)
    _cache_timestamp = now
    return networks


get_local_interfaces_and_subnets = get_local_interface_networks


def is_local_subnet_destination(target_ip: str) -> bool:
    """
    Determines if target_ip belongs to loopback or any local directly-connected
    interface subnet on the host.
    """
    try:
        target_obj = ipaddress.ip_address(target_ip.strip())
        if target_obj.is_loopback:
            return True
        for local_net in get_local_interface_networks():
            if target_obj.version == local_net.version and target_obj in local_net:
                return True
    except Exception:
        pass
    return False
