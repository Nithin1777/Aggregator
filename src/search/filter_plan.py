"""Transport-independent filter routing, validation and alias planning."""
from dataclasses import dataclass, field
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.spec.filters import (build_alias_expansion, equivalent_alias_keys, load_catalog_specs,
                              parse_and_validate_filters, route_alias_key,
                              validate_filter_scope)
from src.spec.vendor_index import load_vendor_specs, normalize_vendor_key


class FilterPlanError(ValueError):
    """Structured domain error; each transport chooses its own envelope."""

    def __init__(self, error):
        self.error = error
        super().__init__(error["message"])


@dataclass
class FilterPlan:
    metadata: Dict[str, Dict[str, Any]]
    filters: Dict[str, Any]
    requested_filters: Dict[str, Any]
    vendor_metadata: Dict[str, Dict[str, Any]]
    vendor_filters: Dict[str, Any]
    aliases: List[Dict[str, Any]]
    alias_disclosed: Dict[str, List[str]]
    alias_routes: List[Dict[str, Any]]
    tier_routes: List[Dict[str, Any]]
    coercions: List[Dict[str, Any]] = field(default_factory=list)


def _validated(filters, metadata, *, category=None, tier=None, coercions=None):
    normalized, error = parse_and_validate_filters(
        filters, metadata, category=category, coercions=coercions,
    )
    if error:
        details = dict(error.get("details") or {})
        if tier:
            details.setdefault("tier", tier)
        raise FilterPlanError({
            **error, "details": details,
            "diagnosis": {"reason": error["error_code"], "severity": "error", **details},
        })
    return normalized or {}


def prepare_filters(conn, filters=None, *, category=None, category_keys=None,
                    vendor_filters=None, include_vendor_specs=True, metadata=None):
    """Prepare the same engineering requirements for any search surface."""
    metadata = load_catalog_specs(conn) if metadata is None else metadata
    if category_keys is None:
        category_keys = [str(row[0]) for row in conn.execute(
            "SELECT DISTINCT key FROM spec_definitions WHERE category = ? ORDER BY key;",
            (category,),
        )] if category else []
    if filters is not None and not isinstance(filters, dict):
        raise FilterPlanError({"error_code": "invalid_filter_value", "message": "filters must be an object."})
    if vendor_filters is not None and not isinstance(vendor_filters, dict):
        raise FilterPlanError({"error_code": "invalid_filter_value", "message": "vendor_filters must be an object."})
    promoted, automatic_vendor, tier_routes = split_filter_tiers(
        filters, metadata, conn, category, category_keys, allow_vendor=include_vendor_specs,
    )
    requested_vendor = {**automatic_vendor,
                        **{normalize_vendor_key(k): v for k, v in (vendor_filters or {}).items()}}
    vendor_metadata = load_vendor_filter_meta(conn, requested_vendor, category)
    missing = [key for key in requested_vendor if key not in vendor_metadata]
    if missing:
        raise FilterPlanError({
            "error_code": "unknown_filter_key",
            "message": (f"Unknown vendor filter key(s) {missing}. "
                        "Use describe_spec(key=..., vendor=True) to inspect vendor attributes."),
            "valid_keys": sorted(vendor_metadata)[:50],
            "suggestions": [{"key": key} for key in missing[:3]],
            "details": {"tier": "vendor", "provided_keys": missing},
        })
    coercions = []
    normalized_vendor = _validated(requested_vendor, vendor_metadata, tier="vendor", coercions=coercions)
    routed, alias_routes = route_filter_aliases(promoted, metadata, category, category_keys)
    requested_filters = _validated(routed, metadata, category=category, coercions=coercions)
    normalized, aliases, disclosed = expand_unscoped_aliases(requested_filters, metadata)
    error = validate_filter_scope(normalized, category, category_keys)
    if error:
        raise FilterPlanError({**error, "diagnosis": {"reason": error["error_code"], **(error.get("details") or {})}})
    return FilterPlan(
        metadata=metadata, filters=normalized, requested_filters=requested_filters,
        vendor_metadata=vendor_metadata, vendor_filters=normalized_vendor,
        aliases=aliases, alias_disclosed=disclosed, alias_routes=alias_routes,
        tier_routes=tier_routes, coercions=coercions,
    )

