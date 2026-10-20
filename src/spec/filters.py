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
# Value normalization
# ---------------------------------------------------------------------------

def normalize_scalar(key: str, val: Any, v_type: str = "string", unit: Optional[str] = None) -> Any:
    """Normalizes units and aliases into standard canonical catalog forms."""
    if val is None:
        return None
    s = str(val).strip()

    # Cells (e.g. 4S, 4 s, 4s lipo)
    if key == "cells" or (isinstance(s, str) and re.search(r"^\d+\s*s(\s+lipo|\s+li-ion)?$", s, re.I)):
        m = re.search(r"^(\d+)\s*s", s, re.I)
        if m:
            return f"{m.group(1)}s".lower()

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
        try:
            number = float(s)
            return int(round(number)) if v_type == "integer" else number
        except ValueError:
            pass

    return s.lower()


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
                f"This key is {spec_meta.get('type', 'numeric')}."
            ),
            "suggestions": [{"key": key, "value": 1}],
            "details": {
                "key": key,
                "provided_value": value,
                "expected_type": spec_meta.get("type") or "number",
                "unit": spec_meta.get("unit"),
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


def parse_and_validate_filters(
    filters: Optional[Dict[str, Any]],
    advertised_specs: Dict[str, Dict[str, Any]],
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
                    value = normalize_scalar(f_key, raw_value, v_type, unit)
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
                value = normalize_scalar(f_key, f_val["eq"], v_type, unit)
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
            value = normalize_scalar(f_key, f_val, v_type, unit)
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
                placeholders = ", ".join(["?"] * len(f_val["in"]))
                where_clauses.append(f"{col} IN ({placeholders})")
                where_params.extend(f_val["in"])

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
                    where_clauses.append(f"{col} = ?")
                    where_params.append(f_val["eq"])
        else:
            where_clauses.append(f"{col} = ?")
            where_params.append(f_val)

    return joins, join_params, where_clauses, where_params
