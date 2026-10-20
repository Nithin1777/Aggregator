"""
Promote vendor/title attributes into the taxonomy-filterable `product_specs` layer.

`product_attributes` is deliberately free-form and can contain hundreds of vendor
keys. Filters need a small, typed, documented vocabulary. This module maps the
high-value vendor keys onto the attributes declared in
`config/filter_extension.yaml`, normalizes units, validates ranges/enums, and
emits rows ready for `product_specs` with source='promoted'.

The mapping is category-specific on purpose:
  * capacitors.voltage      -> voltage_v
  * diodes.voltage          -> reverse_voltage_v
  * transistors.voltage     -> vceo_v
  * semiconductors.voltage  -> vds_v
  * ICs.voltage             -> supply_voltage_v
  * LEDs.voltage            -> forward_voltage_v
  * relays.voltage          -> coil_voltage_v
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.spec.taxonomy_loader import Taxonomy, TaxonomyAttribute
from src.spec.title_attributes import extract_title_attributes

# source_attribute -> (target_key, converter)
PROMOTIONS: Dict[str, List[Tuple[str, str, str]]] = {
    "resistors": [
        ("resistance", "resistance_ohm", "resistance"),
        ("power", "power_w", "power"),
        ("tolerance", "tolerance_pct", "percent"),
        ("package", "package", "package"),
    ],
    "capacitors": [
        ("capacitance", "capacitance_uf", "capacitance"),
        ("voltage", "voltage_v", "voltage"),
        ("tolerance", "tolerance_pct", "percent"),
        ("package", "package", "package"),
    ],
    "inductors": [
        ("inductance", "inductance_uh", "inductance"),
        ("current", "current_a", "current"),
        ("tolerance", "tolerance_pct", "percent"),
        ("package", "package", "package"),
    ],
    "diodes": [
        ("current", "forward_current_a", "current"),
        ("voltage", "reverse_voltage_v", "voltage"),
        ("package", "package", "package"),
    ],
    "transistors": [
        ("voltage", "vceo_v", "voltage"),
        ("current", "ic_a", "current"),
        ("power", "power_w", "power"),
        ("package", "package", "package"),
    ],
    "semiconductors": [
        ("voltage", "vds_v", "voltage"),
        ("current", "id_a", "current"),
        ("power", "power_w", "power"),
        ("package", "package", "package"),
    ],
    "integrated-circuits": [
        ("voltage", "supply_voltage_v", "voltage"),
        ("pins", "pins", "integer"),
        ("package", "package", "package"),
    ],
    "connectors": [
        ("pins", "pins", "integer"),
        ("pitch", "pitch_mm", "pitch"),
        ("package", "package", "package"),
    ],
    "sensors": [
        ("voltage", "voltage_v", "voltage"),
    ],
    "displays": [
        ("screen_size", "screen_size_in", "screen_size"),
        ("resolution", "resolution", "string"),
    ],
    "leds": [
        ("voltage", "forward_voltage_v", "voltage"),
        ("package", "package", "package"),
    ],
    "relay": [
        ("voltage", "coil_voltage_v", "voltage"),
        ("current", "contact_current_a", "current"),
        ("channels", "channels", "integer"),
    ],
    "fuses": [
        ("current", "current_a", "current"),
        ("voltage", "voltage_v", "voltage"),
    ],
    "crystals": [
        ("frequency", "frequency_mhz", "frequency"),
        ("load_capacitance", "load_capacitance_pf", "capacitance_pf"),
        ("package", "package", "package"),
    ],
    "wires": [
        ("awg", "awg", "integer"),
        ("conductor_area", "conductor_area_mm2", "area"),
        ("length", "length_m", "length_m"),
    ],
    "power": [
        ("voltage", "output_voltage_v", "voltage"),
        ("current", "output_current_a", "current"),
        ("power", "power_w", "power"),
    ],
    "modules": [
        ("voltage", "voltage_v", "voltage"),
    ],
    "thermal": [
        ("power", "power_w", "power"),
        ("voltage", "voltage_v", "voltage"),
        ("size", "size_mm", "size_mm"),
    ],
    "hardware": [
        ("thread_size", "thread_size", "string"),
        ("length", "length_mm", "length_mm"),
    ],
}

_UNIT_FACTORS: Dict[str, Dict[str, float]] = {
    # target unit first; values convert into the target unit
    "resistance": {"ohm": 1.0, "ohms": 1.0, "r": 1.0, "k": 1e3, "kohm": 1e3, "m": 1e6, "mohm": 1e6},
    "capacitance": {"uf": 1.0, "µf": 1.0, "μf": 1.0, "nf": 1e-3, "pf": 1e-6, "mf": 1e3},
    "capacitance_pf": {"pf": 1.0, "nf": 1e3, "uf": 1e6, "µf": 1e6, "mf": 1e9},
    "inductance": {"uh": 1.0, "µh": 1.0, "μh": 1.0, "mh": 1e3, "h": 1e6},
    "voltage": {"v": 1.0, "mv": 1e-3, "kv": 1e3},
    "current": {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6},
    "power": {"w": 1.0, "mw": 1e-3, "kw": 1e3},
    "percent": {"%": 1.0, "percent": 1.0},
    "frequency": {"mhz": 1.0, "khz": 1e-3, "hz": 1e-6, "ghz": 1e3},
    "length_m": {"m": 1.0, "meter": 1.0, "meters": 1.0, "cm": 0.01, "mm": 1e-3},
    "length_mm": {"mm": 1.0, "cm": 10.0, "m": 1000.0},
    "pitch": {"mm": 1.0, "cm": 10.0, "in": 25.4},
    "size_mm": {"mm": 1.0, "cm": 10.0, "in": 25.4},
    "screen_size": {"in": 1.0, "inch": 1.0, '"': 1.0, "mm": 0.0393701},
    "area": {"mm2": 1.0, "sq.mm": 1.0, "mm²": 1.0},
}

_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z%µ°Ω\"/]+(?:\.?[a-zA-Z0-9]+)?)?")


def _merged_attributes(
    title: str,
    category: str,
    vendor_attributes: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for attr in extract_title_attributes(title, category):
        merged[attr["key"]] = attr
    for key, attr in vendor_attributes.items():
        current = merged.get(key)
        # Prefer the title value when it is numeric; prefer a description table
        # when the title has no parsed number (e.g. tolerance/package).
        if current is None:
            merged[key] = attr
        elif current.get("value_num") is None and attr.get("value_num") is not None:
            merged[key] = attr
        elif key in ("package", "tolerance", "pins") and attr.get("value"):
            merged[key] = attr
    return merged


def _numeric(attr: Dict[str, Any], kind: str) -> Optional[float]:
    factors = _UNIT_FACTORS.get(kind)
    if factors is None:
        return None
    number = attr.get("value_num")
    unit = str(attr.get("unit") or "").strip().lower().replace("µ", "u").replace("μ", "u")
    if number is None:
        text = str(attr.get("value") or "").strip()
        fraction = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(?:w|watt|watts)?\s*$", text, re.IGNORECASE)
        if fraction and kind == "power":
            try:
                return float(fraction.group(1)) / float(fraction.group(2))
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        match = _NUM_RE.search(text)
        if not match:
            return None
        try:
            number = float(match.group(1))
        except (TypeError, ValueError):
            return None
        unit = (match.group(2) or "").strip().lower().replace("µ", "u").replace("μ", "u")
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None
    factor = factors.get(unit)
    if factor is None:
        # A missing unit is assumed to already be in the target unit.
        factor = 1.0
    return value * factor


def _package(value: str, allowed: Optional[List[str]]) -> Optional[str]:
    raw = re.sub(r"\s+", "", str(value or "").upper())
    raw = raw.replace("SOT23", "SOT-23").replace("SOT223", "SOT-223")
    raw = raw.replace("TO220", "TO-220").replace("TO247", "TO-247")
    raw = raw.replace("DO41", "DO-41").replace("DO15", "DO-15")
    raw = raw.replace("HC49", "HC-49")
    for prefix in ("DIP", "SOIC", "SOP", "QFN", "TQFP", "LQFP", "BGA"):
        if raw.startswith(prefix):
            raw = prefix
            break
    if allowed and raw and raw.lower() not in {str(v).lower() for v in allowed}:
        # A known family with an unknown suffix is still useful for equality
        # filters; keep the canonical family name.
        return raw.lower()
    return raw.lower() or None


def _detect(text: str, patterns: Dict[str, str]) -> Optional[str]:
    lowered = (text or "").lower()
    for value, pattern in patterns.items():
        if re.search(pattern, lowered, re.IGNORECASE):
            return value
    return None


def _special_attributes(title: str, category: str, merged: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Title-derived attributes not produced by title_attributes."""
    out: Dict[str, Dict[str, Any]] = {}
    lowered = title.lower()

    def put(key: str, value: str, num: Optional[float], unit: Optional[str], confidence: float = 0.6) -> None:
        if key not in out:
            out[key] = {
                "key": key,
                "value": value,
                "value_num": num,
                "unit": unit,
                "source": "title_heuristic",
                "confidence": confidence,
            }

    resolution = re.search(r"\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b", title, re.IGNORECASE)
    if resolution and category in ("displays", "modules", "imaging"):
        put("resolution", f"{resolution.group(1)}x{resolution.group(2)}", None, None)

    size = re.search(r"(\d+(?:\.\d+)?)\s*(?:inch|in|\")\b", title, re.IGNORECASE)
    if size and category in ("displays",):
        put("screen_size", size.group(1), float(size.group(1)), "in")

    freq = re.search(r"(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz|Hz)\b", title, re.IGNORECASE)
    if freq and category in ("crystals", "modules", "sensors"):
        value = float(freq.group(1))
        unit = freq.group(2).lower()
        mhz = value * {"ghz": 1000.0, "mhz": 1.0, "khz": 1e-3, "hz": 1e-6}[unit]
        put("frequency", f"{mhz:g}", mhz, "mhz")

    pitch = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:pitch|spacing)", title, re.IGNORECASE)
    if not pitch:
        pitch = re.search(r"pitch\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*mm", title, re.IGNORECASE)
    if not pitch and category in ("connectors",):
        pitch = re.search(r"(\d+(?:\.\d+)?)\s*mm\b", title, re.IGNORECASE)
    if pitch and category in ("connectors",):
        put("pitch", pitch.group(1), float(pitch.group(1)), "mm")

    package_pins = re.search(
        r"\b(?:DIP|SOIC|SOP|QFN|TQFP|LQFP|BGA)[- ]?(\d{1,3})\b",
        title,
        re.IGNORECASE,
    )
    if package_pins and category in ("integrated-circuits", "connectors", "modules"):
        put("pins", package_pins.group(1), float(package_pins.group(1)), None)

    awg = re.search(r"\b(\d{1,2})\s*AWG\b", title, re.IGNORECASE)
    if awg and category in ("wires",):
        put("awg", awg.group(1), float(awg.group(1)), None)

    area = re.search(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s*mm|mm2|mm²)\b", title, re.IGNORECASE)
    if area and category in ("wires",):
        put("conductor_area", area.group(1), float(area.group(1)), "mm2")

    length_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:meter|meters|m)\b", title, re.IGNORECASE)
    if length_m and category in ("wires",):
        put("length", f"{length_m.group(1)} m", float(length_m.group(1)), "m")

    thread = re.search(r"\b(M\d+(?:\.\d+)?)\b", title, re.IGNORECASE)
    if thread and category in ("hardware",):
        put("thread_size", thread.group(1).upper(), None, None)

    length_mm = re.search(r"(\d+(?:\.\d+)?)\s*mm\b", title, re.IGNORECASE)
    if length_mm and category in ("hardware",):
        put("length", length_mm.group(1), float(length_mm.group(1)), "mm")

    size_mm = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|x)\s*(\d+(?:\.\d+)?)?\s*mm?", title, re.IGNORECASE)
    if size_mm and category == "thermal":
        try:
            put("size", size_mm.group(1), float(size_mm.group(1)), "mm")
        except (TypeError, ValueError):
            pass

    channels = re.search(r"\b(\d{1,2})[-\s]*(?:channel|ch|way)\b", title, re.IGNORECASE)
    if channels and category == "relay":
        put("channels", channels.group(1), float(channels.group(1)), None)

    return out


