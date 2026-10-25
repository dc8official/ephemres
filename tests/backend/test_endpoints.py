from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.routers.endpoints import _bg_run_initial_discovery


class TestEndpointCreationAsync(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    @patch("app.database.AsyncSessionLocal")
    @patch("app.services.baseline_route.refresh_baseline_route")
    @patch("app.services.topology.topology_manager.full_rebuild")
    def test_bg_run_initial_discovery_uses_new_session(
        self,
        mock_full_rebuild: AsyncMock,
        mock_refresh_route: AsyncMock,
        mock_session_local: MagicMock,
    ) -> None:
        # Mock independent DB session context manager
        mock_bg_db = AsyncMock()
        mock_bg_db.add = MagicMock()
        mock_session_local.return_value.__aenter__.return_value = mock_bg_db

        endpoint_id = uuid4()
        ip_address = "8.8.8.8"

        self.loop.run_until_complete(
            _bg_run_initial_discovery(endpoint_id, ip_address)
        )

        # Verify AsyncSessionLocal was instantiated
        mock_session_local.assert_called_once()

        # Verify refresh_baseline_route was called with new bg_db
        mock_refresh_route.assert_called_once_with(endpoint_id, ip_address, db=mock_bg_db)

        # Verify bg_db was committed
        mock_bg_db.commit.assert_called_once()

        # Verify full_rebuild was called with bg_db
        mock_full_rebuild.assert_called_once_with(mock_bg_db)

    @patch("app.database.AsyncSessionLocal")
    @patch("app.services.baseline_route.refresh_baseline_route")
    @patch("app.services.topology.topology_manager.full_rebuild")
    def test_bg_run_initial_discovery_handles_exception(
        self,
        mock_full_rebuild: AsyncMock,
        mock_refresh_route: AsyncMock,
        mock_session_local: MagicMock,
    ) -> None:
        mock_bg_db = AsyncMock()
        mock_bg_db.add = MagicMock()
        mock_session_local.return_value.__aenter__.return_value = mock_bg_db
        mock_refresh_route.side_effect = Exception("Network timeout")

        endpoint_id = uuid4()
        ip_address = "1.1.1.1"

        # Should not raise exception
        self.loop.run_until_complete(
            _bg_run_initial_discovery(endpoint_id, ip_address)
        )

        mock_refresh_route.assert_called_once()


if __name__ == "__main__":
    unittest.main()
