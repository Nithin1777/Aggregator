"""Title-derived attribute tests (the no-network fallback for Robu)."""
from src.spec.title_attributes import extract_title_attributes


def test_resistor_title_yields_value_power_tolerance_package():
    attrs = {
        a["key"]: a
        for a in extract_title_attributes(
            "10K Ohm 1/4W 1% 0603 SMD Resistor", "resistors"
        )
    }

    assert attrs["resistance"]["value_num"] == 10_000.0
    assert attrs["resistance"]["unit"] == "ohm"
    assert attrs["power"]["value_num"] == 0.25
    assert attrs["tolerance"]["value_num"] == 1.0
    assert attrs["package"]["value"] == "0603"


def test_capacitor_title_yields_capacitance_and_voltage():
    attrs = {
        a["key"]: a
        for a in extract_title_attributes("100uF 25V Electrolytic Capacitor", "capacitors")
    }

    assert attrs["capacitance"]["value_num"] == 100_000_000.0
    assert attrs["voltage"]["value_num"] == 25.0


def test_battery_title_yields_voltage_and_capacity():
    attrs = {
        a["key"]: a
        for a in extract_title_attributes("3.7V 1300mAh LiPo Battery", "battery")
    }

    assert attrs["voltage"]["value_num"] == 3.7
    assert attrs["capacity_mah"]["value_num"] == 1300.0


def test_kit_titles_are_skipped():
    assert extract_title_attributes(
        "500Pcs 50 Resistance Values 1/2W 1% Metal Film Resistors Kit", "resistors"
    ) == []


def test_value_ranges_do_not_become_single_values():
    attrs = {a["key"]: a for a in extract_title_attributes("10 ohm - 1M ohm Resistor", "resistors")}
    assert "resistance" not in attrs


def test_title_attributes_are_marked_low_confidence():
    attrs = extract_title_attributes("100uF 25V Capacitor", "capacitors")
    assert attrs
    assert all(a["source"] == "title_heuristic" for a in attrs)
    assert all(a["confidence"] == 0.6 for a in attrs)
