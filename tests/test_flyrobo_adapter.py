"""
Unit tests for the Flyrobo adapter.

Covers the deterministic IoI cookie challenge, schema.org JSON-LD product
parsing, OpenCart search-card parsing, sitemap registry sync, and the stable
offset/checkpoint behaviour required by ``run_vendor_ingest``.
"""
import asyncio
import json

import pytest

from adapters.base import VendorFetchError
from adapters.flyrobo import FlyroboAdapter
from config import settings
import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "flyrobo_test.db")
    db.init_db()
    return tmp_path / "flyrobo_test.db"


@pytest.fixture()
def adapter(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, "flyrobo_request_delay", 0)
    monkeypatch.setattr(settings, "max_retries", 2)
    return FlyroboAdapter()


def _run(coro):
    return asyncio.run(coro)


def _product_jsonld(*, availability="https://schema.org/InStock", price="83.00"):
    return {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "L293D Motor Drive Module ",
        "image": "https://www.flyrobo.in/image/cache/product/l293d-600x600.webp",
        "sku": "D-4508",
        "mpn": "",
        "model": "8",
        "offers": {
            "@type": "Offer",
            "priceCurrency": "INR",
            "price": price,
            "availability": availability,
        },
        "brand": {"@type": "Brand", "name": "FlyRobo"},
    }


def _product_page(jsonld):
    return f"""
    <html><head>
      <script type="application/ld+json">{json.dumps(jsonld)}</script>
    </head><body>
      <ul class="breadcrumb">
        <li><a href="/"><img alt="Flyrobo"></a></li>
        <li><a href="/motor-and-driver">Motor and Driver</a></li>
        <li><a href="/motor-and-driver/motor-drivers">Motor Drivers</a></li>
      </ul>
      <div class="product-info">
        <h1 class="page-title"><span>L293D Motor Drive Module</span></h1>
        <input type="hidden" name="product_id" value="8">
        <div class="product-price-new">₹83</div>
      </div>
    </body></html>
    """


def test_solve_ioi_cookie_challenge():
    assert FlyroboAdapter._solve_ioi("var x = H='abc';") == "1a47e90b"
    assert FlyroboAdapter._solve_ioi("<html>no token</html>") is None
    assert FlyroboAdapter._is_cookie_challenge("sessionStorage.IoI = 1")
    assert not FlyroboAdapter._is_cookie_challenge("ordinary product page")


def test_fetch_text_solves_challenge_and_retries(adapter):
    class FakeCookies:
        def __init__(self):
            self.set_calls = []

        def set(self, name, value, domain=None):
            self.set_calls.append((name, value, domain))

    class FakeResponse:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    class ChallengeSession:
        def __init__(self):
            self.calls = 0
            self.cookies = FakeCookies()

        async def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    200,
                    "<script>sessionStorage.IoI = 1; var H='abc';</script>",
                )
            return FakeResponse(200, "<html>real product page</html>")

    session = ChallengeSession()
    adapter._session = session

    text = _run(adapter._fetch_text("https://www.flyrobo.in/l293d_motor_drive_module"))
    assert text == "<html>real product page</html>"
    assert session.calls == 2
    assert session.cookies.set_calls == [("IoI", "1a47e90b", "www.flyrobo.in")]


def test_parse_product_page_jsonld(adapter):
    product = adapter._parse_product_page(
        _product_page(_product_jsonld()),
        "https://www.flyrobo.in/l293d_motor_drive_module",
    )

    assert product is not None
    assert product.vendor == "flyrobo"
    assert product.external_id == "8"
    assert product.sku == "D-4508"
    assert product.title == "L293D Motor Drive Module"
    assert product.price == pytest.approx(83.0)
    assert product.available is True
    assert product.image_url.endswith("l293d-600x600.webp")
    assert product.product_url == "https://www.flyrobo.in/l293d_motor_drive_module"
    assert product.category == "Motor and Driver > Motor Drivers"
    assert product.extra["brand"] == "FlyRobo"
    assert product.extra["slug"] == "l293d_motor_drive_module"
    assert product.extra["price_includes_gst"] is True
    assert product.extra["source"] == "sitemap"


def test_parse_out_of_stock_product(adapter):
    product = adapter._parse_product_page(
        _product_page(_product_jsonld(availability="https://schema.org/OutOfStock")),
        "https://www.flyrobo.in/l293d_motor_drive_module",
    )
    assert product is not None
    assert product.available is False


def test_stock_badge_overrides_stale_jsonld_availability(adapter):
    html = _product_page(_product_jsonld()).replace(
        "</div>\n    </body>",
        '<div class="product-label-diagonal">Out Of Stock (Restock: 22-10-2026)</div>'
        "</div>\n    </body>",
    )
    product = adapter._parse_product_page(
        html, "https://www.flyrobo.in/l293d_motor_drive_module"
    )
    assert product is not None
    assert product.available is False
    assert "Restock" in product.extra["stock_note"]


