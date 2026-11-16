"""Names for outside addresses (decided 2026-11-15, build plan 11b; review O5): the providers'
published ranges first, reverse DNS in the background for the rest."""
import asyncio
import subprocess
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


def test_a_resolver_that_does_not_answer_holds_only_its_own_threads(monkeypatch):
    """First-phase review M2: lookups ran in the event loop's default thread pool, and the 2 s
    limit freed the waiter but not the thread. With a dead resolver the pool filled, and
    everything else that uses it (FastStore commands, PDF rendering, to_thread) waited 8 s."""
    import threading
    import time

    answer = threading.Event()
    lock = threading.Lock()
    running = {"now": 0, "most": 0}

    def stuck_reverse(address):
        with lock:
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
        answer.wait(10)
        with lock:
            running["now"] -= 1
        return "late-pc.corp.example" if address == "10.9.0.0" else None

    monkeypatch.setattr(address_names, "_reverse", stuck_reverse)
    monkeypatch.setattr(address_names, "DNS_TIMEOUT_SECONDS", 0.05)
    addresses = [f"10.9.0.{i}" for i in range(40)]

    async def go():
        try:
            assert address_names.names_for(addresses) == {}
            # Several times the limit: the old code had handed every lookup to the shared pool by now.
            await asyncio.sleep(0.5)
            started = time.monotonic()
            assert await asyncio.to_thread(lambda: "free") == "free"
            waited = time.monotonic() - started
            # The first eight are past their limit: no name for now, and not asked for again.
            assert address_names._cached_dns("10.9.0.0", time.monotonic()) == (True, None)
            assert address_names.names_for(addresses[:8]) == {}
        finally:
            answer.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if address_names._cached_dns("10.9.0.0", time.monotonic())[1]:
                break
        return waited, address_names.names_for(["10.9.0.0"])

    waited, late = asyncio.run(go())
    assert waited < 0.3
    # Never more lookups at once than their own threads, however many are queued.
    assert running["most"] == address_names.DNS_PARALLEL
    # The resolver's late answer replaced "no name".
    assert late == {"10.9.0.0": {"name": "late-pc.corp.example", "source": "dns"}}


@pytest.mark.parametrize("content", [
    "[1, 2, 3]",                                   # valid JSON, no object (review L3: AttributeError)
    '"provider ranges"',
    "null",
    '{"names": ["Zoom"], "v4": [[1, 2]]}',         # a row with a field missing
    '{"names": ["Zoom"], "v4": [5]}',              # a row that is no list
    '{"names": ["Zoom"], "v4": [["1", "9", 3]]}',  # names an entry the list does not have
    '{"names": 7, "v4": []}',
    '{"names": ["Zoom"], "v4": [["a", "b", 0]]}',
    "{not json",
])
def test_a_table_file_of_any_shape_costs_only_the_provider_names(tmp_path, monkeypatch, content):
    path = tmp_path / "ranges.json"
    path.write_text(content)
    monkeypatch.setattr(address_names, "TABLE", path)
    assert address_names.provider_name("0.0.0.5") is None
    assert address_names.names_for(["0.0.0.5", "52.97.146.2"]) == {}


def test_the_dns_cache_stays_within_its_limit(monkeypatch):
    """Review L3: nothing ever left the cache, and a scan shows new addresses all day."""
    import time

    monkeypatch.setattr(address_names, "DNS_CACHE_LIMIT", 100)
    now = time.monotonic()
    # Twenty answers already past their time, then far more than the limit.
    for i in range(20):
        address_names._dns_cache[f"10.0.0.{i}"] = (None, now - 1)
    address_names._remember("10.1.0.1", "kept-pc.corp.example")
    for i in range(1000):
        address_names._remember(f"10.2.{i // 250}.{i % 250}", None)
    assert len(address_names._dns_cache) <= 100
    assert "10.0.0.3" not in address_names._dns_cache
    # A name is kept a day and "no name" an hour, so the names outlast the rest.
    assert address_names._dns_cache["10.1.0.1"][0] == "kept-pc.corp.example"
    assert "10.2.3.249" in address_names._dns_cache  # the newest answer


