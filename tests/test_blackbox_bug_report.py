"""Regressions derived from BUG_REPORT/AGGREGATE_MCP_BUG_REPORT.md.

Use controlled listings to test contracts independently of catalog churn.
"""
import asyncio
import json
import pytest
import db
from config import settings
from mcp_server import server
from src.spec.filters import parse_and_validate_filters, parse_engineering_value
from src.spec.vendor_index import normalize_vendor_value, recompute_vendor_stats
from src.search.product_role import identify_product


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "blackbox.db")
    db.init_db()
    types = {"resistance_ohm": ("float", "ohm"), "capacitance_uf": ("float", "uF"),
             "channel_type": ("enum", None), "fuse_type": ("enum", None),
             "thread_size": ("string", None), "resolution": ("string", None),
             "voltage": ("enum", "V"), "voltage_v": ("float", "V")}
    meta = {k: {"key": k, "type": t, "unit": u, "display_name": k, "count": 50,
                "enum_values": ["5", "12", "24"] if k == "voltage" else None,
                "value_counts": {}, "min": 0, "max": 1e9} for k, (t, u) in types.items()}
    monkeypatch.setattr(server, "_load_catalog_specs", lambda conn: meta)
    rows = [(1, "Surface Mount Resistor 10k", "resistors", "R10", 0),
            (2, "10k Resistor", "resistors", "R10", 5),
            (3, "N Channel Surface Mount MOSFET", "transistors", "Q1", 10),
            (4, "Automotive Blade Fuse", "fuses", "F1", 4),
            (5, "M3 screw", "mechanical", "M3", 2),
            (6, "LCD Display 1024 x 600 Pixels", "displays", "LCD1", 100),
            (7, "AMS1117 Regulator IC", "integrated-circuits", "AMS1117", 4),
            (8, "AMS1117 Power Module", "modules", "AMS1117", 20),
            (9, "Servo Bracket for SG90", "motor", "SG90", 2),
            (10, "SG90 Servo Motor", "motor", "SG90", 99),
            (11, "100nF Capacitor", "capacitors", "C1", 1)]
    conn = db.get_db_connection()
    for pid, title, cat, mpn, price in rows:
        conn.execute("INSERT INTO products (id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical,spec_category,mpn) VALUES (?,'robu','Robu',?,?,?,?,1,?,?,?,?)",
                     (pid, str(pid), title, title.lower(), price, f"https://test.invalid/{pid}", cat, cat, mpn))
    specs = [(1,"resistance_ohm","10000",10000),(2,"resistance_ohm","10000",10000),
             (3,"channel_type","N-Ch",None),(4,"fuse_type","blade",None),
             (5,"thread_size","M3",None),(6,"resolution","1024 × 600",None),
             (1,"voltage_v","12",12),(2,"voltage","12",None),(11,"capacitance_uf","0.1",0.1)]
    for pid,key,value,number in specs:
        conn.execute("INSERT INTO product_specs (product_id,key,value,value_num,value_type) VALUES (?,?,?,?,?)", (pid,key,value,number,types[key][0]))
    for key, (typ, unit) in types.items():
        for cat in {r[2] for r in rows}:
            conn.execute("INSERT INTO spec_definitions (category,key,display_name,value_type,unit) VALUES (?,?,?,?,?)", (cat,key,key,typ,unit))
    conn.execute("INSERT INTO product_vendor_specs (product_id,key,value,value_num,value_type,unit,raw_key,source) VALUES (1,'resistance','10000',10000,'float','ohm','resistance','description_text')")
    conn.commit()
    recompute_vendor_stats(conn)
    conn.close()
    return meta


@pytest.mark.parametrize("control", [chr(n) for n in range(32)])
def test_b01_query_control_characters_never_escape(catalog, control):
    for tool in (server.count_components, server.search_components):
        kwargs = {"refresh": False} if tool is server.search_components else {}
        result = tool(query=f"resistor{control}motor", **kwargs)
        assert isinstance(result, server.ToolResponseEnvelope)
        if control == "\0":
            assert not result.ok and result.error_code == "invalid_query"