_ENUMS: Dict[str, Dict[str, Dict[str, str]]] = {
    "capacitors": {
        "capacitor_type": {
            "ceramic": r"\bceramic|mlcc",
            "electrolytic": r"\belectrolytic",
            "tantalum": r"\btantalum",
            "film": r"\bfilm\b",
            "supercapacitor": r"\bsupercap",
        },
    },
    "diodes": {
        "diode_type": {
            "schottky": r"\bschottky",
            "zener": r"\bzener",
            "rectifier": r"\brectifier",
            "tvs": r"\btvs\b",
            "switching": r"\bswitching\b",
        },
    },
    "transistors": {
        "transistor_type": {"darlington": r"\bdarlington", "pnp": r"\bpnp\b", "npn": r"\bnpn\b"},
    },
    "semiconductors": {
        "channel_type": {"p_channel": r"\bp[- ]?channel", "n_channel": r"\bn[- ]?channel"},
    },
    "connectors": {
        "connector_type": {
            "usb": r"\busb\b",
            "rj45": r"\brj45\b",
            "jst": r"\bjst\b",
            "ffc": r"\bffc\b|\bfpc\b",
            "terminal": r"\bterminal\b",
            "header": r"\bheader\b",
            "db9": r"\bdb9\b",
        },
    },
    "sensors": {
        "interface": {
            "i2c": r"\bi2c\b|i²c",
            "spi": r"\bspi\b",
            "uart": r"\buart\b|\brs485\b|\brs232\b",
            "onewire": r"\bone[- ]?wire\b",
            "analog": r"\banalog\b|\badc\b",
        },
        "sensor_type": {
            "temperature": r"\btemperature|thermistor|thermocouple|ds18b20|lm35",
            "humidity": r"\bhumidity|dht\d+|bme\d+",
            "pressure": r"\bpressure|barometer|bmp\d+",
            "light": r"\bldr\b|photoresistor|light sensor|bh1750",
            "motion": r"\bpir\b|motion|accelerometer|gyro|mpu\d+|adxl",
            "gas": r"\bgas sensor|mq-?\d+",
            "current": r"\bcurrent sensor|acs\d+|ina\d+",
            "position": r"\bencoder|hall|position",
        },
    },
    "displays": {
        "interface": {
            "i2c": r"\bi2c\b|i²c",
            "spi": r"\bspi\b",
            "uart": r"\buart\b",
            "hdmi": r"\bhdmi\b",
            "dsi": r"\bdsi\b",
            "parallel": r"\bparallel\b",
        },
        "touch": {"yes": r"\btouch\b|touchscreen"},
    },
    "leds": {
        "color": {
            "rgb": r"\brgb\b",
            "ir": r"\binfrared\b|\bir\b",
            "red": r"\bred\b",
            "green": r"\bgreen\b",
            "blue": r"\bblue\b",
            "white": r"\bwhite\b",
            "yellow": r"\byellow\b",
            "orange": r"\borange\b",
        },
    },
    "fuses": {
        "fuse_type": {
            "glass": r"\bglass\b",
            "ceramic": r"\bceramic\b",
            "blade": r"\bblade\b",
            "resettable": r"\bresettable|ptc\b",
        },
    },
}


