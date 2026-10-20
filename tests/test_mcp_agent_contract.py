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
