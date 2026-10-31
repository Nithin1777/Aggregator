"""Shared, exhaustive field discovery for people and MCP clients.

Discovery counts describe stored evidence, not independently verified products.
The value inspector uses the actual retrieval predicates for usable coverage.
Pagination has a deterministic coverage/key order and never cuts off the long tail.
"""
import base64
import hashlib
import json
import re

from src.spec.filters import load_catalog_specs, resolve_category
from src.spec.vendor_index import load_vendor_specs, normalize_vendor_key
from src.search.filter_plan import FilterPlanError


def category_scope(conn, category):
    if not category:
        return None
    from src.spec.taxonomy_loader import get_taxonomy
    resolved, _ = resolve_category(category, list(get_taxonomy().categories))
    if not resolved:
        raise FilterPlanError({"error_code": "unknown_category", "message": f"Unknown category '{category}'."})
    return resolved


def field_metadata(conn, category=None):
    """Return every populated field, including fields with a single observation."""
    promoted = load_catalog_specs(conn)
    vendor = load_vendor_specs(conn, category=category, min_count=1)
    if category:
        counts = dict(conn.execute(
            "SELECT s.key,COUNT(DISTINCT s.product_id) FROM product_specs s "
            "JOIN products p ON p.id=s.product_id WHERE p.category_canonical=? GROUP BY s.key", (category,),
        ))
        promoted = {key: {**meta, "count": counts[key]} for key, meta in promoted.items() if counts.get(key)}
        vendor = {key: meta for key, meta in vendor.items() if meta["count"]}
    return promoted, vendor


def _cursor(offset, fingerprint):
    return base64.urlsafe_b64encode(json.dumps([offset, fingerprint]).encode()).decode()


def _offset(cursor, fingerprint):
    if not cursor:
        return 0
    try:
        offset, digest = json.loads(base64.urlsafe_b64decode(cursor))
        if type(offset) is not int or offset < 0 or digest != fingerprint:
            raise ValueError()
        return offset
    except Exception:
        raise FilterPlanError({"error_code": "invalid_cursor", "message": "Cursor does not belong to this discovery request."})


