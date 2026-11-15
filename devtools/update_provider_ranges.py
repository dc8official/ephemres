"""Rebuilds backend/app/data/provider_ranges.json from the providers' published IP ranges.

Run before a release (decided 2026-11-15, build plan 11b): the Traffic page names an outside
address such as 52.97.146.2 "Microsoft 365" from this table, with no lookup at run time, so a
branch server needs no Internet access for it. Standard library only.

    python devtools/update_provider_ranges.py            # download and write the table
    python devtools/update_provider_ranges.py --check    # download and report, write nothing

Sources (all public, no account needed):
- Microsoft 365: endpoints.office.com, worldwide instance (each service area named)
- Google: www.gstatic.com/ipranges/goog.json (Google's own services, not customer cloud)
- Amazon: ip-ranges.amazonaws.com (CloudFront named apart from the rest of AWS)
- Cloudflare: www.cloudflare.com/ips-v4 and ips-v6
- Zoom: assets.zoom.us/docs/ipranges/Zoom.txt
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import sys
import urllib.request
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "backend" / "app" / "data" / "provider_ranges.json"
AGENT = "ephemres-provider-ranges/1.0"

MICROSOFT_AREAS = {"Exchange": "Microsoft 365 (Exchange)", "SharePoint": "Microsoft 365 (SharePoint)",
                   "Skype": "Microsoft Teams", "Common": "Microsoft 365"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def microsoft():
    url = f"https://endpoints.office.com/endpoints/worldwide?clientrequestid={uuid.uuid4()}"
    for entry in json.loads(_get(url)):
        name = MICROSOFT_AREAS.get(entry.get("serviceArea"), "Microsoft 365")
        for net in entry.get("ips", []):
            yield name, net


def google():
    for p in json.loads(_get("https://www.gstatic.com/ipranges/goog.json")).get("prefixes", []):
        net = p.get("ipv4Prefix") or p.get("ipv6Prefix")
        if net:
            yield "Google", net


def amazon():
    data = json.loads(_get("https://ip-ranges.amazonaws.com/ip-ranges.json"))
    for p in data.get("prefixes", []) + data.get("ipv6_prefixes", []):
        net = p.get("ip_prefix") or p.get("ipv6_prefix")
        if net:
            yield ("Amazon CloudFront" if p.get("service") == "CLOUDFRONT" else "Amazon AWS"), net


def cloudflare():
    for url in ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"):
        for line in _get(url).decode().split():
            yield "Cloudflare", line.strip()


def zoom():
    for line in _get("https://assets.zoom.us/docs/ipranges/Zoom.txt").decode().split():
        if line.strip():
            yield "Zoom", line.strip()


SOURCES = {"Microsoft 365": microsoft, "Google": google, "Amazon": amazon, "Cloudflare": cloudflare, "Zoom": zoom}


def build():
    """{"v4"|"v6": [[first, last, name_index], ...] sorted, non-overlapping}, names, counts.

    A network listed by two providers keeps the more specific one (the longer prefix): AWS's
    AMAZON set covers its CloudFront ranges, and CloudFront is the useful name there.
    """
    by_name: dict[str, list] = {}
    counts = {}
    for label, fetch in SOURCES.items():
        n = 0
        for name, net in fetch():
            try:
                by_name.setdefault(name, []).append(ipaddress.ip_network(net, strict=False))
                n += 1
            except ValueError:
                continue
        counts[label] = n
    names = sorted(by_name)
    out = {"v4": [], "v6": []}
    for family, version in (("v4", 4), ("v6", 6)):
        # Most specific first, so a CloudFront /16 isn't hidden behind AMAZON's covering /11.
        nets = sorted(((net, names.index(name)) for name, ns in by_name.items()
                       for net in ipaddress.collapse_addresses(n for n in ns if n.version == version)),
                      key=lambda x: -x[0].prefixlen)
        taken: list[tuple[int, int]] = []
        rows = []
        for net, idx in nets:
            first, last = int(net.network_address), int(net.broadcast_address)
            # Cut out what a more specific network already holds.
            pieces = [(first, last)]
            for a, b in taken:
                pieces = [p for lo, hi in pieces for p in ((lo, min(hi, a - 1)), (max(lo, b + 1), hi)) if p[0] <= p[1]
                          ] if not (b < first or a > last) else pieces
            for lo, hi in pieces:
                rows.append([lo, hi, idx])
                taken.append((lo, hi))
        rows.sort()
        out[family] = [[str(lo), str(hi), idx] for lo, hi, idx in rows]  # strings: v6 ints exceed JSON numbers
    return names, out, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="download and report only")
    args = ap.parse_args()
    names, ranges, counts = build()
    print("networks per source:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"ranges: {len(ranges['v4'])} IPv4, {len(ranges['v6'])} IPv6; names: {', '.join(names)}")
    if args.check:
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": datetime.date.today().isoformat(),
        "sources": list(SOURCES),
        "names": names,
        "v4": ranges["v4"],
        "v6": ranges["v6"],
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
