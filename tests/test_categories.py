"""
Tests for the unified catalog taxonomy and the category-aware search path.

The taxonomy lives in `config/taxonomy.yaml` and is loaded by
`src/spec/taxonomy_loader.py`; there is no second, parallel taxonomy.
"""
import pytest

import db
from src.spec.taxonomy_loader import (
    classify_product,
    contains_phrase,
    get_taxonomy,
    is_junk_raw,
    tokenize,
)


# ---------------------------------------------------------------------------
# Tokenisation / plural tolerant matching
# ---------------------------------------------------------------------------

def test_tokenize_splits_and_lowercases():
    assert tokenize("MLCCs- Multi Layer Ceramic Capacitors") == [
        "mlccs", "multi", "layer", "ceramic", "capacitors"
    ]
    assert tokenize(None) == []


@pytest.mark.parametrize(
    "haystack,phrase,expected",
    [
        (["tactile", "switches"], ["switch"], True),
        (["switches"], ["switch"], True),
        (["diodes"], ["diode"], True),
        (["batteries"], ["battery"], True),
        (["receptacles"], ["receptacle"], True),
        (["modules"], ["module"], True),
        (["ics"], ["ic"], True),
        (["motor", "drivers"], ["motor"], True),
        (["motor", "drivers"], ["drivers"], True),
        (["resistors"], ["resistor"], True),
        (["resistors"], ["capacitor"], False),
    ],
)
def test_contains_phrase_plural_tolerance(haystack, phrase, expected):
    assert contains_phrase(haystack, phrase) is expected


# ---------------------------------------------------------------------------
# Junk categories
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw", ["New Products 5", "Popular Products", "Default", "COMPONENTS", None, ""]
)
def test_junk_raw_categories_are_detected(raw):
    assert is_junk_raw(raw) is True


@pytest.mark.parametrize("raw", ["Film Resistors", "IoT Development Boards", "Tactile Switches"])
def test_meaningful_raw_categories_are_not_junk(raw):
    assert is_junk_raw(raw) is False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Film Resistors", "resistors"),
        ("SMD Resistors", "resistors"),
        ("MLCCs- Multi Layer Ceramic Capacitors", "capacitors"),
        ("Tactile Switches", "switches"),
        ("MOSFET", "semiconductors"),
        ("Schottky Diodes", "diodes"),
        ("Logic IC", "integrated-circuits"),
        ("8-bit Microcontrollers IC", "microcontrollers"),
        ("PCB Receptacles", "connectors"),
        ("Power Relays", "relay"),
        ("TFT LCD Displays", "displays"),
        ("IoT Development Boards", "modules"),
        ("Buck Converter", "power"),
        ("Li-ion Charger/BMS", "battery"),
        ("N20 Metal Gear Motors", "motor"),
        ("Drone & Accessories", "drones"),
        ("Resettable Fuses", "fuses"),
        ("Crystals", "crystals"),
        ("Digital Multimeters", "tools"),
        ("Arduino Kit", "kits"),
    ],
)
def test_classify_uses_vendor_category(raw, expected):
    slug, source, _confidence = classify_product(raw, "some product title")
    assert slug == expected
    assert source == "vendor"


def test_classify_falls_back_to_title_when_category_is_junk():
    slug, source, _confidence = classify_product(
        "New Products 5", "DHT11 Temperature and Humidity Sensor"
    )
    assert slug == "sensors"
    assert source == "title"


def test_classify_falls_back_to_spec_scope_as_last_resort():
    slug, source, _confidence = classify_product("New Products 5", "zzzz qqqq", spec_category="battery")
    assert slug == "battery"
    assert source == "spec"


def test_classify_returns_none_when_nothing_matches():
    slug, source, _confidence = classify_product("New Products 5", "zzzz qqqq")
    assert slug is None
    assert source == "none"


def test_taxonomy_has_unique_ids_and_labels():
    taxonomy = get_taxonomy()
    ids = list(taxonomy.categories.keys())
    assert len(ids) == len(set(ids))
    assert all(c.display for c in taxonomy.categories.values())
    assert all(c.icon for c in taxonomy.categories.values())
    assert "other" in taxonomy.categories