def test_b02_b03_identifier_is_not_sold_item_identity(catalog):
    detail = server.get_product(7, refresh=False)
    assert all(not o["price_comparable"] for o in detail.data["vendor_offers"] if o["product_id"] != 7)
    mixed = server.search_by_mpn("AMS1117", refresh=False)
    assert not mixed.ok and mixed.error_code == "mpn_ambiguous"
    servo = server.search_by_mpn("SG90", refresh=False)
    assert servo.ok and [r["product_id"] for r in servo.data["results"]] == [10]
    broad = server.search_by_mpn("SG90", refresh=False, allow_ambiguous=True)
    assert broad.total_matches == 2


@pytest.mark.parametrize("key,value", [("thread_size","m3"),("resolution","1024x600"),
    ("resolution",{"in":["128x64","1024x600"]}),("channel_type","n channel"),
    ("channel_type","n-channel"),("channel_type","N-Ch"),("fuse_type","blade")])
def test_b04_b12_textual_values_are_filterable(catalog,key,value):
    count = server.count_components(filters={key:value})
    search = server.search_components(filters={key:value},refresh=False)
    assert count.ok and search.ok and count.total_matches == search.total_matches == 1


def test_b06_lists_are_membership_not_python_repr(catalog):
    a = server.count_components(filters={"channel_type":["n channel","p channel"]})
    b = server.count_components(filters={"channel_type":{"in":["n channel","p channel"]}})
    assert a.ok and a.total_matches == b.total_matches == 1
    assert isinstance(a.filters_applied["filters"]["channel_type"],dict)


@pytest.mark.parametrize("key",["resistance_ohm","channel_type","thread_size","resolution"])
def test_b07_b09_b11_b20_introspection_roundtrips(catalog,key):
    result = server.describe_spec(key)
    assert result.ok
    assert server.describe_spec(" " + key.upper() + " ").data == result.data
    if result.data["range"]:
        bounds = result.data["range"]
        count = server.count_components(filters={key:{"gte":bounds["min"],"lte":bounds["max"]}})
        assert count.total_matches == result.data["coverage_count"]
    for value,n in (result.data["values"] or {}).items():
        assert server.count_components(filters={key:value}).total_matches == n


def test_b13_all_operators_are_conjoined(catalog):
    result = server.count_components(filters={"resistance_ohm":{"eq":10000,"gte":1e6}})
    assert result.ok and result.total_matches == 0


@pytest.mark.parametrize("value",[{},None,True,{"eq":100,"tolerance":True},
    {"eq":100,"tolerance":"0.05"},{"eq":100,"tolerance":[]},{"eq":[]},{"in":[{}]}])
def test_b14_b19_malformed_filters_rejected(catalog,value):
    result = server.count_components(filters={"resistance_ohm":value})
    assert not result.ok and result.error_code == "invalid_filter_value"


def test_b15_vendor_typo_is_not_an_empty_search(catalog):
    result = server.count_components(vendor_filters={"typo_key":1})
    assert not result.ok and result.error_code == "unknown_filter_key"


def test_b16_sort_does_not_change_population(catalog):
    expected = server.count_components(price_max=0).total_matches
    assert expected == 1
    for sort in ("relevance","price_asc","price_desc"):
        result = server.search_components(price_max=0,sort=sort,refresh=False)
        assert result.ok and result.total_matches == expected and result.returned == 1


@pytest.mark.parametrize("key,text,number,unit,expected",[
    ("capacitance","82nF",82,"nf",0.082),
    ("capacitance_uf","100nF",None,"nf",0.1),
    ("inductance_uh","2.2mH",2.2,"mh",2200),
    ("current_a","500mA",500,"ma",0.5),
    ("frequency_mhz","20000kHz",20000,"khz",20)])
def test_b17_dimension_conversion_once(key,text,number,unit,expected):
    result = normalize_vendor_value(key,text,number,unit)
    assert result[1] == pytest.approx(expected)


def test_b18_b29_search_detail_same_specs_and_explicit_refresh(catalog):
    rows = server.search_components(filters={"resistance_ohm":10000},refresh=False).data["results"]
    for row in rows:
        detail = server.get_product(row["product_id"],refresh=False).data
        assert row["specs"] == detail["specs"]
        assert row["spec_sources"] == detail["spec_sources"]
        assert detail["price_stock_refresh"]["status"] == "skipped"


