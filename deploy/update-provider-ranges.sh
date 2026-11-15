#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ephemres Network Monitoring Platform - Provider Address Ranges Update
# Usage: sudo /opt/ephemres/deploy/update-provider-ranges.sh [--check] [--from-dir DIR] [--output FILE]
#
# The Traffic page names outside addresses from the address ranges the big providers publish
# (52.97.146.2 is "Microsoft 365 (Exchange)"): Microsoft 365 and Teams, Google, AWS and
# CloudFront, Cloudflare, Zoom. The table ships with each release in
# backend/app/data/provider_ranges.json; providers add ranges now and then, so run this before
# a release, or on a server whenever names look stale. The API picks up the new table within
# five minutes; no restart is needed.
#
#   --check          download and report, change nothing
#   --from-dir DIR   use lists already downloaded into DIR (a server without Internet access):
#                    microsoft.json, google.json, amazon.json, cloudflare-v4.txt,
#                    cloudflare-v6.txt, zoom.txt, as fetched from the URLs below
#   --output FILE    write somewhere else (default: this install's backend/app/data/)
#
# All or nothing: if any list can't be fetched or read, the table in place is left as it is.
# Needs curl (unless --from-dir) and python3 (standard library only).
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${SCRIPT_DIR}/../backend/app/data/provider_ranges.json"
CHECK=false
FROM_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK=true ;;
        --from-dir) FROM_DIR="${2:?--from-dir needs a directory}"; shift ;;
        --output) OUTPUT="${2:?--output needs a file}"; shift ;;
        -h|--help) sed -n '4,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

# The install's own interpreter when there is one (a server), else the system's.
PYTHON="python3"
[[ -x "${SCRIPT_DIR}/../venv/bin/python3" ]] && PYTHON="${SCRIPT_DIR}/../venv/bin/python3"
command -v "${PYTHON}" >/dev/null 2>&1 || { echo "python3 is needed." >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

if [[ -n "${FROM_DIR}" ]]; then
    SRC="${FROM_DIR}"
else
    command -v curl >/dev/null 2>&1 || { echo "curl is needed (or use --from-dir)." >&2; exit 1; }
    SRC="${WORK}"
    # Microsoft asks every client for a request id (any GUID).
    REQUEST_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || "${PYTHON}" -c 'import uuid; print(uuid.uuid4())')"
    fetch() {
        echo "Downloading $2..."
        if ! curl -fsSL --retry 2 --max-time 120 -A "ephemres-provider-ranges/1.0" -o "${WORK}/$1" "$2"; then
            echo "Could not download $2: the table in place is left as it is." >&2
            exit 1
        fi
    }
    fetch microsoft.json "https://endpoints.office.com/endpoints/worldwide?clientrequestid=${REQUEST_ID}"
    fetch google.json "https://www.gstatic.com/ipranges/goog.json"
    fetch amazon.json "https://ip-ranges.amazonaws.com/ip-ranges.json"
    fetch cloudflare-v4.txt "https://www.cloudflare.com/ips-v4"
    fetch cloudflare-v6.txt "https://www.cloudflare.com/ips-v6"
    fetch zoom.txt "https://assets.zoom.us/docs/ipranges/Zoom.txt"
fi

# Reduce the lists to one table: per address family, sorted non-overlapping ranges
# [first, last, name index]. A network two providers list keeps the more specific name (AWS's
# AMAZON set covers CloudFront's ranges, and CloudFront is the useful name there).
"${PYTHON}" - "${SRC}" "${OUTPUT}" "${CHECK}" <<'PY'
import datetime
import ipaddress
import json
import os
import sys
import tempfile

src, output, check = sys.argv[1], sys.argv[2], sys.argv[3] == "true"
MICROSOFT_AREAS = {"Exchange": "Microsoft 365 (Exchange)", "SharePoint": "Microsoft 365 (SharePoint)",
                   "Skype": "Microsoft Teams", "Common": "Microsoft 365"}


def read(name):
    with open(os.path.join(src, name), encoding="utf-8") as f:
        return f.read()


def lists():
    for entry in json.loads(read("microsoft.json")):
        for net in entry.get("ips", []):
            yield "Microsoft 365", MICROSOFT_AREAS.get(entry.get("serviceArea"), "Microsoft 365"), net
    for p in json.loads(read("google.json")).get("prefixes", []):
        yield "Google", "Google", p.get("ipv4Prefix") or p.get("ipv6Prefix")
    data = json.loads(read("amazon.json"))
    for p in data.get("prefixes", []) + data.get("ipv6_prefixes", []):
        name = "Amazon CloudFront" if p.get("service") == "CLOUDFRONT" else "Amazon AWS"
        yield "Amazon", name, p.get("ip_prefix") or p.get("ipv6_prefix")
    for f in ("cloudflare-v4.txt", "cloudflare-v6.txt"):
        for line in read(f).split():
            yield "Cloudflare", "Cloudflare", line
    for line in read("zoom.txt").split():
        yield "Zoom", "Zoom", line


try:
    by_name, counts = {}, {}
    for source, name, net in lists():
        if not net:
            continue
        try:
            by_name.setdefault(name, []).append(ipaddress.ip_network(net.strip(), strict=False))
        except ValueError:
            continue
        counts[source] = counts.get(source, 0) + 1
except (OSError, ValueError, AttributeError) as exc:
    sys.exit(f"Could not read the lists ({exc}): the table in place is left as it is.")
if len(counts) < 5:
    sys.exit(f"Only {', '.join(counts) or 'no'} lists gave ranges: the table in place is left as it is.")

names = sorted(by_name)
table = {}
for family, version in (("v4", 4), ("v6", 6)):
    nets = sorted(((net, names.index(name)) for name, ns in by_name.items()
                   for net in ipaddress.collapse_addresses(n for n in ns if n.version == version)),
                  key=lambda x: -x[0].prefixlen)
    taken, rows = [], []
    for net, idx in nets:
        pieces = [(int(net.network_address), int(net.broadcast_address))]
        for a, b in taken:
            pieces = [p for lo, hi in pieces for p in ((lo, min(hi, a - 1)), (max(lo, b + 1), hi)) if p[0] <= p[1]]
        for lo, hi in pieces:
            rows.append((lo, hi, idx))
            taken.append((lo, hi))
    # Strings: IPv6 addresses don't fit in a JSON number.
    table[family] = [[str(lo), str(hi), idx] for lo, hi, idx in sorted(rows)]

print("Networks per source: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
print(f"Ranges: {len(table['v4'])} IPv4, {len(table['v6'])} IPv6; names: {', '.join(names)}")
if check:
    print("--check: nothing written.")
    sys.exit(0)

body = json.dumps({"generated": datetime.date.today().isoformat(),
                   "sources": ["Microsoft 365", "Google", "Amazon", "Cloudflare", "Zoom"],
                   "names": names, "v4": table["v4"], "v6": table["v6"]}, separators=(",", ":")) + "\n"
# Written beside the old table and renamed over it, so the API never reads half a file; the old
# file's owner and mode are kept (ephemres on a server).
folder = os.path.dirname(os.path.abspath(output))
os.makedirs(folder, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=folder, prefix=".provider_ranges.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(body)
if os.path.exists(output):
    st = os.stat(output)
    os.chmod(tmp, st.st_mode & 0o777)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except PermissionError:
        pass
else:
    os.chmod(tmp, 0o644)
os.replace(tmp, output)
print(f"Wrote {os.path.abspath(output)} ({len(body) // 1024} KB). The API uses it within five minutes.")
PY
