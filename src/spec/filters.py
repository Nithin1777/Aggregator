"""
Shared spec-attribute filtering.

This module holds the filtering logic that used to live only inside
`mcp_server/server.py`: loading the advertised spec keys, validating a filter
expression against them, and turning it into SQL over the `product_specs`
table.

Both the MCP tools and the web UI use it, so a filter like
`{"voltage": {"gte": 12}}` means exactly the same thing in both places.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Advertised spec metadata
# ---------------------------------------------------------------------------

def load_catalog_specs(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """
    Loads all advertised spec key metadata from spec_stats_global.

    A spec key can be defined by several categories (e.g. voltage exists for
    motors and batteries). Unit/display metadata is merged deterministically,
    and an enum value list is advertised only when every definition agrees —
    if any definition is open-ended, the key is advertised as open-ended so
    agents do not mistake valid values for invalid ones.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT key, value_type, applicable_count, value_min, value_max, value_counts
        FROM spec_stats_global
        WHERE applicable_count >= 50
        ORDER BY applicable_count DESC;
    """)
    specs: Dict[str, Dict[str, Any]] = {}
    for r in cur.fetchall():
        specs[r[0]] = {
            "key": r[0],
            "type": r[1],
            "unit": None,
            "count": r[2],
            "display_name": r[0].replace("_", " ").title(),
            "enum_values": None,
            "min": r[3],
            "max": r[4],
            "value_counts": json.loads(r[5]) if r[5] else {},
        }

    enum_by_key: Dict[str, List[Optional[List[str]]]] = {}
    cur.execute("SELECT key, unit, display_name, enum_values FROM spec_definitions ORDER BY key;")
    for key, unit, display_name, enum_values in cur.fetchall():
        meta = specs.get(key)
        if meta is None:
            continue
        if meta["unit"] is None and unit:
            meta["unit"] = unit
        if display_name:
            meta["display_name"] = display_name
        if enum_values:
            try:
                parsed = json.loads(enum_values)
            except (TypeError, ValueError):
                parsed = None
        else:
            parsed = None
        enum_by_key.setdefault(key, []).append(parsed)

    for key, meta in specs.items():
        definitions = enum_by_key.get(key)
        if not definitions:
            continue
        if any(values is None for values in definitions):
            meta["enum_values"] = None  # open-ended in at least one category
        elif all(values == definitions[0] for values in definitions):
            meta["enum_values"] = definitions[0]
        else:
            meta["enum_values"] = None  # divergent vocabularies: stay open

    # Canonicalize the advertised vocabulary. Vendor descriptions contain many
    # spelling variants of the same package ("sot-23(surface mount device)" vs
    # "sot-23"); exposing all of them teaches an agent values that are not
    # independent packages. Keep the raw -> canonical map so SQL can still match
    # rows written before the canonicalization migration.
    for key, meta in specs.items():
        counts = meta.get("value_counts") or {}
        if not counts:
            continue
        canonical_counts: Dict[str, int] = {}
        variants: Dict[str, List[str]] = {}
        for raw_value, count in counts.items():
            canonical = canonical_enum_value(raw_value, meta)
            canonical_counts[str(canonical)] = canonical_counts.get(str(canonical), 0) + int(count or 0)
            variants.setdefault(str(canonical), []).append(str(raw_value))
        meta["value_counts"] = canonical_counts
        meta["variant_map"] = {
            canonical: sorted(set(values))
            for canonical, values in variants.items()
            if len(values) > 1 or values[0] != canonical
        }
    return specs


def enum_is_numeric(spec_meta: Dict[str, Any]) -> bool:
    """
    True when an enum's stored values are numeric strings (e.g. voltage '24').

    Ordering operators are only meaningful for these. For categorical enums
    (chemistry='lipo') they must be rejected: enums leave value_num NULL, so an
    unguarded `value_num >= x` silently matches nothing.
    """
    if spec_meta.get("type") != "enum":
        return True

    samples: List[str] = []
    enum_vals = spec_meta.get("enum_values")
    if isinstance(enum_vals, (list, tuple)) and enum_vals:
        samples = [str(v) for v in enum_vals]
    if not samples:
        samples = [str(v) for v in list((spec_meta.get("value_counts") or {}).keys())]
    if not samples:
        return False

    numeric = 0
    for s in samples:
        try:
            float(str(s).strip())
            numeric += 1
        except (TypeError, ValueError):
            pass
    # Tolerate a few stray non-numeric members (e.g. an "N/A" bucket).
    return numeric >= max(1, int(len(samples) * 0.9))


def filter_column(alias: str, spec_meta: Dict[str, Any], ordering: bool = False) -> str:
    """
    SQL expression to compare against for a filter key.

    Numeric-typed keys use the indexed value_num column. Numeric-valued enums have
    NULL value_num, so ordering comparisons must CAST the text value instead of
    comparing NULL (which matches zero rows and looks like 'no such part').
    """
    if spec_meta.get("type") in ("integer", "float"):
        return f"{alias}.value_num"
    if ordering and enum_is_numeric(spec_meta):
        return f"CAST({alias}.value AS REAL)"
    return f"{alias}.value"


# ---------------------------------------------------------------------------
# Engineering-value parsing and enum canonicalization
# ---------------------------------------------------------------------------

# Prefix multipliers. Case matters: "m" is milli, "M" is mega.
_PREFIX_FACTORS: Dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "c": 1e-2,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

_BASE_UNIT_ALIASES: Dict[str, set] = {
    "ohm": {"ohm", "ohms", "r", "ω"},
    "f": {"f", "farad", "farads"},
    "h": {"h", "henry", "henries"},
    "v": {"v", "volt", "volts", "vdc", "vac"},
    "a": {"a", "amp", "amps", "ampere", "amperes"},
    "w": {"w", "watt", "watts"},
    "hz": {"hz", "hertz"},
    "m": {"m", "meter", "meters", "metre", "metres"},
    "mm": {"mm", "millimeter", "millimeters", "millimetre", "millimetres"},
    "mm2": {"mm2", "mm²", "sqmm", "sq.mm"},
    "in": {"in", "inch", "inches", '"'},
    "percent": {"%", "percent", "pct"},
}

_ALIAS_TO_BASE: Dict[str, str] = {
    alias: base for base, aliases in _BASE_UNIT_ALIASES.items() for alias in aliases
}

# Target unit -> (base unit, factor that converts base unit into target unit).
_TARGET_UNITS: Dict[str, Tuple[str, float]] = {
    "ohm": ("ohm", 1.0),
    "ω": ("ohm", 1.0),
    "f": ("f", 1.0),
    "uf": ("f", 1e6),
    "µf": ("f", 1e6),
    "μf": ("f", 1e6),
    "nf": ("f", 1e9),
    "pf": ("f", 1e12),
    "mf": ("f", 1e3),
    "h": ("h", 1.0),
    "uh": ("h", 1e6),
    "µh": ("h", 1e6),
    "μh": ("h", 1e6),
    "nh": ("h", 1e9),
    "mh": ("h", 1e3),
    "v": ("v", 1.0),
    "mv": ("v", 1e3),
    "kv": ("v", 1e-3),
    "a": ("a", 1.0),
    "ma": ("a", 1e3),
    "ua": ("a", 1e6),
    "µa": ("a", 1e6),
    "μa": ("a", 1e6),
    "w": ("w", 1.0),
    "mw": ("w", 1e3),
    "kw": ("w", 1e-3),
    "hz": ("hz", 1.0),
    "khz": ("hz", 1e3),
    "mhz": ("hz", 1e6),
    "ghz": ("hz", 1e-3),
    "m": ("m", 1.0),
    "cm": ("m", 100.0),
    "mm": ("mm", 1.0),
    "mm2": ("mm2", 1.0),
    "mm²": ("mm2", 1.0),
    "in": ("in", 1.0),
    "inch": ("in", 1.0),
    "%": ("percent", 1.0),
    "percent": ("percent", 1.0),
    "pct": ("percent", 1.0),
}

_NUM_UNIT_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*"
    r"([pnuµmkKMGc]?)\s*([a-zA-Z%°Ωµμ\"]*)\s*$"
)
_RKM_RE = re.compile(
    r"^\s*([+-]?)(\d+)([pnuµmkKMGRc])(\d+)\s*([a-zA-Z%°Ωµμ\"]*)\s*$"
)
_UNIT_WORD_RE = re.compile(r"[a-zA-Z%°Ωµμ\"]+")


def _clean_unit_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("µ", "u").replace("μ", "u").replace("°", "")
    text = text.replace("ohms", "ohm").replace("ohm", "ohm")
    return text


def _target_unit(unit: Optional[str]) -> Tuple[Optional[str], float]:
    """Returns (base_unit, factor_from_base_to_target) for a declared unit."""
    raw = _clean_unit_text(unit or "")
    if not raw:
        return None, 1.0
    raw = raw.replace(" ", "")
    if raw in _TARGET_UNITS:
        return _TARGET_UNITS[raw]
    # A target unit such as "ohm" or "V" that only appears in the alias table.
    if raw in _ALIAS_TO_BASE:
        return _ALIAS_TO_BASE[raw], 1.0
    return None, 1.0


def _clean_number(value: float) -> Optional[float]:
    """Removes binary-float noise (100nF -> 0.1, not 0.09999999999999999)."""
    if not math.isfinite(value):
        return None
    return float(f"{value:.12g}")


def parse_engineering_value(
    key: str,
    value: Any,
    unit: Optional[str] = None,
) -> Optional[float]:
    """
    Parses an engineering value into the key's declared unit.

    Examples:
      resistance_ohm: "10k" -> 10000, "4k7" -> 4700, "1M" -> 1000000
      capacitance_uf: "100nF" -> 0.1, "1000pF" -> 0.001, "100 uF" -> 100
      inductance_uh: "2.2mH" -> 2200

    Case matters for the multiplier: "m" is milli, "M" is mega.
    Returns None when the text is not a number with understood units.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("µ", "u").replace("μ", "u").replace("Ω", "Ω")

    base_unit, target_factor = _target_unit(unit)

    # RKM notation: 4k7 == 4.7k, 4R7 == 4.7 ohm.
    rkm = _RKM_RE.match(text)
    if rkm:
        sign, whole, prefix, fraction, unit_tail = rkm.groups()
        try:
            number = float(f"{whole}.{fraction}")
        except ValueError:
            return None
        if sign == "-":
            number = -number
        prefix_factor = 1.0 if prefix == "R" else _PREFIX_FACTORS.get(prefix)
        if prefix_factor is None:
            return None
        unit_text = _clean_unit_text(unit_tail)
        if unit_text:
            unit_base = _ALIAS_TO_BASE.get(unit_text)
            if base_unit is not None and unit_base is not None and unit_base != base_unit:
                return None
            base_factor = 1.0
        else:
            base_factor = 1.0
        result = number * prefix_factor * base_factor * target_factor
        return _clean_number(result)

    match = _NUM_UNIT_RE.match(text)
    if not match:
        return None
    number_text, prefix, unit_tail = match.groups()
    try:
        number = float(number_text)
    except ValueError:
        return None

    prefix_factor = _PREFIX_FACTORS.get(prefix, 1.0) if prefix else 1.0
    unit_text = _clean_unit_text(unit_tail)
    if unit_text:
        unit_base = _ALIAS_TO_BASE.get(unit_text)
        if unit_base is None:
            # Accept the declared unit itself even if it is not in the alias table.
            declared = _clean_unit_text(unit or "")
            if unit_base is None and declared and unit_text == declared:
                unit_base = base_unit
            if unit_base is None:
                return None
        if base_unit is not None and unit_base != base_unit:
            return None
        base_factor = 1.0
    else:
        base_factor = 1.0

    result = number * prefix_factor * base_factor * target_factor
    return _clean_number(result)


