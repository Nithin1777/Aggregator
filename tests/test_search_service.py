"""
Unit tests for aggregator search service comparison and clustering logic.
"""
import pytest
from models import UnifiedProduct
from search_service import AggregatorEngine

def test_group_and_compare_clustering():
    engine = AggregatorEngine()

    items = [
        # Robu item
        UnifiedProduct(
            vendor="robu",
            vendor_name="Robu.in",
            external_id="1",
            title="ESP32-CAM WiFi + Bluetooth Camera Module OV2640",
            normalized_title="esp32-cam wifi bluetooth camera module ov2640",
            mpn="ESP32-CAM",
            price=450.0,
            available=True,
            product_url="https://robu.in/product/esp32-cam/"
        ),
        # QuartzComponents item (cheaper and in stock)
        UnifiedProduct(
            vendor="quartzcomponents",
            vendor_name="Quartz Components",
            external_id="2",
            title="ESP32 CAM WiFi Bluetooth Camera Module Development Board",
            normalized_title="esp32 cam wifi bluetooth camera module development board",
            mpn="ESP32-CAM",
            price=399.0,
            available=True,
            product_url="https://quartzcomponents.com/products/esp32-cam"
        ),
        # MakerBazar item (out of stock, lower price)
        UnifiedProduct(
            vendor="makerbazar",
            vendor_name="MakerBazar",
            external_id="3",
            title="ESP32-CAM Development Board with OV2640 Camera",
            normalized_title="esp32-cam development board ov2640 camera",
            mpn="ESP32-CAM",
            price=380.0,
            available=False,
            product_url="https://makerbazar.in/products/esp32-cam"
        )
    ]

    groups = engine.group_and_compare(items)
    assert len(groups) == 1
    g = groups[0]
    assert g.mpn == "ESP32-CAM"
    assert g.total_offers == 3
    assert g.in_stock_count == 2
    # Best price among IN-STOCK items should be QuartzComponents at 399.0 (even though MakerBazar is 380 but out of stock)
    assert g.best_price == 399.0
    assert g.best_vendor == "Quartz Components"
