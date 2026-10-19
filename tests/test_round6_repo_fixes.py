"""
Round 6 regression tests: repo-level fixes outside the MCP layer.

1. A clean database gets the full spec schema from init_db() alone.
2. Ingestion treats a fetch failure as an error, never as "catalog complete".
3. Clustering audit logging does not do per-pair file I/O.
"""
import asyncio
import pathlib

import pytest

from adapters.base import VendorFetchError
from config import settings
import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Points the whole app at a brand-new empty database file."""
    db_file = tmp_path / "fresh_components.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


def _schema(db_file):
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        return tables, cols
    finally:
        conn.close()


def test_fresh_database_gets_spec_schema(fresh_db):
    db.init_db()
    tables, cols = _schema(fresh_db)

    assert {"products", "products_fts", "scrape_checkpoints"} <= tables
    # The spec layer must exist after init_db alone (previously migration-only).
    assert {"product_specs", "spec_stats_global", "spec_definitions", "spec_stats"} <= tables
    assert "spec_category" in cols
    # Index used by every spec-filtered query.
    import sqlite3
    conn = sqlite3.connect(str(fresh_db))
    try:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert "idx_products_spec_category" in indexes


def test_init_db_is_idempotent(fresh_db):
    db.init_db()
    db.init_db()  # must not raise on the second run
    tables, cols = _schema(fresh_db)
    assert "product_specs" in tables and "spec_category" in cols


def test_ingest_works_end_to_end_on_fresh_db(fresh_db):
    """Schema must be usable: write a product, then read it back through FTS."""
    from models import UnifiedProduct

    db.init_db()
    conn = db.get_db_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO products (vendor, vendor_name, external_id, title, normalized_title, "
                "mpn, price, available, product_url, raw_json) "
                "VALUES ('robu', 'Robu.in', 'x1', 'ESP32 Dev Board', 'esp32 dev board', 'ESP32', 499.0, 1, "
                "'https://robu.in/product/x1/', '{}')"
            )
    finally:
        conn.close()

    results = db.search_products("esp32", limit=10)
    assert len(results) == 1
    assert results[0].title == "ESP32 Dev Board"


def test_mcp_tools_work_on_empty_fresh_db(fresh_db):
    """A brand-new catalog must degrade gracefully, not raise."""
    import importlib
    import mcp_server.server as server

    db.init_db()
    importlib.reload(server)  # rebuild the engine against the new db_path

    catalog = server.describe_catalog()
    assert catalog.ok is True
    assert catalog.total_matches == 0
    assert catalog.data["spec_keys"] == []

    keys = server.list_spec_keys()
    assert keys.ok is True and keys.data["spec_keys"] == []

    health = server.health()
    assert health.ok is True and health.data["status"] == "ok"

    # No advertised keys yet -> a filter key is legitimately unknown, and that is
    # reported as a structured error rather than an empty success.
    search = server.search_components(filters={"voltage": "24"})
    assert search.ok is False
    assert search.error_code == "unknown_filter_key"


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _AlwaysFailingSession:
    """Simulates a vendor that never returns a usable response."""

    def __init__(self, status_code=500):
        self.status_code = status_code
        self.calls = 0

    async def get(self, *args, **kwargs):
        self.calls += 1
        return _FakeResponse(self.status_code)

    async def post(self, *args, **kwargs):
        self.calls += 1
        return _FakeResponse(self.status_code)

    async def close(self):
        return None


class _NotFoundSession(_AlwaysFailingSession):
    def __init__(self):
        super().__init__(status_code=404)


@pytest.fixture()
def fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 2)
    monkeypatch.setattr(settings, "shopify_page_delay", 0)
    monkeypatch.setattr(settings, "robokits_page_delay", 0)
    monkeypatch.setattr(settings, "robu_request_delay", 0)


def _run(coro):
    return asyncio.run(coro)


def test_shopify_fetch_failure_raises_not_empty(fast_retries):
    from adapters.shopify import ShopifyAdapter

    adapter = ShopifyAdapter("quartzcomponents", "Quartz Components", "https://quartzcomponents.com")
    adapter._session = _AlwaysFailingSession(500)
    with pytest.raises(VendorFetchError):
        _run(adapter.fetch_catalog_page(page=7))


def test_shopify_404_is_a_real_end_of_catalog(fast_retries):
    from adapters.shopify import ShopifyAdapter

    adapter = ShopifyAdapter("quartzcomponents", "Quartz Components", "https://quartzcomponents.com")
    adapter._session = _NotFoundSession()
    products, has_more = _run(adapter.fetch_catalog_page(page=999))
    assert products == [] and has_more is False


def test_shopify_empty_product_list_is_a_real_end(fast_retries):
    from adapters.shopify import ShopifyAdapter

    class _EmptySession(_AlwaysFailingSession):
        async def get(self, *args, **kwargs):
            return _FakeResponse(200, {"products": []})

    adapter = ShopifyAdapter("quartzcomponents", "Quartz Components", "https://quartzcomponents.com")
    adapter._session = _EmptySession()
    products, has_more = _run(adapter.fetch_catalog_page(page=999))
    assert products == [] and has_more is False


def test_robokits_fetch_failure_raises_not_empty(fast_retries):
    from adapters.robokits import RobokitsAdapter

    adapter = RobokitsAdapter()
    adapter._session = _AlwaysFailingSession(503)
    with pytest.raises(VendorFetchError):
        _run(adapter.fetch_catalog_page(page=4))


def test_robokits_unparsable_page_raises_rather_than_completing(fast_retries):
    """A markup change must not be reported as 'catalog finished'."""
    from adapters.robokits import RobokitsAdapter

    class _MarkupChangedSession(_AlwaysFailingSession):
        async def get(self, *args, **kwargs):
            return _FakeResponse(200, text="<html><body><div class='new-layout'>hi</div></body></html>")

    adapter = RobokitsAdapter()
    adapter._session = _MarkupChangedSession()
    with pytest.raises(VendorFetchError):
        _run(adapter.fetch_catalog_page(page=2))


def test_robu_search_failure_raises_not_empty(fast_retries):
    from adapters.robu import RobuAdapter

    adapter = RobuAdapter()
    adapter._session = _AlwaysFailingSession(500)
    adapter._warmed_up = True
    with pytest.raises(VendorFetchError):
        _run(adapter.search_live("esp32", limit=5))


def test_ingest_marks_error_status_on_fetch_failure(fresh_db, fast_retries):
    """run_vendor_ingest must persist status='error' and re-raise, not mark 'completed'."""
    from search_service import AggregatorEngine

    db.init_db()
    engine = AggregatorEngine()
    engine.adapters["robokits"]._session = _AlwaysFailingSession(503)

    async def drive():
        return [step async for step in engine.run_vendor_ingest("robokits", max_pages=1)]

    with pytest.raises(VendorFetchError):
        _run(drive())

    checkpoint = db.get_checkpoint("robokits")
    assert checkpoint.status == "error"
    assert checkpoint.status != "completed"


def test_clustering_queues_audit_lines_and_flushes_once(fresh_db, monkeypatch):
    """Audit logging must not open the file once per pair."""
    from models import UnifiedProduct
    import search_service

    db.init_db()
    engine = search_service.AggregatorEngine()

    opens = {"count": 0}
    real_open = open

    def counting_open(*args, **kwargs):
        if args and str(args[0]).endswith("matcher_decisions.log"):
            opens["count"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    monkeypatch.setattr(settings, "matcher_audit_logging", True)

    products = [
        UnifiedProduct(
            id=i, vendor="robu", vendor_name="Robu.in", external_id=f"e{i}",
            title=t, normalized_title=t.lower(), mpn=mpn, price=100.0 + i,
            available=True, product_url=f"https://robu.in/product/e{i}/",
        )
        for i, (t, mpn) in enumerate([
            ("ESP32 Dev Board", "ESP32"),
            ("ESP32 WROOM Module", "ESP32"),
            ("LM2596 Buck Converter", "LM2596"),
            ("MPU6050 Sensor", "MPU6050"),
            ("Arduino Uno R3", "ARDUINO-UNO"),
        ])
    ]

    engine.group_and_compare(products, query="test")
    assert opens["count"] <= 1, f"expected at most one audit-file open, saw {opens['count']}"
    assert engine._pending_decisions == [], "queue must be drained after a grouping pass"