def _base_unit_example(key: str, unit: Optional[str]) -> str:
    """A concrete, correct example for an invalid numeric filter."""
    samples = {
        "resistance_ohm": "10000 or 10k",
        "capacitance_uf": "0.1 or 100nF",
        "inductance_uh": "2200 or 2.2mH",
        "voltage_v": "12 or 12V",
        "current_a": "0.5 or 500mA",
        "power_w": "0.25 or 250mW",
    }
    example = samples.get(key, "10")
    if unit:
        return f"{example} (base unit: {unit})"
    return example


def canonical_enum_value(value: Any, spec_meta: Optional[Dict[str, Any]] = None) -> Any:
    """Canonicalizes a categorical enum value for storage and filtering."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    text = text.lower()
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("µ", "u").replace("μ", "u").replace("Ω", "ohm")
    # Drop parenthetical detail: "sot-23(surface mount device)" -> "sot-23".
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = text.replace("surfacemountdevice", "smd")
    text = text.replace("surface mount device", "smd")
    text = text.replace("surface-mount", "smd")
    text = text.replace("surface mount", "smd")
    text = text.replace("through-hole", "throughhole").replace("through hole", "throughhole")
    text = re.sub(r"\s+", "", text)
    # Common compound spellings: "to92throughhole", "axialleadthroughhole".
    # Keep a standalone "through-hole" intact: it is an advertised umbrella.
    if text != "throughhole":
        text = re.sub(r"throughhole$", "", text)
    text = re.sub(r"(?:smd|surfacemount|surfacemountdevice)$", "", text)
    text = text.replace("axiallead", "axial")
    text = re.sub(r"-+", "-", text).strip("-")
    text = {
        "sot23": "sot-23",
        "sot223": "sot-223",
        "to220": "to-220",
        "to247": "to-247",
        "to92": "to-92",
        "to252": "to-252",
        "to263": "to-263",
        "do41": "do-41",
        "do15": "do-15",
        "hc49": "hc-49",
    }.get(text, text)
    # SOT-23/SOT-23-5/SOT-23-6 are distinct packages only when a pin count
    # suffix is present. The bare family is the canonical surface-mount part.
    if text in ("sot-23smd", "sot-23surfacemount"):
        text = "sot-23"
    return text


def coerce_enum_value(key: str, value: Any) -> Tuple[Any, bool]:
    """Coerces unambiguous numeric enum spellings ("603" -> "0603", 3 -> "3s")."""
    if value is None:
        return value, False
    text = str(value).strip()
    if key == "cells" and re.fullmatch(r"\d+", text):
        return f"{int(text)}s", True
    if key == "package" and re.fullmatch(r"\d{3,4}", text):
        padded = text.zfill(4) if len(text) == 3 else text
        return padded, padded != text
    return value, False


# Through-hole is an umbrella over the concrete leaded packages. A filter for
# the umbrella must expand to the observed package names for that category.
_THROUGH_HOLE_UMBRELLA: Dict[Tuple[str, str], List[str]] = {
    ("resistors", "package"): ["axial", "radial"],
    ("capacitors", "package"): ["axial", "radial"],
    ("inductors", "package"): ["axial", "radial"],
    ("diodes", "package"): ["do-41", "do-15", "to-220"],
    ("transistors", "package"): ["to-92", "to-220", "to-247", "dip"],
    ("semiconductors", "package"): ["to-220", "to-247"],
    ("leds", "package"): ["3mm", "5mm"],
    ("crystals", "package"): ["hc-49"],
}


def expand_enum_umbrella(
    key: str,
    value: Any,
    category: Optional[str],
) -> Optional[List[str]]:
    """Expands an advertised umbrella enum value into concrete stored values."""
    if str(value).strip().lower() not in ("through-hole", "throughhole", "through hole"):
        return None
    if category is not None:
        return _THROUGH_HOLE_UMBRELLA.get((str(category), key))
    if key == "package":
        values: List[str] = []
        for concrete in _THROUGH_HOLE_UMBRELLA.values():
            for item in concrete:
                if item not in values:
                    values.append(item)
        return values
    return None


# Equivalent filter keys. `equivalent=True` groups are genuinely the same
# property under different names; `equivalent=False` groups are discovery-only
# (contact current is not motor current, but an agent should see the family).
ALIAS_GROUPS: Dict[str, Dict[str, Any]] = {
    "voltage": {
        "equivalent": True,
        "keys": ["voltage", "voltage_v"],
        "description": "Supply/rated voltage; the two keys are interchangeable aliases.",
    },
    "current": {
        "equivalent": False,
        "keys": [
            "current_a", "contact_current_a", "forward_current_a",
            "id_a", "output_current_a", "ic_a",
        ],
        "description": "Current family; keep contact/forward/drain/output currents distinct.",
    },
}


def alias_catalog_payload() -> Dict[str, Any]:
    """Discovery payload for describe_catalog."""
    return {
        group: {
            "keys": list(info["keys"]),
            "equivalent": bool(info["equivalent"]),
            "description": info["description"],
        }
        for group, info in ALIAS_GROUPS.items()
    }


def equivalent_alias_keys(key: str) -> List[str]:
    info = ALIAS_GROUPS.get(str(key))
    if info and info.get("equivalent"):
        return list(info["keys"])
    for info in ALIAS_GROUPS.values():
        if info.get("equivalent") and key in info["keys"]:
            return list(info["keys"])
    return []


def route_alias_key(
    key: str,
    category: Optional[str],
    category_keys: Optional[List[str]],
) -> Optional[str]:
    """Returns the category-applicable equivalent key, if a route exists."""
    keys = equivalent_alias_keys(key)
    if not keys:
        return None
    if category and category_keys:
        applicable = [k for k in keys if k in category_keys]
        if key in applicable:
            return key
        if applicable:
            return applicable[0]
    return None


def build_alias_expansion(
    key: str,
    value: Any,
    advertised_specs: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Expansion descriptor for an unscoped equivalent-key union."""
    keys = [k for k in equivalent_alias_keys(key) if k in advertised_specs]
    if len(keys) < 2:
        return None
    return {"group": key if key in ALIAS_GROUPS else "voltage", "keys": keys, "value": value}


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

