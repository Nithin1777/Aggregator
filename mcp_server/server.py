"""
Spec-Driven MCP Query Server for Electronic Components (Agent-First).
Provides generic discovery, parametric introspection, counting, single-product lookup,
and targeted search with uniform response/error envelopes and operator syntax.
"""
import base64
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp.server.fastmcp import FastMCP
import db
from models import UnifiedProduct
from search_service import AggregatorEngine

logger = logging.getLogger(__name__)

SERVER_VERSION = "1.5.0"
SCHEMA_VERSION = "1.5.0"

# Hard ceiling on page size. An agent asking for 1000 rows is almost always
# paginating by accident, and a negative limit becomes SQL `LIMIT -1` (= no limit).
MAX_PAGE_SIZE = 50

# Initialize FastMCP Server
mcp = FastMCP("octopart-india-agent-first")
_engine = AggregatorEngine()

class ToolResponseEnvelope(BaseModel):
    """Uniform MCP tool response envelope adhering to Agent-First DoD."""
    ok: bool = Field(..., description="Success indicator: true if request succeeded, false on error")
    data: Optional[Dict[str, Any]] = Field(None, description="Tool-specific response payload")
    total_matches: Optional[int] = Field(None, description="Total matching items before pagination")
    returned: Optional[int] = Field(None, description="Number of items returned in this page")
    truncated: Optional[bool] = Field(None, description="True if more results exist beyond this page")
    next_cursor: Optional[str] = Field(None, description="Opaque pagination cursor for next page")
    filters_applied: Optional[Dict[str, Any]] = Field(None, description="Normalized echo of applied filters")
    coverage: Optional[Dict[str, Any]] = Field(None, description="Catalog coverage stats per filter key used")
    warnings: Optional[List[Dict[str, Any]]] = Field(None, description="Advisory warnings and diagnostic notices")
    server_version: str = Field(default=SERVER_VERSION, description="Server version")
    schema_version: Optional[str] = Field(default=SCHEMA_VERSION, description="Schema version")
    error_code: Optional[str] = Field(None, description="Standard machine-readable error code")
    message: Optional[str] = Field(None, description="Human/agent-readable error description")
    valid_keys: Optional[List[str]] = Field(None, description="List of valid queryable spec keys")
    suggestions: Optional[List[Dict[str, Any]]] = Field(None, description="Suggested keys, values, or operator fixes")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional context or debugging info")

def get_db() -> sqlite3.Connection:
    return db.get_db_connection()

