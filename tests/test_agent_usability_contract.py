"""
Agent-usability contract: engineering values, diagnostics, aliases, and paging.

Rows mirror the regression suite in the third-party agent review. They are
deliberately end-to-end (through the real MCP tool functions) so a change in
the SQL, taxonomy, or response envelope cannot make them pass independently.
"""
from __future__ import annotations

import json

from mcp_server.server import (
    count_components,
    describe_catalog,
    health,
    list_spec_keys,
    search_by_mpn,
    search_components,
)


def _codes(res) -> list[str]:
    return [w["code"] for w in (res.warnings or [])]


def _count(category=None, filters=None, **kwargs) -> int:
    res = count_components(category=category, filters=filters, **kwargs)
    assert res.ok is True, res.message
    return int(res.total_matches)


# 1. Engineering suffixes -----------------------------------------------------

def test_resistance_suffixes_normalize_to_ohms():
    base = _count("resistors", {"resistance_ohm": 10000})
    assert base > 0
    for value in ("10k", "10K", "10kohm", "10 k\u03a9"):
        assert _count("resistors", {"resistance_ohm": value}) == base


def test_capacitance_suffixes_normalize_to_microfarads():
    assert _count("capacitors", {"capacitance_uf": "100nF"}) == _count(
        "capacitors", {"capacitance_uf": 0.1}
    )
    assert _count("capacitors", {"capacitance_uf": "1000pF"}) == _count(
        "capacitors", {"capacitance_uf": 0.001}
    )
    assert _count("capacitors", {"capacitance_uf": "100uF"}) == _count(
        "capacitors", {"capacitance_uf": 100}
    )


def test_inductance_suffix_normalizes_to_microhenries():
    assert _count("inductors", {"inductance_uh": "2.2mH"}) == _count(
        "inductors", {"inductance_uh": 2200}
    )


def test_invalid_numeric_value_explains_the_base_unit():
    res = count_components(
        category="resistors", filters={"resistance_ohm": "not-a-value"}
    )
    assert res.ok is False
    assert res.error_code == "invalid_filter_value"
    assert "base unit" in res.diagnosis["expected"].lower()
    assert res.suggestions


# 2. count/search diagnostics parity -----------------------------------------

def test_count_reports_filter_recall_gap_like_search():
    count = count_components(category="motor", filters={"torque_kgcm": {"gte": 4}})
    search = search_components(
        category="motor", filters={"torque_kgcm": {"gte": 4}}, refresh=False, limit=1
    )
    assert count.total_matches == search.total_matches
    assert "filter_recall_gap" in _codes(count)
    assert "filter_recall_gap" in _codes(search)
    assert count.data["filter_recall"]["missing_spec"] > 0
    motor_total = count_components(category="motor").total_matches
    assert count.data["filter_recall"]["base_matches"] == motor_total


def test_count_coverage_is_category_scoped():
    res = count_components(category="motor", filters={"torque_kgcm": {"gte": 4}})
    category = res.coverage["torque_kgcm"]["category"]
    motor_total = count_components(category="motor", include_related=True).total_matches
    assert category["of"] == motor_total
    assert category["scope"] == "discovery_category"
    assert 0 < category["present"] <= motor_total


def test_count_echoes_scope_and_query_status():
    res = count_components(query="motor", category="motor", filters={"voltage": "12"})
    assert res.filters_applied["filter_scope"] == "extracted_only"
    assert res.filters_applied["query_status"] in ("exact", "relaxed", "low_confidence")
    assert res.filters_applied["category_source"] == "argument"


# 3. Diagnosis precedence ----------------------------------------------------

def test_servo_voltage_is_reported_as_an_extraction_gap():
    res = count_components(
        query="sg90 servo", category="motor", filters={"voltage": "5"}
    )
    assert res.ok is True
    assert res.diagnosis["reason"] == "filter_key_not_extracted"
    assert res.diagnosis["missing_spec"] == res.diagnosis["base_matches"]
    assert res.diagnosis["base_matches"] > 0
    assert res.diagnosis["severity"] == "warning"


def test_unresolved_engineering_requirement_has_a_diagnosis():
    res = search_components(
        query="battery to power an arduino for 8 hours", refresh=False, limit=2
    )
    assert res.ok is True
    assert res.diagnosis["reason"] == "unresolved_engineering_constraints"
    assert res.diagnosis["unresolved_constraints"][0]["value_hours"] == 8
    assert res.data["requirements_verified"] is False


def test_unknown_mpn_has_a_diagnosis():
    res = search_by_mpn(mpn="ZZZZ-NOT-A-PART", refresh=False)
    assert res.ok is True
    assert res.total_matches == 0
    assert res.diagnosis["reason"] == "mpn_not_found"
    assert res.data["results"] == []
    assert res.data["listings"] == []


def test_valid_but_empty_intersection_is_no_matching_combination():
    res = count_components(
        category="resistors",
        filters={"resistance_ohm": {"gte": 1e9}, "package": "0201"},
    )
    assert res.ok is True
    assert res.total_matches == 0
    assert res.diagnosis["reason"] == "no_matching_combination"


# 4. Aliases -----------------------------------------------------------------

def test_unscoped_voltage_aliases_are_equal_and_disclosed():
    direct = count_components(filters={"voltage": 12})
    alias = count_components(filters={"voltage_v": 12})
    assert direct.total_matches == alias.total_matches
    assert direct.total_matches > 0
    expansion = alias.filters_applied["filter_expanded"]
    assert expansion["voltage"] == ["voltage", "voltage_v"]
    assert expansion["per_key_counts"]["voltage"] > 0
    assert expansion["per_key_counts"]["voltage_v"] > 0


