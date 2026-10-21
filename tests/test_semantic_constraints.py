"""Cross-family identity checks and constraint-algebra invariants on synthetic data.

These do not depend on today's live catalog counts or vendor availability.
"""
import sqlite3
import pytest

from src.search.product_role import identify_product, identity_matches, register_semantics, spec_subject
from src.spec.constraints import compile_predicates, summarize_states, discover_candidates
from src.search.requirements import extract_requirements


@pytest.mark.parametrize("noun,family", [
    ("motor", "motor"), ("battery", "battery"), ("speaker", "speakers"),
    ("sensor", "sensors"), ("OLED display", "displays"), ("ESP32", "microcontrollers"),
])
@pytest.mark.parametrize("attachment,role", [("case", "enclosure"), ("bracket", "accessory"), ("holder", "accessory")])
def test_attachment_ownership_generalizes_across_families(noun, family, attachment, role):
    title = f"24V 100A {noun} {attachment}"
    identity = identify_product(title, family)
    assert identity.role == role
    assert identity_matches(title, family, family) == 0
    assert spec_subject(title, family, "voltage") == "supported_component"
    assert spec_subject(title, family, "material") == "sold_item"


@pytest.mark.parametrize("title,family", [
    ("24V DC motor with mounting bracket", "motor"),
    ("24V battery pack with BMS", "battery"),
    ("8 ohm speaker with grille", "speakers"),
    ("OLED display with protective case", "displays"),
    ("Temperature sensor with enclosure", "sensors"),
])
def test_included_accessory_does_not_replace_sold_item(title, family):
    assert identity_matches(title, family, family) == 1


@pytest.mark.parametrize("title", ["24V 100A BMS board", "24V motor driver", "Battery charger 24V"])
def test_subsystem_rating_never_verifies_a_complete_component(title):
    assert identity_matches(title, "battery", "battery") == 0
    assert identity_matches(title, "motor", "motor") == 0


def test_spec_ownership_depends_on_the_entity_not_just_units():
    assert spec_subject("24V 100A BMS board", "battery", "continuous_discharge_current_a") == "sold_item"
    assert spec_subject("24V 100A BMS board", "battery", "capacity_mah") == "supported_component"
    assert spec_subject("24V battery charger for 10Ah packs", "battery", "capacity_mah") == "supported_component"


def test_units_alone_do_not_establish_identity():
    assert identify_product("24V 100A 10000mAh").role == "unknown"
    assert identity_matches("24V 100A 10000mAh", "battery", "battery") == 0


def test_power_source_is_not_sold_entity():
    assert identity_matches("Battery powered speaker", "speakers", "speakers") == 1
    assert identity_matches("Battery powered speaker", "battery", "battery") == 0


def test_ambiguous_entities_abstain_instead_of_using_category_as_proof():
    item = identify_product("ESP32 sensor board", "sensors")
    assert item.role == "unknown" and item.confidence == 0


@pytest.mark.parametrize("title", ["HX711 Load Cell Amplifier Module", "5kg Load Cell", "Solar Cell"])
def test_polysemous_nouns_need_positive_entity_context(title):
    assert identity_matches(title, "battery", "battery") == 0
    assert spec_subject(title, "battery", "capacity_mah") != "sold_item"


@pytest.fixture
def evidence_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    register_semantics(conn)
    conn.executescript("""
        CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT, category_canonical TEXT);
        CREATE TABLE product_specs (product_id INTEGER, key TEXT, value TEXT, value_num REAL);
        CREATE TABLE product_vendor_specs (product_id INTEGER, key TEXT, value TEXT, value_num REAL, raw_key TEXT, source TEXT DEFAULT 'vendor');
    """)
    for pid in range(1, 8):
        conn.execute("INSERT INTO products VALUES (?, ?, 'battery')", (pid, f"Battery pack {pid}"))
    # 1 confirmed, 2 partial, 3 failed+unknown, 4 all unknown, 5 promoted
    # mismatch vs conflicting vendor evidence, 6 unusable, 7 vendor fallback.
    conn.executemany("INSERT INTO product_specs VALUES (?, ?, ?, ?)", [
        (1, "capacity_mah", "20000", 20000), (1, "current_a", "30", 30),
        (2, "capacity_mah", "20000", 20000), (3, "capacity_mah", "5000", 5000),
        (5, "capacity_mah", "5000", 5000), (5, "current_a", "30", 30),
        (6, "capacity_mah", "N/A", None), (6, "current_a", "30", 30),
    ])
    conn.executemany("INSERT INTO product_vendor_specs (product_id,key,value,value_num) VALUES (?, ?, ?, ?)", [
        (5, "capacity_mah", "20000", 20000), (6, "capacity_mah", "20000", 20000),
        (7, "capacity_mah", "20000", 20000), (7, "current_a", "30", 30),
    ])
    yield conn
    conn.close()


META = {key: {"type": "float", "unit": unit} for key, unit in [("capacity_mah", "mAh"), ("current_a", "A")]}
FILTERS = {"capacity_mah": {"gte": 10000}, "current_a": {"gte": 20}}
BASE = "SELECT id FROM products"


def _matching_ids(conn, predicates):
    clauses = " AND ".join(p.match_sql for p in predicates)
    return {r[0] for r in conn.execute(f"SELECT p.id FROM products p WHERE {clauses}",
                                      [v for p in predicates for v in p.match_params])}


