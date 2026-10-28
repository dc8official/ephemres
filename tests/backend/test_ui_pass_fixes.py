"""Fixes from the v3.3.5 UI pass against a real systemd install.

- Restarting ephemres-api in FastStore mode signed everyone out: the first initialize() copied
  PostgreSQL's (older) session lists over FastStore's.
- Exporters enrolled or linked as aliases stayed under "Unmatched Flow Exporters" for 24 h.
- The topology map opened empty and its link tooltips (flow status) were never visible.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.bandwidth import _forget_registered_exporters
from app.services.driver_manager import StorageDriverManager

ROOT = Path(__file__).resolve().parents[2]


def _settings_session(performance_mode: str):
    row = SimpleNamespace(setting_value=performance_mode)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result

    class Ctx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    return lambda: Ctx()


class FakeFastStoreClient:
    def __init__(self, *args, **kwargs):
        self.ping = AsyncMock()
        self.aclose = AsyncMock()


@pytest.mark.anyio
async def test_process_start_in_faststore_mode_keeps_faststore_sessions():
    manager = StorageDriverManager()
    with patch("app.services.driver_manager.AsyncSessionLocal", _settings_session("true")), \
         patch("app.core.faststore.FastStoreClient", FakeFastStoreClient), \
         patch("app.services.driver_manager.migrate_sessions_pg_to_faststore", AsyncMock()) as to_faststore:
        await manager.initialize()
    assert manager.driver_mode == "redis"
    to_faststore.assert_not_awaited()


@pytest.mark.anyio
async def test_switching_faststore_on_at_runtime_still_copies_sessions():
    manager = StorageDriverManager()
    with patch("app.services.driver_manager.AsyncSessionLocal", _settings_session("false")):
        await manager.initialize()
    with patch("app.services.driver_manager.AsyncSessionLocal", _settings_session("true")), \
         patch("app.core.faststore.FastStoreClient", FakeFastStoreClient), \
         patch("app.services.driver_manager.migrate_sessions_pg_to_faststore", AsyncMock()) as to_faststore:
        await manager.initialize()
    to_faststore.assert_awaited_once()


@pytest.mark.anyio
async def test_registered_exporter_addresses_leave_the_unmatched_lists():
    db = AsyncMock()
    rows = MagicMock()
    rows.all.return_value = [("172.17.0.1", ["127.0.0.1"]), ("192.168.100.1", ["10.77.0.2/32"])]
    db.execute.return_value = rows
    store = AsyncMock()
    store.zrangebyscore.return_value = [b"127.0.0.1", b"203.0.113.9"]
    store.smembers.return_value = {b"10.77.0.2", b"198.51.100.4"}

    await _forget_registered_exporters(store, db)

    store.zrem.assert_awaited_once_with("flow:unmatched:zset", b"127.0.0.1")
    store.srem.assert_awaited_once_with("flow:unmatched:set", b"10.77.0.2")


def test_topology_map_opens_on_the_graph_with_visible_tooltips():
    view = (ROOT / "frontend/src/components/TopologyMap.vue").read_text(encoding="utf-8")
    assert "import 'vis-network/styles/vis-network.min.css'" in view
    assert ":deep(.vis-tooltip)" in view
    done = view.index("network.on('stabilizationIterationsDone'")
    assert "network.fit()" in view[done:done + 600]


def test_drawer_labels_last_seen_as_the_state_start():
    # last_seen is the latest event's start_time: "Last Contact" showed the start of a weeks-long
    # UP period as if the endpoint had not answered since.
    drawer = (ROOT / "frontend/src/components/EndpointInspectorDrawer.vue").read_text(encoding="utf-8")
    assert "Last Contact" not in drawer
    assert "In State Since" in drawer and "formatTimeAgo(props.endpoint?.last_seen)" in drawer
