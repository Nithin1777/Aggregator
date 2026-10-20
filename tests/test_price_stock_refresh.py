"""
Acceptance tests for search-time price/stock refresh with DB-cached fallback.

Covers:
1. Schema migration for the price/stock cache columns.
2. The refresh path updates ONLY price/availability (never titles/specs/URLs).
3. Live values are cached back to SQLite.
4. Offline/vendor failure falls back to the last cached values.
5. A failed pass opens a short circuit breaker so page views stay fast.
6. Fresh cache (inside the TTL) skips vendor calls.
7. The MCP tools go through the same refresh path (and fall back safely).
"""
import asyncio
import sqlite3

import pytest

import db
from config import settings
from models import UnifiedProduct
from search_service import AggregatorEngine


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "refresh_test.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()
    return db_path


def _product(price=199.0, available=True, **overrides):
    data = dict(
        vendor="robu",
        vendor_name="Robu.in",
        external_id="101",
        title="IG52 160KGCM High Torque DC Motor",
        normalized_title="ig52 160kgcm high torque dc motor",
        mpn="IG52",
        sku="ROBU-IG52",
        price=price,
        available=available,
        product_url="https://robu.in/product/ig52-160kgcm/",
    )
    data.update(overrides)
    return UnifiedProduct(**data)


def _backdate_refresh_cache():
    """Makes every cached product look stale so the next search refreshes it."""
    conn = db.get_db_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE products SET price_stock_attempted_at = NULL, "
                "price_stock_updated_at = NULL, price_stock_source = 'cache';"
            )
    finally:
        conn.close()


class FakeAdapter:
    """Minimal async adapter used to simulate a live (or dead) vendor."""

    def __init__(self, vendor_id="robu", vendor_name="Robu.in", products=None, error=None):
        self._vendor_id = vendor_id
        self._vendor_name = vendor_name
        self.products = products or []
        self.error = error
        self.calls = []

    @property
    def vendor_id(self):
        return self._vendor_id

    @property
    def vendor_name(self):
        return self._vendor_name

    async def search_live(self, query, limit=20):
        self.calls.append(query)
        if self.error:
            raise self.error
        return list(self.products)

    async def close(self):
        return None


def _engine_with(adapter):
    engine = AggregatorEngine()
    engine.adapters = {adapter.vendor_id: adapter}
    return engine


def test_schema_has_price_stock_cache_columns(temp_db):
    conn = sqlite3.connect(str(temp_db))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    finally:
        conn.close()
    assert {"price_stock_updated_at", "price_stock_attempted_at", "price_stock_source"} <= columns


def test_price_stock_update_preserves_everything_else(temp_db):
    db.upsert_products([_product()])
    stored = db.search_products("IG52")[0]

    db.update_price_stock_batch(
        [{"id": stored.id, "price": 149.0, "available": False}]
    )
    refreshed = db.search_products("IG52")[0]

    assert refreshed.price == 149.0
    assert refreshed.available is False
    # Only price/stock change; catalog fields stay exactly as ingested.
    assert refreshed.title == stored.title
    assert refreshed.mpn == stored.mpn
    assert refreshed.sku == stored.sku
    assert refreshed.product_url == stored.product_url
    assert refreshed.price_source == "live"
    assert refreshed.price_updated_at


def test_refresh_uses_live_values_and_caches_them(temp_db):
    db.upsert_products([_product(price=199.0, available=True)])
    products = db.search_products("IG52")
    _backdate_refresh_cache()

    adapter = FakeAdapter(products=[_product(price=149.0, available=False)])
    engine = _engine_with(adapter)

    info = asyncio.run(engine.refresh_price_stock(products, query="IG52 motor"))

    assert info.status == "live"
    assert info.attempted is True
    assert info.refreshed_products == 1
    assert info.queries[0] == "IG52 motor"
    assert products[0].price == 149.0
    assert products[0].available is False

    stored = db.search_products("IG52")[0]
    assert stored.price == 149.0
    assert stored.available is False
    assert stored.price_source == "live"
    assert stored.price_updated_at == info.fetched_at
    assert adapter.calls == ["IG52 motor"]


def test_refresh_does_not_import_unmatched_live_products(temp_db):
    db.upsert_products([_product(external_id="101")])
    products = db.search_products("IG52")
    _backdate_refresh_cache()

    unrelated = _product(
        external_id="999",
        title="Unrelated Servo Motor",
        normalized_title="unrelated servo motor",
        mpn="SERVO-9",
        sku="SERVO-9",
        product_url="https://robu.in/product/unrelated-servo/",
    )
    adapter = FakeAdapter(products=[_product(price=149.0), unrelated])
    engine = _engine_with(adapter)

    asyncio.run(engine.refresh_price_stock(products, query="IG52 motor"))

    # Refresh updates matched products only; it never seeds new live listings.
    assert db.search_products("Unrelated Servo") == []
    assert len(db.search_products("IG52")) == 1


