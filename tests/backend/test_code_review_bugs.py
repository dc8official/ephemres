import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.asgi import HTTPException
from app.core.schema import ValidationError

from app.routers.reports import parse_datetime_param
from app.routers.users import reset_password
from app.routers.endpoints import update_endpoint, UpdateEndpointRequest
from app.schemas.common import PaginationMeta
from monitoring.ping import PingResult, classify_ping_result


class TestCodeReviewBugFixes(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_invalid_date_parsing_returns_400(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            parse_datetime_param("invalid-date-string", is_end=False)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Invalid ISO 8601 date format", cm.exception.detail)

    def test_zero_stddev_baseline_jitter_floor(self) -> None:
        result = PingResult(success_count=5, failed_count=0, avg_rtt_ms=1.10)
        # Mean = 1.0, stddev = 0.00
        op, detailed = classify_ping_result(result, baseline_mean=1.0, baseline_stddev=0.0)
        # With 2.0ms floor, threshold is 1.0 + 3*2.0 = 7.0ms -> 1.10ms should be UP, not UP-UNSTABLE
        self.assertEqual(op, "UP")
        self.assertEqual(detailed, "UP")

    def test_admin_self_reset_protection(self) -> None:
        mock_req = MagicMock()
        mock_req.password = "newpass123"
        admin_id = uuid4()
        current_user = {"sub": str(admin_id), "role": "ADMIN"}
        mock_db = AsyncMock()

        with self.assertRaises(HTTPException) as cm:
            self.loop.run_until_complete(
                reset_password(
                    user_id=admin_id,
                    request=mock_req,
                    current_user=current_user,
                    db=mock_db,
                )
            )

        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("own password", cm.exception.detail)

    def test_cyclic_parent_relationship_protection(self) -> None:
        ep_id = uuid4()
        mock_req = UpdateEndpointRequest(manual_parent_id=ep_id)
        current_user = {"sub": str(uuid4()), "role": "ADMIN"}
        mock_db = AsyncMock()
        
        db_res = MagicMock()
        db_res.fetchone.return_value = MagicMock(id=str(ep_id))
        mock_db.execute.return_value = db_res

        with self.assertRaises(HTTPException) as cm:
            self.loop.run_until_complete(
                update_endpoint(
                    endpoint_id=ep_id,
                    request=mock_req,
                    current_user=current_user,
                    db=mock_db,
                )
            )

        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("own parent", cm.exception.detail)

    def test_pagination_meta_field_constraints(self) -> None:
        with self.assertRaises(ValidationError):
            PaginationMeta(total=-1, page=1, page_size=10, total_pages=1)


if __name__ == "__main__":
    unittest.main()