def normalize_scalar(
    key: str,
    val: Any,
    v_type: str = "string",
    unit: Optional[str] = None,
    category: Optional[str] = None,
) -> Any:
    """Normalizes units and aliases into standard canonical catalog forms."""
    if val is None:
        return None
    s = str(val).strip()

    # Cells (e.g. 4S, 4 s, 4s lipo, or a bare count 4 -> 4s)
    if key == "cells" or (isinstance(s, str) and re.search(r"^\d+\s*s(\s+lipo|\s+li-ion)?$", s, re.I)):
        if re.fullmatch(r"\d+", s):
            return f"{int(s)}s"
        m = re.search(r"^(\d+)\s*s", s, re.I)
        if m:
            return f"{m.group(1)}s".lower()

    if key == "package":
        coerced, changed = coerce_enum_value(key, s)
        if changed:
            s = str(coerced)
        return canonical_enum_value(s)

    # Torque (2.5kg-cm / 2.5 kgcm)
    if "torque" in key or (isinstance(s, str) and re.search(r"kg[\s-]*cm", s, re.I)):
        m_t = re.search(r"^([\d\.]+)\s*kg[\s-]*cm$", s, re.I)
        if m_t:
            return float(m_t.group(1))

    # Capacity (mAh / Ah)
    if key == "capacity_mah" or unit == "mAh":
        m_ah = re.search(r"^([\d\.]+)\s*ah$", s, re.I)
        if m_ah:
            return int(round(float(m_ah.group(1)) * 1000))
        m_mah = re.search(r"^([\d\.]+)\s*(?:mah)?$", s, re.I)
        if m_mah:
            return int(round(float(m_mah.group(1))))

    # Voltage (V / VDC)
    if key == "voltage" or unit == "V":
        m_v = re.search(r"^([\d\.]+)\s*(?:v(?:dc)?)?$", s, re.I)
        if m_v:
            v_num = m_v.group(1)
            if v_num.endswith(".0"):
                v_num = v_num[:-2]
            return v_num

    # RPM
    if key == "rpm" or unit == "RPM":
        m_rpm = re.search(r"^(\d+)\s*(?:rpm)?$", s, re.I)
        if m_rpm:
            return int(m_rpm.group(1))

    # Current (A / mA)
    if key == "current_a" or unit == "A":
        m_ma = re.search(r"^([\d\.]+)\s*ma$", s, re.I)
        if m_ma:
            return round(float(m_ma.group(1)) / 1000.0, 3)
        m_a = re.search(r"^([\d\.]+)\s*(?:a|amp|amps)?$", s, re.I)
        if m_a:
            return float(m_a.group(1))

    # Discharge C-rate (10C / 10 C)
    if key == "discharge_c":
        m_c = re.search(r"^([\d\.]+)\s*c$", s, re.I)
        if m_c:
            return float(m_c.group(1))

    # Channels
    if key == "channels":
        m_ch = re.search(r"^(\d+)\s*(?:ch|channel|channels)?$", s, re.I)
        if m_ch:
            return int(m_ch.group(1))

    # NEMA
    if re.search(r"^nema\s*\d+$", s, re.I):
        return re.sub(r"\s+", "", s).lower()

    if v_type in ("integer", "float"):
        # An int/float argument is already in the key's declared unit. Only
        # string input may carry a prefix that needs conversion (2200 uH vs
        # "2.2mH").
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            number = float(val)
            return int(round(number)) if v_type == "integer" else number
        number = parse_engineering_value(key, s, unit)
        if number is not None:
            return int(round(number)) if v_type == "integer" else number
        return s.lower()

    # Remaining enums: canonical spelling, dashes and case.
    text, _coerced = coerce_enum_value(key, s)
    return canonical_enum_value(text)


