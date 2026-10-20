"""
Unit tests for SQLite database operations and continuable scraping checkpoints.
"""
from pathlib import Path
import pytest
from config import settings
import db
from models import UnifiedProduct

@pytest.fixture(autouse=True)
def temp_test_db(tmp_path, monkeypatch):
    test_db_path = tmp_path / "test_components.db"
    monkeypatch.setattr(settings, "db_path", test_db_path)
    db.init_db()
    return test_db_path

def test_upsert_and_search_products():
    sample_products = [
        UnifiedProduct(
            vendor="robu",
            vendor_name="Robu.in",
            external_id="101",
            title="ESP32 Development Board WiFi Bluetooth",
            normalized_title="esp32 development board wifi bluetooth",
            mpn="ESP32",
            sku="ROBU-ESP32-1",
            price=299.0,
            available=True,
            product_url="https://robu.in/product/esp32-dev-board/",
            category="IoT"
        ),
        UnifiedProduct(
            vendor="quartzcomponents",
            vendor_name="Quartz Components",
            external_id="202",
            title="ESP32 NodeMCU Development Board",
            normalized_title="esp32 nodemcu development board",
            mpn="ESP32",
            sku="QC-ESP-01",
            price=275.0,
            available=False,
            product_url="https://quartzcomponents.com/products/esp32-nodemcu",
            category="Microcontrollers"
        )
    ]

    count = db.upsert_products(sample_products)
    assert count == 2

    # Verify search
    results = db.search_products("ESP32")
    assert len(results) == 2

    # Verify update / upsert idempotency
    updated_product = sample_products[0].model_copy(update={"price": 285.0, "available": False})
    db.upsert_products([updated_product])

    re_fetched = db.search_products("ROBU-ESP32-1")
    assert len(re_fetched) == 1
    assert re_fetched[0].price == 285.0
    assert re_fetched[0].available is False

def test_apply_vendor_gst_is_idempotent():
    db.upsert_products([
        UnifiedProduct(
            vendor="quartzcomponents",
            vendor_name="Quartz Components",
            external_id="qc-1",
            title="Raspberry Pi Pico - Original",
            normalized_title="raspberry pi pico original",
            mpn="RASPBERRY-PI-PICO",
            price=325.0,
            available=True,
            product_url="https://quartzcomponents.com/products/raspberry-pi-pico",
            extra={"handle": "raspberry-pi-pico"},
        )
    ])

    first = db.apply_vendor_gst("quartzcomponents", 0.18)
    assert first["updated"] == 1
    stored = db.search_products("Raspberry Pi Pico")[0]
    assert stored.price == pytest.approx(383.5)
    assert stored.extra["price_ex_gst"] == 325.0
    assert stored.extra["gst_rate"] == 0.18

    second = db.apply_vendor_gst("quartzcomponents", 0.18)
    assert second["updated"] == 0
    assert second["skipped"] == 1
    stored_again = db.search_products("Raspberry Pi Pico")[0]
    assert stored_again.price == pytest.approx(383.5)


def test_scrape_checkpoint_continuity():
    # 1. Initially empty
    chk = db.get_checkpoint("makerbazar")
    assert chk.last_page == 0
    assert chk.status == "idle"

    # 2. Update checkpoint after scraping 2 pages
    db.save_checkpoint(
        vendor="makerbazar",
        last_page=2,
        last_offset=0,
        total_ingested=500,
        status="in_progress"
    )

    chk2 = db.get_checkpoint("makerbazar")
    assert chk2.last_page == 2
    assert chk2.total_ingested == 500
    assert chk2.status == "in_progress"

    # 3. Reset checkpoint
    db.reset_checkpoint("makerbazar")
    chk3 = db.get_checkpoint("makerbazar")
    assert chk3.last_page == 0