def test_parse_search_card_uses_sale_price_not_discount(adapter):
    html = """
    <div class="product-layout out-of-stock">
      <div class="product-thumb">
        <div class="image">
          <a href="https://www.flyrobo.in/l293d_motor_drive_module?search=l293d">
            <img data-src="https://www.flyrobo.in/image/cache/product/l293d-250x250.webp">
          </a>
        </div>
        <div class="name">
          <a href="https://www.flyrobo.in/l293d_motor_drive_module?search=l293d">L293D Motor Drive Module</a>
        </div>
        <p class="price">
          <span class="price-new">₹83</span>
          <span class="price-old">₹129</span>
          <span class="price-tax">-35%</span>
        </p>
        <div class="stats">
          <div class="stat-1"><a href="/flyrobo-brand">FlyRobo</a></div>
          <div class="stat-2"><span>SKU:</span><span>8</span></div>
        </div>
        <input type="hidden" name="product_id" value="8">
        <button onclick="quickview('8'); GAClick('L293D Motor Drive Module','8','83.00','FlyRobo','Motor and Driver > Motor Drivers','Motor and Driver');">View</button>
      </div>
    </div>
    """

    products = adapter._parse_search_page(html, "l293d")
    assert len(products) == 1
    product = products[0]
    assert product.external_id == "8"
    assert product.price == pytest.approx(83.0)
    assert product.extra["price_old"] == pytest.approx(129.0)
    assert product.available is False
    assert product.product_url == "https://www.flyrobo.in/l293d_motor_drive_module"
    assert product.category == "Motor and Driver > Motor Drivers"
    assert product.extra["brand"] == "FlyRobo"


def test_sitemap_index_and_product_parsing(adapter):
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://www.flyrobo.in/sitemap-category.xml</loc></sitemap>
      <sitemap><loc>https://www.flyrobo.in/sitemap-product-2.xml</loc></sitemap>
      <sitemap><loc>https://www.flyrobo.in/sitemap-product-1.xml</loc></sitemap>
    </sitemapindex>
    """
    product_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.flyrobo.in/l293d_motor_drive_module</loc></url>
      <url><loc>https://www.flyrobo.in/arduino-nano-r3</loc></url>
    </urlset>
    """

    assert adapter._parse_sitemap_index(index_xml) == [
        "https://www.flyrobo.in/sitemap-product-1.xml",
        "https://www.flyrobo.in/sitemap-product-2.xml",
    ]
    assert adapter._parse_product_sitemap(product_xml) == [
        ("l293d_motor_drive_module", "https://www.flyrobo.in/l293d_motor_drive_module"),
        ("arduino-nano-r3", "https://www.flyrobo.in/arduino-nano-r3"),
    ]


def test_sync_sitemaps_marks_stale_urls_missing(adapter, monkeypatch):
    index_xml = """<sitemapindex>
      <sitemap><loc>https://www.flyrobo.in/sitemap-product-1.xml</loc></sitemap>
    </sitemapindex>"""

    async def fake_fetch(url, params=None, timeout=30):
        if url.endswith("sitemap.xml"):
            return index_xml
        return """<urlset>
          <url><loc>https://www.flyrobo.in/l293d_motor_drive_module</loc></url>
        </urlset>"""

    monkeypatch.setattr(adapter, "_fetch_text", fake_fetch)

    # Seed a URL that is no longer in the sitemap.
    db.populate_vendor_registry(
        "flyrobo", [("retired-product", "https://www.flyrobo.in/retired-product")]
    )

    added = _run(adapter.sync_sitemaps_to_registry())
    assert added == 1
    stats = db.get_vendor_registry_stats("flyrobo")
    assert stats["total_registry"] == 2
    assert stats["indexed"] == 0
    assert stats["missing"] == 1
    assert stats["pending"] == 1


def test_sync_sitemaps_skips_stale_sweep_on_empty_partial_sitemap(adapter, monkeypatch):
    index_xml = """<sitemapindex>
      <sitemap><loc>https://www.flyrobo.in/sitemap-product-1.xml</loc></sitemap>
      <sitemap><loc>https://www.flyrobo.in/sitemap-product-2.xml</loc></sitemap>
    </sitemapindex>"""

    async def fake_fetch(url, params=None, timeout=30):
        if url.endswith("sitemap.xml"):
            return index_xml
        if url.endswith("sitemap-product-2.xml"):
            return "<urlset></urlset>"
        return """<urlset>
          <url><loc>https://www.flyrobo.in/l293d_motor_drive_module</loc></url>
        </urlset>"""

    monkeypatch.setattr(adapter, "_fetch_text", fake_fetch)

    db.populate_vendor_registry(
        "flyrobo", [("retired-product", "https://www.flyrobo.in/retired-product")]
    )

    added = _run(adapter.sync_sitemaps_to_registry())
    assert added == 1
    stats = db.get_vendor_registry_stats("flyrobo")
    # The incomplete sync must not mark still-published products as retired.
    assert stats["missing"] == 0
    assert stats["pending"] == 2


