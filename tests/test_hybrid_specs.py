"""
Hybrid specification retrieval: promoted specs first, normalized vendor specs
as a disclosed fallback, and explicit vendor filters for the long tail.
"""
from __future__ import annotations

import json

from mcp_server.server import (
    count_components,
    describe_spec,
    search_components,
)


def test_vendor_only_attribute_is_queryable_on_a_gap_category():
    res = count_components(
        category="battery",
        filters={"max_discharge_current_a": {"gte": 20}},
    )
    assert res.ok is True
    assert res.total_matches > 0
    assert res.filters_applied["filter_mode"] == "hybrid"
    assert res.filters_applied["vendor_filters"]["max_discharge_current_a"]["gte"] == 20.0
    assert res.filters_applied["filter_routed_to_vendor"][0]["tier"] == "vendor"


def test_microcontroller_pins_are_available_without_a_taxonomy_entry():
    res = count_components(category="microcontrollers", filters={"pins": {"gte": 8}})
    assert res.ok is True
    assert res.total_matches > 0
    assert "pins" in res.filters_applied["vendor_filters"]


def test_manufacturer_is_a_global_vendor_key():
    res = count_components(filters={"manufacturer": "3peak"})
    assert res.ok is True
    assert res.total_matches > 0
    assert res.filters_applied["vendor_filters"]["manufacturer"] == "3peak"


def test_unscoped_ambiguous_key_is_not_silently_routed():
    # "torque" exists for motor shafts and connector screws. Without a category
    # the safe behavior is still to reject it and suggest torque_kgcm.
    res = search_components(filters={"torque": {"gte": 2}}, refresh=False)
    assert res.ok is False
    assert res.error_code == "unknown_filter_key"


def test_include_vendor_specs_false_is_promoted_only():
    hybrid = count_components(category="motor", filters={"voltage": 12})
    promoted = count_components(
        category="motor", filters={"voltage": 12}, include_vendor_specs=False
    )
    assert hybrid.ok is True and promoted.ok is True
    assert promoted.total_matches <= hybrid.total_matches
    assert promoted.filters_applied["filter_mode"] == "promoted"


def test_vendor_filter_count_and_search_agree():
    count = count_components(
        category="battery", filters={"max_discharge_current_a": {"gte": 20}}
    )
    search = search_components(
        category="battery",
        filters={"max_discharge_current_a": {"gte": 20}},
        limit=5,
        refresh=False,
    )
    assert count.total_matches == search.total_matches
    assert search.returned == min(5, search.total_matches)


def test_vendor_filtered_rows_carry_provenance_and_values():
    res = search_components(
        category="microcontrollers", filters={"pins": {"gte": 8}}, limit=3, refresh=False
    )
    assert res.ok is True
    assert res.data["results"]
    for row in res.data["results"]:
        assert "pins" in row["specs"]
        assert row["spec_sources"]["pins"] == "vendor"
        assert "pins" in row["vendor_specs"]


def test_vendor_layer_is_discoverable_through_describe_spec():
    res = describe_spec("manufacturer", vendor=True)
    assert res.ok is True
    assert res.data["tier"] == "vendor"
    assert res.data["coverage_count"] > 0
    assert res.data["values"]


def test_filter_recall_discloses_vendor_fallback():
    res = count_components(category="motor", filters={"voltage": 12})
    recall = res.data["filter_recall"]
    assert recall["mode"] == "hybrid"
    assert recall["hybrid_matches"] >= recall["promoted_matches"]
    assert "vendor_fallback_matches" in recall
    assert "promoted_missing" in recall


def test_unknown_explicit_vendor_filter_is_rejected():
    res = count_components(vendor_filters={"definitely_not_a_spec": 1})
    assert res.ok is False
    assert res.error_code == "unknown_filter_key"
    assert res.details["tier"] == "vendor"


def test_compact_search_does_not_expand_with_vendor_specs():
    res = search_components(
        category="microcontrollers",
        filters={"pins": {"gte": 8}},
        limit=5,
        compact=True,
        refresh=False,
    )
    payload = json.dumps(res.model_dump(), default=str)
    assert res.ok is True
    assert "vendor_specs" not in res.data["results"][0]
    assert len(payload.encode()) < 25_000