def build_promoted_specs(
    title: str,
    category: Optional[str],
    vendor_attributes: Dict[str, Dict[str, Any]],
    taxonomy: Taxonomy,
) -> List[Dict[str, Any]]:
    """Returns product_specs-shaped rows for one product."""
    if not category or category not in PROMOTIONS:
        return []
    cat = taxonomy.categories.get(category)
    if cat is None:
        return []

    merged = _merged_attributes(title, category, vendor_attributes)
    merged.update(_special_attributes(title, category, merged))
    promoted: Dict[str, Dict[str, Any]] = {}

    for source_key, target_key, kind in PROMOTIONS[category]:
        attr = merged.get(source_key)
        if not attr:
            continue
        definition = cat.attributes.get(target_key)
        if definition is None:
            continue
        value: Optional[Any] = None
        value_num: Optional[float] = None
        value_type = definition.type

        if kind == "string":
            value = str(attr.get("value") or "").strip()
        elif kind == "package":
            value = _package(str(attr.get("value") or ""), definition.values)
        elif kind == "integer":
            num = _numeric(attr, "percent") if attr.get("value_num") is not None else None
            try:
                value_num = float(attr.get("value_num") if attr.get("value_num") is not None else attr.get("value"))
            except (TypeError, ValueError):
                value_num = None
            if value_num is not None:
                value = str(int(round(value_num)))
        else:
            value_num = _numeric(attr, kind)
            if value_num is not None:
                value = f"{value_num:g}"

        if value is None or (value == "" and value_num is None):
            continue
        if definition.type in ("integer", "float") and value_num is None:
            continue
        if definition.range and value_num is not None:
            low, high = definition.range
            if value_num < low or value_num > high:
                continue
        if definition.type == "enum" and definition.values and value is not None:
            value_text = str(value).lower()
            allowed = {str(v).lower() for v in definition.values}
            if value_text not in allowed:
                # Keep a canonical family name even when the vendor adds a suffix.
                if not any(value_text.startswith(str(v).lower()) for v in definition.values):
                    continue
        promoted[target_key] = {
            "key": target_key,
            "value": str(value),
            "value_num": value_num,
            "value_type": value_type,
            "confidence": float(attr.get("confidence", 0.7)),
            "evidence": str(attr.get("source") or "vendor_attribute"),
            "source": "promoted",
        }

    # Category-specific enums from the title.
    for target_key, patterns in _ENUMS.get(category, {}).items():
        value = _detect(title, patterns)
        if value:
            promoted[target_key] = {
                "key": target_key,
                "value": value,
                "value_num": None,
                "value_type": "enum",
                "confidence": 0.65,
                "evidence": "title_pattern",
                "source": "promoted",
            }

    return list(promoted.values())
