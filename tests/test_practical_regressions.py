"""User-visible regressions with isolated catalogs and fake vendor responses."""
import asyncio
import hashlib

import httpx
import pytest

import db
import main
from adapters.base import VendorFetchError
from adapters.shopify import ShopifyAdapter
from config import settings
from models import UnifiedProduct
from normalizer import ComponentItem
from search_service import AggregatorEngine


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "catalog.db")
    monkeypatch.setattr(settings, "matcher_audit_logging", False)
    monkeypatch.setattr(settings, "shopify_page_delay", 0)
    db.init_db()
    engine = AggregatorEngine()
    monkeypatch.setattr(main, "engine_instance", engine)
    return engine


def product(external_id="1", vendor="robu", price=400, available=True):
    return UnifiedProduct(
        vendor=vendor, vendor_name=vendor, external_id=external_id,
        title="BME280 Temperature Sensor Module", normalized_title="bme280 temperature sensor module",
        mpn="BME280", price=price, available=available,
        product_url=f"https://example.invalid/{vendor}/{external_id}",
    )


def request(params):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.web_app),
                                     base_url="http://test") as client:
            return await client.get("/api/search", params=params)
    return asyncio.run(run())


@pytest.mark.parametrize("filters", ["{broken", "[]", "null", "true", "42", '"voltage"'])
def test_invalid_filter_json_is_a_client_error(catalog, filters):
    response = request({"q": "BME280", "spec_filters": filters, "refresh": "off"})
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_filter_value"


def test_unknown_filter_is_a_client_error(catalog):
    response = request({"q": "BME280", "spec_filters": '{"nonexistent":5}', "refresh": "off"})
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "unknown_filter_key"


class Vendor:
    def __init__(self, products):
        self.products = products

    async def search_live(self, query, limit=20):
        return self.products[:limit]

    async def close(self):
        pass


@pytest.mark.parametrize("bounds", [{"vendor": "robu"}, {"price_max": 500},
                                     {"price_min": 300}, {"in_stock": "true"}])
def test_live_search_enforces_each_filter(catalog, bounds):
    # Each constraint excludes a different offer; the good offer must survive.
    catalog.adapters = {"robu": Vendor([
        product("good"), product("wrong_vendor", vendor="makerbazar"),
        product("expensive", price=900), product("cheap", price=100),
        product("sold_out", available=False),
    ])}
    response = request({"q": "BME280", "source": "live", **bounds})
    assert response.status_code == 200
    rows = response.json()["results"]
    assert any(row["external_id"] == "good" for row in rows)
    for row in rows:
        if "vendor" in bounds:
            assert row["vendor"] == bounds["vendor"]
        if "price_max" in bounds:
            assert row["price"] <= bounds["price_max"]
        if "price_min" in bounds:
            assert row["price"] >= bounds["price_min"]
        if "in_stock" in bounds:
            assert row["available"]


@pytest.mark.parametrize("live", [product(price=900), product(available=False)])
def test_refresh_removes_newly_ineligible_offers(catalog, live):
    db.upsert_products([product()])
    catalog.adapters = {"robu": Vendor([live])}
    response = request({"q": "BME280", "price_max": 500, "in_stock": "true", "refresh": "force"})
    assert response.status_code == 200
    data = response.json()
    assert data["price_refresh"]["refreshed_products"] == 1
    assert data["results"] == data["comparison_groups"] == []
    assert data["total_results"] == 0
    stored = db.search_products("BME280")[0]
    assert stored.price == live.price and stored.available == live.available


