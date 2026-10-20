"""
Agent-contract regression tests for the MCP server.

PR 1 of the remediation sequence: request validation and the `diagnosis`
contract. These tests encode the failures two independent agent testers found
while exercising the server, so the next regression is caught here rather than
in production.
"""
from __future__ import annotations

import json

from mcp_server.server import (
    count_components,
    get_product,
    search_by_mpn,
    search_components,
)


def _codes(res) -> list[str]:
    return [w["code"] for w in (res.warnings or [])]


# ---------------------------------------------------------------------------
# Filter applicability: a key must be valid *for the requested category*.
# ---------------------------------------------------------------------------

def test_chemistry_is_rejected_for_motor_category():
    res = count_components(category="motor", filters={"chemistry": "lipo"})

    assert res.ok is False
    assert res.error_code == "filter_not_applicable_to_category"
    assert res.diagnosis["reason"] == "filter_not_applicable_to_category"
    assert res.diagnosis["category"] == "motor"
    assert res.diagnosis["not_applicable_keys"] == ["chemistry"]
    assert "rpm" in res.diagnosis["applicable_keys"]
    assert res.diagnosis["filter_support"] == "partial"


def test_rpm_is_rejected_for_battery_category():
    res = count_components(category="battery", filters={"rpm": {"gte": 100}})

    assert res.ok is False
    assert res.error_code == "filter_not_applicable_to_category"
    assert res.diagnosis["not_applicable_keys"] == ["rpm"]
    assert res.diagnosis["filter_support"] == "partial"


def test_filter_on_category_without_attributes_reports_filter_support_none():
    res = count_components(category="resistors", filters={"voltage": "5"})

    assert res.ok is False
    assert res.error_code == "filter_not_applicable_to_category"
    assert res.diagnosis["filter_support"] == "none"
    assert res.diagnosis["applicable_keys"] == []


def test_relay_only_advertises_channels():
    res = count_components(category="relay", filters={"current_a": {"gte": 10}})

    assert res.ok is False
    assert res.error_code == "filter_not_applicable_to_category"
    assert res.diagnosis["applicable_keys"] == ["channels"]


def test_applicable_filter_still_works():
    res = count_components(category="motor", filters={"voltage": "12"})

    assert res.ok is True
    assert res.total_matches > 0
    assert res.filters_applied["filter_support"] == "partial"


# ---------------------------------------------------------------------------
# Tolerance is a fraction of eq, not an absolute value.
# ---------------------------------------------------------------------------

def test_tolerance_above_one_is_rejected():
    res = count_components(filters={"rpm": {"eq": 200, "tolerance": 5}})

    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert "fraction" in res.message.lower()
    assert res.diagnosis["expected"] == "0 < tolerance <= 1 (fraction of eq)"


def test_tolerance_fraction_is_a_relative_window():
    res = search_components(
        filters={"rpm": {"eq": 200, "tolerance": 0.05}},
        limit=50,
        refresh=False,
    )

    assert res.ok is True
    assert res.total_matches > 0
    for row in res.data["results"]:
        assert 190.0 <= float(row["specs"]["rpm"]) <= 210.0


def test_tolerance_without_eq_is_rejected():
    res = count_components(filters={"rpm": {"tolerance": 0.05}})

    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert "without 'eq'" in res.message


# ---------------------------------------------------------------------------
# Invalid numeric values must not look like "no such part exists".
# ---------------------------------------------------------------------------

def test_non_numeric_value_for_numeric_key_is_rejected():
    res = count_components(filters={"rpm": "abc"})

    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert res.diagnosis["expected_type"] == "integer"
    assert res.diagnosis["provided_value"] == "abc"


def test_non_numeric_bound_is_rejected():
    res = count_components(filters={"rpm": {"gte": "fast"}})

    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert res.diagnosis["operator"] == "gte"


# ---------------------------------------------------------------------------
# Category resolution: case, plural and typo tolerant.
# ---------------------------------------------------------------------------

def test_category_is_case_and_plural_tolerant():
    for name in ("Motor", "motors", "MOTORS"):
        res = search_components(category=name, limit=1, refresh=False)
        assert res.ok is True, f"{name!r} should resolve: {res.message}"
        assert res.filters_applied["category"] == "motor"


def test_unknown_category_suggests_the_nearest_category():
    res = search_components(category="motr", limit=1, refresh=False)

    assert res.ok is False
    assert res.error_code == "unknown_category"
    assert res.diagnosis["reason"] == "unknown_category"
    assert res.suggestions[0]["category"] == "motor"


