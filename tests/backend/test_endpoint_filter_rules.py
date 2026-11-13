"""The shared endpoint filter without a database (v3.3.6 build plan, phase 2).

Matching itself runs in PostgreSQL and is tested in tests/integration/test_endpoint_filter.py.
Here: how values are normalised, how a filter is built from request values and described on a
PDF, the SQL it produces, and that tags survive a backup's text form on restore.
"""
import pytest

from app.core.orm import CompileContext, select
from app.core.restore import _prepare_column_val
from app.models.endpoint import Endpoint
from app.services.endpoint_filter import (
    EndpointFilter,
    conditions,
    normalise_label,
    normalise_tag,
    normalise_tags,
)


def test_labels_keep_case_and_collapse_spaces():
    assert normalise_label("  Building   A ") == "Building A"
    assert normalise_label("   ") is None
    assert normalise_label(None) is None


def test_tags_are_lower_case_deduplicated_and_ordered():
    assert normalise_tags(["MPLS", " mpls ", "Core  Router", "", None]) == ["mpls", "core router"]


@pytest.mark.parametrize("bad", ["a,b", 'say "hi"', "back\\slash", "{x}"])
def test_tags_refuse_characters_that_break_arrays(bad):
    with pytest.raises(ValueError):
        normalise_tag(bad)


def test_too_many_tags_is_refused():
    with pytest.raises(ValueError):
        normalise_tags([f"t{i}" for i in range(21)])


def test_build_accepts_single_values_and_drops_duplicates_case_insensitively():
    f = EndpointFilter.build(site="Abuja", location=["HQ", "hq"], tag=["MPLS"], type=None, q="  gw  ")
    assert f.sites == ("Abuja",)
    assert f.locations == ("HQ",)
    assert f.tags == ("mpls",)
    assert f.device_types == ()
    assert f.q == "gw"
    assert not f.is_empty()
    assert EndpointFilter.build().is_empty()


def test_description_for_people():
    f = EndpointFilter.build(site=["Abuja"], tag=["mpls", "core"], q="gw")
    assert f.describe() == 'Site Abuja · Tags mpls, core · Search "gw"'
    assert EndpointFilter().describe() == "All endpoints"


def test_sql_is_any_within_a_field_and_all_across():
    f = EndpointFilter.build(site=["Abuja", "Lagos"], tag=["mpls", "core"], q="50%_x")
    ctx = CompileContext()
    sql = select(Endpoint.id).where(*conditions(f)).compile(ctx)
    params = ctx.params if hasattr(ctx, "params") else ctx.__dict__.get("params")
    assert 'LOWER("endpoints"."site") IN' in sql
    assert '"endpoints"."tags" &&' in sql
    assert " AND " in sql and "endpoint_status" in sql
    # The user's % and _ are literal in the search pattern.
    assert any(isinstance(p, str) and p == "%50\\%\\_x%" for p in params)
    assert any(p == ["mpls", "core"] for p in params)


def test_restore_reads_tags_from_a_backup():
    assert _prepare_column_val('{mpls,"core router"}', "_text") == ["mpls", "core router"]
    assert _prepare_column_val("{}", "_text") == []
