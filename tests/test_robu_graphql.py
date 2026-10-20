"""Robu ProductDetails GraphQL client tests (no network)."""
from adapters.robu_graphql import (
    build_batch_query,
    description_html,
    extract_detail,
)


def test_build_batch_query_aliases_and_variables():
    payload = build_batch_query(["raspberry-pi-pico", "raspberry-pi-pico-2"])

    query = payload["query"]
    assert payload["operationName"] == "ProductDetailsBatch"
    assert "p0: productdetails(slug: $s0)" in query
    assert "p1: productdetails(slug: $s1)" in query
    assert payload["variables"] == {
        "s0": "raspberry-pi-pico",
        "s1": "raspberry-pi-pico-2",
    }


def test_extract_detail_reads_aliased_payload():
    payload = {
        "data": {
            "p0": {
                "status": True,
                "data": {"name": "Pico", "description": "<p>x</p>"},
            },
            "p1": {"status": False, "message": "not found"},
        }
    }

    assert extract_detail(payload, 0)["name"] == "Pico"
    assert extract_detail(payload, 1) is None


def test_description_prefers_description_then_short_description():
    assert description_html(
        {"description": "<p>full</p>", "short_description": "short"}
    ) == "<p>full</p>"
    assert description_html({"description": "", "short_description": "short"}) == "short"
    assert description_html({}) == ""