def test_without_an_event_loop_nothing_is_queued():
    # Called from synchronous code (a script): provider names only, no crash.
    assert address_names.names_for(["192.0.2.7"]) == {}


def _lists(folder, amazon_extra=()):
    """The six files deploy/update-provider-ranges.sh reads with --from-dir, tiny ones."""
    folder.mkdir(exist_ok=True)
    (folder / "microsoft.json").write_text(json.dumps([
        {"serviceArea": "Exchange", "ips": ["52.96.0.0/14"]}, {"serviceArea": "Skype", "ips": ["52.112.0.0/14"]}]))
    (folder / "google.json").write_text(json.dumps({"prefixes": [{"ipv4Prefix": "8.8.8.0/24"}]}))
    (folder / "amazon.json").write_text(json.dumps({
        "prefixes": [{"ip_prefix": "18.64.0.0/14", "service": "AMAZON"},
                     {"ip_prefix": "18.64.0.0/16", "service": "CLOUDFRONT"}, *amazon_extra],
        "ipv6_prefixes": [{"ipv6_prefix": "2600:1f00::/24", "service": "AMAZON"}]}))
    (folder / "cloudflare-v4.txt").write_text("104.16.0.0/13\n")
    (folder / "cloudflare-v6.txt").write_text("2606:4700::/32\n")
    (folder / "zoom.txt").write_text("170.114.0.0/16\n")
    return folder


def _script(*args):
    return subprocess.run(["bash", str(ROOT / "deploy" / "update-provider-ranges.sh"), *map(str, args)],
                          capture_output=True, text=True)


def test_the_script_builds_the_table_and_the_more_specific_name_wins(tmp_path):
    out = tmp_path / "ranges.json"
    run = _script("--from-dir", _lists(tmp_path / "lists"), "--output", out)
    assert run.returncode == 0, run.stderr
    data = json.loads(out.read_text())
    names = data["names"]
    rows = [(ipaddress.ip_address(int(a)), ipaddress.ip_address(int(b)), names[i]) for a, b, i in data["v4"]]
    # AWS's AMAZON /14 covers CloudFront's /16: CloudFront keeps its name there.
    assert (ipaddress.ip_address("18.64.0.0"), ipaddress.ip_address("18.64.255.255"), "Amazon CloudFront") in rows
    assert (ipaddress.ip_address("18.65.0.0"), ipaddress.ip_address("18.67.255.255"), "Amazon AWS") in rows
    assert {r[2] for r in rows} == {"Microsoft 365 (Exchange)", "Microsoft Teams", "Google", "Amazon CloudFront",
                                    "Amazon AWS", "Cloudflare", "Zoom"}
    assert len(data["v6"]) == 2


def test_check_writes_nothing_and_a_missing_list_keeps_the_old_table(tmp_path):
    out = tmp_path / "ranges.json"
    out.write_text("old")
    lists = _lists(tmp_path / "lists")
    assert _script("--check", "--from-dir", lists, "--output", out).returncode == 0
    assert out.read_text() == "old"
    (lists / "zoom.txt").unlink()
    run = _script("--from-dir", lists, "--output", out)
    assert run.returncode != 0 and "left as it is" in run.stderr
    assert out.read_text() == "old"


def test_the_api_picks_up_a_replaced_table_without_a_restart(tmp_path, monkeypatch):
    _table(tmp_path, monkeypatch, rows_v4=[[_ip("52.96.0.0"), _ip("52.99.255.255"), 0]])
    monkeypatch.setattr(address_names, "TABLE_CHECK_SECONDS", 0)
    assert address_names.provider_name("170.114.52.2") is None
    run = _script("--from-dir", _lists(tmp_path / "lists"), "--output", address_names.TABLE)
    assert run.returncode == 0, run.stderr
    assert address_names.provider_name("170.114.52.2") == "Zoom"