def test_offline_refresh_falls_back_to_cached_values(temp_db):
    db.upsert_products([_product(price=199.0, available=True)])
    products = db.search_products("IG52")
    _backdate_refresh_cache()

    adapter = FakeAdapter(error=ConnectionError("internet unavailable"))
    engine = _engine_with(adapter)

    info = asyncio.run(engine.refresh_price_stock(products, query="IG52 motor"))

    assert info.status == "cached"
    assert info.attempted is True
    assert info.refreshed_products == 0
    assert "cached" in info.message.lower()
    # The user-facing values are the last known (cached) values.
    assert products[0].price == 199.0
    assert products[0].available is True
    stored = db.search_products("IG52")[0]
    assert stored.price == 199.0
    assert stored.available is True
    # The failed attempt is recorded so the next page view can serve cache fast.
    assert stored.price_source == "cache"


def test_failed_pass_opens_circuit_breaker(temp_db):
    db.upsert_products([_product()])
    products = db.search_products("IG52")
    _backdate_refresh_cache()

    adapter = FakeAdapter(error=ConnectionError("offline"))
    engine = _engine_with(adapter)

    asyncio.run(engine.refresh_price_stock(products, query="IG52 motor"))
    calls_after_failure = len(adapter.calls)
    assert calls_after_failure >= 1

    _backdate_refresh_cache()
    info = asyncio.run(engine.refresh_price_stock(products, query="IG52 motor"))

    assert info.status == "offline_cooldown"
    assert len(adapter.calls) == calls_after_failure
    assert products[0].price == 199.0


def test_fresh_cache_skips_vendor_calls(temp_db):
    db.upsert_products([_product(price=199.0)])  # upsert marks it freshly verified
    products = db.search_products("IG52")

    adapter = FakeAdapter(products=[_product(price=1.0)])
    engine = _engine_with(adapter)

    info = asyncio.run(engine.refresh_price_stock(products, query="IG52"))

    assert info.status == "fresh_cache"
    assert adapter.calls == []
    assert products[0].price == 199.0


def test_local_search_refreshed_falls_back_when_offline(temp_db):
    db.upsert_products([_product(price=499.0, available=True)])
    _backdate_refresh_cache()

    adapter = FakeAdapter(error=ConnectionError("offline"))
    engine = _engine_with(adapter)

    resp = asyncio.run(engine.search_local_refreshed("IG52 motor", limit=10))

    assert resp.source == "local_db"
    assert resp.results
    assert resp.results[0].price == 499.0
    assert resp.price_refresh is not None
    assert resp.price_refresh.status == "cached"


def test_mcp_search_components_refreshes_price_stock(temp_db, monkeypatch):
    db.upsert_products([_product(price=199.0, available=True)])
    _backdate_refresh_cache()

    import mcp_server.server as server

    adapter = FakeAdapter(products=[_product(price=149.0, available=False)])
    engine = _engine_with(adapter)
    monkeypatch.setattr(server, "_engine", engine)

    res = server.search_components(query="IG52 motor", limit=5)

    assert res.ok is True, res.message
    assert res.total_matches >= 1
    row = res.data["results"][0]
    assert row["price"] == 149.0
    assert row["available"] is False
    assert res.data["price_stock_refresh"]["status"] == "live"
    assert res.data["price_stock_refresh"]["refreshed_products"] == 1


def test_mcp_search_components_serves_cache_when_offline(temp_db, monkeypatch):
    db.upsert_products([_product(price=199.0, available=True)])
    _backdate_refresh_cache()

    import mcp_server.server as server

    adapter = FakeAdapter(error=ConnectionError("offline"))
    engine = _engine_with(adapter)
    monkeypatch.setattr(server, "_engine", engine)

    res = server.search_components(query="IG52 motor", limit=5)

    assert res.ok is True, res.message
    assert res.data["results"]
    assert res.data["results"][0]["price"] == 199.0
    refresh = res.data["price_stock_refresh"]
    assert refresh["status"] in ("cached", "offline_cooldown")
    warning_codes = [w["code"] for w in (res.warnings or [])]
    assert "price_stock_cache_fallback" in warning_codes