def test_taxonomy_keeps_the_spec_categories_and_their_attributes():
    """The three spec categories keep their ids, keywords and attributes."""
    taxonomy = get_taxonomy()

    motor = taxonomy.categories["motor"]
    assert motor.display == "Motors & Drivers"
    assert motor.detect_keywords == ["motor", "motors", "bldc", "stepper"]
    assert set(motor.attributes.keys()) == {"voltage", "rpm", "current_a", "torque_kgcm"}

    battery = taxonomy.categories["battery"]
    assert battery.detect_keywords == ["battery", "batteries", "lipo", "li-ion", "lifepo4", "18650"]
    assert set(battery.attributes.keys()) == {
        "voltage", "cells", "capacity_mah", "discharge_c", "chemistry"
    }

    relay = taxonomy.categories["relay"]
    assert relay.detect_keywords == ["relay", "relays"]
    assert set(relay.attributes.keys()) == {"channels"}

    assert set(taxonomy.categories_with_attributes()) == {"motor", "battery", "relay"}


def test_taxonomy_covers_the_whole_catalog():
    taxonomy = get_taxonomy()
    assert len(taxonomy.categories) >= 30


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------

def test_db_exposes_category_counts():
    db.init_db()
    counts = db.get_category_counts()
    assert isinstance(counts, dict)
    assert counts, "expected at least one classified category"
    assert all(v >= 0 for v in counts.values())


def test_db_category_filter_narrows_search_results():
    db.init_db()
    products = db.list_products_by_category(category="resistors", limit=25)
    assert products
    assert all(p.category_canonical == "resistors" for p in products)


def test_db_suggestions_include_part_numbers():
    db.init_db()
    suggestions = db.suggest_products("esp3", limit=6)
    assert suggestions
    kinds = {s["type"] for s in suggestions}
    assert "mpn" in kinds
    assert any(s["type"] == "mpn" and s["value"].upper().startswith("ESP3") for s in suggestions)


def test_db_suggestions_are_empty_for_single_character_prefix():
    db.init_db()
    assert db.suggest_products("e", limit=5) == []


# ---------------------------------------------------------------------------
# Spec-attribute filtering (shared with the MCP server)
# ---------------------------------------------------------------------------

def test_db_spec_filter_narrows_search_results():
    db.init_db()
    unfiltered = db.search_products("motor", limit=500)
    filtered = db.search_products("motor", limit=500, spec_filters={"rpm": {"lte": 100}})
    assert filtered, "expected motors with a known rpm"
    assert len(filtered) < len(unfiltered)
    # Every filtered hit must have an rpm of at most 100.
    conn = db.get_db_connection()
    try:
        for product in filtered:
            row = conn.execute(
                "SELECT value_num FROM product_specs WHERE product_id = ? AND key = 'rpm';",
                (product.id,),
            ).fetchone()
            assert row is not None and row[0] <= 100
    finally:
        conn.close()


def test_db_spec_filter_rejects_unknown_keys():
    db.init_db()
    with pytest.raises(ValueError):
        db.search_products("motor", limit=10, spec_filters={"not_a_spec_key": {"gte": 1}})


def test_db_browse_mode_applies_spec_filters():
    db.init_db()
    unfiltered = db.list_products_by_category(category="motor", limit=5000)
    filtered = db.list_products_by_category(
        category="motor", limit=5000, spec_filters={"rpm": {"lte": 100}}
    )
    assert filtered, "expected motors with a known rpm"
    assert len(filtered) < len(unfiltered)


def test_shared_filter_builder_keeps_join_and_where_params_apart():
    from src.spec.filters import build_spec_clauses, load_catalog_specs

    conn = db.get_db_connection()
    try:
        advertised = load_catalog_specs(conn)
    finally:
        conn.close()

    joins, join_params, where_clauses, where_params = build_spec_clauses(
        {"rpm": {"lte": 100}, "voltage": {"gte": 12}}, advertised
    )
    assert len(joins) == 2
    assert join_params == ["rpm", "voltage"]
    assert len(where_clauses) == 2
    assert where_params == [100.0, 12.0]


def test_mcp_and_web_share_the_same_filter_semantics():
    """The MCP server and the web layer must agree on what a filter means."""
    from mcp_server.server import count_components

    res = count_components(category="motor", filters={"rpm": {"lte": 100}})
    assert res.ok is True
    assert res.total_matches > 0