# ---------------------------------------------------------------------------
# Filter validation
# ---------------------------------------------------------------------------

def _nearest_keys(bad_key: str, valid_keys: List[str], limit: int = 5) -> List[Dict[str, Any]]:
    """Suggest the closest valid spec keys instead of dumping the whole list."""
    close = difflib.get_close_matches(bad_key, valid_keys, n=limit, cutoff=0.4)
    if not close:
        close = valid_keys[:limit]
    return [{"key": k} for k in close]


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------

def _singular(token: str) -> str:
    """Conservative singular form used only for category-name matching."""
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def resolve_category(
    value: Optional[str],
    valid_categories: List[str],
    limit: int = 5,
) -> Tuple[Optional[str], List[str]]:
    """
    Resolves a caller-supplied category to a canonical catalog category.

    Returns `(canonical, suggestions)`. Accepts exact names, simple plurals
    ("motors" -> "motor") and close misspellings ("motr" -> "motor"). Always
    suggests the nearest valid categories when it cannot resolve, ranked with
    the same fuzzy matcher used for spec keys.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return None, []

    valid = sorted({str(c) for c in valid_categories})
    if raw in valid:
        return raw, []

    singular = _singular(raw)
    if singular in valid:
        return singular, []

    close = difflib.get_close_matches(raw, valid, n=limit, cutoff=0.4)
    if not close and singular != raw:
        close = difflib.get_close_matches(singular, valid, n=limit, cutoff=0.4)
    if not close:
        close = valid[:limit]
    return None, close


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------

def _numeric_key(spec_meta: Dict[str, Any]) -> bool:
    """True when the key's values are numbers (either typed or numeric enum)."""
    return spec_meta.get("type") in ("integer", "float") or enum_is_numeric(spec_meta)


