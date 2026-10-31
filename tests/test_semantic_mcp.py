"""Exercise shared semantics through both public tools on a controlled catalog."""
import pytest

import db
from config import settings
from mcp_server import server
from src.spec.vendor_index import recompute_vendor_stats


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "semantics.db")
    db.init_db()
    metadata = {"capacity_mah": {"key": "capacity_mah", "type": "float", "unit": "mAh", "count": 6,
                                 "display_name": "Capacity", "enum_values": None, "value_counts": {}}}
    monkeypatch.setattr(server, "_load_catalog_specs", lambda conn: metadata)
    conn = db.get_db_connection()
    conn.execute("INSERT INTO spec_definitions (category,key,display_name,value_type,unit) VALUES ('battery','capacity_mah','Capacity','float','mAh')")
    products = [
        (1, "24V battery pack 40Ah", 40000, None),
        (2, "24V battery pack 10Ah", 10000, 30),
        (3, "24V battery pack 40Ah low current", 40000, 10),
        (4, "24V battery case 50Ah", 50000, 100),
        (5, "24V 100A BMS board", 50000, 100),
        (6, "24V battery pack unspecified", None, None),
    ]
    for pid, title, capacity, current in products:
        conn.execute("INSERT INTO products (id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical,spec_category) "
                     "VALUES (?, 'robu','Robu',?,?,?,100,1,?,'battery','battery')",
                     (pid, str(pid), title, title.lower(), f"https://example.test/{pid}"))
        if capacity is not None:
            conn.execute("INSERT INTO product_specs (product_id,key,value,value_num,value_type) VALUES (?,'capacity_mah',?,?,'float')", (pid, str(capacity), capacity))
        if current is not None:
            conn.execute("INSERT INTO product_vendor_specs (product_id,key,value,value_num,value_type) VALUES (?,'continuous_discharge_current_a',?,?,'float')", (pid, str(current), current))
    conn.commit()
    recompute_vendor_stats(conn)
    conn.close()
    return metadata


def test_sparse_compound_constraints_offer_only_unknown_candidates(catalog):
    filters = {"capacity_mah": {"gte": 30000}, "continuous_discharge_current_a": {"gte": 20}}
    count = server.count_components(category="battery", filters=filters)
    search = server.search_components(category="battery", filters=filters, refresh=False, include_partial_candidates=True)
    assert count.ok and search.ok
    assert count.total_matches == search.total_matches == 0
    assert search.data["confirmed_matches"] == search.data["results"] == []
    assert search.returned == 0 and search.next_cursor is None
    assert search.data["partial_candidates"] == [{
        "product_id": 1, "title": "24V battery pack 40Ah", "satisfied": ["capacity_mah"],
        "unknown": ["continuous_discharge_current_a"], "failed": [], "requires_verification": True,
    }]
    assert count.data["filter_recall"] == search.data["filter_recall"]
    assert count.data["filter_recall"]["base_matches"] == 4


def test_strict_matches_cannot_inherit_subsystem_or_enclosure_specs(catalog):
    params = dict(category="battery", filters={"continuous_discharge_current_a": {"gte": 20}})
    search = server.search_components(**params, refresh=False)
    count = server.count_components(**params)
    assert count.total_matches == search.total_matches == 1
    assert [r["product_id"] for r in search.data["results"]] == [2]
    assert search.data["results"][0]["spec_sources"]["continuous_discharge_current_a"] == "vendor"


def test_broad_discovery_preserves_identity_and_attribute_ownership(catalog):
    result = server.search_components(category="battery", include_related=True, refresh=False)
    assert result.total_matches == 6
    case = next(r for r in result.data["results"] if r["product_id"] == 4)
    assert case["product_role"] == "battery_enclosure"
    assert "capacity_mah" not in case["specs"]
    assert case["related_specs"]["capacity_mah"] == "50000"
    details = server.get_product(4, refresh=False)
    assert details.data["specs"] == {}
    assert details.data["spec_details"]["capacity_mah"]["subject"] == "supported_component"


def test_cursors_bind_identity_and_vendor_mode(catalog):
    first = server.search_components(category="battery", limit=1, refresh=False)
    assert first.next_cursor
    for change in ({"include_related": True}, {"include_vendor_specs": False}):
        result = server.search_components(category="battery", limit=1, refresh=False, cursor=first.next_cursor, **change)
        assert not result.ok and result.error_code == "stale_cursor"


def test_pagination_crosses_fixed_ranking_window_without_duplicates(catalog):
    conn = db.get_db_connection()
    for pid in range(7, 450):
        title = f"24V battery pack variant {pid}"
        conn.execute("INSERT INTO products (id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical) "
                     "VALUES (?,'robu','Robu',?,?,?,100,1,?,'battery')", (pid, str(pid), title, title.lower(), f"https://example.test/{pid}"))
    conn.commit()
    conn.close()
    cursor, seen = None, []
    while True:
        result = server.search_components(category="battery", query="battery", limit=50, cursor=cursor, refresh=False)
        assert result.ok
        seen.extend(r["product_id"] for r in result.data["results"])
        cursor = result.next_cursor
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == result.total_matches == 447
