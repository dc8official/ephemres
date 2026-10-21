"""GET /endpoints: status filters hit the right columns and pagination meta reports real totals."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.orm import select
from app.models.endpoint import Endpoint
from app.repositories.endpoint_repo import EndpointRepository


def _where_sql(status):
    return str(select(Endpoint.id).where(*EndpointRepository._list_filters(status)))


@pytest.mark.parametrize(
    "status, expected, unexpected",
    [
        ("ACTIVE", "endpoint_status", "monitoring_enabled"),
        ("DISABLED", "endpoint_status", "monitoring_enabled"),
        ("MONITORED", "monitoring_enabled", None),
        ("UNMONITORED", "monitoring_enabled", None),
    ],
)
def test_status_filter_targets_correct_column(status, expected, unexpected):
    sql = _where_sql(status)
    assert expected in sql
    if unexpected:
        assert unexpected not in sql
    if status in ("MONITORED", "UNMONITORED"):
        # Only the soft-delete guard (!=) may compare endpoint_status
        assert "endpoints.endpoint_status != " in sql
        assert "endpoints.endpoint_status = " not in sql


def _call_list(page, page_size, total, rows=()):
    from app.routers import endpoints as router

    with patch.object(router.EndpointRepository, "list_with_stats", AsyncMock(return_value=list(rows))), patch.object(
        router.EndpointRepository, "count_listed", AsyncMock(return_value=total)
    ) as count, patch.object(router, "get_service_gap_intervals", AsyncMock(return_value=[])):
        resp = asyncio.run(
            router.list_endpoints(status=None, page=page, page_size=page_size, current_user={"role": "VIEWER"}, db=MagicMock())
        )
    return resp.meta, count


def test_paginated_meta_reports_real_total():
    meta, count = _call_list(page=2, page_size=10, total=25)
    count.assert_awaited_once()
    assert (meta.total, meta.page, meta.page_size, meta.total_pages) == (25, 2, 10, 3)


def test_unpaginated_meta_counts_returned_rows_without_extra_query():
    meta, count = _call_list(page=None, page_size=None, total=999)
    count.assert_not_called()
    assert (meta.total, meta.page, meta.total_pages) == (0, 1, 1)
