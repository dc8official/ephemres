import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.main import app, lifespan


def test_main_module_imports_asyncio():
    """Verify that backend/app/main.py explicitly imports the asyncio module."""
    import app.main
    assert "asyncio" in app.main.__dict__, "asyncio is not imported in app.main"
    assert app.main.asyncio is asyncio


@pytest.mark.anyio
async def test_app_lifespan_startup_and_shutdown():
    """Verify that lifespan(app) starts and stops background tasks cleanly without NameError."""
    mock_session = AsyncMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    async def fake_sync_listener(session_factory):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    with patch("app.main.check_database_connection", new_callable=AsyncMock), \
         patch("app.main.driver_manager.initialize", new_callable=AsyncMock), \
         patch("app.main.AsyncSessionLocal", return_value=mock_session_ctx), \
         patch("app.main.baseline_cache.refresh_from_db", new_callable=AsyncMock), \
         patch("app.main.topology_manager.full_rebuild", new_callable=AsyncMock), \
         patch("app.main.telemetry_relay.start", new_callable=AsyncMock), \
         patch("app.main.telemetry_relay.stop", new_callable=AsyncMock), \
         patch("app.main.alert_dispatcher.start", new_callable=AsyncMock), \
         patch("app.main.alert_dispatcher.stop", new_callable=AsyncMock), \
         patch("app.services.settings_sync.start_settings_sync_listener", side_effect=fake_sync_listener), \
         patch("app.main.start_baseline_refresh_task", new_callable=AsyncMock) as mock_refresh, \
         patch("app.main.start_discovery_worker", new_callable=AsyncMock) as mock_discovery, \
         patch("app.main.start_midnight_discovery_worker", new_callable=AsyncMock) as mock_midnight, \
         patch("app.main.start_diagnostic_cleanup_task", new_callable=AsyncMock) as mock_cleanup, \
         patch("app.main.scheduler_leader.start", new_callable=AsyncMock) as mock_leader_start, \
         patch("app.main.scheduler_leader.stop", new_callable=AsyncMock) as mock_leader_stop:

        dummy_task1 = asyncio.create_task(asyncio.sleep(10))
        dummy_task2 = asyncio.create_task(asyncio.sleep(10))
        dummy_task3 = asyncio.create_task(asyncio.sleep(10))
        dummy_task4 = asyncio.create_task(asyncio.sleep(10))

        mock_refresh.return_value = dummy_task1
        mock_discovery.return_value = dummy_task2
        mock_midnight.return_value = dummy_task3
        mock_cleanup.return_value = dummy_task4

        # Enter and exit the FastAPI lifespan context manager
        async with lifespan(app):
            # Assert background tasks were initiated
            assert not dummy_task1.cancelled()

        mock_leader_start.assert_awaited_once()
        mock_leader_stop.assert_awaited_once()
        # After exiting lifespan, cleanup/cancel should have been triggered
        assert dummy_task1.cancelling() > 0 or dummy_task1.cancelled()
        assert dummy_task2.cancelling() > 0 or dummy_task2.cancelled()
        assert dummy_task3.cancelling() > 0 or dummy_task3.cancelled()
        assert dummy_task4.cancelling() > 0 or dummy_task4.cancelled()

        # Let the event loop process task cancellation
        await asyncio.sleep(0)
        assert dummy_task1.cancelled()
        assert dummy_task2.cancelled()
        assert dummy_task3.cancelled()
        assert dummy_task4.cancelled()