def test_b21_vendor_metadata_is_inspectable(catalog):
    data = server.describe_spec("resistance",vendor=True).data
    assert data["range"] == {"min":10000,"max":10000}
    assert data["values"] == {"10000.0":1}


@pytest.mark.parametrize("tool,args",[("count_components",{"category":123}),
    ("count_components",{"filters":"x"}),("search_components",{"limit":5.5}),
    ("get_product",{"product_id":None}),("count_components",{"typo":True})])
def test_b22_b23_transport_errors_use_envelope(catalog,tool,args):
    result = asyncio.run(server.mcp.call_tool(tool,args))
    payload = result[1] if isinstance(result,tuple) else result
    assert payload["ok"] is False and payload["error_code"] == "invalid_arguments"


def test_b24_b25_category_aliases_expand_and_reconcile(catalog):
    for category in (None,"resistors"):
        result = server.count_components(category=category,filters={"voltage":12})
        assert result.ok and result.total_matches == 2
        counts = result.filters_applied["filter_expanded"]["per_key_counts"]
        assert sum(counts.values()) >= result.total_matches


def test_b27_partial_refresh_warns():
    from models import PriceStockRefreshInfo
    warnings = server._price_stock_fallback_warning(PriceStockRefreshInfo(status="partial",attempted=True,refreshed_products=1,cached_products=9))
    assert warnings[0]["code"] == "price_refresh_partial"


@pytest.mark.parametrize("title",["Surface Mount Resistor","Surface Mount MOSFET","Automotive Blade Fuse"])
def test_modifiers_are_not_separately_sold_accessories(title):
    assert identify_product(title).role not in {"accessory","enclosure"}


def test_b30_every_transformation_is_disclosed(catalog):
    for value in (["N Channel"],"N Channel"):
        result = server.count_components(filters={"channel_type":value})
        assert result.ok and result.filters_applied["filter_value_coerced"]


def test_numeric_strings_are_in_declared_units():
    assert parse_engineering_value("capacitance_uf","100","uF") == 100


def test_category_rebuild_preserves_unrelated_index_rows(catalog):
    from src.spec.vendor_index import rebuild_vendor_specs
    conn = db.get_db_connection()
    rebuild_vendor_specs(conn, categories=["battery"])
    assert conn.execute("SELECT COUNT(*) FROM product_vendor_specs WHERE product_id=1").fetchone()[0] == 1
    conn.close()


def test_b05_b08_b10_catalog_advertises_real_populations(catalog):
    result = server.describe_catalog()
    for category in result.data["taxonomy"]:
        name = category["id"]
        assert server.count_components(category=name).total_matches == category["products_component"]
        assert server.count_components(category=name, include_related=True).total_matches == category["products_with_related"]
        for attr in category["filterable_attributes"]:
            assert attr["key"] in catalog
            for value in attr["values"] or []:
                count = server.count_components(category=name, filters={attr["key"]:value})
                assert count.ok and count.total_matches > 0


def test_b26_vendor_key_population_is_labeled_consistently(catalog):
    result = server.describe_catalog()
    conn = db.get_db_connection()
    expected = conn.execute("SELECT COUNT(*) FROM vendor_spec_stats_global").fetchone()[0]
    conn.close()
    assert result.data["vendor_spec_key_count"] == expected
    assert result.data["vendor_spec_discovery_min_count"] == 5


def test_conversion_property_matrix():
    # A deterministic 1,000-value matrix checks dimensional scaling rather
    # than memorizing the few capacitor IDs from the external report.
    for i in range(1, 1001):
        number = i / 37
        for key, unit, multiplier in (("capacitance_uf","nf",0.001),
                                      ("capacitance_uf","pf",1e-6),
                                      ("inductance_uh","mh",1000)):
            explicit = normalize_vendor_value(key,f"{number:.12g}{unit}",number,unit)
            parsed = normalize_vendor_value(key,f"{number:.12g}{unit}",None,unit)
            assert explicit[1] == pytest.approx(number * multiplier)
            assert parsed[1] == pytest.approx(explicit[1])


