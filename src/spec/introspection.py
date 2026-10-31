"""Filterable metadata computed using the retrieval predicates, not stored-row counts."""
from collections import Counter
import math
from src.spec.constraints import compile_predicates
from src.spec.filters import build_alias_expansion, canonical_enum_value, parse_and_validate_filters, expand_enum_umbrella


def filter_population(conn, key, metadata, *, vendor_metadata=None, hybrid=True,
                      category=None, include_related=False, vendor=False):
    meta = (vendor_metadata if vendor else metadata)[key]
    expansion = None if vendor else build_alias_expansion(key, {}, metadata)
    predicates = compile_predicates(
        {} if vendor or expansion else {key: {}}, metadata,
        vendor_filters={key: {}} if vendor else {}, vendor_metadata=vendor_metadata,
        hybrid=hybrid, aliases=[expansion] if expansion else [],
    )
    predicate = predicates[0]
    scope, params = [predicate.match_sql], list(predicate.match_params)
    if category:
        scope.append("p.category_canonical = ?")
        params.append(category)
        if not include_related:
            scope.append("aggregate_identity_matches(p.title,p.category_canonical,?) = 1")
            params.append(category)
    base = "SELECT p.id FROM products p WHERE " + " AND ".join(scope)
    count = int(conn.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0])
    keys = expansion["keys"] if expansion else [key]
    placeholders = ",".join("?" for _ in keys)
    tables = ["product_vendor_specs"] if vendor else ["product_specs"] + (["product_vendor_specs"] if hybrid else [])
    numbers, values = [], Counter()
    seen = set()
    for table in tables:
        fallback = ""
        if table == "product_vendor_specs":
            fallback = " AND aggregate_vendor_alias_valid(p.category_canonical,s.key,s.raw_key,s.source)=1"
            if not vendor:
                fallback += " AND aggregate_vendor_fallback_key_applies(p.category_canonical,s.key)=1"
                fallback += " AND NOT EXISTS (SELECT 1 FROM product_specs a WHERE a.product_id=s.product_id AND a.key=s.key)"
        rows = conn.execute(
            f"SELECT s.product_id,s.key,s.value,s.value_num FROM {table} s JOIN products p ON p.id=s.product_id "
            f"WHERE s.key IN ({placeholders}) AND s.product_id IN ({base}) "
            "AND aggregate_spec_subject(p.title,p.category_canonical,s.key)='sold_item'" + fallback,
            keys + params,
        )
        for pid, storage_key, value, number in rows:
            if meta["type"] in {"integer", "float"} and number is not None:
                value = number
            canonical = canonical_enum_value(value, {"key": key})
            if (pid, canonical) not in seen:
                values[canonical] += 1
                seen.add((pid, canonical))
            try:
                number = float(number if number is not None else value)
                if math.isfinite(number):
                    numbers.append(number)
            except (ValueError, TypeError):
                pass
    # Equality distributions must use the same evidence conflict policy as
    # retrieval. Only bounded top values are published; never invented totals.
    matched_values = {}
    if meta["type"] in {"enum", "string"} or vendor:
        for value, _ in values.most_common(30):
            if not expansion and not expand_enum_umbrella(key, value, None if vendor else category):
                # The rows above already satisfy this single-key predicate's
                # ownership, precedence and usability checks. Each listing has
                # one authoritative value. Grouping them is exact, and avoids
                # rescanning a vendor population once per advertised value.
                matched_values[value] = values[value]
                continue
            applied = float(value) if meta["type"] in {"integer", "float"} else value
            normalized, error = parse_and_validate_filters({key: applied}, {key: meta}, category=None if vendor else category)
            if error:
                continue
            applied = normalized[key]
            alias = None if vendor else build_alias_expansion(key, applied, metadata)
            p = compile_predicates(
                {} if vendor or alias else {key: applied}, metadata,
                vendor_filters={key: applied} if vendor else {}, vendor_metadata=vendor_metadata,
                hybrid=hybrid, aliases=[alias] if alias else [],
            )[0]
            # base already contains the category and identity scope.
            n = int(conn.execute(f"SELECT COUNT(*) FROM products p WHERE p.id IN ({base}) AND {p.match_sql}",
                                 params + list(p.match_params)).fetchone()[0])
            if n:
                matched_values[value] = n
    return {"coverage_count": count, "values": matched_values or None,
            "range": {"min": min(numbers), "max": max(numbers)} if numbers and meta["type"] in {"integer", "float"} else None,
            "sample_raw_strings": list(matched_values or values)[:5],
            "population": {"measure": "usable_sold_item_evidence", "category": category,
                           "include_related": include_related, "include_vendor_specs": hybrid and not vendor,
                           "tier": "vendor" if vendor else "hybrid" if hybrid else "promoted",
                           "values_limit": 30}}