def test_partition_is_disjoint_and_exhaustive(evidence_db):
    predicates = compile_predicates(FILTERS, META)
    summary = summarize_states(evidence_db, BASE, [], predicates)
    assert summary["base_matches"] == 7
    assert summary["confirmed_matches"] == 2
    assert summary["filtered_out_for_value"] == 2
    assert summary["missing_spec"] == 3
    assert summary["any_unknown"] == 4
    assert summary["confirmed_matches"] == len(_matching_ids(evidence_db, predicates))


def test_adding_constraints_cannot_increase_confirmed_matches(evidence_db):
    both = _matching_ids(evidence_db, compile_predicates(FILTERS, META))
    for key, value in FILTERS.items():
        assert both <= _matching_ids(evidence_db, compile_predicates({key: value}, META))


def test_conflicting_or_malformed_promoted_values_cannot_be_overridden(evidence_db):
    assert _matching_ids(evidence_db, compile_predicates(FILTERS, META)) == {1, 7}
    assert _matching_ids(evidence_db, compile_predicates(FILTERS, META, hybrid=False)) == {1}


def test_partial_discovery_excludes_failures_and_all_unknown_rows(evidence_db):
    predicates = compile_predicates(FILTERS, META)
    candidates = discover_candidates(evidence_db, BASE, [], predicates)
    assert {c["product_id"] for c in candidates} == {2, 6}
    for candidate in candidates:
        assert candidate["satisfied"]
        assert candidate["unknown"]
        assert candidate["failed"] == []
        assert candidate["requires_verification"]


def test_vendor_only_uses_same_state_partition(evidence_db):
    predicates = compile_predicates({}, META, vendor_filters=FILTERS, vendor_metadata=META)
    summary = summarize_states(evidence_db, BASE, [], predicates)
    assert summary["confirmed_matches"] == len(_matching_ids(evidence_db, predicates)) == 1
    assert sum(summary[k] for k in ("confirmed_matches", "filtered_out_for_value", "missing_spec")) == 7


def test_aliases_do_not_turn_conflicting_requirements_into_alternatives(evidence_db):
    from mcp_server.server import _expand_unscoped_aliases
    metadata = {"voltage": {"type": "float", "unit": "V"},
                "voltage_v": {"type": "float", "unit": "V"}}
    _, aliases, _ = _expand_unscoped_aliases({"voltage": 12, "voltage_v": 24}, metadata)
    assert len(aliases) == 2
    evidence_db.execute("INSERT INTO product_specs VALUES (1,'voltage','12',12)")
    predicates = compile_predicates({}, metadata, aliases=aliases)
    assert _matching_ids(evidence_db, predicates) == set()


def test_conflicting_equivalent_evidence_is_unknown(evidence_db):
    metadata = {"voltage": {"type": "float", "unit": "V"},
                "voltage_v": {"type": "float", "unit": "V"}}
    evidence_db.executemany("INSERT INTO product_specs VALUES (1,?,?,?)", [
        ("voltage", "12", 12), ("voltage_v", "24", 24),
    ])
    aliases = [{"group": "voltage", "keys": list(metadata), "value": 12}]
    predicates = compile_predicates({}, metadata, aliases=aliases)
    assert _matching_ids(evidence_db, predicates) == set()
    summary = summarize_states(evidence_db, BASE, [], predicates)
    assert summary["missing_spec"] == 7
    assert summary["filtered_out_for_value"] == 0


def test_vendor_aliases_preserve_continuous_peak_and_unspecified_meaning():
    from src.spec.vendor_index import _rows_for_attribute, vendor_alias_is_current
    for raw_key, expected in (("continuous_discharge_current", "continuous_discharge_current_a"),
                              ("peak_discharge_current", "peak_discharge_current_a"),
                              ("discharge_current", "discharge_current_a")):
        rows = list(_rows_for_attribute(1, "battery", raw_key, "20000 mA", 20000, "ma", "description_table", 0.9))
        aliases = {row[1]: row[3] for row in rows if row[8] == "vendor_alias"}
        assert aliases == {expected: 20}
    assert not vendor_alias_is_current("battery", "max_discharge_current_a", "continuous_discharge_current", "vendor_alias")


def test_legacy_vendor_aliases_cannot_supply_wrong_property_evidence(evidence_db):
    metadata = {"max_discharge_current_a": {"type": "float", "unit": "A"}}
    evidence_db.execute(
        "INSERT INTO product_vendor_specs (product_id,key,value,value_num,raw_key,source) "
        "VALUES (2,'max_discharge_current_a','30',30,'continuous_discharge_current','vendor_alias')"
    )
    predicates = compile_predicates({}, {}, vendor_filters={"max_discharge_current_a": {"gte": 20}}, vendor_metadata=metadata)
    assert _matching_ids(evidence_db, predicates) == set()
    assert summarize_states(evidence_db, BASE, [], predicates)["missing_spec"] == 7


@pytest.mark.parametrize("duration,hours", [("8 hours", 8), ("eight hours", 8), ("90 minutes", 1.5), ("twenty four hours", 24), ("eight-hour", 8)])
def test_runtime_is_separate_from_retrieval(duration, hours):
    retrieval, constraints = extract_requirements(f"battery to power an arduino for {duration}")
    assert retrieval == "battery to"
    assert constraints[0]["value_hours"] == hours
    assert constraints[0]["load_target"] == "arduino"
    assert constraints[0]["status"] == "unknown"


def test_web_response_retains_caller_and_parsed_requirements():
    from models import SearchResponse
    existing = {"kind": "operating_temperature", "value": 50, "status": "unknown"}
    result = SearchResponse(query="battery for eight hours", total_results=0, unresolved_constraints=[existing])
    assert existing in result.unresolved_constraints
    assert any(r["kind"] == "runtime" for r in result.unresolved_constraints)
    assert result.requirements_verified is False