def test_shopify_capped_pages_continue_through_ingestion(catalog, monkeypatch):
    monkeypatch.setattr(settings, "shopify_batch_limit", 2)
    adapter = ShopifyAdapter("robu", "Test", "https://example.invalid")
    calls = []

    async def fetch(url, params=None):
        calls.append(params.copy())
        count = 2 if params["page"] == 1 else 1
        return {"products": [
            {"id": params["page"] * 10 + i, "handle": f"sensor-{params['page']}-{i}",
             "title": "BME280 Sensor Module", "variants": [{"price": "400", "available": True}]}
            for i in range(count)
        ]}

    monkeypatch.setattr(adapter, "_fetch_with_backoff", fetch)
    catalog.adapters = {"robu": adapter}

    async def run():
        return [progress async for progress in catalog.run_vendor_ingest("robu", resume=False)]

    progress = asyncio.run(run())
    assert [call["page"] for call in calls] == [1, 2]
    assert all(call["limit"] == 2 for call in calls)
    assert progress[-1].is_complete
    assert db.get_checkpoint("robu").total_ingested == 3
    assert len(db.search_products("BME280")) == 3


@pytest.mark.parametrize("payload", [{"products": None}, {"products": {}}, [], {"products": "blocked"}])
def test_shopify_bad_envelope_does_not_complete_catalog(catalog, monkeypatch, payload):
    adapter = ShopifyAdapter("robu", "Test", "https://example.invalid")

    async def fetch(*args, **kwargs):
        return payload

    monkeypatch.setattr(adapter, "_fetch_with_backoff", fetch)
    catalog.adapters = {"robu": adapter}

    async def run():
        return [progress async for progress in catalog.run_vendor_ingest("robu", resume=False)]

    with pytest.raises(VendorFetchError):
        asyncio.run(run())
    checkpoint = db.get_checkpoint("robu")
    assert checkpoint.status == "error" and checkpoint.last_page == 0


def test_saved_same_label_cannot_merge_conflicting_pin_counts(catalog):
    a = ComponentItem("ESP32 WiFi Development Board 30 Pins")
    b = ComponentItem("ESP32 WiFi Development Board 38 Pins")
    pair_id = hashlib.sha1("::".join(sorted([a.title.lower(), b.title.lower()])).encode()).hexdigest()[:16]
    catalog._llm_labels = {pair_id: {"label": "same"}}
    assert not catalog._evaluate_pair_merge(a, b)[0]


def test_pin_query_allows_intervening_words_but_preserves_pin_count(catalog):
    rows = []
    for pins in (30, 38):
        row = product(str(pins)).model_copy(update={
            "title": f"ESP32 WiFi Bluetooth Development Board {pins} Pins",
            "normalized_title": f"esp32 wifi bluetooth development board {pins} pins",
            "mpn": "ESP32",
        })
        rows.append(row)
    db.upsert_products(rows)
    response = request({"q": "ESP32 30 pins", "refresh": "off"})
    assert response.status_code == 200
    assert [row["external_id"] for row in response.json()["results"]] == ["30"]


@pytest.mark.parametrize("a,b", [
    ("10K ohm 0603 SMD Resistor 1 percent", "10K ohm 0402 SMD Resistor 1 percent"),
    ("24V battery pack 40Ah 50A continuous discharge", "24V battery pack 40Ah 10A continuous discharge"),
])
def test_incompatible_ratings_stay_separate_even_with_saved_labels(catalog, a, b):
    pair_id = hashlib.sha1("::".join(sorted([a.lower(), b.lower()])).encode()).hexdigest()[:16]
    catalog._llm_labels = {pair_id: {"label": "same"}}
    assert not catalog._evaluate_pair_merge(ComponentItem(a), ComponentItem(b))[0]
    rows = [product(str(i)).model_copy(update={"title": title, "normalized_title": title.lower(), "mpn": None})
            for i, title in enumerate((a, b))]
    assert len(catalog.group_and_compare(rows)) == 2


@pytest.mark.parametrize("a,b", [
    ("10K ohm 0603 SMD Resistor", "10K ohm 0603 SMD Resistor"),
    ("24V battery pack 40Ah 10A continuous discharge", "24V battery pack 40Ah 10.0A continuous discharge"),
])
def test_equal_ratings_still_compare(catalog, a, b):
    assert catalog._evaluate_pair_merge(ComponentItem(a), ComponentItem(b))[0]
