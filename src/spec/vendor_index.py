"""
Normalized vendor-attribute index.

`product_attributes` is the raw vendor layer: useful, but keys are vendor-shaped
and units are inconsistent (``300mA`` is stored as ``value_num=300`` with
``unit='ma'``). This module materializes one best row per (product, key) in
``product_vendor_specs`` with values converted to stable units, plus a compact
statistics layer for discovery and filtering.

It deliberately does not replace ``product_specs``. Promoted taxonomy rows keep
their authority; this layer is the fallback for attributes that were never
promoted and the query surface for the long tail.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.spec.filters import canonical_enum_value, parse_engineering_value


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------

_NON_KEY = re.compile(r"[^a-z0-9]+")


def normalize_vendor_key(key: Any) -> str:
    """Stable lowercase snake_case key for a vendor attribute or caller input."""
    text = str(key or "").strip().lower()
    text = text.replace("µ", "u").replace("μ", "u").replace("°", "")
    text = text.replace("%", "pct")
    text = _NON_KEY.sub("_", text).strip("_")
    return text


# Category-specific aliases into canonical promoted keys. Only map attributes
# whose semantics are genuinely close; ambiguous keys stay as raw vendor keys.
VENDOR_KEY_ALIASES: Dict[str, Dict[str, str]] = {
    "resistors": {
        "resistance": "resistance_ohm",
        "resistance_ohm": "resistance_ohm",
        "power": "power_w",
        "power_watts": "power_w",
        "tolerance": "tolerance_pct",
        "package": "package",
        "case_package": "package",
    },
    "capacitors": {
        "capacitance": "capacitance_uf",
        "capacitance_uf": "capacitance_uf",
        "voltage": "voltage_v",
        "voltage_rating": "voltage_v",
        "voltage_rating_v": "voltage_v",
        "voltage_supply": "voltage_v",
        "supply_voltage": "voltage_v",
        "tolerance": "tolerance_pct",
        "package": "package",
        "case_package": "package",
    },
    "inductors": {
        "inductance": "inductance_uh",
        "inductance_uh": "inductance_uh",
        "current": "current_a",
        "current_rating": "current_a",
        "tolerance": "tolerance_pct",
        "package": "package",
    },
    "diodes": {
        "voltage": "reverse_voltage_v",
        "voltage_rating": "reverse_voltage_v",
        "reverse_voltage": "reverse_voltage_v",
        "current": "forward_current_a",
        "current_rating": "forward_current_a",
        "package": "package",
        "case_package": "package",
    },
    "transistors": {
        "voltage": "vceo_v",
        "collector_emitter_voltage_vceo": "vceo_v",
        "current": "ic_a",
        "current_collector_ic": "ic_a",
        "power": "power_w",
        "pd_power_dissipation": "power_w",
        "package": "package",
        "case_package": "package",
    },
    "semiconductors": {
        "voltage": "vds_v",
        "vds": "vds_v",
        "current": "id_a",
        "id": "id_a",
        "power": "power_w",
        "package": "package",
        "case_package": "package",
    },
    "integrated-circuits": {
        "voltage": "supply_voltage_v",
        "voltage_supply": "supply_voltage_v",
        "supply_voltage": "supply_voltage_v",
        "pins": "pins",
        "no_of_pins": "pins",
        "package": "package",
        "ic_case_package": "package",
        "case_package": "package",
    },
    "microcontrollers": {
        "voltage": "supply_voltage_v",
        "voltage_supply": "supply_voltage_v",
        "supply_voltage": "supply_voltage_v",
        "pins": "pins",
        "no_of_pins": "pins",
        "package": "package",
        "ic_case_package": "package",
        "case_package": "package",
        "operating_frequency": "operating_frequency_mhz",
        "frequency_rating": "operating_frequency_mhz",
        "frequency": "operating_frequency_mhz",
        "program_memory_size": "program_memory_kb",
        "ram_memory_size": "ram_kb",
        "adc_resolution": "adc_bits",
        "adc_bit": "adc_bits",
    },
    "sensors": {
        "voltage": "voltage_v",
        "voltage_supply": "voltage_v",
        "supply_voltage": "voltage_v",
        "interface": "interface",
        "interfaces": "interface",
        "sensor_type": "sensor_type",
        "type": "sensor_type",
    },
    "connectors": {
        "pins": "pins",
        "no_of_pins": "pins",
        "pitch": "pitch_mm",
    },
    "relay": {
        "voltage": "coil_voltage_v",
        "coil_voltage": "coil_voltage_v",
        "coil_voltage_v": "coil_voltage_v",
        "current": "contact_current_a",
        "contact_current": "contact_current_a",
        "contact_current_a": "contact_current_a",
        "channels": "channels",
        "number_of_channels": "channels",
    },
    "fuses": {
        "current": "current_a",
        "current_rating": "current_a",
        "voltage": "voltage_v",
        "voltage_rating": "voltage_v",
        "fuse_type": "fuse_type",
        "type": "fuse_type",
    },
    "crystals": {
        "frequency": "frequency_mhz",
        "frequency_mhz": "frequency_mhz",
        "load_capacitance": "load_capacitance_pf",
        "package": "package",
    },
    "leds": {
        "voltage": "forward_voltage_v",
        "forward_voltage": "forward_voltage_v",
        "package": "package",
        "color": "color",
    },
    "power": {
        "voltage": "output_voltage_v",
        "output_voltage": "output_voltage_v",
        "current": "output_current_a",
        "output_current": "output_current_a",
        "power": "power_w",
        "input_voltage": "input_voltage_v",
    },
    "modules": {
        "voltage": "voltage_v",
        "working_voltage": "voltage_v",
        "voltage_supply": "voltage_v",
        "supply_voltage": "voltage_v",
        "interface": "interface",
        "interfaces": "interface",
    },
    "displays": {
        "display_size": "screen_size_in",
        "screen_size": "screen_size_in",
        "resolution": "resolution",
        "interface": "interface",
        "touch": "touch",
        "touch_type": "touch",
    },
    "wires": {
        "awg": "awg",
        "conductor_area": "conductor_area_mm2",
        "length": "length_m",
    },
    "thermal": {
        "power": "power_w",
        "voltage": "voltage_v",
        "size": "size_mm",
    },
    "hardware": {
        "thread_size": "thread_size",
        "length": "length_mm",
    },
    "switches": {
        "voltage": "voltage_v",
        "voltage_rating": "voltage_v",
        "current": "contact_current_a",
        "contact_current": "contact_current_a",
        "current_rating": "contact_current_a",
        "package": "package",
        "actuator_style": "actuator_style",
        "mounting_type": "mounting_type",
        "mounting_style": "mounting_type",
        "operating_force": "operating_force_n",
    },
    "motor": {
        "voltage": "voltage",
        "rpm": "rpm",
        "rated_speed": "rpm",
        "speed": "rpm",
        "no_load_speed": "rpm",
        "current": "current_a",
        "no_load_current": "no_load_current_a",
        "stall_current": "stall_current_a",
        "stall_torque": "stall_torque_kgcm",
        "rated_torque": "rated_torque_kgcm",
        "torque": "torque_kgcm",
        "shaft_diameter": "shaft_diameter_mm",
        "shaft_length": "shaft_length_mm",
        "motor_type": "motor_type",
        "kv_rating": "kv_rating",
    },
    "battery": {
        "voltage": "voltage",
        "nominal_voltage": "voltage",
        "capacity": "capacity_mah",
        "capacity_mah": "capacity_mah",
        "nominal_capacity": "capacity_mah",
        "discharge_rate": "discharge_c",
        "battery_type": "chemistry",
        "continuous_discharge_current": "max_discharge_current_a",
        "discharge_current": "max_discharge_current_a",
        "charge_current": "charge_current_a",
        "charging_current": "charge_current_a",
        "charge_current_a": "charge_current_a",
        "charging_voltage": "charge_voltage_v",
        "charge_voltage": "charge_voltage_v",
        "bms_y_n": "bms_included",
        "bms": "bms_included",
    },
}


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

# unit text -> (base dimension, multiplier to base)
_UNIT_TO_BASE: Dict[str, Tuple[str, float]] = {
    "v": ("v", 1.0), "mv": ("v", 1e-3), "kv": ("v", 1e3),
    "a": ("a", 1.0), "ma": ("a", 1e-3), "ua": ("a", 1e-6),
    "w": ("w", 1.0), "mw": ("w", 1e-3), "kw": ("w", 1e3),
    "ohm": ("ohm", 1.0), "kohm": ("ohm", 1e3), "mohm": ("ohm", 1e6),
    "f": ("f", 1.0), "uf": ("f", 1e-6), "nf": ("f", 1e-9), "pf": ("f", 1e-12),
    "mf": ("f", 1e-3), "h": ("h", 1.0), "uh": ("h", 1e-6), "mh": ("h", 1e-3),
    "nh": ("h", 1e-9),
    "hz": ("hz", 1.0), "khz": ("hz", 1e3), "mhz": ("hz", 1e6), "ghz": ("hz", 1e9),
    "mm": ("mm", 1.0), "cm": ("mm", 10.0), "m": ("mm", 1000.0), "in": ("mm", 25.4),
    "g": ("g", 1.0), "kg": ("g", 1000.0), "mg": ("g", 0.001),
    "c": ("c", 1.0), "mah": ("mah", 1.0), "ah": ("mah", 1000.0),
    "rpm": ("rpm", 1.0), "%": ("percent", 1.0), "pct": ("percent", 1.0),
    "n": ("n", 1.0), "kgf": ("n", 9.80665), "gf": ("n", 0.00980665),
    "kgcm": ("kgcm", 1.0), "kg-cm": ("kgcm", 1.0), "gcm": ("kgcm", 0.001),
    "ozin": ("kgcm", 0.07200779), "nm": ("kgcm", 10.197162),
}

# base dimension -> (display unit, multiplier from base to display)
_BASE_DISPLAY: Dict[str, Tuple[str, float]] = {
    "v": ("V", 1.0),
    "a": ("A", 1.0),
    "w": ("W", 1.0),
    "ohm": ("ohm", 1.0),
    "f": ("uF", 1e6),
    "h": ("uH", 1e6),
    "hz": ("MHz", 1e-6),
    "mm": ("mm", 1.0),
    "g": ("g", 1.0),
    "c": ("C", 1.0),
    "mah": ("mAh", 1.0),
    "rpm": ("RPM", 1.0),
    "percent": ("%", 1.0),
    "n": ("N", 1.0),
    "kgcm": ("kg-cm", 1.0),
}

_TARGET_UNITS: Dict[str, Tuple[str, float]] = {
    "v": ("v", 1.0), "a": ("a", 1.0), "w": ("w", 1.0), "ohm": ("ohm", 1.0),
    "uf": ("f", 1e6), "nf": ("f", 1e9), "pf": ("f", 1e12), "mf": ("f", 1e3),
    "uh": ("h", 1e6), "mh": ("h", 1e3), "nh": ("h", 1e9),
    "mhz": ("hz", 1e6), "khz": ("hz", 1e3), "hz": ("hz", 1.0),
    "mm": ("mm", 1.0), "m": ("mm", 1000.0), "cm": ("mm", 10.0), "in": ("mm", 25.4),
    "mah": ("mah", 1.0), "ah": ("mah", 1000.0),
    "c": ("c", 1.0), "%": ("percent", 1.0), "pct": ("percent", 1.0),
    "kgcm": ("kgcm", 1.0), "kg-cm": ("kgcm", 1.0), "nm": ("kgcm", 10.197162),
    "g": ("g", 1.0), "kg": ("g", 1000.0),
    "rpm": ("rpm", 1.0), "pins": (None, 1.0), "n": ("n", 1.0),
}


def target_unit_for_key(key: str) -> Optional[str]:
    """Declared/display unit for a canonical or vendor key, when known."""
    k = normalize_vendor_key(key)
    suffixes = (
        ("_v", "V"), ("_a", "A"), ("_w", "W"), ("_ohm", "ohm"),
        ("_uf", "uF"), ("_uh", "uH"), ("_pf", "pF"), ("_mhz", "MHz"),
        ("_mm2", "mm2"), ("_mm", "mm"), ("_mah", "mAh"), ("_pct", "%"),
        ("_c", "C"), ("_kgcm", "kg-cm"), ("_m", "m"),
    )
    for suffix, unit in suffixes:
        if k.endswith(suffix):
            return unit
    if "coefficient" not in k and "temperature" in k:
        return "C"
    if "voltage" in k or k.endswith("_volts"):
        return "V"
    if "current" in k or k.endswith("_amps"):
        return "A"
    if "power" in k or k.endswith("_watts"):
        return "W"
    if "resistance" in k:
        return "ohm"
    if "capacitance" in k:
        return "uF"
    if "inductance" in k:
        return "uH"
    if "frequency" in k:
        return "MHz"
    if "weight" in k:
        return "g"
    special = {
        "voltage": "V", "current": "A", "power": "W", "resistance": "ohm",
        "capacitance": "uF", "inductance": "uH", "frequency": "MHz",
        "capacity": "mAh", "discharge_rate": "C", "pins": None, "awg": None,
        "channels": None, "weight": "g", "operating_temperature": "C",
        "tolerance": "%", "torque": "kg-cm", "size": "mm", "length": "mm",
    }
    return special.get(k)


_UNIT_ALIASES: Dict[str, str] = {
    "volts": "v", "volt": "v", "voltage": "v", "vdc": "v", "vac": "v",
    "watts": "w", "watt": "w", "ohms": "ohm", "amps": "a", "amp": "a",
    "amperes": "a", "ampere": "a", "farads": "f", "farad": "f",
    "henries": "h", "henry": "h", "hertz": "hz", "rpm/min": "rpm",
    "r/min": "rpm", "rpm": "rpm", "kg-cm": "kgcm", "kg/cm": "kgcm",
    "kgcm": "kgcm", "oz-in": "ozin", "n.m": "nm", "n-m": "nm",
    "milliamp": "ma", "milliamps": "ma", "microamp": "ua",
    "microamps": "ua",
}


def _unit_factor(unit: Any) -> Optional[Tuple[str, float]]:
    raw = str(unit or "").strip().lower()
    raw = raw.replace("µ", "u").replace("μ", "u").replace("ω", "ohm")
    raw = raw.replace("°", "").replace(" ", "")
    raw = raw.strip(" .,;:")
    raw = _UNIT_ALIASES.get(raw, raw)
    return _UNIT_TO_BASE.get(raw)


def _convert_to_target(
    number: float,
    raw_unit: Any,
    target_unit: Optional[str],
) -> Tuple[float, Optional[str]]:
    """Converts a numeric value to the target display unit; returns (value, unit)."""
    factor = _unit_factor(raw_unit)
    if factor is None:
        return float(number), (str(raw_unit).strip().lower() or target_unit)
    base_dim, base_mult = factor
    base_value = float(number) * base_mult
    if target_unit:
        target = _TARGET_UNITS.get(normalize_vendor_key(target_unit))
        # Normalize display unit text such as "kg-cm" -> "kgcm".
        if target is None:
            target = _TARGET_UNITS.get(str(target_unit).strip().lower())
        if target and target[0] == base_dim:
            return base_value / target[1], target_unit
    display, display_mult = _BASE_DISPLAY.get(base_dim, (str(raw_unit), 1.0))
    return base_value * display_mult, display


def _is_integer_key(key: str) -> bool:
    return normalize_vendor_key(key) in {
        "pins", "no_of_pins", "channels", "awg", "number_of_pins",
        "number_of_channels", "adc_bits", "adc_resolution",
    }


_STRING_KEYS = {
    "manufacturer", "type", "model", "brand", "material", "color", "colour",
    "package", "case_package", "ic_case_package", "package_type",
    "mounting_type", "mounting_style", "actuator_style", "product_type",
    "connector_type", "sensor_type", "diode_type", "transistor_type",
    "capacitor_type", "fuse_type", "chemistry", "interface", "interfaces",
    "touch", "output_type", "contact_form", "contact_configuration",
    "display_type", "driver_ic", "mcu_series", "mcu_family", "device_core",
    "product_range", "compatibility", "model_type", "speaker_type",
    "shaft_type", "motor_type", "battery_type", "form", "mounting",
}
_STRING_KEY_SUFFIXES = (
    "_package", "_type", "_model", "_material", "_color", "_colour",
    "_manufacturer", "_brand", "_series", "_family", "_interface",
)


def _is_string_key(key: str) -> bool:
    k = normalize_vendor_key(key)
    return k in _STRING_KEYS or any(k.endswith(s) for s in _STRING_KEY_SUFFIXES)


_TORQUE_TEXT_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*(kg[\s-]?cm|oz[\s-]?in|n[\s.-]?m)\b",
    re.IGNORECASE,
)


def _parse_torque_text(text: str) -> Optional[float]:
    match = _TORQUE_TEXT_RE.search(text or "")
    if not match:
        return None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None
    unit = match.group(2).lower().replace(" ", "").replace("-", "").replace(".", "")
    factor = {"kgcm": 1.0, "ozin": 0.07200779, "nm": 10.197162}.get(unit, 1.0)
    return number * factor


def normalize_vendor_value(
    key: str,
    value: Any,
    value_num: Any,
    unit: Any,
) -> Tuple[str, Optional[float], str, Optional[str]]:
    """
    Returns ``(value_text, value_num, value_type, unit)`` for one attribute.

    Numeric comparisons use the converted value. If the source has no single
    number (a range, "N/A", prose), the row stays textual rather than pretending
    an inaccurate number.
    """
    target = target_unit_for_key(key)
    text = str(value or "").strip()
    if _is_string_key(key):
        return canonical_enum_value(text), None, "string", None

    number: Optional[float] = None
    if value_num is not None:
        try:
            number = float(value_num)
        except (TypeError, ValueError):
            number = None
    # `value_num` is sometimes a number scraped out of a model name ("3M",
    # "18650"). Only trust it when the unit is understood, the key declares a
    # numeric target, or the value text is a bare number.
    unit_info = _unit_factor(unit)
    bare_number = bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text))
    starts_number = bool(re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text))
    if number is not None and not (unit_info or _is_integer_key(key) or bare_number or (target and starts_number)):
        number = None
    if number is None and text:
        if target and "torque" in normalize_vendor_key(key):
            number = _parse_torque_text(text)
        if number is None:
            number = parse_engineering_value(key, text, target)
    if number is not None:
        converted, display_unit = _convert_to_target(number, unit, target)
        if not math.isfinite(converted):
            return text.lower(), None, "string", None
        if _is_integer_key(key):
            converted = int(round(converted))
            return str(converted), float(converted), "integer", display_unit
        cleaned = float(f"{converted:.12g}")
        return f"{cleaned:g}", cleaned, "float", display_unit
    return canonical_enum_value(text), None, "string", (str(unit).strip().lower() or None)


# ---------------------------------------------------------------------------
# Build and statistics
# ---------------------------------------------------------------------------

_SOURCE_PRIORITY = {
    "description_table": 4,
    "description_text": 3,
    "title_heuristic": 2,
    "description": 1,
}


def _rows_for_attribute(
    product_id: int,
    category: Optional[str],
    raw_key: str,
    value: Any,
    value_num: Any,
    unit: Any,
    source: str,
    confidence: Any,
) -> Iterable[Tuple[Any, ...]]:
    raw_key = normalize_vendor_key(raw_key)
    if not raw_key:
        return []
    raw_value, raw_num, raw_type, raw_unit = normalize_vendor_value(
        raw_key, value, value_num, unit
    )
    if not raw_value and raw_num is None:
        return []
    try:
        confidence_value = float(confidence if confidence is not None else 0.5)
    except (TypeError, ValueError):
        confidence_value = 0.5
    rows = [(
        product_id, raw_key, raw_value, raw_num, raw_type, raw_unit,
        raw_key, confidence_value, source,
    )]
    target = (VENDOR_KEY_ALIASES.get(category or "") or {}).get(raw_key)
    if target and normalize_vendor_key(target) != raw_key:
        target_key = normalize_vendor_key(target)
        can_value, can_num, can_type, can_unit = normalize_vendor_value(
            target_key, value, value_num, unit
        )
        if can_value or can_num is not None:
            rows.append((
                product_id, target_key, can_value, can_num, can_type, can_unit,
                raw_key, confidence_value, "vendor_alias",
            ))
    return rows


def rebuild_vendor_specs(
    conn: sqlite3.Connection,
    categories: Optional[List[str]] = None,
    batch_size: int = 5000,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Rebuilds product_vendor_specs and its statistics from product_attributes."""
    stats = {"attributes": 0, "rows": 0, "products": 0}
    where = ""
    params: List[Any] = []
    if categories:
        placeholders = ", ".join(["?"] * len(categories))
        where = f"AND p.category_canonical IN ({placeholders})"
        params = list(categories)
    if not dry_run:
        conn.execute("DELETE FROM product_vendor_specs;")
        conn.execute("DELETE FROM vendor_spec_stats_global;")
        conn.execute("DELETE FROM vendor_spec_stats;")

    query = f"""
        SELECT a.product_id, p.category_canonical, a.key, a.value, a.value_num,
               a.unit, a.source, a.confidence
        FROM product_attributes a
        JOIN products p ON p.id = a.product_id
        WHERE 1=1 {where}
        ORDER BY
          CASE a.source
            WHEN 'description_table' THEN 1
            WHEN 'description_text' THEN 2
            WHEN 'title_heuristic' THEN 3
            ELSE 0
          END ASC,
          COALESCE(a.confidence, 0) ASC,
          a.product_id ASC,
          a.key ASC;
    """
    pending: List[Tuple[Any, ...]] = []
    seen_products = set()
    for row in conn.execute(query, params):
        stats["attributes"] += 1
        for out in _rows_for_attribute(*row):
            pending.append(out)
            seen_products.add(out[0])
            if len(pending) >= max(1, batch_size):
                if not dry_run:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO product_vendor_specs
                          (product_id, key, value, value_num, value_type, unit,
                           raw_key, confidence, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        pending,
                    )
                stats["rows"] += len(pending)
                pending.clear()
    if pending:
        if not dry_run:
            conn.executemany(
                """
                INSERT OR REPLACE INTO product_vendor_specs
                  (product_id, key, value, value_num, value_type, unit,
                   raw_key, confidence, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                pending,
            )
        stats["rows"] += len(pending)
    stats["products"] = len(seen_products)
    if not dry_run:
        conn.commit()
        recompute_vendor_stats(conn)
    return stats


def recompute_vendor_stats(conn: sqlite3.Connection) -> None:
    """Rebuilds global and per-category statistics from normalized vendor rows."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM vendor_spec_stats_global;")
    conn.execute(
        """
        INSERT INTO vendor_spec_stats_global
          (key, value_type, unit, applicable_count, value_min, value_max, updated_at)
        SELECT key,
               CASE WHEN SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) > 0
                    THEN 'float' ELSE 'string' END,
               MAX(unit),
               COUNT(DISTINCT product_id),
               MIN(value_num),
               MAX(value_num),
               ?
        FROM product_vendor_specs
        GROUP BY key;
        """,
        (now,),
    )
    conn.execute("DELETE FROM vendor_spec_stats;")
    conn.execute(
        """
        INSERT INTO vendor_spec_stats
          (category, key, applicable_count, filled_count, unit, value_min, value_max, updated_at)
        SELECT p.category_canonical, v.key,
               COUNT(DISTINCT p.id),
               COUNT(DISTINCT v.product_id),
               MAX(v.unit),
               MIN(v.value_num),
               MAX(v.value_num),
               ?
        FROM products p
        JOIN product_vendor_specs v ON v.product_id = p.id
        WHERE p.category_canonical IS NOT NULL
        GROUP BY p.category_canonical, v.key;
        """,
        (now,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def load_vendor_specs(
    conn: sqlite3.Connection,
    category: Optional[str] = None,
    min_count: int = 5,
    keys: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns metadata for vendor keys, shaped like ``load_catalog_specs``.

    With a category, the count is the number of products in that category that
    carry the key, not the global count.
    """
    params: List[Any] = []
    if keys:
        placeholders = ", ".join(["?"] * len(keys))
        key_clause = f"AND g.key IN ({placeholders})"
        params.extend(str(k) for k in keys)
    else:
        key_clause = ""
    if category:
        rows = conn.execute(
            f"""
            SELECT g.key, g.value_type, g.unit, g.applicable_count, g.value_min,
                   g.value_max, s.filled_count, s.applicable_count
            FROM vendor_spec_stats_global g
            LEFT JOIN vendor_spec_stats s ON s.key = g.key AND s.category = ?
            WHERE g.applicable_count >= ? {key_clause}
            ORDER BY COALESCE(s.filled_count, 0) DESC, g.applicable_count DESC;
            """,
            [category, max(1, min_count)] + params,
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT g.key, g.value_type, g.unit, g.applicable_count, g.value_min,
                   g.value_max, NULL, NULL
            FROM vendor_spec_stats_global g
            WHERE g.applicable_count >= ? {key_clause}
            ORDER BY g.applicable_count DESC;
            """,
            [max(1, min_count)] + params,
        ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key, vtype, unit, count, vmin, vmax, filled, applicable = row
        present = int(filled or 0) if category else int(count or 0)
        result[str(key)] = {
            "key": str(key),
            "type": "float" if vtype == "float" else "enum",
            "unit": unit,
            "count": present,
            "global_count": int(count or 0),
            "of": int(applicable or 0) if category else None,
            "min": vmin,
            "max": vmax,
            "display_name": str(key).replace("_", " ").title(),
            "value_counts": {},
            "enum_values": None,
            "source": "vendor",
        }
    return result


def vendor_key_value_counts(
    conn: sqlite3.Connection,
    key: str,
    limit: int = 20,
) -> Dict[str, int]:
    rows = conn.execute(
        """
        SELECT value, COUNT(1) FROM product_vendor_specs
        WHERE key = ? GROUP BY value ORDER BY COUNT(1) DESC LIMIT ?;
        """,
        (str(key), max(1, int(limit))),
    ).fetchall()
    return {str(value): int(count) for value, count in rows}


def vendor_category_keys(
    conn: sqlite3.Connection,
    category: str,
    min_filled: int = 1,
) -> List[str]:
    rows = conn.execute(
        """
        SELECT key FROM vendor_spec_stats
        WHERE category = ? AND filled_count >= ? ORDER BY filled_count DESC;
        """,
        (str(category), max(1, int(min_filled))),
    ).fetchall()
    return [str(row[0]) for row in rows]
