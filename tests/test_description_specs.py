"""Vendor-description extraction tests (the data layer behind vendor_specs)."""
import sqlite3

import db
from src.spec.description import (
    description_text,
    extract_description_attributes,
    normalize_key,
)


def test_extracts_two_column_specification_table():
    html = """
    <table>
      <tr><th>Specification</th><th>Value</th></tr>
      <tr><td>Operating Voltage</td><td>3.3V</td></tr>
      <tr><td>Output Current</td><td>800mA</td></tr>
      <tr><td>Tolerance</td><td>±1%</td></tr>
      <tr><td>Package</td><td>DIP-8</td></tr>
    </table>
    """
    attrs = {a["key"]: a for a in extract_description_attributes(html)}

    assert attrs["voltage"]["value"] == "3.3V"
    assert attrs["voltage"]["value_num"] == 3.3
    assert attrs["voltage"]["unit"] == "v"
    assert attrs["current"]["value_num"] == 800.0
    assert attrs["current"]["unit"] == "ma"
    assert "tolerance" in attrs
    assert attrs["package"]["value"] == "DIP-8"


def test_extracts_prose_key_value_lines_without_stealing_sentences():
    html = """
    <p>This module is easy to use.</p>
    <ul>
      <li><strong>Input Voltage:</strong> 5V</li>
      <li>Channels: 4</li>
      <li>Note: do not exceed the rating</li>
    </ul>
    """
    attrs = {a["key"]: a for a in extract_description_attributes(html)}

    assert attrs["voltage"]["value"] == "5V"
    assert attrs["channels"]["value"] == "4"
    assert "note" not in attrs
    assert "this_module_is_easy_to_use" not in attrs


def test_skips_headers_navigation_and_empty_values():
    html = """
    <table>
      <tr><td>Parameter</td><td>Details</td></tr>
      <tr><td>Also check these products</td><td>-</td></tr>
      <tr><td>Brand</td><td>WANPTEK</td></tr>
    </table>
    """
    attrs = {a["key"]: a for a in extract_description_attributes(html)}

    assert "brand" in attrs
    assert "parameter" not in attrs
    assert "also_check_these_products" not in attrs


def test_key_aliases_collapse_voltage_variants():
    assert normalize_key("Operating Voltage") == "voltage"
    assert normalize_key("Input Voltage") == "voltage"
    assert normalize_key("Rated Voltage") == "voltage"
    assert normalize_key("Product Dimensions") == "dimensions"


def test_description_text_is_readable_and_capped():
    html = "<h1>Title</h1><p>First line.</p><script>bad()</script><p>Second line.</p>"
    text = description_text(html, limit=100)

    assert "First line" in text
    assert "Second line" in text
    assert "bad()" not in text
    assert len(text) <= 100


def test_schema_round_trip_is_idempotent():
    conn = sqlite3.connect(":memory:")
    db._ensure_description_schema(conn)

    db.save_product_description(1, "hello", conn=conn)
    attrs = [
        {"key": "voltage", "label": "Voltage", "value": "3.3V", "value_num": 3.3, "unit": "v"},
        {"key": "package", "label": "Package", "value": "DIP-8", "value_num": None, "unit": None},
    ]
    assert db.replace_product_attributes(1, attrs, conn=conn) == 2
    assert db.replace_product_attributes(1, attrs[:1], conn=conn) == 1

    description = conn.execute(
        "SELECT description FROM product_descriptions WHERE product_id = 1;"
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT key, value FROM product_attributes WHERE product_id = 1 ORDER BY key;"
    ).fetchall()
    assert description == "hello"
    assert rows == [("voltage", "3.3V")]
    conn.close()
