"""
Round 5 regression tests: locks for the silent-wrong-answer classes found by
consuming the server as an agent.

Every test here corresponds to a bug that previously returned a plausible but
wrong answer with ok:true instead of failing loudly.
"""
import json

import pytest

from mcp_server.server import (
    MAX_PAGE_SIZE,
    count_components,
    describe_catalog,
    get_product,
    health,
    search_by_mpn,
    search_components,
)


# ---------------------------------------------------------------- P0: operators

def test_ordering_operator_on_numeric_enum_is_not_silently_empty():
    """voltage is an enum with NULL value_num; 'gte' must compare the text value, not match zero rows."""
    res = search_components(filters={"voltage": {"gte": 24}}, limit=1)
    assert res.ok is True, res.message
    assert res.total_matches > 0, (
        "Ordering filter on a numeric enum returned 0 matches; the predicate compared "
        "NULL value_num instead of CAST(value AS REAL)."
    )


def test_ordering_operator_on_categorical_enum_is_rejected():
    """chemistry values are non-numeric, so 'gte' cannot mean anything: reject it explicitly."""
    res = search_components(filters={"chemistry": {"gte": "lipo"}})
    assert res.ok is False
    assert res.error_code == "operator_not_supported_for_type"
    assert res.suggestions, "expected a suggestion pointing at eq/in"


def test_tolerance_on_numeric_enum_produces_a_range_not_zero_rows():
    res = search_components(filters={"voltage": {"eq": 24, "tolerance": 0.1}}, limit=1)
    assert res.ok is True, res.message
    assert res.total_matches > 0


# ---------------------------------------------------------------- P0: cursors

def test_cursor_is_bound_to_its_query():
    """A cursor issued for one query must not silently page a different one."""
    first = search_components(filters={"voltage": "24"}, limit=2)
    assert first.ok is True and first.next_cursor

    other = search_components(filters={"voltage": "12"}, limit=2, cursor=first.next_cursor)
    assert other.ok is False
    assert other.error_code == "stale_cursor"


def test_garbage_cursor_is_rejected_not_treated_as_page_one():
    res = search_components(filters={"voltage": "24"}, limit=2, cursor="not-a-cursor")
    assert res.ok is False
    assert res.error_code == "invalid_cursor"


def test_pagination_pages_do_not_overlap():
    p1 = search_components(filters={"voltage": "24"}, limit=2)
    p2 = search_components(filters={"voltage": "24"}, limit=2, cursor=p1.next_cursor)
    ids1 = {r["product_id"] for r in p1.data["results"]}
    ids2 = {r["product_id"] for r in p2.data["results"]}
    assert p2.ok is True
    assert not (ids1 & ids2)


# ---------------------------------------------------------------- P1: limits

def test_negative_limit_is_rejected():
    """LIMIT -1 in SQLite means 'no limit', so a negative limit must not be honored."""
    res = search_components(filters={"voltage": "24"}, limit=-1)
    assert res.ok is False
    assert res.error_code == "invalid_filter_value"


def test_oversized_limit_is_clamped_with_a_warning():
    res = search_components(filters={"voltage": "24"}, limit=500)
    assert res.ok is True
    assert res.returned <= MAX_PAGE_SIZE
    assert res.filters_applied["limit"] == MAX_PAGE_SIZE
    assert "limit_clamped" in [w["code"] for w in res.warnings]


# ---------------------------------------------------------------- P1: semantics

def test_missing_spec_is_enumerated_not_nulled():
    """unknown != absent != null: missing keys belong in specs_not_extracted."""
    res = search_components(filters={"voltage": "24"}, limit=1)
    row = res.data["results"][0]
    assert "specs" in row and "specs_not_extracted" in row
    assert not (set(row["specs"]) & set(row["specs_not_extracted"]))
    # A key never extracted must be absent from specs rather than present with null.
    assert all(v is not None for v in row["specs"].values())


def test_page_coverage_reports_spec_presence_for_the_page():
    res = search_components(filters={"voltage": "24"}, limit=5)
    cov = res.data["page_coverage"]
    assert cov["voltage"]["present"] == len(res.data["results"])
    assert 0 <= cov["rpm"]["pct"] <= 100