def test_offer_comparability_requires_evidence_across_groups():
    from src.search.listing_identity import comparison_evidence
    for i in range(100):
        anchor = {"title":f"X{i} Resistor 10k", "mpn":f"X{i}"}
        assert comparison_evidence(anchor,dict(anchor))["price_comparable"]
        for title in (f"X{i} Resistor Module",f"Resistor Kit for X{i}",
                      f"Resistor Holder for X{i}",f"X{i} Resistor 20k"):
            assert not comparison_evidence(anchor,{"title":title,"mpn":f"X{i}"})["price_comparable"]


def test_umbrella_values_preserve_literal_rows_and_roundtrip_metadata(catalog):
    catalog["package"] = {"key":"package","type":"enum","unit":None,"display_name":"Package",
                          "count":3,"enum_values":None,"value_counts":{}}
    conn = db.get_db_connection()
    for pid, value in ((1,"axial"),(2,"radial"),(11,"through-hole")):
        conn.execute("INSERT INTO product_specs (product_id,key,value,value_type) VALUES (?,'package',?,'enum')",(pid,value))
    conn.commit()
    conn.close()
    result = server.describe_spec("package",include_vendor_specs=False)
    assert result.ok
    count = server.count_components(filters={"package":"through-hole"},include_vendor_specs=False)
    assert count.total_matches == result.data["values"]["throughhole"] == 3


def test_numeric_alias_range_includes_enum_backed_evidence(catalog):
    conn = db.get_db_connection()
    conn.execute("UPDATE product_specs SET value='48' WHERE product_id=2 AND key='voltage'")
    conn.commit()
    conn.close()
    result = server.describe_spec("voltage_v",include_vendor_specs=False)
    assert result.data["range"] == {"min":12,"max":48}
    count = server.count_components(filters={"voltage_v":{"gte":12,"lte":48}},include_vendor_specs=False)
    assert result.data["coverage_count"] == count.total_matches == 2


def test_identity_guard_precedes_mpn_and_cached_merge_labels(catalog):
    import hashlib
    from normalizer import ComponentItem, match_components_v2
    a, b = "NE555 Precision Timer IC", "NE555 Adjustable Timer Module"
    assert not match_components_v2(a,b,4,20)[0]
    engine = server._engine
    pair = hashlib.sha1("::".join(sorted([a.lower(),b.lower()])).encode()).hexdigest()[:16]
    previous = engine._llm_labels
    try:
        engine._llm_labels = {pair:{"label":"same","reason":"old_mpn_rollup"}}
        merged, reason, source = engine._evaluate_pair_merge(ComponentItem(a,4,mpn="NE555"),ComponentItem(b,20,mpn="NE555"))
        assert not merged and source == "identity_guard"
    finally:
        engine._llm_labels = previous


def test_empty_categories_do_not_capture_inferred_query_scope(catalog):
    conn = db.get_db_connection()
    conn.execute("UPDATE products SET category_canonical=NULL,spec_category=NULL")
    conn.commit()
    conn.close()
    result = server.search_components(query="motor",refresh=False)
    assert result.ok and result.total_matches > 0
    explicit = server.count_components(category="motor")
    assert explicit.ok and explicit.total_matches == 0


def test_vendor_fallback_preserves_the_canonical_property_meaning(catalog):
    catalog["reverse_voltage_v"] = {"key":"reverse_voltage_v","type":"float","unit":"V",
        "display_name":"Reverse voltage","count":1,"enum_values":None,"value_counts":{}}
    conn = db.get_db_connection()
    conn.execute("UPDATE products SET category_canonical='diodes', title='24V Zener Diode' WHERE id=3")
    for key,source in (("voltage","description_text"),("reverse_voltage_v","vendor_alias")):
        conn.execute("INSERT INTO product_vendor_specs (product_id,key,value,value_num,value_type,unit,raw_key,source) VALUES (3,?,'24',24,'float','V','voltage',?)",(key,source))
    conn.commit()
    recompute_vendor_stats(conn)
    conn.close()
    generic = server.count_components(filters={"voltage":24})
    assert generic.ok and generic.total_matches == 0
    reverse = server.count_components(filters={"reverse_voltage_v":24})
    assert reverse.ok and reverse.total_matches == 1
    raw = server.count_components(vendor_filters={"voltage":24})
    assert raw.ok and raw.total_matches == 1
    compact = server.search_components(vendor_filters={"voltage":24},compact=True,refresh=False)
    assert compact.data["results"][0]["specs"]["reverse_voltage_v"] == "24"
