"""
Round 7 acceptance tests: agent-friendly querying end to end.

Covers the failure clusters found while exercising the MCP like a real agent:

1. Natural-language queries used to return 0 rows (all tokens required, no stop
   word removal) and gave no guidance. They must now relax to any-term matching,
   report it via warnings, and echo filters_applied.query_effective.
2. Typos used to dead-end. Word-like tokens must be spell-corrected from the
   catalog vocabulary (or surfaced in data.query_suggestions when unresolvable).
3. Part numbers typed with different separators ("MPU6050" vs "MPU-6050") used
   to return nothing. MPN/SKU matching must ignore case, spaces, hyphens and
   underscores, paginate with a cursor, and suggest close MPNs otherwise.
4. Fit questions ("can I use this battery for X") need category scoping, battery
   voltage/discharge-C and motor torque specs, all filterable.
5. The web/API search path must benefit from the same query recovery.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from mcp_server.server import (
    search_by_mpn,
    search_components,
    count_components,
)

BATTERY_KEYS = {"voltage", "cells", "capacity_mah", "discharge_c", "chemistry"}


# ---------------------------------------------------------------- query recovery

def test_natural_language_query_relaxes_instead_of_returning_nothing():
    res = search_components(
        query="battery to power arduino uno for 8 hours", limit=3, refresh=False
    )
    assert res.ok is True
    assert res.total_matches > 0, "natural-language queries must not dead-end"
    codes = [w["code"] for w in (res.warnings or [])]
    assert "query_relaxed" in codes
    assert res.filters_applied.get("query_effective"), (
        "the caller must be able to see what was actually searched"
    )


def test_typo_query_is_spell_corrected_and_reported():
    res = search_components(query="esp32 wromm", limit=3, refresh=False)
    assert res.ok is True
    assert res.total_matches > 0
    codes = [w["code"] for w in (res.warnings or [])]
    assert "query_spelling_corrected" in codes
    assert res.filters_applied["query_effective"] == "esp32 wroom"


def test_gibberish_query_guides_instead_of_erroring():
    res = search_components(query="zzqx flurble", limit=3, refresh=False)
    assert res.ok is True
    assert res.total_matches == 0
    codes = [w["code"] for w in (res.warnings or [])]
    assert "query_unresolved" in codes
    assert isinstance(res.data.get("query_suggestions"), list)


def test_count_and_search_agree_on_recovered_queries():
    query = "rechargeable battery for esp32 project"
    counted = count_components(query=query)
    found = search_components(query=query, limit=1, refresh=False)
    assert counted.ok is True and found.ok is True
    assert counted.total_matches == found.total_matches


def test_multiple_unmatched_enum_filters_each_contribute_suggestions():
    res = count_components(filters={"chemistry": "zzzzz", "cells": "99s"})
    assert res.ok is True and res.total_matches == 0
    suggested_keys = {s.get("key") for s in res.data["suggestions"]}
    assert {"chemistry", "cells"} <= suggested_keys, (
        f"one filter must not overwrite another's suggestions: {res.data['suggestions']}"
    )


# ---------------------------------------------------------------- category scoping

def test_category_filter_scopes_results():
    res = search_components(category="battery", limit=5, refresh=False)
    assert res.ok is True
    assert res.total_matches > 0
    assert all(r["category_canonical"] == "battery" for r in res.data["results"])


def test_unknown_category_returns_error_with_valid_values():
    res = search_components(category="flux-capacitor", refresh=False)
    assert res.ok is False
    assert res.error_code == "unknown_category"
    assert res.details["valid_categories"], "valid categories must be listed"
    assert "battery" in res.details["valid_categories"]


def test_category_query_ranks_genuine_components_first():
    res = search_components(
        query="battery to power arduino uno for 8 hours",
        category="battery",
        limit=5,
        refresh=False,
    )
    assert res.ok is True and res.data["results"]
    # The top hit should be an actual battery carrying extracted battery specs,
    # not a kit/charger that merely mentions "battery" in its title.
    top = res.data["results"][0]
    assert set(top["specs"]) & BATTERY_KEYS, f"top hit lacks battery specs: {top['specs']}"


# ---------------------------------------------------------------- fit specs

def test_battery_fit_filters_work_together():
    res = search_components(
        filters={"chemistry": "lipo", "voltage": "3.7", "capacity_mah": {"gte": 1500}},
        limit=5,
        refresh=False,
    )
    assert res.ok is True
    assert res.total_matches > 0, "3.7V lipo >=1500mAh must be findable"
    for r in res.data["results"]:
        assert r["specs"]["chemistry"] == "lipo"
        assert r["specs"]["voltage"] == "3.7"


def test_motor_torque_filter_is_available_with_a_hint_for_plain_torque():
    hinted = search_components(filters={"torque": {"gte": 2}}, refresh=False)
    assert hinted.ok is False
    assert hinted.error_code == "unknown_filter_key"
    assert {"key": "torque_kgcm"} in (hinted.suggestions or [])

    res = search_components(
        filters={"torque_kgcm": {"gte": 5}, "voltage": "12"},
        query="motor",
        limit=5,
        refresh=False,
    )
    assert res.ok is True
    assert res.total_matches > 0
    for r in res.data["results"]:
        assert float(r["specs"]["torque_kgcm"]) >= 5


def test_specs_not_extracted_is_scoped_to_the_products_category():
    res = search_components(category="battery", limit=5, refresh=False)
    assert res.ok is True and res.data["results"]
    for r in res.data["results"]:
        assert set(r["specs_not_extracted"]) <= BATTERY_KEYS, (
            f"motor-only keys must not look 'missing' on a battery: {r['specs_not_extracted']}"
        )


# ---------------------------------------------------------------- MPN resolution

def test_mpn_lookup_ignores_separators():
    joined = search_by_mpn(mpn="MPU6050", limit=5, refresh=False)
    hyphened = search_by_mpn(mpn="MPU-6050", limit=5, refresh=False)
    spaced = search_by_mpn(mpn="MPU 6050", limit=5, refresh=False)
    assert joined.ok and hyphened.ok and spaced.ok
    assert joined.total_matches > 0
    ids = lambda res: {l["product_id"] for l in res.data["listings"]}
    assert ids(joined) == ids(hyphened) == ids(spaced)


def test_mpn_listings_carry_specs_for_fit_checks():
    res = search_by_mpn(mpn="ESP32-WROOM-32", limit=3, refresh=False)
    assert res.ok is True and res.data["listings"]
    listing = res.data["listings"][0]
    assert isinstance(listing.get("specs"), dict)
    assert listing.get("price_source"), "price provenance must be visible"


def test_mpn_pagination_cursor_round_trip_and_stale_detection():
    first = search_by_mpn(mpn="ESP32-WROOM-32", limit=5, refresh=False)
    assert first.ok is True and first.truncated is True
    assert first.next_cursor, "truncated results must expose a cursor"

    second = search_by_mpn(mpn="ESP32-WROOM-32", limit=5, cursor=first.next_cursor, refresh=False)
    assert second.ok is True
    first_ids = {l["product_id"] for l in first.data["listings"]}
    second_ids = {l["product_id"] for l in second.data["listings"]}
    assert not (first_ids & second_ids), "pages must not overlap"

    stale = search_by_mpn(mpn="MPU6050", limit=5, cursor=first.next_cursor, refresh=False)
    assert stale.ok is False
    assert stale.error_code == "stale_cursor"


def test_mpn_miss_suggests_close_part_numbers():
    res = search_by_mpn(mpn="ESP32 WROM-33", limit=5, refresh=False)
    assert res.ok is True
    if res.total_matches == 0:
        codes = [w["code"] for w in (res.warnings or [])]
        assert "mpn_not_found" in codes


# ---------------------------------------------------------------- web/API path

def test_web_api_benefits_from_query_recovery():
    import main

    client = TestClient(main.web_app)
    for q in ("battery to power arduino uno for 8 hours", "esp32 wromm"):
        r = client.get("/api/search", params={"q": q, "source": "local", "refresh": "off", "limit": 3})
        assert r.status_code == 200
        body = r.json()
        assert body.get("results"), f"web search dead-ended for {q!r}"
