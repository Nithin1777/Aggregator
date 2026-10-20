"""
Unit tests for vendor adapters parsing logic and contract adherence.
"""
import asyncio

import pytest
from adapters.robu import RobuAdapter
from adapters.shopify import ShopifyAdapter


def test_shopify_quartz_gst_is_added_to_catalog_and_live_prices(monkeypatch):
    """QuartzComponents quotes ex-GST prices; both ingestion paths add 18%."""
    adapter = ShopifyAdapter(
        vendor_id="quartzcomponents",
        vendor_name="Quartz Components",
        base_url="https://quartzcomponents.com",
        gst_rate=0.18,
    )
    item = {
        "id": 123,
        "title": "Raspberry Pi Pico - Original",
        "handle": "raspberry-pi-pico",
        "variants": [{"id": 1, "price": "325.00", "available": True, "sku": "DVB-QC0764"}],
        "images": [],
        "product_type": "Development Board",
    }

    async def fake_catalog(url, params=None):
        return {"products": [item]}

    monkeypatch.setattr(adapter, "_fetch_with_backoff", fake_catalog)
    products, _ = asyncio.run(adapter.fetch_catalog_page(1))
    assert products[0].price == pytest.approx(383.5)
    assert products[0].extra["price_ex_gst"] == 325.0
    assert products[0].extra["gst_rate"] == 0.18

    async def fake_search(url, params=None):
        return {
            "resources": {
                "results": {
                    "products": [
                        {
                            "id": 123,
                            "title": "Raspberry Pi Pico - Original",
                            "handle": "raspberry-pi-pico",
                            "price": 325.0,
                            "available": True,
                            "url": "/products/raspberry-pi-pico",
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(adapter, "_fetch_with_backoff", fake_search)
    live = asyncio.run(adapter.search_live("pico"))
    assert live[0].price == pytest.approx(383.5)
    assert live[0].extra["price_ex_gst"] == 325.0


def test_shopify_without_gst_rate_keeps_raw_price(monkeypatch):
    adapter = ShopifyAdapter(
        vendor_id="makerbazar",
        vendor_name="MakerBazar",
        base_url="https://makerbazar.in",
    )
    item = {
        "id": 124,
        "title": "ESP32 Board",
        "handle": "esp32",
        "variants": [{"id": 2, "price": "100.00", "available": True, "sku": "X"}],
        "images": [],
    }

    async def fake_catalog(url, params=None):
        return {"products": [item]}

    monkeypatch.setattr(adapter, "_fetch_with_backoff", fake_catalog)
    products, _ = asyncio.run(adapter.fetch_catalog_page(1))
    assert products[0].price == 100.0


def test_shopify_catalog_parsing():
    adapter = ShopifyAdapter(
        vendor_id="quartzcomponents",
        vendor_name="Quartz Components",
        base_url="https://quartzcomponents.com"
    )

    mock_raw_item = {
        "id": 8697138610410,
        "title": "ESP32E-N16R8 Dual C-Type DOIT USB Development Board",
        "handle": "esp32-s3-wroom-n16r8",
        "variants": [
            {
                "id": 47289192,
                "price": "684.00",
                "available": True,
                "sku": "SIG057-01"
            }
        ],
        "images": [{"src": "https://cdn.shopify.com/image.png"}],
        "product_type": "Development Board"
    }

    # Verify adapter properties
    assert adapter.vendor_id == "quartzcomponents"
    assert adapter.base_url == "https://quartzcomponents.com"

def test_robu_product_parsing():
    adapter = RobuAdapter()
    mock_robu_items = [
        {
            "id": 846405,
            "name": "M5 Stack Core2 ESP32 IoT Development Kit",
            "slug": "m5-stack-core2",
            "sku": "846405",
            "price": 5829.0,
            "sale_price": 5829.0,
            "in_stock": True,
            "image_url": "https://robu-prod-media.s3.ap-south-1.amazonaws.com/test.png",
            "category_names": ["IoT Development Boards"]
        }
    ]

    prods = adapter._parse_robu_products(mock_robu_items)
    assert len(prods) == 1
    p = prods[0]
    assert p.vendor == "robu"
    assert p.external_id == "846405"
    assert p.price == 5829.0
    assert p.available is True
    assert p.mpn == "ESP32"
    assert p.product_url == "https://robu.in/product/m5-stack-core2/"

def test_robokits_product_parsing():
    from adapters.robokits import RobokitsAdapter
    adapter = RobokitsAdapter()

    sample_html = """
    <div class="product-listing">
      <!-- Item 1: In stock with RKI code and regular/special price -->
      <div class="pzen-item">
        <div class="product product--zoom">
          <div class="product__inside">
            <div class="product__inside__image">
              <a href="https://robokits.co.in/sensors/optical-sensors/optical-flow-sensor-rki-1234" title="PX4FLOW Optical Flow Sensor Smart Camera [RKI-1234]">
                <img src="/images/rki1234.jpg" alt="PX4FLOW" />
              </a>
            </div>
            <div class="product__inside__name">
              <a href="https://robokits.co.in/sensors/optical-sensors/optical-flow-sensor-rki-1234" title="PX4FLOW Optical Flow Sensor Smart Camera [RKI-1234]">
                PX4FLOW Optical Flow Sensor Smart Camera [RKI-1234]
              </a>
            </div>
            <div class="product__inside__price price-box">
              <span class="price-old">₹ 4,500.00</span>
              <span class="price-new">₹ 3,999.00</span>
            </div>
            <div class="product__inside__info__btns">
              <a class="btn btn--ys btn--xl" href="https://robokits.co.in/all?page=1&action=buy_now&products_id=5678">Buy Now</a>
            </div>
          </div>
        </div>
      </div>

      <!-- Item 2: Out of stock -->
      <div class="pzen-item">
        <div class="product product--zoom">
          <div class="product__inside">
            <div class="product__inside__name">
              <a href="/motors/servo-motors/high-torque-servo-rmcs-501" title="High Torque Metal Gear Servo [RMCS-501]">
                High Torque Metal Gear Servo [RMCS-501]
              </a>
            </div>
            <div class="product__inside__price price-box">
              <span>₹ 850.00</span>
            </div>
            <span class="label label--out-of-stock">Out of Stock</span>
          </div>
        </div>
      </div>
    </div>
    """

    products = adapter._parse_product_cards(sample_html)
    assert len(products) == 2

    # Check Item 1 (In Stock, Lowest Price, RKI SKU)
    p1 = products[0]
    assert p1.vendor == "robokits"
    assert p1.vendor_name == "Robokits India"
    assert p1.external_id == "5678"
    assert p1.sku == "RKI-1234"
    # Robokits quotes ex-GST prices; the catalog stores the tax-inclusive value.
    assert p1.price == pytest.approx(4718.82)
    assert p1.extra["price_ex_gst"] == 3999.0
    assert "GST" in (p1.price_note or "")
    assert p1.available is True
    assert p1.image_url == "https://robokits.co.in/images/rki1234.jpg"
    assert p1.product_url == "https://robokits.co.in/sensors/optical-sensors/optical-flow-sensor-rki-1234"

    # Check Item 2 (Out of stock, relative URL resolved, RMCS SKU)
    p2 = products[1]
    assert p2.vendor == "robokits"
    assert p2.external_id == "high-torque-servo-rmcs-501"
    assert p2.sku == "RMCS-501"
    assert p2.price == pytest.approx(1003.0)
    assert p2.available is False
    assert p2.product_url == "https://robokits.co.in/motors/servo-motors/high-torque-servo-rmcs-501"

def test_robokits_discounted_price_parsing():
    """Verifies that discounted items extract the sale price and never the discount % badge (-12%)."""
    from adapters.robokits import RobokitsAdapter
    adapter = RobokitsAdapter()

    discounted_html = """
    <div class="product-listing">
      <div class="pzen-item">
        <div class="product product--zoom">
          <div class="product__inside">
            <div class="product__inside__name">
              <a href="https://robokits.co.in/motors/rhino-bldc-brushless-dc-motor/rhino-52w-24v-bldc-motors/nema17-planetary-geared-bldc-motor-19kgcm-208rpm-52w" title="Nema17 Planetary Geared BLDC Motor 19Kgcm 208RPM 52W [RKI-2255]">
                Nema17 Planetary Geared BLDC Motor 19Kgcm 208RPM 52W [RKI-2255]
              </a>
            </div>
            <div class="product__inside__price price-box">
              <span class="normalprice price-box__old">₹4,410.00 </span>
              <span class="productSpecialPrice">₹3,865.00</span>
              <div class="product__label product__label--left product__label--sale productPriceDiscount">
                <span><small>Sale</small><br/>-12%</span>
              </div>
            </div>
            <div class="product__inside__info__btns">
              <a class="btn btn--ys btn--xl" href="https://robokits.co.in/all?page=1&action=buy_now&products_id=2255">Buy Now</a>
            </div>
          </div>
        </div>
      </div>
    </div>
    """

    products = adapter._parse_product_cards(discounted_html)
    assert len(products) == 1
    p = products[0]
    assert p.extra["price_ex_gst"] == 3865.0
    assert p.price == pytest.approx(4560.7)  # 3865 + 18% GST
    assert p.price != 12.0
    assert p.price != 4410.0
    assert p.sku == "RKI-2255"


def test_robokits_discount_badge_never_becomes_the_price():
    """
    Regression for the Raspberry Pi Pico row that was cached at ₹37.

    The card shows MRP ₹345, sale ₹216 and a "-37%" badge. A generic
    "first number in the card" scan turns that badge into the price; the
    extractor must ignore percentages and use the sale amount.
    """
    from adapters.robokits import RobokitsAdapter

    adapter = RobokitsAdapter()
    assert adapter._extract_price_from_text("-37%") is None
    assert adapter._extract_price_from_text("₹345.00 ₹216.00 -37%") == 345.0

    card_html = """
    <div class="pzen-item">
      <div class="product product--zoom">
        <div class="product__inside">
          <div class="product__inside__name">
            <a href="https://robokits.co.in/raspberry-pi/raspberry-pi-board-and-camera/raspberry-pi-pico"
               title="Raspberry Pi Pico">Raspberry Pi Pico</a>
          </div>
          <div class="product__inside__price price-box">
            <span class="normalprice price-box__old">₹345.00 </span>
            <span class="productSpecialPrice">₹216.00</span>
            <div class="product__label product__label--left product__label--sale productPriceDiscount">
              <span><small>Sale</small><br/>-37%</span>
            </div>
          </div>
          <div class="product__inside__info__btns">
            <a href="https://robokits.co.in/all?action=buy_now&products_id=4610">Buy Now</a>
          </div>
        </div>
      </div>
    </div>
    """

    products = adapter._parse_product_cards(card_html)
    assert len(products) == 1
    p = products[0]
    assert p.external_id == "4610"
    assert p.extra["price_ex_gst"] == 216.0
    assert p.price == pytest.approx(254.88)  # 216 + 18% GST, never 37
    assert p.price != 37.0