def _validate_numeric_value(
    key: str,
    value: Any,
    spec_meta: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Rejects values that cannot be compared numerically.

    Without this, `rpm: "abc"` reaches SQL as `value_num = 'abc'`, matches zero
    rows, and is indistinguishable from "no such part exists".
    """
    if not _numeric_key(spec_meta):
        return None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return {
            "error_code": "invalid_filter_value",
            "message": (
                f"Filter '{key}' expects a numeric value; got {value!r}. "
                f"This key is {spec_meta.get('type', 'numeric')}. "
                f"Engineering suffixes are accepted (e.g. {_base_unit_example(key, spec_meta.get('unit'))})."
            ),
            "suggestions": [{
                "key": key,
                "value": 1,
                "unit": spec_meta.get("unit"),
                "example": _base_unit_example(key, spec_meta.get("unit")),
            }],
            "details": {
                "key": key,
                "provided_value": value,
                "expected_type": spec_meta.get("type") or "number",
                "unit": spec_meta.get("unit"),
                "expected": _base_unit_example(key, spec_meta.get("unit")),
            },
        }
    if not math.isfinite(number):
        return {
            "error_code": "invalid_filter_value",
            "message": f"Filter '{key}' expects a finite number; got {value!r}.",
            "suggestions": [{"key": key, "value": 1}],
            "details": {"key": key, "provided_value": value, "expected_type": "number"},
        }
    return None


def _normalize_enum_filter_value(
    key: str,
    raw_value: Any,
    spec_meta: Dict[str, Any],
    category: Optional[str],
    coercions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Canonicalizes one enum value and expands umbrella values when needed."""
    coerced, changed = coerce_enum_value(key, raw_value)
    if changed and coercions is not None:
        coercions.append({"key": key, "from": raw_value, "to": coerced})
    # Reuse the key-specific normalizers (voltage "24V" -> "24", cells 3 -> "3s").
    normalized = normalize_scalar(key, coerced, "enum", spec_meta.get("unit"), category)
    canonical = canonical_enum_value(normalized, spec_meta)
    if canonical != str(coerced).strip().lower() and coercions is not None:
        # A normalization such as "SOT-23(SMD)" -> "sot-23" is still worth
        # disclosing; it tells the caller the stored vocabulary differs.
        if canonical != str(raw_value).strip().lower():
            coercions.append({"key": key, "from": raw_value, "to": canonical})
    umbrella = expand_enum_umbrella(key, canonical, category)
    if umbrella:
        return {"in": umbrella}
    return canonical


def parse_and_validate_filters(
    filters: Optional[Dict[str, Any]],
    advertised_specs: Dict[str, Dict[str, Any]],
    category: Optional[str] = None,
    coercions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Validates filter keys and operators, rejecting deprecated string range forms.

    Returns `(normalized_filters, error)` where `error` is a plain dict shaped
    like the MCP error envelope fields (`error_code`, `message`, `valid_keys`,
    `suggestions`, `details`) so any caller can render it in its own envelope.
    """
    clean_filters = filters or {}
    all_valid_keys = sorted(advertised_specs.keys())
    normalized: Dict[str, Any] = {}

    for f_key, f_val in clean_filters.items():
        if f_key not in advertised_specs:
            return None, {
                "error_code": "unknown_filter_key",
                "message": f"Unknown filter key '{f_key}'.",
                "valid_keys": all_valid_keys,
                "suggestions": _nearest_keys(f_key, all_valid_keys),
                "details": {"provided_key": f_key},
            }

        spec_meta = advertised_specs[f_key]
        v_type = spec_meta["type"]
        unit = spec_meta["unit"]

        # Check for deprecated string range syntax e.g. "150-250"
        if isinstance(f_val, str) and "-" in f_val and not f_val.strip().startswith("-"):
            parts = f_val.split("-")
            if len(parts) == 2:
                try:
                    p1 = float(parts[0].strip())
                    p2 = float(parts[1].strip())
                    return None, {
                        "error_code": "unsupported_operator",
                        "message": (
                            f"String range syntax '{f_val}' for '{f_key}' is deprecated. "
                            f"Use operator form: {{'{f_key}': {{'gte': {p1}, 'lte': {p2}}}}}"
                        ),
                        "valid_keys": all_valid_keys,
                        "suggestions": [{"key": f_key, "value": {"gte": p1, "lte": p2}}],
                        "details": {"deprecated_input": f_val},
                    }
                except ValueError:
                    pass

        # Operator dictionary
        if isinstance(f_val, dict):
            allowed_ops = {"in", "gte", "lte", "gt", "lt", "eq", "tolerance"}
            unknown_ops = set(f_val.keys()) - allowed_ops
            if unknown_ops:
                return None, {
                    "error_code": "unsupported_operator",
                    "message": (
                        f"Unsupported operator(s) {list(unknown_ops)} for filter '{f_key}'. "
                        f"Allowed operators: {sorted(list(allowed_ops))}"
                    ),
                    "valid_keys": all_valid_keys,
                    "suggestions": [
                        {"operator": "in"},
                        {"operator": "gte"},
                        {"operator": "lte"},
                        {"operator": "eq"},
                    ],
                    "details": {"provided_key": f_key},
                }

            # Ordering / tolerance only make sense on values that can be ordered.
            ordering_ops = {"gte", "lte", "gt", "lt", "tolerance"} & set(f_val.keys())
            if ordering_ops and not enum_is_numeric(spec_meta):
                sample_vals = list((spec_meta.get("value_counts") or {}).keys())[:8]
                return None, {
                    "error_code": "operator_not_supported_for_type",
                    "message": (
                        f"Operator(s) {sorted(ordering_ops)} cannot be applied to '{f_key}' because it is a "
                        "categorical enum with non-numeric values. Use 'eq' or 'in' instead."
                    ),
                    "valid_keys": all_valid_keys,
                    "suggestions": [{"key": f_key, "operator": "in", "values": sample_vals}],
                    "details": {"key": f_key, "value_type": v_type, "sample_values": sample_vals},
                }

            if any(f_val.get(op) is None for op in ordering_ops | {"eq"} & set(f_val.keys())):
                return None, {
                    "error_code": "invalid_filter_value",
                    "message": f"Filter '{f_key}' has a null value for one of its operators.",
                    "valid_keys": all_valid_keys,
                    "suggestions": [],
                    "details": {"provided_value": f_val},
                }

            norm_op_dict: Dict[str, Any] = {}
            if "in" in f_val:
                in_list = f_val["in"]
                if not isinstance(in_list, (list, tuple)):
                    return None, {
                        "error_code": "invalid_filter_value",
                        "message": f"Operator 'in' for filter '{f_key}' requires a list of values.",
                        "valid_keys": all_valid_keys,
                        "suggestions": [],
                        "details": {"provided_value": in_list},
                    }
                if not in_list:
                    return None, {
                        "error_code": "invalid_filter_value",
                        "message": f"Operator 'in' for filter '{f_key}' requires at least one value.",
                        "valid_keys": all_valid_keys,
                        "suggestions": [],
                        "details": {"provided_value": in_list},
                    }
                norm_op_dict["in"] = []
                for raw_value in in_list:
                    if v_type == "enum":
                        value = _normalize_enum_filter_value(
                            f_key, raw_value, spec_meta, category, coercions
                        )
                    else:
                        value = normalize_scalar(f_key, raw_value, v_type, unit, category)
                    if isinstance(value, dict) and "in" in value:
                        norm_op_dict["in"].extend(value["in"])
                        continue
                    invalid = _validate_numeric_value(f_key, value, spec_meta)
                    if invalid:
                        invalid["valid_keys"] = all_valid_keys
                        invalid["details"]["operator"] = "in"
                        return None, invalid
                    norm_op_dict["in"].append(value)

            for bound in ("gte", "lte", "gt", "lt"):
                if bound in f_val:
                    value = normalize_scalar(f_key, f_val[bound], v_type, unit)
                    invalid = _validate_numeric_value(f_key, value, spec_meta)
                    if invalid:
                        invalid["valid_keys"] = all_valid_keys
                        invalid["details"]["operator"] = bound
                        return None, invalid
                    norm_op_dict[bound] = value

            if "eq" in f_val:
                if v_type == "enum":
                    value = _normalize_enum_filter_value(
                        f_key, f_val["eq"], spec_meta, category, coercions
                    )
                else:
                    value = normalize_scalar(f_key, f_val["eq"], v_type, unit, category)
                if isinstance(value, dict) and "in" in value:
                    norm_op_dict["in"] = value["in"]
                else:
                    invalid = _validate_numeric_value(f_key, value, spec_meta)
                    if invalid:
                        invalid["valid_keys"] = all_valid_keys
                        invalid["details"]["operator"] = "eq"
                        return None, invalid
                    norm_op_dict["eq"] = value

            if "tolerance" in f_val:
                if "eq" not in f_val:
                    return None, {
                        "error_code": "invalid_filter_value",
                        "message": (
                            f"Filter '{f_key}' supplied 'tolerance' without 'eq'. Tolerance is a "
                            "fraction of an equality value (0.05 = +/-5%)."
                        ),
                        "valid_keys": all_valid_keys,
                        "suggestions": [
                            {"key": f_key, "value": {"eq": f_val.get("eq", 1), "tolerance": 0.05}}
                        ],
                        "details": {"provided_value": f_val},
                    }
                try:
                    tolerance = float(f_val["tolerance"])
                except (ValueError, TypeError):
                    return None, {
                        "error_code": "invalid_filter_value",
                        "message": (
                            f"Tolerance value for '{f_key}' must be a fraction of the equality "
                            "value (e.g. 0.05 for +/-5%)."
                        ),
                        "valid_keys": all_valid_keys,
                        "suggestions": [],
                        "details": {"provided_value": f_val},
                    }
                if not math.isfinite(tolerance) or not (0.0 < tolerance <= 1.0):
                    return None, {
                        "error_code": "invalid_filter_value",
                        "message": (
                            f"Tolerance for '{f_key}' must be a fraction greater than 0 and at most 1. "
                            f"Got {f_val['tolerance']!r}; 0.05 means +/-5% of the 'eq' value."
                        ),
                        "valid_keys": all_valid_keys,
                        "suggestions": [{"key": f_key, "tolerance": 0.05}],
                        "details": {
                            "key": f_key,
                            "provided_value": f_val["tolerance"],
                            "expected": "0 < tolerance <= 1 (fraction of eq)",
                        },
                    }
                norm_op_dict["tolerance"] = tolerance

            normalized[f_key] = norm_op_dict
        else:
            # Primitive scalar equality
            if v_type == "enum":
                value = _normalize_enum_filter_value(
                    f_key, f_val, spec_meta, category, coercions
                )
                normalized[f_key] = value
                continue
            value = normalize_scalar(f_key, f_val, v_type, unit, category)
            invalid = _validate_numeric_value(f_key, value, spec_meta)
            if invalid:
                invalid["valid_keys"] = all_valid_keys
                return None, invalid
            normalized[f_key] = value

    return normalized, None


def validate_filter_scope(
    normalized_filters: Dict[str, Any],
    category: Optional[str],
    category_keys: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Rejects filters that cannot apply to the requested category.

    A key such as `chemistry` exists globally (batteries) but is meaningless for
    motors. Previously that returned an empty success and a suggestion list from
    the global value distribution, which told the caller the opposite of the
    truth. The category is the scope; the key must be defined inside it.
    """
    if not category or not normalized_filters:
        return None

    applicable = list(dict.fromkeys(str(k) for k in (category_keys or [])))
    bad_keys = [k for k in normalized_filters if k not in applicable]
    if not bad_keys:
        return None

    filter_support = "none" if not applicable else "partial"
    if not applicable:
        message = (
            f"Category '{category}' has no filterable spec attributes. "
            "Search it by text, or use a category that advertises attributes "
            "(see describe_catalog data.taxonomy[].filterable_attributes)."
        )
    else:
        message = (
            f"Filter key(s) {bad_keys} do not apply to category '{category}'. "
            f"Applicable keys: {applicable}."
        )

    return {
        "error_code": "filter_not_applicable_to_category",
        "message": message,
        "suggestions": [{"key": k} for k in applicable[:8]],
        "details": {
            "category": category,
            "not_applicable_keys": bad_keys,
            "applicable_keys": applicable,
            "filter_support": filter_support,
        },
    }


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def _enum_match_values(value: Any, spec_meta: Dict[str, Any]) -> List[str]:
    """Canonical value plus any pre-migration raw variants stored in the DB."""
    canonical = str(value)
    variants = (spec_meta.get("variant_map") or {}).get(canonical) or []
    values = [canonical] + [str(v) for v in variants if str(v) != canonical]
    return list(dict.fromkeys(values))


def build_alias_clauses(
    alias_filters: Optional[List[Dict[str, Any]]],
    advertised_specs: Dict[str, Dict[str, Any]],
) -> Tuple[List[str], List[Any]]:
    """
    SQL for an equivalent-key union, e.g. voltage OR voltage_v.

    Each key becomes an EXISTS subquery so a product carrying both keys is
    counted once (a normal JOIN would duplicate it before COUNT(DISTINCT)).
    """
    clauses: List[str] = []
    params: List[Any] = []
    for entry in alias_filters or []:
        group_clauses: List[str] = []
        for key in entry.get("keys") or []:
            meta = advertised_specs.get(str(key))
            if meta is None:
                continue
            alias = "psa"
            col = filter_column(alias, meta)
            num_col = filter_column(alias, meta, ordering=True)
            value = entry.get("value")
            conditions: List[str] = [f"{alias}.key = ?"]
            local_params: List[Any] = [str(key)]
            if isinstance(value, dict):
                if "in" in value:
                    values = []
                    for v in value["in"]:
                        values.extend(_enum_match_values(v, meta) if meta.get("type") == "enum" else [v])
                    ph = ", ".join(["?"] * len(values))
                    conditions.append(f"{col} IN ({ph})")
                    local_params.extend(values)
                for op, sql_op in (("gte", ">="), ("gt", ">"), ("lte", "<="), ("lt", "<")):
                    if op in value:
                        conditions.append(f"{num_col} {sql_op} ?")
                        local_params.append(float(value[op]))
                if "eq" in value:
                    if "tolerance" in value:
                        center = float(value["eq"])
                        tol = float(value["tolerance"])
                        conditions.append(f"{num_col} >= ? AND {num_col} <= ?")
                        local_params.extend([center * (1 - tol), center * (1 + tol)])
                    else:
                        vals = _enum_match_values(value["eq"], meta) if meta.get("type") == "enum" else [value["eq"]]
                        ph = ", ".join(["?"] * len(vals))
                        conditions.append(f"{col} IN ({ph})")
                        local_params.extend(vals)
            else:
                vals = _enum_match_values(value, meta) if meta.get("type") == "enum" else [value]
                ph = ", ".join(["?"] * len(vals))
                conditions.append(f"{col} IN ({ph})")
                local_params.extend(vals)
            group_clauses.append(
                "EXISTS (SELECT 1 FROM product_specs psa WHERE psa.product_id = p.id AND "
                + " AND ".join(conditions)
                + ")"
            )
            params.extend(local_params)
        if group_clauses:
            clauses.append("(" + " OR ".join(group_clauses) + ")")
    return clauses, params


def build_spec_clauses(
    normalized_filters: Dict[str, Any],
    advertised_specs: Dict[str, Dict[str, Any]],
    alias_prefix: str = "ps",
) -> Tuple[List[str], List[Any], List[str], List[Any]]:
    """
    Builds the JOINs and WHERE clauses for a set of spec filters.

    Returns `(joins, join_params, where_clauses, where_params)`. The two
    parameter lists are kept apart on purpose: the generated SQL renders the
    joins before the WHERE clause, so the join parameters have to be bound
    first. Each filter key gets its own `product_specs` join so multiple keys
    can be constrained independently.
    """
    joins: List[str] = []
    join_params: List[Any] = []
    where_clauses: List[str] = []
    where_params: List[Any] = []

    for i, (f_key, f_val) in enumerate(normalized_filters.items()):
        alias = f"{alias_prefix}_{i}"
        spec_meta = advertised_specs[f_key]

        joins.append(
            f"JOIN product_specs {alias} ON p.id = {alias}.product_id AND {alias}.key = ?"
        )
        join_params.append(f_key)

        col = filter_column(alias, spec_meta)
        num_col = filter_column(alias, spec_meta, ordering=True)

        if isinstance(f_val, dict):
            if "in" in f_val:
                expanded: List[Any] = []
                for value in f_val["in"]:
                    if spec_meta.get("type") == "enum":
                        expanded.extend(_enum_match_values(value, spec_meta))
                    else:
                        expanded.append(value)
                placeholders = ", ".join(["?"] * len(expanded))
                where_clauses.append(f"{col} IN ({placeholders})")
                where_params.extend(expanded)

            if "gte" in f_val:
                where_clauses.append(f"{num_col} >= ?")
                where_params.append(float(f_val["gte"]))
            if "gt" in f_val:
                where_clauses.append(f"{num_col} > ?")
                where_params.append(float(f_val["gt"]))
            if "lte" in f_val:
                where_clauses.append(f"{num_col} <= ?")
                where_params.append(float(f_val["lte"]))
            if "lt" in f_val:
                where_clauses.append(f"{num_col} < ?")
                where_params.append(float(f_val["lt"]))

            if "eq" in f_val:
                if "tolerance" in f_val:
                    center = float(f_val["eq"])
                    tol = float(f_val["tolerance"])
                    where_clauses.append(f"{num_col} >= ? AND {num_col} <= ?")
                    where_params.extend([center * (1.0 - tol), center * (1.0 + tol)])
                else:
                    values = (
                        _enum_match_values(f_val["eq"], spec_meta)
                        if spec_meta.get("type") == "enum"
                        else [f_val["eq"]]
                    )
                    if len(values) > 1:
                        placeholders = ", ".join(["?"] * len(values))
                        where_clauses.append(f"{col} IN ({placeholders})")
                        where_params.extend(values)
                    elif spec_meta.get("type") == "float":
                        where_clauses.append(f"ABS({col} - ?) <= 1e-9 * MAX(1.0, ABS(?))")
                        where_params.extend([f_val["eq"], f_val["eq"]])
                    else:
                        where_clauses.append(f"{col} = ?")
                        where_params.append(f_val["eq"])
        else:
            values = (
                _enum_match_values(f_val, spec_meta)
                if spec_meta.get("type") == "enum"
                else [f_val]
            )
            if len(values) > 1:
                placeholders = ", ".join(["?"] * len(values))
                where_clauses.append(f"{col} IN ({placeholders})")
                where_params.extend(values)
            elif spec_meta.get("type") == "float":
                where_clauses.append(f"ABS({col} - ?) <= 1e-9 * MAX(1.0, ABS(?))")
                where_params.extend([f_val, f_val])
            else:
                where_clauses.append(f"{col} = ?")
                where_params.append(f_val)

    return joins, join_params, where_clauses, where_params