def _seed_registry(count=4):
    entries = [(f"product-{i}", f"https://www.flyrobo.in/product-{i}") for i in range(1, count + 1)]
    db.populate_vendor_registry("flyrobo", entries)
    return entries


def test_registry_pagination_is_continuable(adapter, monkeypatch):
    _seed_registry(5)

    async def no_sync():
        return 0

    async def fake_fetch(url, params=None, timeout=30):
        slug = url.rstrip("/").split("/")[-1]
        return _product_page(_product_jsonld()).replace(
            "L293D Motor Drive Module", slug
        )

    monkeypatch.setattr(adapter, "sync_sitemaps_to_registry", no_sync)
    monkeypatch.setattr(adapter, "_fetch_text", fake_fetch)

    page1, has_more1 = _run(adapter.fetch_catalog_page(page=1, limit=2))
    page2, has_more2 = _run(adapter.fetch_catalog_page(page=2, limit=2))
    page3, has_more3 = _run(adapter.fetch_catalog_page(page=3, limit=2))

    assert [p.extra["slug"] for p in page1] == ["product-1", "product-2"]
    assert [p.extra["slug"] for p in page2] == ["product-3", "product-4"]
    assert [p.extra["slug"] for p in page3] == ["product-5"]
    assert (has_more1, has_more2, has_more3) == (True, True, False)
    assert db.get_vendor_registry_stats("flyrobo")["indexed"] == 5


def test_404_does_not_shift_later_page_offsets(adapter, monkeypatch):
    """A missing product stays in the enumeration so page * batch offsets hold."""
    _seed_registry(4)
    requested = []

    async def no_sync():
        return 0

    async def fake_fetch(url, params=None, timeout=30):
        requested.append(url)
        if url.endswith("product-1"):
            return None  # genuine 404
        return _product_page(_product_jsonld())

    monkeypatch.setattr(adapter, "sync_sitemaps_to_registry", no_sync)
    monkeypatch.setattr(adapter, "_fetch_text", fake_fetch)

    page1, has_more1 = _run(adapter.fetch_catalog_page(page=1, limit=2))
    page2, has_more2 = _run(adapter.fetch_catalog_page(page=2, limit=2))

    assert [p.extra["slug"] for p in page1] == ["product-2"]
    assert [p.extra["slug"] for p in page2] == ["product-3", "product-4"]
    assert has_more1 is True and has_more2 is False
    # product-1 was not retried on page 2, and nothing was skipped.
    assert requested.count("https://www.flyrobo.in/product-1") == 1
    stats = db.get_vendor_registry_stats("flyrobo")
    assert stats["missing"] == 1
    assert stats["indexed"] == 3
    assert stats["coverage_pct"] == 100.0


def test_all_product_failures_raise_and_mark_failed(adapter, monkeypatch):
    _seed_registry(2)

    async def no_sync():
        return 0

    async def failing_fetch(url, params=None, timeout=30):
        raise VendorFetchError("boom")

    monkeypatch.setattr(adapter, "sync_sitemaps_to_registry", no_sync)
    monkeypatch.setattr(adapter, "_fetch_text", failing_fetch)

    with pytest.raises(VendorFetchError):
        _run(adapter.fetch_catalog_page(page=1, limit=2))

    stats = db.get_vendor_registry_stats("flyrobo")
    assert stats["failed"] == 2
    assert stats["indexed"] == 0


def test_search_live_parses_results(adapter, monkeypatch):
    html = """
    <div class="product-layout">
      <div class="product-thumb">
        <div class="name"><a href="/l293d_motor_drive_module?search=l293d">L293D Motor Drive Module</a></div>
        <p class="price"><span class="price-new">₹83</span></p>
        <button onclick="quickview('8')">View</button>
      </div>
    </div>
    """

    async def fake_fetch(url, params=None, timeout=30):
        assert params["route"] == "product/search"
        assert params["search"] == "l293d"
        return html

    monkeypatch.setattr(adapter, "_fetch_text", fake_fetch)
    products = _run(adapter.search_live("l293d", limit=5))

    assert len(products) == 1
    assert products[0].external_id == "8"
    assert products[0].price == pytest.approx(83.0)
    assert products[0].extra["search_query"] == "l293d"
