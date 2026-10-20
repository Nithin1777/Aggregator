"""Price constraints in search queries (explicit params and natural language)."""
import pytest

import db
from main import api_search
from src.search.query_terms import parse_price_constraints


@pytest.mark.parametrize(
    "raw,cleaned,low,high",
    [
        ("resistor under 500", "resistor", None, 500.0),
        ("12v motor below ₹1,200", "12v motor", None, 1200.0),
        ("esp32 over 300", "esp32", 300.0, None),
        ("sensor 100 to 500", "sensor", 100.0, 500.0),
        ("servo 200-900", "servo", 200.0, 900.0),
        ("raspberry pi pico", "raspberry pi pico", None, None),
        ("MAX232 driver", "MAX232 driver", None, None),
    ],
)
def test_parse_price_constraints(raw, cleaned, low, high):
    assert parse_price_constraints(raw) == (cleaned, low, high)


def test_db_price_filter_keeps_only_affordable_rows():
    rows = db.search_products("resistor", limit=200, price_max=1.0)
    assert rows
    assert all(p.price <= 1.0 for p in rows)
    total = db.count_search_products("resistor", price_max=1.0)
    assert total >= len(rows)


def test_db_browse_price_filter():
    rows = db.list_products_by_category(category="resistors", limit=50, price_max=1.0)
    assert rows
    assert all(p.price <= 1.0 for p in rows)
    total = db.count_products_by_category(category="resistors", price_max=1.0)
    assert total >= len(rows)


@pytest.mark.asyncio
async def test_api_search_under_price_from_text_query():
    data = await api_search(q="resistor under 1", source="local", limit=10, refresh="off")
    assert data.comparison_groups
    for group in data.comparison_groups:
        for offer in group.offers:
            assert offer.price <= 1.0


@pytest.mark.asyncio
async def test_api_search_explicit_price_params():
    data = await api_search(
        q="resistor", source="local", limit=10, refresh="off", price_max=1.0
    )
    assert data.comparison_groups
    assert all(o.price <= 1.0 for g in data.comparison_groups for o in g.offers)
