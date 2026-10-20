"""Pagination and relevance-ranking regressions for the web/API search path."""
import asyncio

import db
from models import UnifiedProduct
from search_service import AggregatorEngine


def _product(index, title, mpn, vendor="robu"):
    return UnifiedProduct(
        vendor=vendor,
        vendor_name=vendor.title(),
        external_id=f"e{index}",
        title=title,
        normalized_title=title.lower(),
        mpn=mpn,
        price=100.0 + index,
        available=True,
        product_url=f"https://example.test/{index}",
    )


def test_group_ranking_puts_exact_match_first():
    engine = AggregatorEngine()
    products = [
        _product(0, "Raspberry Pi 5 Board 4GB", "RPI5-4GB"),
        _product(1, "Raspberry Pi Pico", "PICO"),
        _product(2, "ESP32 Camera Module", "ESP32-CAM"),
    ]

    groups = engine.group_and_compare(products, query="raspberry pi pico")
    assert groups[0].canonical_name == "Raspberry Pi Pico"


def test_browse_is_paginated_and_pages_do_not_overlap():
    engine = AggregatorEngine()

    async def run():
        page1 = await engine.browse_local(page=1, page_size=20, category="resistors")
        page2 = await engine.browse_local(page=2, page_size=20, category="resistors")
        return page1, page2

    page1, page2 = asyncio.run(run())
    assert len(page1.comparison_groups) == 20
    assert page1.page == 1 and page1.has_prev is False
    assert page2.page == 2 and page2.has_prev is True
    names1 = {g.canonical_name for g in page1.comparison_groups}
    names2 = {g.canonical_name for g in page2.comparison_groups}
    assert names1 and names2
    assert not (names1 & names2), "a comparison card must not repeat on the next page"


def test_search_page_size_and_no_duplicate_cards_across_pages():
    engine = AggregatorEngine()

    async def run():
        page1 = await engine.search_local_refreshed(
            "sensor", page=1, page_size=10, refresh_mode="off"
        )
        page2 = await engine.search_local_refreshed(
            "sensor", page=2, page_size=10, refresh_mode="off"
        )
        return page1, page2

    page1, page2 = asyncio.run(run())
    assert len(page1.comparison_groups) <= 10
    assert len(page2.comparison_groups) <= 10
    assert page1.total_results > 0
    assert page1.returned_results >= len(page1.comparison_groups)
    names1 = {g.canonical_name for g in page1.comparison_groups}
    names2 = {g.canonical_name for g in page2.comparison_groups}
    assert not (names1 & names2)


def test_search_reports_total_matches_not_just_the_page():
    total = db.count_search_products("resistor")
    products = db.search_products("resistor", limit=5)
    assert total >= len(products) > 0


def test_deep_page_expands_beyond_the_initial_window():
    """Page 20 must keep working instead of returning an empty page."""
    engine = AggregatorEngine()

    async def run():
        return await engine.search_local_refreshed(
            "resistor", page=20, page_size=10, refresh_mode="off"
        )

    resp = asyncio.run(run())
    assert resp.page == 20
    assert resp.has_prev is True
    assert resp.comparison_groups, "deep pages must keep returning cards while matches remain"
