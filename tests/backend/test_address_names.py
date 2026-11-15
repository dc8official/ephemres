"""Names for outside addresses (decided 2026-11-15, build plan 11b; review O5): the providers'
published ranges first, reverse DNS in the background for the rest."""
import asyncio
import importlib.util
import ipaddress
import json
from pathlib import Path

import pytest

from app.services import address_names

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh():
    address_names.reset_for_tests()
    yield
    address_names.reset_for_tests()


def _table(tmp_path, monkeypatch, rows_v4=(), rows_v6=(), names=("Microsoft 365", "Zoom")):
    path = tmp_path / "ranges.json"
    path.write_text(json.dumps({"names": list(names), "v4": list(rows_v4), "v6": list(rows_v6)}))
    monkeypatch.setattr(address_names, "TABLE", path)


def _ip(a):
    return str(int(ipaddress.ip_address(a)))


def test_an_address_in_a_published_range_is_named(tmp_path, monkeypatch):
    _table(tmp_path, monkeypatch, rows_v4=[[_ip("52.96.0.0"), _ip("52.99.255.255"), 0],
                                           [_ip("170.114.0.0"), _ip("170.114.255.255"), 1]])
    assert address_names.provider_name("52.97.146.2") == "Microsoft 365"
    assert address_names.provider_name("52.97.146.2/32") == "Microsoft 365"
    assert address_names.provider_name("170.114.52.2") == "Zoom"
    assert address_names.provider_name("52.100.0.1") is None  # past the range's end
    assert address_names.provider_name("10.0.0.1") is None
    assert address_names.provider_name("not an address") is None


def test_the_shipped_table_names_the_reviews_example():
    # The review's "52.97.146.2 → 10.1.20.12 is Abuja, but nothing says what it is".
    assert address_names.provider_name("52.97.146.2").startswith("Microsoft")


def test_without_a_table_only_reverse_dns_names(tmp_path, monkeypatch):
    monkeypatch.setattr(address_names, "TABLE", tmp_path / "missing.json")
    assert address_names.provider_name("52.97.146.2") is None


def test_unknown_addresses_are_resolved_in_the_background_and_named_next_time(tmp_path, monkeypatch):
    _table(tmp_path, monkeypatch, rows_v4=[[_ip("52.96.0.0"), _ip("52.99.255.255"), 0]])
    asked = []

    def fake_reverse(address):
        asked.append(address)
        return {"10.0.20.15": "acc-pc-15.corp.example"}.get(address)

    monkeypatch.setattr(address_names, "_reverse", fake_reverse)

    async def go():
        first = address_names.names_for(["52.97.146.2", "10.0.20.15", "41.58.10.5"])
        # A provider name at once; the rest are not waited for.
        assert first == {"52.97.146.2": {"name": "Microsoft 365", "source": "provider"}}
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(address_names._dns_cache) == 2:
                break
        second = address_names.names_for(["52.97.146.2", "10.0.20.15", "41.58.10.5"])
        third = address_names.names_for(["10.0.20.15", "41.58.10.5"])
        return second, third

    second, third = asyncio.run(go())
    assert second["10.0.20.15"] == {"name": "acc-pc-15.corp.example", "source": "dns"}
    assert "41.58.10.5" not in second  # no name: remembered, not asked again
    assert third == {"10.0.20.15": {"name": "acc-pc-15.corp.example", "source": "dns"}}
    assert sorted(asked) == ["10.0.20.15", "41.58.10.5"]  # each asked once; never the provider one


def test_without_an_event_loop_nothing_is_queued():
    # Called from synchronous code (a script): provider names only, no crash.
    assert address_names.names_for(["192.0.2.7"]) == {}


def test_the_build_keeps_the_more_specific_provider(monkeypatch):
    spec = importlib.util.spec_from_file_location("upr", ROOT / "devtools" / "update_provider_ranges.py")
    upr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upr)
    # AWS's AMAZON set covers its CloudFront ranges; CloudFront is the useful name there.
    monkeypatch.setattr(upr, "SOURCES", {"Amazon": lambda: iter([
        ("Amazon AWS", "18.64.0.0/14"), ("Amazon CloudFront", "18.64.0.0/16"), ("Amazon AWS", "2600:1f00::/24"),
    ])})
    names, ranges, counts = upr.build()
    rows = [(ipaddress.ip_address(int(a)), ipaddress.ip_address(int(b)), names[i]) for a, b, i in ranges["v4"]]
    assert rows == [
        (ipaddress.ip_address("18.64.0.0"), ipaddress.ip_address("18.64.255.255"), "Amazon CloudFront"),
        (ipaddress.ip_address("18.65.0.0"), ipaddress.ip_address("18.67.255.255"), "Amazon AWS"),
    ]
    assert len(ranges["v6"]) == 1 and counts == {"Amazon": 3}