def make_response(
    data: Any,
    total_matches: int = 0,
    returned: int = 0,
    truncated: bool = False,
    next_cursor: Optional[str] = None,
    filters_applied: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
) -> ToolResponseEnvelope:
    """Builds a successful uniform response envelope."""
    payload = {
        "ok": True,
        "data": data,
        "total_matches": total_matches,
        "returned": returned,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "filters_applied": filters_applied or {},
        "coverage": coverage or {},
        "warnings": warnings or [],
        "server_version": SERVER_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    return ToolResponseEnvelope(**payload)

def make_error(
    error_code: str,
    message: str,
    valid_keys: Optional[List[str]] = None,
    suggestions: Optional[List[Dict[str, Any]]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> ToolResponseEnvelope:
    """Builds a structured error envelope without throwing bare ValueErrors."""
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "valid_keys": valid_keys,
        "suggestions": suggestions or [],
        "details": details or {},
        "server_version": SERVER_VERSION,
    }
    return ToolResponseEnvelope(**payload)

def _load_catalog_specs(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Loads all advertised spec key metadata from spec_stats_global."""
    cur = conn.cursor()
    cur.execute("""
        SELECT g.key, g.value_type, sd.unit, g.applicable_count, sd.display_name, sd.enum_values, g.value_min, g.value_max, g.value_counts
        FROM spec_stats_global g
        LEFT JOIN (
            SELECT key, unit, display_name, enum_values FROM spec_definitions GROUP BY key
        ) sd ON g.key = sd.key
        WHERE g.applicable_count >= 50
        ORDER BY g.applicable_count DESC;
    """)
    specs = {}
    for r in cur.fetchall():
        specs[r[0]] = {
            "key": r[0],
            "type": r[1],
            "unit": r[2],
            "count": r[3],
            "display_name": r[4] or r[0].replace("_", " ").title(),
            "enum_values": json.loads(r[5]) if r[5] else None,
            "min": r[6],
            "max": r[7],
            "value_counts": json.loads(r[8]) if r[8] else {}
        }
    return specs

def _enum_is_numeric(spec_meta: Dict[str, Any]) -> bool:
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


def _filter_column(alias: str, spec_meta: Dict[str, Any], ordering: bool = False) -> str:
    """
    SQL expression to compare against for a filter key.

    Numeric-typed keys use the indexed value_num column. Numeric-valued enums have
    NULL value_num, so ordering comparisons must CAST the text value instead of
    comparing NULL (which matches zero rows and looks like 'no such part').
    """
    if spec_meta.get("type") in ("integer", "float"):
        return f"{alias}.value_num"
    if ordering and _enum_is_numeric(spec_meta):
        return f"CAST({alias}.value AS REAL)"
    return f"{alias}.value"


def _query_fingerprint(
    norm_filters: Optional[Dict[str, Any]],
    query: Optional[str],
    price_max: Optional[float],
    in_stock_only: bool,
    sort: str,
) -> str:
    """Stable hash binding a pagination cursor to the exact query that produced it."""
    payload = json.dumps(
        {
            "filters": norm_filters or {},
            "query": (query or "").strip(),
            "price_max": price_max,
            "in_stock_only": bool(in_stock_only),
            "sort": sort,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]


def _nearest_keys(bad_key: str, valid_keys: List[str], limit: int = 5) -> List[Dict[str, Any]]:
    """Suggest the closest valid spec keys instead of dumping the whole list."""
    close = difflib.get_close_matches(bad_key, valid_keys, n=limit, cutoff=0.4)
    if not close:
        close = valid_keys[:limit]
    return [{"key": k} for k in close]


def _enum_suggestions(key: str, spec_meta: Dict[str, Any], requested: Any, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Rank candidate values for an unmatched enum filter.

    Numeric enums are ranked by distance to the requested value (asking for 99
    should surface 48/36, not the five most populous voltages); categorical
    enums are ranked by population.
    """
    counts = spec_meta.get("value_counts") or {}
    if not counts:
        return []
    items = list(counts.items())

    def distance(item: Tuple[str, int]) -> float:
        try:
            return abs(float(str(item[0]).strip()) - float(str(requested).strip()))
        except (TypeError, ValueError):
            return float("inf")

    if _enum_is_numeric(spec_meta):
        items.sort(key=distance)
    else:
        items.sort(key=lambda item: -item[1])

    return [{"key": key, "value": v, "count": c} for v, c in items[:limit]]


def _build_coverage_and_warnings(
    catalog_specs: Dict[str, Dict[str, Any]],
    total_products: int,
    norm_filters: Dict[str, Any],
    total_matches: int,
    result_spec_counts: Optional[Dict[str, Dict[str, int]]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Coverage for each filter key, plus warnings that are actually actionable.

    Catalog-wide coverage is reported as data, not as a warning: on a filtered
    query the coverage of the filtered key inside the result set is 100% by
    construction, so a blanket 'low coverage' warning fired on every call and
    trained the caller to ignore the warnings channel entirely.
    """
    coverage: Dict[str, Any] = {}
    warnings: List[Dict[str, Any]] = []

    for fk in norm_filters:
        meta = catalog_specs[fk]
        present = meta["count"]
        pct = round(present / max(total_products, 1) * 100, 2)
        entry: Dict[str, Any] = {"present": present, "of": total_products, "pct": pct}
        if result_spec_counts is not None:
            rp = result_spec_counts.get(fk, 0)
            entry["result_present"] = rp
            entry["result_of"] = total_matches
            entry["result_pct"] = round(rp / max(total_matches, 1) * 100, 2) if total_matches else 0.0
        coverage[fk] = entry

        # Sparse extraction only distorts the answer when the result set is small
        # enough that a coverage gap could be mistaken for "no such part exists".
        if pct < 1.0 and 0 < total_matches < 25:
            warnings.append({
                "code": "filter_key_sparse",
                "key": fk,
                "catalog_pct": pct,
                "message": (
                    f"'{fk}' is extracted on only {pct}% of the catalog ({present} of {total_products}) "
                    f"and this query returned just {total_matches} matches. A thin result set may reflect "
                    f"missing extraction rather than a genuine catalog gap."
                ),
            })

        if fk == "current_a":
            warnings.append({
                "code": "low_precision_advisory",
                "key": fk,
                "message": "Filter key 'current_a' is advisory only (~70% precision in catalog titles).",
            })

    return coverage, warnings


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
            return int(s) if v_type == "integer" else float(s)
        except ValueError:
            pass

    return s.lower()

def _parse_and_validate_filters(
    filters: Optional[Dict[str, Any]],
    advertised_specs: Dict[str, Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[ToolResponseEnvelope]]:
    """
    Validates filter keys and operators, rejecting deprecated string range forms.
    Returns (normalized_filters, error_envelope_if_any).
    """
    clean_filters = filters or {}
    all_valid_keys = sorted(advertised_specs.keys())
    normalized: Dict[str, Any] = {}

    for f_key, f_val in clean_filters.items():
        if f_key not in advertised_specs:
            return None, make_error(
                error_code="unknown_filter_key",
                message=f"Unknown filter key '{f_key}'.",
                valid_keys=all_valid_keys,
                suggestions=_nearest_keys(f_key, all_valid_keys),
                details={"provided_key": f_key}
            )

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
                    return None, make_error(
                        error_code="unsupported_operator",
                        message=f"String range syntax '{f_val}' for '{f_key}' is deprecated. Use operator form: {{'{f_key}': {{'gte': {p1}, 'lte': {p2}}}}}",
                        suggestions=[{"key": f_key, "value": {"gte": p1, "lte": p2}}],
                        details={"deprecated_input": f_val}
                    )
                except ValueError:
                    pass

        # Operator dictionary
        if isinstance(f_val, dict):
            allowed_ops = {"in", "gte", "lte", "gt", "lt", "eq", "tolerance"}
            unknown_ops = set(f_val.keys()) - allowed_ops
            if unknown_ops:
                return None, make_error(
                    error_code="unsupported_operator",
                    message=f"Unsupported operator(s) {list(unknown_ops)} for filter '{f_key}'. Allowed operators: {sorted(list(allowed_ops))}",
                    suggestions=[{"operator": "in"}, {"operator": "gte"}, {"operator": "lte"}, {"operator": "eq"}]
                )

            # Ordering / tolerance only make sense on values that can be ordered.
            ordering_ops = {"gte", "lte", "gt", "lt", "tolerance"} & set(f_val.keys())
            if ordering_ops and not _enum_is_numeric(spec_meta):
                sample_vals = list((spec_meta.get("value_counts") or {}).keys())[:8]
                return None, make_error(
                    error_code="operator_not_supported_for_type",
                    message=(
                        f"Operator(s) {sorted(ordering_ops)} cannot be applied to '{f_key}' because it is a "
                        f"categorical enum with non-numeric values. Use 'eq' or 'in' instead."
                    ),
                    suggestions=[{"key": f_key, "operator": "in", "values": sample_vals}],
                    details={"key": f_key, "value_type": v_type, "sample_values": sample_vals}
                )

            if any(f_val.get(op) is None for op in ordering_ops | {"eq"} & set(f_val.keys())):
                return None, make_error(
                    error_code="invalid_filter_value",
                    message=f"Filter '{f_key}' has a null value for one of its operators.",
                    details={"provided_value": f_val}
                )

            norm_op_dict: Dict[str, Any] = {}
            if "in" in f_val:
                in_list = f_val["in"]
                if not isinstance(in_list, (list, tuple)):
                    return None, make_error(
                        error_code="invalid_filter_value",
                        message=f"Operator 'in' for filter '{f_key}' requires a list of values.",
                        details={"provided_value": in_list}
                    )
                norm_op_dict["in"] = [normalize_scalar(f_key, v, v_type, unit) for v in in_list]

            for bound in ("gte", "lte", "gt", "lt"):
                if bound in f_val:
                    norm_op_dict[bound] = normalize_scalar(f_key, f_val[bound], v_type, unit)

            if "eq" in f_val:
                norm_op_dict["eq"] = normalize_scalar(f_key, f_val["eq"], v_type, unit)

            if "tolerance" in f_val:
                try:
                    norm_op_dict["tolerance"] = float(f_val["tolerance"])
                except (ValueError, TypeError):
                    return None, make_error(
                        error_code="invalid_filter_value",
                        message=f"Tolerance value for '{f_key}' must be a float multiplier (e.g. 0.1 for +/-10%)."
                    )

            normalized[f_key] = norm_op_dict
        else:
            # Primitive scalar equality
            normalized[f_key] = normalize_scalar(f_key, f_val, v_type, unit)

    return normalized, None

def _build_sql_query(
    normalized_filters: Dict[str, Any],
    advertised_specs: Dict[str, Dict[str, Any]],
    query: Optional[str] = None,
    price_max: Optional[float] = None,
    in_stock_only: bool = False,
    sort: str = "relevance",
    limit: int = 10,
    offset: int = 0
) -> Tuple[str, List[Any], str, List[Any]]:
    """Builds parameterized SQL for count and select queries."""
    joins: List[str] = []
    join_params: List[Any] = []
    where_clauses: List[str] = []
    where_params: List[Any] = []

    for i, (f_key, f_val) in enumerate(normalized_filters.items()):
        alias = f"ps_{i}"
        spec_meta = advertised_specs[f_key]
        v_type = spec_meta["type"]

        joins.append(f"JOIN product_specs {alias} ON p.id = {alias}.product_id AND {alias}.key = ?")
        join_params.append(f_key)

        col = _filter_column(alias, spec_meta)
        num_col = _filter_column(alias, spec_meta, ordering=True)

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

    if price_max is not None:
        where_clauses.append("p.price <= ?")
        where_params.append(float(price_max))

    if in_stock_only:
        where_clauses.append("p.available = 1")

    if query and query.strip():
        clean_q = re.sub(r'["\*\(\)\:]', '', query.strip()).strip()
        tokens = clean_q.split()
        if tokens:
            fts_expr = " ".join([f'"{t}"*' for t in tokens])
            joins.append("JOIN products_fts fts ON p.id = fts.rowid")
            where_clauses.append("products_fts MATCH ?")
            where_params.append(fts_expr)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # Sort
    if sort == "price_asc":
        order_by = "ORDER BY p.price ASC"
    elif sort == "price_desc":
        order_by = "ORDER BY p.price DESC"
    elif query and query.strip():
        order_by = "ORDER BY rank"
    else:
        order_by = "ORDER BY p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"

    count_sql = f"SELECT COUNT(DISTINCT p.id) FROM products p {' '.join(joins)} {where_sql};"
    select_sql = f"SELECT p.* FROM products p {' '.join(joins)} {where_sql} {order_by} LIMIT ? OFFSET ?;"

    params = join_params + where_params
    select_params = params + [limit, offset]
    return count_sql, params, select_sql, select_params

# =====================================================================
# 6 AGENT-FIRST TOOLS
# =====================================================================

@mcp.tool()
def describe_catalog() -> ToolResponseEnvelope:
    """
    Return comprehensive catalog-level metadata, total product counts, vendor distributions, category counts, queryable spec keys with coverage percentages, and data freshness timestamps.

    Recommended Call Order: Call describe_catalog first before running any searches to inspect available attributes and catalog size.

    Parameter Syntax: Takes no arguments.

    Example Invocation: describe_catalog()

    Behavior When No Matches: Always returns ok:true with complete catalog statistics.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        cur.execute("SELECT vendor, COUNT(1) FROM products GROUP BY vendor ORDER BY COUNT(1) DESC;")
        products_by_vendor = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute("SELECT coalesce(spec_category, 'unclassified'), COUNT(1) FROM products GROUP BY spec_category ORDER BY COUNT(1) DESC;")
        products_by_spec_category = {r[0]: r[1] for r in cur.fetchall()}

        catalog_specs = _load_catalog_specs(conn)
        spec_keys_list = []
        for k, s in catalog_specs.items():
            cov_pct = round(s["count"] / max(total_products, 1) * 100, 2)
            spec_keys_list.append({
                "key": k,
                "display_name": s["display_name"],
                "type": s["type"],
                "unit": s["unit"],
                "coverage_pct": cov_pct,
                "count": s["count"],
                "values": s["enum_values"] if s["type"] == "enum" else None,
                "range": {"min": s["min"], "max": s["max"]} if s["type"] in ("integer", "float") else None,
                "confidence": "low" if k == "current_a" else "high",
                "precision_note": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)" if k == "current_a" else None
            })

        cur.execute("SELECT MIN(updated_at), MAX(updated_at) FROM products;")
        oldest_crawl, newest_crawl = cur.fetchone()

        cur.execute("SELECT vendor, MIN(updated_at), MAX(updated_at) FROM products GROUP BY vendor;")
        per_vendor_freshness = {r[0]: {"oldest": r[1], "newest": r[2]} for r in cur.fetchall()}

        data = {
            "total_products": total_products,
            "counts": {
                "products": total_products,
                "spec_rows": cur.execute("SELECT COUNT(1) FROM product_specs;").fetchone()[0],
                "spec_keys_advertised": len(spec_keys_list),
            },
            "products_by_vendor": products_by_vendor,
            "products_by_spec_category": products_by_spec_category,
            "spec_keys": spec_keys_list,
            "currency": "INR",
            "freshness": {
                "oldest_crawl": oldest_crawl,
                "newest_crawl": newest_crawl,
                "per_vendor": per_vendor_freshness
            }
        }
        # `returned` counts payload items in this response (1 document), not products;
        # `total_matches` is the catalog size. Conflating them corrupted any aggregate.
        return make_response(data=data, total_matches=total_products, returned=1)
    finally:
        conn.close()

@mcp.tool()
def describe_spec(key: str) -> ToolResponseEnvelope:
    """
    Return detailed metadata, value distributions, ranges, and coverage diagnostics for a single spec attribute.

    Recommended Call Order: Call after describe_catalog to introspect a specific spec key before building filters.

    Parameter Syntax: key: str (e.g. "rpm", "voltage", "cells", "capacity_mah").

    Example Invocation: describe_spec(key="rpm")

    Behavior When No Matches: If key is not in the taxonomy, returns ok:false with error_code='unknown_filter_key' and a list of valid keys.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        catalog_specs = _load_catalog_specs(conn)
        if key not in catalog_specs:
            all_valid_keys = sorted(catalog_specs.keys())
            return make_error(
                error_code="unknown_filter_key",
                message=f"Spec key '{key}' is not in the catalog taxonomy.",
                valid_keys=all_valid_keys,
                suggestions=_nearest_keys(key, all_valid_keys)
            )

        spec = catalog_specs[key]
        cov_pct = round(spec["count"] / max(total_products, 1) * 100, 2)
        status = "key_extracted_low_coverage" if cov_pct < 1.0 else "key_healthy"

        cur.execute("SELECT DISTINCT value FROM product_specs WHERE key = ? LIMIT 5;", (key,))
        sample_raw = [r[0] for r in cur.fetchall()]

        warnings = []
        if key == "current_a":
            warnings.append({
                "code": "low_precision_advisory",
                "message": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)."
            })
        if status == "key_extracted_low_coverage":
            warnings.append({
                "code": "low_coverage_warning",
                "message": f"Attribute has low catalog coverage ({cov_pct}% of total products)."
            })

        data = {
            "key": key,
            "status": status,
            "display_name": spec["display_name"],
            "type": spec["type"],
            "unit": spec["unit"],
            "coverage_pct": cov_pct,
            "coverage_count": spec["count"],
            "values": spec["value_counts"] if spec["type"] == "enum" else None,
            "range": {"min": spec["min"], "max": spec["max"]} if spec["type"] in ("integer", "float") else None,
            "sample_raw_strings": sample_raw
        }
        return make_response(data=data, total_matches=spec["count"], returned=1, warnings=warnings)
    finally:
        conn.close()

@mcp.tool()
def count_components(
    filters: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    price_max: Optional[float] = None,
    in_stock_only: bool = False
) -> ToolResponseEnvelope:
    """
    Count matching components and inspect filter coverage without fetching product rows.

    Recommended Call Order: Call after describe_catalog and before search_components to size query results and verify filter coverage.

    Parameter Syntax:
      - filters: dict (supports scalar equality or operators in, gte, lte, gt, lt, eq, tolerance, default: null)
      - query: str (FTS text search string, default: null)
      - price_max: float (maximum price in INR, default: null)
      - in_stock_only: bool (default: false)

    Example Invocation: count_components(filters={"voltage": "12", "rpm": {"gte": 190, "lte": 210}}, query="motor")

    Behavior When No Matches: Returns ok:true with total_matches:0, data: {"count": 0}, and suggestions if an enum value was unmatched.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        catalog_specs = _load_catalog_specs(conn)
        norm_filters, err = _parse_and_validate_filters(filters, catalog_specs)
        if err:
            return err

        if query is not None and not str(query).strip():
            return make_error(
                error_code="empty_query",
                message=(
                    "query was provided but is empty/whitespace. Omit it to count without a "
                    "full-text filter, or pass actual search terms."
                ),
                suggestions=[{"query": "motor"}],
            )

        count_sql, params, _, _ = _build_sql_query(
            norm_filters or {}, catalog_specs, query=query, price_max=price_max, in_stock_only=in_stock_only
        )
        cur.execute(count_sql, params)
        total_count = cur.fetchone()[0]

        coverage, warnings = _build_coverage_and_warnings(
            catalog_specs, total_products, norm_filters or {}, total_count
        )

        suggestions: List[Dict[str, Any]] = []
        if total_count == 0 and norm_filters:
            for fk in norm_filters:
                if catalog_specs[fk]["type"] == "enum":
                    requested = norm_filters[fk]
                    if isinstance(requested, dict):
                        requested = requested.get("eq", requested.get("in"))
                    suggestions = _enum_suggestions(fk, catalog_specs[fk], requested)
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter matched 0 catalog rows; see data.suggestions for the closest populated values."
                })

        return make_response(
            data={"count": total_count, "suggestions": suggestions},
            total_matches=total_count,
            returned=0,
            filters_applied={
                "filters": norm_filters or {},
                "query": (query or "").strip() or None,
                "price_max": price_max,
                "in_stock_only": bool(in_stock_only),
            },
            coverage=coverage,
            warnings=warnings
        )
    finally:
        conn.close()

@mcp.tool()
def get_product(product_id: int) -> ToolResponseEnvelope:
    """
    Retrieve the full record for an individual component, including extracted specs, multi-vendor offers, crawl metadata, and extraction confidence.

    Recommended Call Order: Call after search_components to inspect the full detail page and all vendor pricing for a specific product.

    Parameter Syntax: product_id: int (positive integer product ID from search results).

    Example Invocation: get_product(product_id=4395)

    Behavior When No Matches: Returns ok:false with error_code='product_not_found' if the product ID does not exist in the catalog.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE id = ?;", (product_id,))
        col_names = [d[0] for d in cur.description]
        row = cur.fetchone()
        if not row:
            return make_error(
                error_code="product_not_found",
                message=f"Product with id {product_id} not found in catalog.",
                details={"product_id": product_id}
            )

        p = dict(zip(col_names, row))

        # Extracted specs
        cur.execute("""
            SELECT key, value, value_num, value_type, confidence, evidence, source
            FROM product_specs
            WHERE product_id = ?;
        """, (product_id,))
        extracted_specs = {}
        spec_extraction_method = {}
        for r in cur.fetchall():
            k = r[0]
            extracted_specs[k] = {
                "value": r[1],
                "value_num": r[2],
                "type": r[3],
                "confidence": r[4],
                "evidence": r[5]
            }
            spec_extraction_method[k] = "regex" if r[6] == "rule" else r[6]

        # Multi-vendor offers for sibling/matching components
        cur.execute("""
            SELECT id, vendor, vendor_name, title, price, available, product_url, sku, mpn
            FROM products
            WHERE (canonical_for IS NOT NULL AND canonical_for = ?)
               OR (mpn IS NOT NULL AND mpn != '' AND mpn = ?)
               OR id = ?
            ORDER BY available DESC, price ASC;
        """, (p.get("canonical_for"), p.get("mpn"), product_id))
        offer_rows = cur.fetchall()

        offers = [
            {
                "product_id": o[0],
                "vendor": o[2] or o[1],
                "title": o[3],
                "price": float(o[4]),
                "available": bool(o[5]),
                "url": o[6],
                "vendor_sku": o[7],
                "mpn": o[8]
            }
            for o in offer_rows
        ]

        raw_meta = json.loads(p["raw_json"]) if p.get("raw_json") else {}

        catalog_keys = sorted(_load_catalog_specs(conn).keys())
        specs_not_extracted = [k for k in catalog_keys if k not in extracted_specs]

        if raw_meta.get("body"):
            description, description_source = raw_meta["body"], "vendor_page"
        elif raw_meta.get("short_description"):
            description, description_source = raw_meta["short_description"], "vendor_page_short"
        else:
            # Never present the title as if it were a description.
            description, description_source = p.get("title"), "title_fallback"

        data = {
            "product_id": p["id"],
            "title": p["title"],
            "normalized_title": p.get("normalized_title"),
            "mpn": p.get("mpn"),
            "sku": p.get("sku"),
            "price": float(p["price"]),
            "currency": "INR",
            "available": bool(p["available"]),
            "product_url": p["product_url"],
            "image_url": p.get("image_url"),
            "datasheet_url": raw_meta.get("datasheet_url"),
            "description": description,
            "description_source": description_source,
            "raw_attributes": raw_meta,
            "crawl_timestamps": {
                "when_scraped": p.get("updated_at") or p.get("created_at"),
                "when_specs_extracted": None,
                "note": (
                    "Per-spec extraction timestamps are not tracked yet, so this is null "
                    "rather than a fabricated date."
                )
            },
            "extracted_specs": extracted_specs,
            "specs_not_extracted": specs_not_extracted,
            "spec_extraction_method": spec_extraction_method,
            "vendor_offers": offers
        }
        return make_response(data=data, total_matches=1, returned=1)
    finally:
        conn.close()

@mcp.tool()
def search_components(
    filters: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    price_max: Optional[float] = None,
    in_stock_only: bool = False,
    sort: str = "relevance",
    limit: int = 10,
    cursor: Optional[str] = None
) -> ToolResponseEnvelope:
    """
    Search electronic components by parametric spec filters and/or full-text query with side-by-side vendor offers and cursor pagination.

    Recommended Call Order: Call after sizing the query with count_components to retrieve paginated component comparison cards.

    Parameter Syntax:
      - filters: dict (supports scalar equality or operators in, gte, lte, gt, lt, eq, tolerance, default: null)
      - query: str (FTS query string, default: null)
      - price_max: float (max price in INR, default: null)
      - in_stock_only: bool (default: false; listings flag out-of-stock rather than silently omitting)
      - sort: str (one of 'relevance', 'price_asc', 'price_desc', default: 'relevance')
      - limit: int (page size, 1 to 50 inclusive; values >50 are clamped with a warning, <1 is rejected, default: 10)
      - cursor: str (opaque cursor from previous page's next_cursor; bound to the query, default: null)

    Rows Are Listings: each result is one vendor listing, so the same physical part may appear
    once per vendor. Compare on mpn/specs instead of assuming one row per part.

    Spec Fields: 'specs' contains only extracted keys. Keys that were never extracted are named
    in 'specs_not_extracted' (unknown is never reported as null). 'low_confidence_specs' lists
    keys whose extraction confidence is below 1.0.

    Example Invocation: search_components(filters={"voltage": "12", "rpm": {"gte": 190, "lte": 210}}, query="motor", in_stock_only=false, sort="relevance", limit=10)

    Behavior When No Matches: Returns ok:true with total_matches:0, data: {"results": []}, and rank-ordered suggestions if an enum value was unmatched — never an error.
    Errors: unknown_filter_key, unsupported_operator, operator_not_supported_for_type,
    invalid_filter_value, empty_query, invalid_cursor, stale_cursor.
    """
    if sort not in ("relevance", "price_asc", "price_desc"):
        return make_error(
            error_code="invalid_filter_value",
            message=f"Unknown sort '{sort}'. Valid options: ['relevance', 'price_asc', 'price_desc'].",
            suggestions=[{"sort": "relevance"}, {"sort": "price_asc"}, {"sort": "price_desc"}]
        )

    # --- argument validation: fail loudly instead of silently changing intent ---
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        return make_error(
            error_code="invalid_filter_value",
            message=f"limit must be an integer (got {limit!r}).",
            suggestions=[{"limit": 10}]
        )
    if limit_int < 1:
        return make_error(
            error_code="invalid_filter_value",
            message=(
                f"limit must be >= 1 (got {limit_int}). A negative limit becomes SQL 'LIMIT -1', "
                f"which means 'no limit' and would return the whole result set."
            ),
            suggestions=[{"limit": 10}]
        )

    warnings: List[Dict[str, Any]] = []
    effective_limit = min(limit_int, MAX_PAGE_SIZE)
    if effective_limit != limit_int:
        warnings.append({
            "code": "limit_clamped",
            "message": f"limit {limit_int} exceeds the maximum page size {MAX_PAGE_SIZE}; at most {effective_limit} rows are returned per page.",
            "requested": limit_int,
            "effective": effective_limit,
        })

    if query is not None and not str(query).strip():
        return make_error(
            error_code="empty_query",
            message=(
                "query was provided but is empty/whitespace. Omitting it matches the whole catalog; "
                "pass actual search terms, or leave query out entirely."
            ),
            suggestions=[{"query": "motor"}]
        )

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        catalog_specs = _load_catalog_specs(conn)
        all_catalog_keys = sorted(catalog_specs.keys())

        norm_filters, err = _parse_and_validate_filters(filters, catalog_specs)
        if err:
            return err

        # --- cursor is bound to the query that produced it ---
        fingerprint = _query_fingerprint(norm_filters, query, price_max, in_stock_only, sort)
        offset = 0
        if cursor:
            try:
                raw_cursor = base64.b64decode(str(cursor).encode("ascii")).decode("utf-8")
                offset_part, _, cursor_fp = raw_cursor.partition(":")
                offset = int(offset_part)
                if not cursor_fp:
                    raise ValueError("cursor is missing its query fingerprint")
            except Exception:
                return make_error(
                    error_code="invalid_cursor",
                    message="The pagination cursor could not be decoded.",
                    suggestions=[{"cursor": None}],
                    details={"hint": "Omit cursor to start from the first page."}
                )
            if cursor_fp != fingerprint:
                return make_error(
                    error_code="stale_cursor",
                    message=(
                        "This cursor belongs to a different query: filters, query, price_max, "
                        "in_stock_only or sort changed since it was issued."
                    ),
                    suggestions=[{"cursor": None}],
                    details={"hint": "Drop the cursor and restart pagination with the new arguments."}
                )
            if offset < 0:
                offset = 0

        count_sql, count_params, select_sql, select_params = _build_sql_query(
            norm_filters or {},
            catalog_specs,
            query=query,
            price_max=price_max,
            in_stock_only=in_stock_only,
            sort=sort,
            limit=effective_limit,
            offset=offset
        )

        cur.execute(count_sql, count_params)
        total_matches = cur.fetchone()[0]

        coverage, cov_warnings = _build_coverage_and_warnings(
            catalog_specs, total_products, norm_filters or {}, total_matches
        )
        warnings.extend(cov_warnings)

        filters_applied = {
            "filters": norm_filters or {},
            "query": (query or "").strip() or None,
            "price_max": price_max,
            "in_stock_only": bool(in_stock_only),
            "sort": sort,
            "limit": effective_limit,
            "offset": offset,
        }

        if total_matches == 0:
            suggestions: List[Dict[str, Any]] = []
            for fk in (norm_filters or {}):
                if catalog_specs[fk]["type"] == "enum":
                    requested = norm_filters[fk]
                    if isinstance(requested, dict):
                        requested = requested.get("eq", requested.get("in"))
                    suggestions.extend(_enum_suggestions(fk, catalog_specs[fk], requested))
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter value matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            return make_response(
                data={"results": [], "suggestions": suggestions},
                total_matches=0,
                returned=0,
                truncated=False,
                next_cursor=None,
                filters_applied=filters_applied,
                coverage=coverage,
                warnings=warnings
            )

        cur.execute(select_sql, select_params)
        rows = cur.fetchall()

        ids = [r["id"] for r in rows]
        placeholders = ", ".join(["?"] * len(ids))
        result_spec_counts: Dict[str, int] = {}
        cur.execute(
            f"SELECT key, COUNT(1) FROM product_specs WHERE product_id IN ({placeholders}) GROUP BY key;",
            ids
        )
        for spec_key, spec_count in cur.fetchall():
            result_spec_counts[spec_key] = spec_count

        # Spec presence across the returned page: tells the caller whether a field
        # is genuinely absent from the part or simply never extracted for it.
        result_coverage = {
            k: {
                "present": result_spec_counts.get(k, 0),
                "of": len(ids),
                "pct": round(result_spec_counts.get(k, 0) / max(len(ids), 1) * 100, 1),
            }
            for k in all_catalog_keys
        }
        for fk, entry in coverage.items():
            entry["result_present"] = result_spec_counts.get(fk, 0)
            entry["result_of"] = total_matches
            entry["result_pct"] = round(result_spec_counts.get(fk, 0) / max(total_matches, 1) * 100, 2) if total_matches else 0.0

        results = []
        for r in rows:
            pid = r["id"]
            cur.execute(
                "SELECT key, value, confidence FROM product_specs WHERE product_id = ?;", (pid,)
            )
            specs: Dict[str, Any] = {}
            low_confidence_specs: List[str] = []
            for spec_key, spec_value, conf in cur.fetchall():
                specs[spec_key] = spec_value
                try:
                    if conf is not None and float(conf) < 1.0:
                        low_confidence_specs.append(spec_key)
                except (TypeError, ValueError):
                    pass

            # unknown != absent != null: missing keys are enumerated, never filled with null
            specs_not_extracted = [k for k in all_catalog_keys if k not in specs]

            stock_status = "in_stock" if r["available"] else "out_of_stock"
            results.append({
                "product_id": pid,
                "title": r["title"],
                "vendor": r["vendor_name"] or r["vendor"],
                "price": float(r["price"]),
                "currency": "INR",
                "available": bool(r["available"]),
                "stock_status": stock_status,
                "category": r["category"],
                "mpn": r["mpn"],
                "vendor_sku": r["sku"],
                "product_url": r["product_url"],
                "specs": specs,
                "specs_not_extracted": specs_not_extracted,
                "low_confidence_specs": low_confidence_specs,
            })

        returned = len(results)
        truncated = (offset + returned) < total_matches
        next_cursor = None
        if truncated:
            next_cursor = base64.b64encode(
                f"{offset + returned}:{fingerprint}".encode("utf-8")
            ).decode("ascii")

        return make_response(
            data={
                "results": results,
                "items_are": "listings",
                "note": (
                    "Each result is one vendor listing. The same physical part can appear once per vendor; "
                    "compare on mpn/specs rather than assuming one row per part."
                ),
                "result_coverage": result_coverage,
            },
            total_matches=total_matches,
            returned=returned,
            truncated=truncated,
            next_cursor=next_cursor,
            filters_applied=filters_applied,
            coverage=coverage,
            warnings=warnings
        )
    finally:
        conn.close()

@mcp.tool()
def list_spec_keys() -> ToolResponseEnvelope:
    """
    Return a list of all queryable spec attributes with fill counts and valid values/ranges for backward compatibility.

    Recommended Call Order: Maintained for backward compatibility; agents should prefer describe_catalog.

    Parameter Syntax: Takes no arguments.

    Example Invocation: list_spec_keys()

    Behavior When No Matches: Returns ok:true with data: {"spec_keys": [...]} in a single text content block.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        catalog_specs = _load_catalog_specs(conn)
        spec_keys_list = []
        for k, s in catalog_specs.items():
            cov_pct = round(s["count"] / max(total_products, 1) * 100, 2)
            spec_keys_list.append({
                "key": k,
                "display_name": s["display_name"],
                "type": s["type"],
                "unit": s["unit"],
                "coverage_pct": cov_pct,
                "count": s["count"],
                "values": s["enum_values"] if s["type"] == "enum" else None,
                "range": {"min": s["min"], "max": s["max"]} if s["type"] in ("integer", "float") else None,
                "confidence": "low" if k == "current_a" else "high",
                "precision_note": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)" if k == "current_a" else None
            })

        return make_response(
            data={"spec_keys": spec_keys_list},
            total_matches=len(spec_keys_list),
            returned=len(spec_keys_list)
        )
    finally:
        conn.close()

@mcp.tool()
def health() -> ToolResponseEnvelope:
    """
    Report server liveness, dependency versions, database path and integrity so a caller can distinguish a broken deployment from an empty catalog.

    Recommended Call Order: Call first when any other tool returns an unexpected result or a transport error.

    Parameter Syntax: Takes no arguments.

    Example Invocation: health()

    Behavior When No Matches: Returns ok:false with error_code='db_unavailable' if the catalog database cannot be opened or read.
    """
    try:
        conn = get_db()
    except Exception as exc:  # noqa: BLE001 - surfaced as structured data, not a traceback
        return make_error(
            error_code="db_unavailable",
            message=f"Could not open the catalog database: {exc}",
            details={"db_path": str(getattr(getattr(db, "settings", None), "db_path", "unknown"))}
        )

    try:
        cur = conn.cursor()
        integrity = cur.execute("PRAGMA integrity_check;").fetchone()[0]
        counts = {}
        for table in ("products", "product_specs", "spec_stats_global", "spec_definitions"):
            try:
                counts[table] = cur.execute(f"SELECT COUNT(1) FROM {table};").fetchone()[0]
            except sqlite3.Error as exc:
                counts[table] = f"error: {exc}"

        try:
            from importlib import metadata as importlib_metadata
            import pydantic
            dependency_versions = {
                "python": sys.version.split()[0],
                "pydantic": pydantic.VERSION,
            }
            for pkg in ("mcp", "fastmcp"):
                try:
                    dependency_versions[pkg] = importlib_metadata.version(pkg)
                except importlib_metadata.PackageNotFoundError:
                    dependency_versions[pkg] = "not installed"
        except Exception:  # noqa: BLE001
            dependency_versions = {"python": sys.version.split()[0]}

        data = {
            "status": "ok" if integrity == "ok" else "degraded",
            "db_path": str(getattr(getattr(db, "settings", None), "db_path", "unknown")),
            "integrity_check": integrity,
            "counts": counts,
            "dependency_versions": dependency_versions,
            "server_version": SERVER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
        }
        return make_response(data=data, total_matches=counts.get("products") if isinstance(counts.get("products"), int) else 0, returned=1)
    except sqlite3.Error as exc:
        return make_error(error_code="db_unavailable", message=f"Database error: {exc}")
    finally:
        conn.close()


@mcp.tool()
def search_by_mpn(mpn: str, limit: int = 20) -> ToolResponseEnvelope:
    """
    Resolve an exact manufacturer part number (or vendor SKU) to its vendor listings, without fuzzy text matching.

    Recommended Call Order: Call when the user supplies a concrete part number rather than a spec sheet; follow with get_product for full detail.

    Parameter Syntax:
      - mpn: str (part number or SKU; matched case-insensitively, surrounding whitespace ignored)
      - limit: int (max listings, 1 to 50 inclusive, default: 20)

    Example Invocation: search_by_mpn(mpn="RMCS-2077")

    Behavior When No Matches: Returns ok:true with total_matches:0 and a hint that MPN coverage in the catalog is uneven, rather than an error.
    """
    needle = (mpn or "").strip()
    if not needle:
        return make_error(
            error_code="invalid_filter_value",
            message="mpn must be a non-empty string.",
            suggestions=[{"mpn": "RMCS-2077"}]
        )

    try:
        limit_int = max(1, min(int(limit), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        limit_int = 20

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, vendor, vendor_name, title, price, available, product_url, sku, mpn, category
            FROM products
            WHERE upper(trim(mpn)) = upper(?) OR upper(trim(sku)) = upper(?)
            ORDER BY available DESC, price ASC
            LIMIT ?;
            """,
            (needle, needle, limit_int)
        )
        rows = cur.fetchall()

        cur.execute(
            "SELECT COUNT(1) FROM products WHERE upper(trim(mpn)) = upper(?) OR upper(trim(sku)) = upper(?);",
            (needle, needle)
        )
        total = cur.fetchone()[0]

        listings = [
            {
                "product_id": r[0],
                "vendor": r[2] or r[1],
                "title": r[3],
                "price": float(r[4]),
                "currency": "INR",
                "available": bool(r[5]),
                "stock_status": "in_stock" if r[5] else "out_of_stock",
                "product_url": r[6],
                "vendor_sku": r[7],
                "mpn": r[8],
                "category": r[9],
            }
            for r in rows
        ]

        warnings = []
        if not listings:
            warnings.append({
                "code": "mpn_not_found",
                "message": (
                    "No listing carries this exact MPN/SKU. MPN extraction is uneven across vendors, "
                    "so fall back to search_components with a free-text query before concluding the "
                    "part is absent from the catalog."
                ),
            })

        return make_response(
            data={
                "mpn": needle,
                "listings": listings,
                "items_are": "listings",
                "note": "Matched exact MPN or vendor SKU only; no fuzzy title matching was applied.",
            },
            total_matches=total,
            returned=len(listings),
            truncated=len(listings) < total,
            filters_applied={"mpn": needle, "limit": limit_int},
            warnings=warnings
        )
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Octopart India Spec-Driven MCP Server (Agent-First)")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="Transport mode: stdio (default) or sse")
    parser.add_argument("--port", type=int, default=8001, help="Port for SSE transport (default: 8001)")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport (default: 127.0.0.1)")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"Starting MCP Server (SSE) on http://{args.host}:{args.port}/sse ...", file=sys.stderr)
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