def discover_specs(conn, *, category=None, search=None, tier="all", limit=30, cursor=None):
    """Search keys/labels and page through all stored specs without fixed caps."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise FilterPlanError({"error_code": "invalid_arguments", "message": "limit must be between 1 and 100."})
    if tier not in {"all", "promoted", "vendor"}:
        raise FilterPlanError({"error_code": "invalid_arguments", "message": "tier must be all, promoted or vendor."})
    category = category_scope(conn, category)
    search = normalize_vendor_key(search)
    terms = search.split("_") if search else []
    # Page in SQL. A 40,000-key vendor vocabulary must not be loaded and sorted
    # in Python once per page, nor serialized into every MCP overview.
    params = []
    if category:
        promoted_sql = ("SELECT s.key,COUNT(DISTINCT s.product_id) n,'promoted' tier FROM product_specs s "
                        "JOIN products p ON p.id=s.product_id JOIN spec_stats_global g ON g.key=s.key "
                        "WHERE p.category_canonical=? GROUP BY s.key")
        vendor_sql = ("SELECT g.key,s.filled_count n,'vendor' tier FROM vendor_spec_stats_global g "
                      "JOIN vendor_spec_stats s ON s.key=g.key WHERE s.category=? AND s.filled_count>0")
        params += [category, category]
    else:
        promoted_sql = "SELECT key,applicable_count n,'promoted' tier FROM spec_stats_global WHERE applicable_count>0"
        vendor_sql = "SELECT key,applicable_count n,'vendor' tier FROM vendor_spec_stats_global WHERE applicable_count>0"
    selected = ("SELECT * FROM promoted" if tier == "promoted" else "SELECT * FROM vendor" if tier == "vendor" else
                "SELECT * FROM promoted UNION ALL SELECT * FROM vendor v WHERE NOT EXISTS(SELECT 1 FROM promoted p WHERE p.key=v.key)")
    cte = f"WITH promoted AS ({promoted_sql}), vendor AS ({vendor_sql}), fields AS ({selected}), labels AS (SELECT key,MIN(display_name) label FROM spec_definitions GROUP BY key) "
    conditions = []
    for term in terms:
        conditions.append("(lower(f.key) LIKE ? OR lower(COALESCE(l.label,'')) LIKE ?)")
        params += ["%"+term+"%", "%"+term+"%"]
    relation = "FROM fields f LEFT JOIN labels l ON l.key=f.key" + (" WHERE " + " AND ".join(conditions) if conditions else "")
    fingerprint = hashlib.sha256(json.dumps([category, search, tier, limit], sort_keys=True).encode()).hexdigest()[:20]
    offset = _offset(cursor, fingerprint)
    total = conn.execute(cte + "SELECT COUNT(*) " + relation, params).fetchone()[0]
    selected_rows = conn.execute(cte + "SELECT f.key,f.n,f.tier " + relation + " ORDER BY f.n DESC,f.key ASC LIMIT ? OFFSET ?", params+[limit,offset]).fetchall()
    promoted = load_catalog_specs(conn)
    vendor_keys = [row[0] for row in selected_rows if row[2] == "vendor"]
    vendor = load_vendor_specs(conn,category=category,min_count=1,keys=vendor_keys) if vendor_keys else {}
    fields = []
    for key, count, selected in selected_rows:
        source = vendor if selected == "vendor" else promoted
        meta = source[key]
        numeric = meta["type"] in {"float", "integer"}
        from src.spec.filters import enum_is_numeric
        numeric = numeric or enum_is_numeric(meta)
        fields.append({
            "key": key, "display_name": meta.get("display_name", key.replace("_", " ").title()),
            "type": "number" if numeric else "text", "storage_type": meta["type"],
            "unit": meta.get("unit"), "tier": selected,
            "filter_parameter": "vendor_filters" if selected == "vendor" else "filters",
            "operators": ["eq", "in", "gte", "lte", "gt", "lt", "tolerance", "all"] if numeric else ["eq", "in", "all"],
            "operator_notes": {"all": "Every listed requirement must be satisfied by the same field.",
                               **({"tolerance": "Requires eq; fractional tolerance greater than 0 and at most 1."} if numeric else {})},
            "stored_product_count": count, "coverage_scope": "stored_evidence_before_ownership_checks",
            "range": {"min": meta.get("min"), "max": meta.get("max")} if numeric else None,
            "evidence": "vendor_reported" if selected == "vendor" else "extracted_catalog_claim",
        })
    page = fields
    next_offset = offset + len(page)
    return {"fields": page, "total_fields": total, "returned": len(page),
            "category": category, "search": search, "tier": tier,
            "next_cursor": _cursor(next_offset, fingerprint) if next_offset < total else None,
            "has_more": next_offset < total, "coverage_scope": "stored_evidence",
            "values_tool": "spec_values", "values_endpoint": "/api/specs/values",
            "pagination_consistency": "deterministic_for_an_unchanged_catalog"}


def spec_values(conn, key, *, tier="auto", category=None, search=None, limit=30, cursor=None):
    """Usable value distributions, ranges and missingness, using search semantics."""
    from src.spec.introspection import filter_population
    if tier not in {"auto", "vendor", "promoted"} or type(limit) is not int or not 1 <= limit <= 100:
        raise FilterPlanError({"error_code": "invalid_arguments", "message": "Invalid tier or limit."})
    category = category_scope(conn, category)
    key = normalize_vendor_key(key)
    promoted, vendor = field_metadata(conn, category)
    use_vendor = tier == "vendor" or (tier == "auto" and key not in promoted)
    metadata = vendor if use_vendor else promoted
    if key not in metadata:
        raise FilterPlanError({"error_code": "unknown_filter_key", "message": f"No stored field '{key}' in this scope. Use discover_specs."})
    fingerprint = hashlib.sha256(json.dumps([key, tier, category, search, limit]).encode()).hexdigest()[:20]
    offset = _offset(cursor, fingerprint)
    # Select the field in category scope, but retain every global equivalent
    # storage key when compiling predicates, just as search/count do.
    population = filter_population(conn, key, load_catalog_specs(conn), vendor_metadata=vendor,
                                   category=category, vendor=use_vendor,
                                   values_limit=limit, value_offset=offset, value_search=search,
                                   include_numeric_values=True)
    values = sorted((population.pop("values") or {}).items(), key=lambda item: (-item[1], str(item[0])))
    page = values
    after = population.pop('next_value_offset')
    candidate_count = population.pop('value_candidates_count')
    total_values = population.pop('exact_value_count')
    clauses, params = [], []
    if category:
        clauses = ["p.category_canonical=?", "aggregate_identity_matches(p.title,p.category_canonical,?)=1"]
        params = [category, category]
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = conn.execute("SELECT COUNT(*) FROM products p" + where, params).fetchone()[0]
    return {"key": key, "tier": "vendor" if use_vendor else "promoted", "category": category,
            "type": metadata[key]["type"], "unit": metadata[key].get("unit"),
            **population, "scope_products": total, "missing_or_unusable": total-population["coverage_count"],
            "values": [{"value": v, "count": n} for v, n in page], "total_values": total_values,
            "total_value_candidates": candidate_count, "values_count_is_exact": total_values is not None,
            "has_more": after < candidate_count, "next_cursor": _cursor(after, fingerprint) if after < candidate_count else None,
            "pagination_note": "Follow next_cursor until null, including empty pages caused by conflicting alias evidence. Unknown exact totals are null.",
            "filter_parameter": "vendor_filters" if use_vendor else "filters"}