# ---------------------------------------------------------------------------
# Price bounds.
# ---------------------------------------------------------------------------

def test_negative_price_max_is_rejected():
    res = count_components(price_max=-5)

    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert res.diagnosis["key"] == "price_max"
    assert res.diagnosis["expected"] == ">= 0"


# ---------------------------------------------------------------------------
# Diagnosis must be present on empty or unresolved results.
# ---------------------------------------------------------------------------

def test_unresolved_query_has_a_diagnosis():
    res = search_components(query="zzqx flurble", limit=1, refresh=False)

    assert res.ok is True
    assert res.total_matches == 0
    assert res.diagnosis["reason"] == "query_unresolved"


def test_enum_suggestions_are_scoped_to_the_requested_category():
    res = search_components(
        category="battery",
        filters={"chemistry": "zzzz"},
        limit=1,
        refresh=False,
    )

    assert res.ok is True
    assert res.total_matches == 0
    assert res.diagnosis["reason"] == "filter_value_not_present"
    suggestion_values = {s["value"] for s in res.data["suggestions"] if s.get("key") == "chemistry"}
    # Battery chemistry values only; a motor/battery mixed global list would be wrong.
    assert "lipo" in suggestion_values
    assert suggestion_values <= {"lipo", "liion", "lifepo4", "nimh", "leadacid"}


def test_error_envelope_and_json_are_serializable():
    res = search_components(category="motor", filters={"chemistry": "lipo"}, refresh=False)

    assert res.ok is False
    payload = res.model_dump()
    assert payload["diagnosis"]["reason"] == "filter_not_applicable_to_category"
    json.dumps(payload)


# ---------------------------------------------------------------------------
# PR 2: query semantics
# ---------------------------------------------------------------------------

def test_boolean_operator_is_rejected_not_silently_anded():
    res = search_components(query="esp32 OR esp8266", refresh=False)

    assert res.ok is False
    assert res.error_code == "unsupported_query_syntax"
    assert res.diagnosis["operators"] == ["OR"]


def test_quoted_phrase_is_rejected_not_silently_stripped():
    res = search_components(query='"dc geared motor"', refresh=False)

    assert res.ok is False
    assert res.error_code == "unsupported_query_syntax"
    assert '"' in res.diagnosis["characters"]


def test_lowercase_boolean_word_warns_instead_of_silently_anding():
    res = search_components(query="esp32 or esp8266", refresh=False, limit=1)

    assert res.ok is True
    assert "query_operator_dropped" in _codes(res)


def test_natural_language_query_relaxes_to_a_category_not_the_whole_catalog():
    res = search_components(
        query="battery to power an arduino for 8 hours", refresh=False, limit=3
    )

    assert res.ok is True
    assert res.filters_applied["category"] == "battery"
    assert res.filters_applied["category_source"] == "query_head"
    assert res.total_matches < 10000, (
        "relaxation must not fall back to a near-whole-catalog result"
    )
    assert res.data["query_status"] in ("relaxed", "low_confidence")
    assert "query_relaxed" in _codes(res)
    assert res.data["query_tokens_dropped"]


def test_over_narrow_query_gets_a_narrowing_warning():
    res = search_components(query="motor for robot arm", refresh=False, limit=3)

    assert res.ok is True
    assert "query_narrowed" in _codes(res)
    warning = next(w for w in res.warnings if w["code"] == "query_narrowed")
    assert warning["head_token"] == "motor"
    assert warning["head_matches"] > res.total_matches * 10


def test_cheapest_intent_changes_sort_to_price_asc():
    res = search_components(query="cheapest 4 channel relay", refresh=False, limit=5)

    assert res.ok is True
    assert res.filters_applied["sort"] == "price_asc"
    assert res.filters_applied["sort_source"] == "query_intent"
    assert "sort_inferred" in _codes(res)
    assert res.data["results"]
    for row in res.data["results"]:
        assert row["price"] > 0
        assert "relay" in row["title"].lower()


def test_price_sort_excludes_zero_price_services():
    res = search_components(
        query="ESP32", sort="price_asc", refresh=False, limit=20
    )

    assert res.ok is True
    assert res.data["results"]
    assert all(row["price"] > 0 for row in res.data["results"])
    assert res.filters_applied["price_zero_excluded"] is True


def test_budget_in_query_is_applied_as_price_max():
    res = count_components(query="resistor under 500")

    assert res.ok is True
    assert res.filters_applied["price_max"] == 500.0
    assert res.filters_applied["price_max_source"] == "query"