def route_filter_aliases(
    filters: Optional[Dict[str, Any]],
    catalog_specs: Dict[str, Dict[str, Any]],
    category: Optional[str],
    category_keys: List[str],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Routes equivalent keys to the category-applicable key before validation."""
    if not filters or not category:
        return filters, []
    routed: Dict[str, Any] = {}
    applied: List[Dict[str, Any]] = []
    for key, value in filters.items():
        target = route_alias_key(key, category, category_keys)
        if target and target != key:
            routed[key] = value
            applied.append({"group": "voltage", "from": key, "to": target})
        else:
            routed[key] = value
    return routed, applied


GLOBAL_VENDOR_KEYS = {
    "manufacturer", "brand", "operating_temperature", "mounting_type",
    "material", "color", "colour", "weight",
}


def split_filter_tiers(
    filters: Optional[Dict[str, Any]],
    catalog_specs: Dict[str, Dict[str, Any]],
    conn: sqlite3.Connection,
    category: Optional[str],
    category_keys: List[str],
    allow_vendor: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Splits caller filters into taxonomy/promoted filters and vendor-tier filters.

    Auto-routing is category-scoped on purpose. An unscoped key like "torque"
    exists on motor shafts and connector screws; silently unioning those would
    produce a confident but semantically wrong answer. Without a category, only
    genuinely cross-category keys (manufacturer, operating temperature, ...) are
    auto-routed; everything else must use the explicit vendor_filters argument.
    """
    promoted: Dict[str, Any] = {}
    vendor: Dict[str, Any] = {}
    routed: List[Dict[str, Any]] = []
    for key, value in (filters or {}).items():
        key = str(key).strip().lower()
        applicable_to_category = (not category) or key in category_keys or bool(set(equivalent_alias_keys(key)) & set(category_keys))
        if key in catalog_specs and applicable_to_category:
            promoted[key] = value
            continue
        vkey = normalize_vendor_key(key)
        if not allow_vendor:
            promoted[key] = value
            continue
        if category is None and key not in catalog_specs and vkey not in GLOBAL_VENDOR_KEYS:
            promoted[key] = value
            continue
        meta = load_vendor_specs(
            conn, category=category, min_count=1, keys=[vkey]
        )
        if meta and next(iter(meta.values()))["count"] > 0:
            vendor[vkey] = value
            routed.append({"from": key, "to": vkey, "tier": "vendor"})
        else:
            promoted[key] = value
    return promoted, vendor, routed


def load_vendor_filter_meta(
    conn: sqlite3.Connection,
    vendor_filters: Dict[str, Any],
    category: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    keys = [normalize_vendor_key(k) for k in (vendor_filters or {})]
    if not keys:
        return {}
    return load_vendor_specs(conn, category=category, min_count=1, keys=keys)


def expand_unscoped_aliases(
    norm_filters: Dict[str, Any],
    catalog_specs: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, List[str]]]:
    """
    Replaces equivalent-key filters with one OR-expansion so `voltage:12` and
    `voltage_v:12` return the same union without double-counting products.
    """
    expanded: Dict[str, Any] = {}
    alias_filters: List[Dict[str, Any]] = []
    disclosed: Dict[str, List[str]] = {}
    for key, value in norm_filters.items():
        expansion = build_alias_expansion(key, value, catalog_specs)
        if not expansion:
            expanded[key] = value
            continue
        group = expansion["group"]
        keys = expansion["keys"]
        existing = next((a for a in alias_filters if a["group"] == group), None)
        if existing is None or existing["value"] != value:
            # Each supplied requirement remains an AND predicate. The OR is
            # over equivalent storage keys, never over different requirements.
            alias_filters.append({"group": group, "keys": keys, "value": value,
                                  "requested_key": key})
        disclosed[group] = keys
    return expanded, alias_filters, disclosed