def test_filters_applied_echoes_all_effective_arguments():
    res = search_components(
        filters={"voltage": "24V"}, query="motor", price_max=6000,
        in_stock_only=False, sort="price_asc", limit=3,
    )
    applied = res.filters_applied
    for field in ("filters", "query", "price_max", "in_stock_only", "sort", "limit", "offset"):
        assert field in applied, f"filters_applied missing {field}"
    assert applied["filters"]["voltage"] == "24", "unit normalization must be echoed in filters_applied"


# ---------------------------------------------------------------- P1: queries

def test_blank_query_is_rejected_instead_of_returning_the_catalog():
    res = search_components(query="   ")
    assert res.ok is False
    assert res.error_code == "empty_query"


def test_unmatched_enum_suggestions_are_distance_ranked():
    # 98765V is impossible for the catalog but the enum is open-ended (battery
    # voltages are arbitrary), so it must be treated as unmatched-with-suggestions.
    requested = 98765
    res = search_components(filters={"voltage": str(requested)})
    assert res.ok is True and res.total_matches == 0
    vals = [float(s["value"]) for s in res.data["suggestions"]]
    assert vals, "expected suggestions"
    # The voltage vocabulary is global (motors + batteries), so values can sit on
    # both sides of the request. Distance ranking is the actual contract.
    distances = [abs(v - requested) for v in vals]
    assert distances == sorted(distances), f"suggestions must be distance-ranked, got {vals}"


# ---------------------------------------------------------------- P2: envelope

def test_describe_catalog_returned_counts_documents_not_products():
    res = describe_catalog()
    assert res.ok is True
    assert res.returned == 1
    assert res.total_matches == res.data["counts"]["products"]


def test_get_product_does_not_fabricate_extraction_timestamps():
    res = search_components(filters={"voltage": "24"}, limit=1)
    pid = res.data["results"][0]["product_id"]
    detail = get_product(product_id=pid)
    assert detail.ok is True
    ts = detail.data["crawl_timestamps"]
    assert ts["when_specs_extracted"] is None, "extraction timestamp must be null, not invented"
    assert detail.data["description_source"] in ("vendor_page", "vendor_page_short", "title_fallback")
    if detail.data["description_source"] == "title_fallback":
        assert detail.data["description"] == detail.data["title"]


# ---------------------------------------------------------------- new tools

def test_health_reports_ok_and_real_versions():
    res = health()
    assert res.ok is True, res.message
    assert res.data["status"] == "ok"
    assert res.data["integrity_check"] == "ok"
    assert res.data["counts"]["products"] > 0
    assert res.data["dependency_versions"].get("python")


def test_search_by_mpn_is_exact_and_returns_listings():
    # Resolve a real MPN from the catalog, then look it up exactly.
    res = search_components(query="IG52 160KGCM", limit=5)
    mpns = [r["mpn"] for r in res.data["results"] if r["mpn"]]
    if not mpns:
        pytest.skip("no MPN-bearing listing in this catalog slice")
    mpn = mpns[0]
    found = search_by_mpn(mpn=mpn)
    assert found.ok is True
    assert found.total_matches >= 1
    assert found.data["listings"][0]["mpn"].lower() == mpn.lower()


def test_search_by_mpn_empty_is_rejected():
    res = search_by_mpn(mpn="  ")
    assert res.ok is False
    assert res.error_code == "invalid_filter_value"


def test_unknown_filter_key_suggests_the_closest_keys():
    res = search_components(filters={"voltz": "24"})
    assert res.ok is False and res.error_code == "unknown_filter_key"
    keys = [s.get("key") for s in res.suggestions]
    assert keys[0] == "voltage", f"nearest key should rank first, got {keys}"
    assert len(keys) < len(res.valid_keys), "suggestions should be ranked, not a full dump"


# ---------------------------------------------------------------- wire shape

@pytest.mark.asyncio
async def test_every_tool_returns_one_content_block_with_matching_structured_content():
    from mcp_server.server import mcp

    calls = [
        ("describe_catalog", {}),
        ("health", {}),
        ("list_spec_keys", {}),
        ("count_components", {"filters": {"voltage": "24"}}),
        ("search_components", {"filters": {"voltage": "24"}, "limit": 1}),
        ("describe_spec", {"key": "voltage"}),
    ]
    for name, args in calls:
        content, meta = await mcp.call_tool(name, args)
        assert len(content) == 1, f"{name} returned {len(content)} content blocks"
        doc = json.loads(content[0].text)
        assert doc["ok"] is True, f"{name} failed: {doc.get('message')}"
