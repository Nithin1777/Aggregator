"""Robu GraphQL catalog-completion mapping tests (no network)."""
from scripts.ingest_robu_graphql import _product_from_detail


def test_product_from_detail_maps_vendor_fields():
    detail = {
        "id": "894292",
        "sku": "894292",
        "name": "Raspberry Pi Pico",
        "url_key": "raspberry-pi-pico",
        "price": {"regular": 384, "final": 384, "special": None, "discount": 0},
        "in_stock": True,
        "categories": [{"id": "1", "name": "Raspberry Pi Pico", "url_path": "raspberry-pi-pico"}],
        "images": [{"url": "https://example.com/pico.jpg"}],
    }

    product = _product_from_detail("raspberry-pi-pico", detail)

    assert product.vendor == "robu"
    assert product.external_id == "894292"
    assert product.title == "Raspberry Pi Pico"
    assert product.price == 384.0
    assert product.available is True
    assert product.product_url == "https://robu.in/product/raspberry-pi-pico/"
    assert product.category == "Raspberry Pi Pico"
    assert product.image_url == "https://example.com/pico.jpg"
    assert product.extra["slug"] == "raspberry-pi-pico"
