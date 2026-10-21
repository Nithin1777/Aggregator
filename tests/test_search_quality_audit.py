"""Regressions from the v2.2.0 black-box engineering search audit."""
import pytest

from mcp_server import server


@pytest.mark.parametrize("tool", [server.search_components, server.count_components])
@pytest.mark.parametrize("mounting", ["surface mount", "definitely absent mounting"])
def test_vendor_only_filter_always_returns_envelope(tool, mounting):
    kwargs = {"refresh": False} if tool is server.search_components else {}
    result = tool(category="switches", filters={"mounting_type": mounting}, **kwargs)
    assert result.ok, result.message
    recall = result.data["filter_recall"]
    assert recall["missing_spec"] >= 0
    assert recall["base_matches"] == (
        result.total_matches + recall["missing_spec"] + recall["filtered_out_for_value"]
    )


def test_runtime_remains_an_unresolved_requirement():
    result = server.search_components(query="battery to power an arduino for 8 hours", refresh=False)
    requirement = result.data["unresolved_constraints"][0]
    assert requirement["kind"] == "runtime"
    assert requirement["value_hours"] == 8
    assert requirement["load_target"] == "arduino"
    assert "load_current_a" in requirement["requires"]
    assert result.data["requirements_verified"] is False


def test_health_reports_product_coverage_percentage():
    result = server.health()
    assert 0 <= result.data["spec_backfill_pct"] <= 100
    assert result.data["spec_coverage"]["unit"] == "products"


@pytest.mark.parametrize("category", ["motor", "battery"])
def test_component_category_rejects_related_products_at_every_price(category):
    from src.search.product_role import classify_role

    result = server.search_components(category=category, sort="price_asc", limit=50, refresh=False)
    expected = {"motor"} if category == "motor" else {"battery_pack", "battery_cell"}
    assert result.ok
    for row in result.data["results"]:
        assert classify_role(row["title"], category) in expected
        assert row["product_role"] in expected


def test_discovery_exposes_vendor_attributes_without_filters():
    result = server.search_components(category="microcontrollers", query="ESP32", refresh=False)
    assert result.ok
    for row in result.data["results"]:
        assert "available_vendor_spec_keys" in row
        assert set(row["vendor_specs"]) == set(row["available_vendor_spec_keys"])


def test_vendor_filter_counts_agree_with_search():
    params = dict(category="switches", filters={"mounting_type": "surface mount"})
    assert server.count_components(**params).total_matches == server.search_components(
        **params, refresh=False
    ).total_matches