# ---------------------------------------------------------------------------
# PR 3: identity and price semantics
# ---------------------------------------------------------------------------

def test_family_token_is_not_an_mpn():
    res = search_by_mpn(mpn="ESP32", refresh=False)

    assert res.ok is False
    assert res.error_code == "mpn_family_token"
    assert res.diagnosis["reason"] == "mpn_family_token"
    assert any(s.get("tool") == "search_components" for s in res.suggestions)


def test_real_mpn_is_still_exact():
    res = search_by_mpn(mpn="MPU6050", refresh=False, limit=2)

    assert res.ok is True
    assert res.total_matches > 0
    assert res.data["items_are"] == "listings"
    assert res.data["total_listings"] == res.total_matches


def test_ambiguous_mpn_can_be_opted_into_with_a_warning():
    res = search_by_mpn(mpn="ESP32", refresh=False, limit=2, allow_ambiguous=True)

    assert res.ok is True
    assert "mpn_ambiguous" in _codes(res)


def test_listing_prices_carry_pack_and_unit_basis():
    res = search_components(query="pack of 10 resistor", refresh=False, limit=20)

    assert res.ok is True
    assert res.data["total_listings"] == res.total_matches
    packed = [r for r in res.data["results"] if r.get("pack_quantity")]
    assert packed, "at least one pack listing should expose its quantity"
    row = packed[0]
    assert row["price_basis"] == "per_pack"
    assert row["unit_price"] == round(row["price"] / row["pack_quantity"], 4)
    assert row["price_is_quote"] is False


# ---------------------------------------------------------------------------
# PR 4: one response contract
# ---------------------------------------------------------------------------

def test_list_tools_expose_items_and_keep_legacy_alias():
    res = search_components(query="relay", refresh=False, limit=2)

    assert res.ok is True
    assert res.data["items_are"] == "listings"
    assert res.data["items"] == res.data["results"]


def test_unclassified_product_has_no_phantom_spec_gaps():
    # 72 is the QuartzComponents 18AWG wire: classifiable as `wires`, but the
    # taxonomy has no attribute set for it, so no "missing battery spec" list.
    res = get_product(product_id=72, refresh=False)

    assert res.ok is True
    assert res.data["spec_extraction_scope"] == "unclassified"
    assert res.data["specs_not_extracted"] == []
    assert res.data["specs"] == {
        key: str(value["value"]) for key, value in res.data["spec_details"].items()
    }


def test_coverage_has_catalog_and_page_denominators():
    res = search_components(
        filters={"current_a": {"gte": 1}},
        refresh=False,
        limit=2,
    )

    assert res.ok is True
    cov = res.coverage["current_a"]
    assert "catalog" in cov and "page" in cov
    assert {"present", "of", "pct"} <= set(cov["catalog"])
    assert cov["page"]["of"] == res.returned


# ---------------------------------------------------------------------------
# PR 5: recall transparency
# ---------------------------------------------------------------------------

def test_filtered_search_reports_products_dropped_for_missing_specs():
    res = search_components(
        query="servo",
        filters={"torque_kgcm": {"gte": 1}},
        refresh=False,
        limit=2,
    )

    assert res.ok is True
    assert res.filters_applied["filter_scope"] == "extracted_only"
    recall = res.data["filter_recall"]
    assert recall["scope"] == "extracted_only"
    assert recall["base_matches"] == 704
    assert recall["missing_spec"] > 500
    assert recall["missing_spec"] + res.total_matches <= recall["base_matches"]
    assert "filter_recall_gap" in _codes(res)


# ---------------------------------------------------------------------------
# PR 6: category ownership and accessory contamination
# ---------------------------------------------------------------------------

def test_category_filter_uses_canonical_category_not_extraction_scope():
    res = search_components(
        category="battery",
        filters={"voltage": {"gte": 5}},
        refresh=False,
        limit=10,
    )

    assert res.ok is True
    assert res.total_matches > 0
    for row in res.data["results"]:
        assert row["category_canonical"] == "battery"
    titles = " ".join(row["title"].lower() for row in res.data["results"])
    assert "snap connector" not in titles


def test_describe_catalog_advertises_filter_support_per_category():
    from mcp_server.server import describe_catalog

    res = describe_catalog()
    assert res.ok is True
    support = {
        c["id"]: c["filter_support"] for c in res.data["taxonomy"]
    }
    assert support["battery"] == "partial"
    assert support["resistors"] == "none"
