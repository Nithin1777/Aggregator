"""Vendor/title promotion tests for the extended filter taxonomy."""
from src.spec.taxonomy_loader import load_taxonomy
from src.spec.vendor_specs import build_promoted_specs


def _specs(title, category, attrs=None):
    taxonomy, _ = load_taxonomy(reload=True)
    return {s["key"]: s for s in build_promoted_specs(title, category, attrs or {}, taxonomy)}


def test_resistor_numeric_package_and_tolerance():
    specs = _specs("100 Ohm 1/4W 1% 0603 Resistor", "resistors")

    assert specs["resistance_ohm"]["value_num"] == 100.0
    assert specs["power_w"]["value_num"] == 0.25
    assert specs["tolerance_pct"]["value_num"] == 1.0
    assert specs["package"]["value"] == "0603"


def test_capacitor_units_and_type():
    specs = _specs("100uF 25V 10% Electrolytic Capacitor", "capacitors")

    assert specs["capacitance_uf"]["value_num"] == 100.0
    assert specs["voltage_v"]["value_num"] == 25.0
    assert specs["capacitor_type"]["value"] == "electrolytic"


def test_diode_uses_reverse_voltage_not_generic_voltage():
    specs = _specs("1N4007 1A 1000V Rectifier Diode", "diodes")

    assert specs["forward_current_a"]["value_num"] == 1.0
    assert specs["reverse_voltage_v"]["value_num"] == 1000.0
    assert specs["diode_type"]["value"] == "rectifier"


def test_transistor_milliamps_and_package():
    specs = _specs("BC547 NPN 45V 100mA TO-92 Transistor", "transistors")

    assert specs["vceo_v"]["value_num"] == 45.0
    assert specs["ic_a"]["value_num"] == 0.1
    assert specs["package"]["value"] == "to-92"
    assert specs["transistor_type"]["value"] == "npn"


def test_ic_package_pins_and_supply_voltage():
    specs = _specs("NE555 DIP-8 Timer IC 5V", "integrated-circuits")

    assert specs["pins"]["value_num"] == 8.0
    assert specs["package"]["value"] == "dip"
    assert specs["supply_voltage_v"]["value_num"] == 5.0


def test_connector_pitch_and_pins():
    specs = _specs("2.54mm 40 Pin Header Connector", "connectors")

    assert specs["pins"]["value_num"] == 40.0
    assert specs["pitch_mm"]["value_num"] == 2.54
    assert specs["connector_type"]["value"] == "header"


def test_display_size_resolution_and_interface():
    specs = _specs("5 inch 800x480 TFT Display SPI", "displays")

    assert specs["screen_size_in"]["value_num"] == 5.0
    assert specs["resolution"]["value"] == "800x480"
    assert specs["interface"]["value"] == "spi"


def test_wire_awg_area_and_length():
    specs = _specs("18AWG 0.8sq.mm Wire 1 Meter", "wires")

    assert specs["awg"]["value_num"] == 18.0
    assert specs["conductor_area_mm2"]["value_num"] == 0.8
    assert specs["length_m"]["value_num"] == 1.0


def test_units_in_vendor_attributes_are_normalized():
    specs = _specs(
        "Resistor",
        "resistors",
        {
            "resistance": {"key": "resistance", "value": "10K", "value_num": 10.0, "unit": "k"},
            "power": {"key": "power", "value": "1/4W", "value_num": None, "unit": None},
        },
    )

    assert specs["resistance_ohm"]["value_num"] == 10000.0
    assert specs["power_w"]["value_num"] == 0.25


def test_out_of_range_values_are_dropped():
    specs = _specs(
        "Resistor",
        "resistors",
        {
            "resistance": {"key": "resistance", "value": "999999999999", "value_num": 999999999999.0, "unit": "ohm"},
        },
    )

    assert "resistance_ohm" not in specs


def test_extension_taxonomy_exposes_filterable_attributes():
    taxonomy, _ = load_taxonomy(reload=True)

    assert {"resistance_ohm", "power_w", "package"} <= set(
        taxonomy.categories["resistors"].attributes
    )
    assert "capacitance_uf" in taxonomy.categories["capacitors"].attributes
    assert taxonomy.categories["resistors"].attributes["resistance_ohm"].extract == []