def test_category_scoped_alias_routes_to_the_applicable_key():
    res = count_components(category="motor", filters={"voltage_v": 12})
    assert res.ok is True
    assert res.total_matches > 0
    applied = res.filters_applied["filter_alias_applied"]
    assert applied[0]["from"] == "voltage_v"
    assert applied[0]["to"] == "voltage"


# 5. Enum canonicalization ---------------------------------------------------

def test_through_hole_is_an_umbrella_not_an_empty_value():
    res = count_components(category="resistors", filters={"package": "through-hole"})
    assert res.ok is True
    assert res.total_matches > 0


def test_cells_numeric_value_coerces_to_the_canonical_enum():
    numeric = count_components(category="battery", filters={"cells": 3})
    enum = count_components(category="battery", filters={"cells": "3s"})
    assert numeric.ok is True and enum.ok is True
    assert numeric.total_matches == enum.total_matches
    assert numeric.filters_applied["filter_value_coerced"][0]["to"] == "3s"


def test_package_numeric_value_coerces_to_zero_padded_smd_code():
    numeric = count_components(category="resistors", filters={"package": 603})
    enum = count_components(category="resistors", filters={"package": "0603"})
    assert numeric.total_matches == enum.total_matches
    assert numeric.filters_applied["filter_value_coerced"][0]["to"] == "0603"


def test_extraction_canonicalizes_dirty_vendor_package_spellings():
    from src.spec.vendor_specs import _package

    assert _package("SOT-23(Surface Mount Device)", None) == "sot-23"
    assert _package("Axial Lead Through-Hole", None) == "axial"
    assert _package("TO-92 Through-Hole", None) == "to-92"


# 6. Query signals -----------------------------------------------------------

def test_query_narrowed_count_matches_the_returned_total():
    res = search_components(query="motor driver l298n", refresh=False, limit=2)
    warning = next(w for w in res.warnings if w["code"] == "query_narrowed")
    assert warning["total_matches"] == res.total_matches
    assert f"Only {res.total_matches} listing(s)" in warning["message"]


def test_single_token_typo_is_corrected_when_the_fix_is_much_more_common():
    res = search_components(query="ardunio", refresh=False, limit=2)
    warning = next(w for w in res.warnings if w["code"] == "query_spelling_corrected")
    assert warning["corrected_count"] > warning["original_count"] * 10
    assert res.filters_applied["query_effective"] == "arduino"


def test_category_inference_is_disclosed_when_it_changes_results():
    res = search_components(
        query="battery to power an arduino for 8 hours", refresh=False, limit=2
    )
    warning = next(w for w in res.warnings if w["code"] == "category_inferred")
    assert warning["category"] == "battery"
    assert warning["total_without_category"] != warning["total_with_category"]


# 7. Response ergonomics -----------------------------------------------------

def test_compact_search_payload_is_under_25kb():
    res = search_components(limit=50, compact=True, refresh=False)
    payload = json.dumps(res.model_dump(), default=str).encode()
    assert len(payload) < 25_000
    assert set(res.data["results"][0]) == {
        "product_id", "title", "vendor", "price", "currency",
        "stock_status", "mpn", "specs", "product_url",
    }


def test_summary_catalog_payload_is_under_6kb_and_exposes_aliases():
    res = describe_catalog(summary=True)
    payload = json.dumps(res.model_dump(), default=str).encode()
    assert len(payload) < 6_000
    assert res.data["spec_columns"][0] == "key"
    assert res.data["aliases"]["voltage"]["keys"] == ["voltage", "voltage_v"]


def test_list_spec_keys_is_marked_deprecated():
    res = list_spec_keys()
    assert res.ok is True
    assert res.data["deprecated"] is True
    assert res.data["replacement"] == "describe_catalog(summary=True)"
    assert "deprecated_tool" in _codes(res)


# 8. Count and search parity -------------------------------------------------

def test_count_and_search_totals_agree_for_filter_combinations():
    cases = [
        ("resistors", {"resistance_ohm": "10k"}),
        ("capacitors", {"capacitance_uf": "100nF"}),
        ("motor", {"voltage": 12}),
        ("battery", {"cells": 3}),
        ("resistors", {"package": "through-hole"}),
    ]
    for category, filters in cases:
        count = count_components(category=category, filters=filters)
        search = search_components(
            category=category, filters=filters, refresh=False, limit=1
        )
        assert count.total_matches == search.total_matches, (category, filters)


# 9. Do-not-regress rows -----------------------------------------------------

def test_family_token_is_still_rejected():
    res = search_by_mpn(mpn="ESP32", refresh=False)
    assert res.ok is False
    assert res.error_code == "mpn_family_token"


def test_boolean_syntax_is_still_rejected():
    res = search_components(query="esp32 OR esp8266", refresh=False)
    assert res.ok is False
    assert res.error_code == "unsupported_query_syntax"


def test_tolerance_still_requires_a_fraction():
    rejected = count_components(filters={"rpm": {"eq": 200, "tolerance": 5}})
    accepted = count_components(filters={"rpm": {"eq": 200, "tolerance": 0.05}})
    assert rejected.ok is False
    assert accepted.ok is True


# 10. Freshness and price policy --------------------------------------------

def test_health_exposes_spec_index_freshness():
    res = health()
    assert res.ok is True
    assert res.data["spec_index_updated_at"]
    assert "resistance_ohm" in res.data["spec_backfill_pct_by_key"]


def test_count_discloses_zero_price_policy():
    res = count_components(filters={"package": "0603"})
    assert res.filters_applied["price_zero_excluded"] is False
    assert "zero_price" in res.filters_applied["price_zero_policy"]
    assert res.data["zero_price_rows_in_catalog"] >= 0
