"""
Spec-Driven MCP Query Server for Electronic Components (Agent-First).
Provides generic discovery, parametric introspection, counting, single-product lookup,
and targeted search with uniform response/error envelopes and operator syntax.
"""
import base64
import concurrent.futures
import asyncio
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp.server.fastmcp import FastMCP
from config import settings
import db
from models import PriceStockRefreshInfo, UnifiedProduct
from search_service import AggregatorEngine
from src.search.query_terms import QueryPlan, relaxed_fts_expr, strict_fts_expr
from src.spec.filters import (
    _nearest_keys,
    build_spec_clauses,
    enum_is_numeric as _enum_is_numeric,
    filter_column as _filter_column,
    load_catalog_specs as _load_catalog_specs,
    normalize_scalar,
    parse_and_validate_filters,
)
from src.spec.taxonomy_loader import get_taxonomy

logger = logging.getLogger(__name__)

SERVER_VERSION = "1.7.0"
SCHEMA_VERSION = "1.6.0"

# Hard ceiling on page size. An agent asking for 1000 rows is almost always
# paginating by accident, and a negative limit becomes SQL `LIMIT -1` (= no limit).
MAX_PAGE_SIZE = 50

# Initialize FastMCP Server
mcp = FastMCP("aggregate-agent-first")
_engine = AggregatorEngine()


# ---------------------------------------------------------------------------
# Search-time price/stock refresh bridge (sync MCP tools -> async engine)
# ---------------------------------------------------------------------------
# MCP tools are synchronous functions. The refresh engine is async, so it runs on
# one dedicated background event loop that also owns the engine's HTTP sessions.
# This avoids `asyncio.run` failing under SSE (where a loop is already running),
# keeps session lifetime pinned to a single loop, and lets tools stay sync for
# backward compatibility.
_refresh_loop: Optional[asyncio.AbstractEventLoop] = None
_refresh_loop_thread: Optional[threading.Thread] = None
_refresh_runtime_lock = threading.Lock()


def _get_refresh_engine() -> AggregatorEngine:
    """Engine used for price/stock refresh (separate function so tests can inject one)."""
    return _engine


def _get_refresh_loop() -> asyncio.AbstractEventLoop:
    """Returns (starting if needed) the dedicated loop that owns refresh sessions."""
    global _refresh_loop, _refresh_loop_thread
    with _refresh_runtime_lock:
        if _refresh_loop is None or _refresh_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="price-stock-refresh-loop",
                daemon=True,
            )
            thread.start()
            _refresh_loop = loop
            _refresh_loop_thread = thread
    return _refresh_loop


def refresh_price_stock_for_products(
    products: List[UnifiedProduct],
    query: Optional[str] = None,
    force: bool = False,
) -> PriceStockRefreshInfo:
    """
    Runs the engine's live price/stock refresh from synchronous MCP tools.

    Vendor/network failures never raise: the cached catalog values are served
    instead and the failure is reported in the returned metadata.
    """
    if not products:
        return PriceStockRefreshInfo(status="fresh_cache", message="No products to refresh.")
    if not getattr(settings, "price_stock_refresh_enabled", True):
        return PriceStockRefreshInfo(
            status="disabled",
            cached_products=len(products),
            message="Live price/stock refresh is disabled (PRICE_STOCK_REFRESH=0).",
        )

    coro = _get_refresh_engine().refresh_price_stock(products, query=query, force=force)
    wait_seconds = float(getattr(settings, "price_stock_refresh_timeout_seconds", 8.0)) + 5.0
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _get_refresh_loop())
    except RuntimeError as exc:  # loop refused further work (shutting down)
        coro.close()
        return PriceStockRefreshInfo(
            status="error",
            cached_products=len(products),
            message=f"Price/stock refresh could not be scheduled ({exc}); serving cached values.",
        )

    try:
        return future.result(timeout=wait_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return PriceStockRefreshInfo(
            status="timeout",
            attempted=True,
            cached_products=len(products),
            message=(
                "Price/stock refresh exceeded its wall-clock budget; serving cached values "
                "from the local catalog."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - cache fallback must survive any failure
        return PriceStockRefreshInfo(
            status="error",
            attempted=True,
            cached_products=len(products),
            message=f"Price/stock refresh failed ({exc}); serving cached values.",
        )


def _price_stock_fallback_warning(refresh_info: Optional[PriceStockRefreshInfo]) -> List[Dict[str, Any]]:
    """Warns when displayed price/stock came from cache because live refresh was impossible."""
    if refresh_info is None or not refresh_info.attempted:
        return []
    if refresh_info.status in ("cached", "offline_cooldown", "timeout", "error"):
        return [{
            "code": "price_stock_cache_fallback",
            "status": refresh_info.status,
            "message": refresh_info.message or "Serving cached price/stock.",
        }]
    return []

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

def _query_fingerprint(
    norm_filters: Optional[Dict[str, Any]],
    query: Optional[str],
    price_max: Optional[float],
    in_stock_only: bool,
    sort: str,
    category: Optional[str] = None,
) -> str:
    """Stable hash binding a pagination cursor to the exact query that produced it."""
    payload = json.dumps(
        {
            "filters": norm_filters or {},
            "query": (query or "").strip(),
            "price_max": price_max,
            "in_stock_only": bool(in_stock_only),
            "sort": sort,
            "category": category,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]


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


def _enum_filter_suggestions(
    norm_filters: Optional[Dict[str, Any]],
    catalog_specs: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Closest populated enum values for every unmatched-filter key (not just the last one)."""
    suggestions: List[Dict[str, Any]] = []
    for fk in (norm_filters or {}):
        if catalog_specs[fk]["type"] != "enum":
            continue
        requested = norm_filters[fk]
        if isinstance(requested, dict):
            requested = requested.get("eq", requested.get("in"))
        suggestions.extend(_enum_suggestions(fk, catalog_specs[fk], requested))
    return suggestions


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


def _parse_and_validate_filters(
    filters: Optional[Dict[str, Any]],
    advertised_specs: Dict[str, Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[ToolResponseEnvelope]]:
    """Validates filter keys and operators, rejecting deprecated string range forms."""
    normalized, error = parse_and_validate_filters(filters, advertised_specs)
    if error is None:
        return normalized, None
    return None, make_error(
        error_code=error["error_code"],
        message=error["message"],
        valid_keys=error.get("valid_keys"),
        suggestions=error.get("suggestions"),
        details=error.get("details"),
    )


def _valid_categories(conn: sqlite3.Connection) -> List[str]:
    """Canonical taxonomy categories actually present in the catalog."""
    rows = conn.execute(
        "SELECT DISTINCT category_canonical FROM products "
        "WHERE category_canonical IS NOT NULL AND category_canonical != '' "
        "ORDER BY category_canonical;"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _category_spec_keys(conn: sqlite3.Connection, category: str) -> List[str]:
    """Spec keys owned by a taxonomy category; used to rank genuine components first."""
    rows = conn.execute(
        "SELECT DISTINCT key FROM spec_definitions WHERE category = ? ORDER BY key;",
        (category,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _applicable_spec_keys(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """category -> spec keys map, for scoping 'not extracted' lists to fields that apply."""
    mapping: Dict[str, List[str]] = {}
    for category, key in conn.execute(
        "SELECT category, key FROM spec_definitions ORDER BY category, key;"
    ):
        mapping.setdefault(str(category), []).append(str(key))
    return mapping


def _category_coverage(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """
    key -> per-category extraction coverage (filled / applicable).

    Global coverage counts every product, including unclassified ones, so it makes
    battery voltage look 2% extracted. This is the number an agent needs before
    trusting a filter for a specific component category.
    """
    coverage: Dict[str, List[Dict[str, Any]]] = {}
    for key, category, filled, applicable in conn.execute(
        "SELECT key, category, filled_count, applicable_count FROM spec_stats "
        "ORDER BY key, category;"
    ):
        applicable = int(applicable or 0)
        filled = int(filled or 0)
        coverage.setdefault(str(key), []).append({
            "category": str(category),
            "filled": filled,
            "applicable": applicable,
            "pct": round(filled / applicable * 100, 1) if applicable else 0.0,
        })
    return coverage


def _build_sql_query(
    normalized_filters: Dict[str, Any],
    advertised_specs: Dict[str, Dict[str, Any]],
    fts_tokens: Optional[List[str]] = None,
    fts_relaxed: bool = False,
    price_max: Optional[float] = None,
    in_stock_only: bool = False,
    category: Optional[str] = None,
    category_spec_keys: Optional[List[str]] = None,
    require_spec_scope: bool = False,
    sort: str = "relevance",
    limit: int = 10,
    offset: int = 0
) -> Tuple[str, List[Any], str, List[Any]]:
    """Builds parameterized SQL for count and select queries.

    Text matching is driven by a resolved token list: `fts_tokens` with
    `fts_relaxed=False` requires every token to match (precision), while
    `fts_relaxed=True` matches any token (recall after query relaxation).
    """
    joins: List[str] = []
    join_params: List[Any] = []
    where_clauses: List[str] = []
    where_params: List[Any] = []

    spec_joins, spec_join_params, spec_where, spec_where_params = build_spec_clauses(
        normalized_filters, advertised_specs
    )
    joins.extend(spec_joins)
    join_params.extend(spec_join_params)
    where_clauses.extend(spec_where)
    where_params.extend(spec_where_params)

    if price_max is not None:
        where_clauses.append("p.price <= ?")
        where_params.append(float(price_max))

    if in_stock_only:
        where_clauses.append("p.available = 1")

    if category:
        # One namespace: the canonical catalog category.
        #
        # Categories that carry filterable spec attributes keep their original
        # scope (products whose specs were actually extracted for that
        # category), so `category="battery"` still means "batteries with
        # usable specs". Categories without attributes use the catalog bucket.
        if require_spec_scope:
            where_clauses.append("p.spec_category = ?")
        else:
            where_clauses.append("p.category_canonical = ?")
        where_params.append(category)

    if fts_tokens:
        if fts_relaxed:
            fts_expr = relaxed_fts_expr(fts_tokens)
        else:
            fts_expr = strict_fts_expr(fts_tokens)
        joins.append("JOIN products_fts fts ON p.id = fts.rowid")
        where_clauses.append("products_fts MATCH ?")
        where_params.append(fts_expr)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # Sort. When a category is requested, rank genuine components above catalog
    # entries that merely mention the category word (kits, bundles, chargers):
    # products carrying more of that category's extracted specs come first.
    order_params: List[Any] = []
    spec_boost = None
    if category_spec_keys and sort == "relevance":
        spec_placeholders = ", ".join(["?"] * len(category_spec_keys))
        spec_boost = (
            "(SELECT COUNT(1) FROM product_specs psc "
            f"WHERE psc.product_id = p.id AND psc.key IN ({spec_placeholders})) DESC"
        )
        order_params = list(category_spec_keys)

    if sort == "price_asc":
        order_by = "ORDER BY p.price ASC"
    elif sort == "price_desc":
        order_by = "ORDER BY p.price DESC"
    elif spec_boost and fts_tokens:
        order_by = f"ORDER BY {spec_boost}, rank"
    elif spec_boost:
        order_by = f"ORDER BY {spec_boost}, p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"
    elif fts_tokens:
        order_by = "ORDER BY rank"
    else:
        order_by = "ORDER BY p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"

    count_sql = f"SELECT COUNT(DISTINCT p.id) FROM products p {' '.join(joins)} {where_sql};"
    select_sql = f"SELECT p.* FROM products p {' '.join(joins)} {where_sql} {order_by} LIMIT ? OFFSET ?;"

    params = join_params + where_params
    select_params = params + order_params + [limit, offset]
    return count_sql, params, select_sql, select_params

# =====================================================================
# 6 AGENT-FIRST TOOLS
# =====================================================================

@mcp.tool()
def describe_catalog() -> ToolResponseEnvelope:
    """
    Return comprehensive catalog-level metadata, total product counts, vendor distributions, category counts, queryable spec keys with coverage percentages, and data freshness timestamps.

    Recommended Call Order: Call describe_catalog first before running any searches to inspect available attributes and catalog size.

    Using The Output: counts.products_by_spec_category lists the values accepted by the
    `category` parameter of search_components/count_components (e.g. "battery", "motor",
    "relay"). spec_keys lists every filterable key with coverage; `values` is null for
    open-ended enums (e.g. battery voltage), where describe_spec shows observed distributions.

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
        category_coverage = _category_coverage(conn)
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
                "category_coverage": category_coverage.get(k, []),
                "values": s["enum_values"] if s["type"] == "enum" else None,
                "range": {"min": s["min"], "max": s["max"]} if s["type"] in ("integer", "float") else None,
                "confidence": "low" if k == "current_a" else "high",
                "precision_note": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)" if k == "current_a" else None
            })

        cur.execute("SELECT MIN(updated_at), MAX(updated_at) FROM products;")
        oldest_crawl, newest_crawl = cur.fetchone()

        cur.execute("SELECT vendor, MIN(updated_at), MAX(updated_at) FROM products GROUP BY vendor;")
        per_vendor_freshness = {r[0]: {"oldest": r[1], "newest": r[2]} for r in cur.fetchall()}

        # The canonical catalog taxonomy: every category in the catalog, the
        # attributes that can be filtered on for it, and how many products
        # carry it.
        cur.execute(
            "SELECT coalesce(category_canonical, 'other'), COUNT(1) FROM products "
            "GROUP BY category_canonical ORDER BY COUNT(1) DESC;"
        )
        products_by_category = {r[0]: r[1] for r in cur.fetchall()}

        taxonomy = get_taxonomy()
        taxonomy_payload = []
        for cat in taxonomy.categories.values():
            taxonomy_payload.append({
                "id": cat.id,
                "display": cat.display,
                "description": cat.description,
                "products": products_by_category.get(cat.id, 0),
                "filterable_attributes": [
                    {
                        "key": attr.key,
                        "display_name": attr.display_name,
                        "type": attr.type,
                        "unit": attr.unit,
                        "values": attr.values,
                        "range": {"min": attr.range[0], "max": attr.range[1]} if attr.range else None,
                    }
                    for attr in cat.attributes.values()
                ],
            })

        data = {
            "total_products": total_products,
            "counts": {
                "products": total_products,
                "spec_rows": cur.execute("SELECT COUNT(1) FROM product_specs;").fetchone()[0],
                "spec_keys_advertised": len(spec_keys_list),
            },
            "products_by_vendor": products_by_vendor,
            "products_by_spec_category": products_by_spec_category,
            "taxonomy": taxonomy_payload,
            "spec_keys": spec_keys_list,
            "currency": "INR",
            "freshness": {
                "oldest_crawl": oldest_crawl,
                "newest_crawl": newest_crawl,
                "per_vendor": per_vendor_freshness,
                "price_stock": db.get_price_stock_stats()
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

    Using The Output: `coverage_pct` is global (all products, most of which are unclassified);
    `category_coverage` breaks the same key down per component category (e.g. voltage is
    extracted on ~61% of batteries but only ~22% of motors), which is the number to trust
    when deciding whether a filter is meaningful for a category.

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
            "category_coverage": _category_coverage(conn).get(key, []),
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
    in_stock_only: bool = False,
    category: Optional[str] = None
) -> ToolResponseEnvelope:
    """
    Count matching components and inspect filter coverage without fetching product rows.

    Recommended Call Order: Call after describe_catalog and before search_components to size query results and verify filter coverage.

    Parameter Syntax:
      - filters: dict (supports scalar equality or operators in, gte, lte, gt, lt, eq, tolerance, default: null)
      - query: str (FTS text search string, default: null)
      - price_max: float (maximum price in INR, default: null)
      - in_stock_only: bool (default: false)
      - category: str (restrict to a catalog category such as 'battery', 'motor', 'resistors'; see describe_catalog data.taxonomy, default: null)

    Natural-Language Queries: phrases like "battery to power an arduino" are handled by dropping
    filler words and relaxing an all-terms match to an any-term match when needed; warnings report it.

    Freshness Note: counts are computed on the cached catalog snapshot. Search-time live
    price/stock verification happens in search_components, get_product and search_by_mpn.

    Example Invocation: count_components(filters={"voltage": "12", "rpm": {"gte": 190, "lte": 210}}, query="motor", category="motor")

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

        category_norm: Optional[str] = None
        require_spec_scope = False
        if category is not None and str(category).strip():
            category_norm = str(category).strip().lower()
            valid_categories = _valid_categories(conn)
            if category_norm not in valid_categories:
                return make_error(
                    error_code="unknown_category",
                    message=f"Component category '{category}' is not present in the catalog.",
                    suggestions=[{"category": c} for c in valid_categories[:10]],
                    details={"valid_categories": valid_categories},
                )
            # Categories with filterable attributes keep their original scope:
            # only products whose specs were extracted for that category match.
            require_spec_scope = bool(_category_spec_keys(conn, category_norm))


        if query is not None and not str(query).strip():
            return make_error(
                error_code="empty_query",
                message=(
                    "query was provided but is empty/whitespace. Omit it to count without a "
                    "full-text filter, or pass actual search terms."
                ),
                suggestions=[{"query": "motor"}],
            )

        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = db.resolve_query_plan(query, conn=conn)

        if query_plan is not None and not query_plan.resolved:
            coverage, warnings = _build_coverage_and_warnings(
                catalog_specs, total_products, norm_filters or {}, 0
            )
            warnings.extend(query_plan.warnings)
            suggestions = _enum_filter_suggestions(norm_filters, catalog_specs)
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            return make_response(
                data={
                    "count": 0,
                    "suggestions": suggestions,
                    "query_suggestions": query_plan.suggestions,
                },
                total_matches=0,
                returned=0,
                filters_applied={
                    "filters": norm_filters or {},
                    "query": (query or "").strip() or None,
                    "query_effective": query_plan.effective_query,
                    "price_max": price_max,
                    "in_stock_only": bool(in_stock_only),
                    "category": category_norm,
                },
                coverage=coverage,
                warnings=warnings
            )

        count_sql, params, _, _ = _build_sql_query(
            norm_filters or {},
            catalog_specs,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max,
            in_stock_only=in_stock_only,
            category=category_norm,
            require_spec_scope=require_spec_scope
        )
        cur.execute(count_sql, params)
        total_count = cur.fetchone()[0]

        coverage, warnings = _build_coverage_and_warnings(
            catalog_specs, total_products, norm_filters or {}, total_count
        )

        suggestions: List[Dict[str, Any]] = []
        if total_count == 0 and norm_filters:
            suggestions.extend(_enum_filter_suggestions(norm_filters, catalog_specs))
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter matched 0 catalog rows; see data.suggestions for the closest populated values."
                })

        if query_plan is not None:
            warnings.extend(query_plan.warnings)

        filters_applied = {
            "filters": norm_filters or {},
            "query": (query or "").strip() or None,
            "price_max": price_max,
            "in_stock_only": bool(in_stock_only),
            "category": category_norm,
        }
        if query_plan is not None and query_plan.resolved:
            original = (query or "").strip()
            if query_plan.effective_query.lower() != original.lower():
                filters_applied["query_effective"] = query_plan.effective_query

        return make_response(
            data={"count": total_count, "suggestions": suggestions},
            total_matches=total_count,
            returned=0,
            filters_applied=filters_applied,
            coverage=coverage,
            warnings=warnings
        )
    finally:
        conn.close()

@mcp.tool()
def get_product(product_id: int, refresh: bool = True, force_refresh: bool = False) -> ToolResponseEnvelope:
    """
    Retrieve the full record for an individual component, including extracted specs, multi-vendor offers, crawl metadata, and extraction confidence.

    Recommended Call Order: Call after search_components to inspect the full detail page and all vendor pricing for a specific product.

    Parameter Syntax:
      - product_id: int (positive integer product ID from search results)
      - refresh: bool (default: true; verify this product's and its sibling offers' price/stock live, TTL-aware)
      - force_refresh: bool (default: false; bypass the refresh TTL to re-verify now)

    Price/Stock Freshness: price and stock for this product and its multi-vendor siblings are
    re-verified against live vendors before the response is built (only those two fields are
    updated; refreshed values are cached in the local catalog). When vendors are unreachable,
    the last cached values are served and data.price_stock_refresh reports the status.

    Example Invocation: get_product(product_id=4395)

    Spec Fields: 'extracted_specs' holds the taxonomy attributes found for this product (with
    evidence and confidence). 'spec_category' is the taxonomy category and 'spec_extraction_scope'
    is "unclassified" when the product is outside the taxonomy — in that case specs are not
    extracted and 'raw_attributes' / 'description' are the authoritative details.
    'specs_not_extracted' lists applicable keys that were not found (never fabricated as null).

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

        # Multi-vendor offers for sibling/matching components
        offers_sql = """
            SELECT id, vendor, vendor_name, title, price, available, product_url, sku, mpn
            FROM products
            WHERE (canonical_for IS NOT NULL AND canonical_for = ?)
               OR (mpn IS NOT NULL AND mpn != '' AND mpn = ?)
               OR id = ?
            ORDER BY available DESC, price ASC;
        """
        offer_params = (p.get("canonical_for"), p.get("mpn"), product_id)

        # Verify price/stock (product + siblings) before building the response.
        refresh_info: Optional[PriceStockRefreshInfo] = None
        if refresh:
            cur.execute(offers_sql, offer_params)
            candidate_ids = [r[0] for r in cur.fetchall()]
            refresh_info = refresh_price_stock_for_products(
                db.get_products_by_ids(candidate_ids),
                query=(p.get("mpn") or p.get("sku") or None),
                force=force_refresh,
            )
            if refresh_info.attempted and refresh_info.refreshed_products > 0:
                # Re-read the product and its offers so both reflect the refreshed values.
                cur.execute("SELECT * FROM products WHERE id = ?;", (product_id,))
                col_names = [d[0] for d in cur.description]
                row = cur.fetchone()
                if row:
                    p = dict(zip(col_names, row))
                    offer_params = (p.get("canonical_for"), p.get("mpn"), product_id)

        cur.execute(offers_sql, offer_params)
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

        raw_meta = json.loads(p["raw_json"]) if p.get("raw_json") else {}

        catalog_keys = sorted(_load_catalog_specs(conn).keys())
        spec_category = p.get("spec_category")
        # Scope the "not extracted" list to keys that apply to this product's
        # category: a battery missing rpm would otherwise look like a data gap.
        applicable_keys = _applicable_spec_keys(conn).get(spec_category or "", catalog_keys)
        specs_not_extracted = [k for k in applicable_keys if k not in extracted_specs]

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
            "spec_category": spec_category,
            "spec_extraction_scope": "category_scoped" if spec_category else "unclassified",
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
            "vendor_offers": offers,
            "price_stock_refresh": refresh_info.model_dump() if refresh_info else None
        }
        return make_response(
            data=data,
            total_matches=1,
            returned=1,
            warnings=_price_stock_fallback_warning(refresh_info),
        )
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
    cursor: Optional[str] = None,
    refresh: bool = True,
    force_refresh: bool = False,
    category: Optional[str] = None
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
      - refresh: bool (default: true; verify price/stock live before returning, TTL-aware)
      - force_refresh: bool (default: false; bypass the refresh TTL to re-verify now)
      - category: str (restrict to a catalog category, e.g. 'battery', 'motor', 'resistors'; see describe_catalog data.taxonomy, default: null)

    Natural-Language Queries: phrases like "battery to power an arduino for 8 hours" are
    understood: filler words are dropped, an all-terms match is relaxed to an any-term match
    when nothing matches, and close spellings are corrected from the catalog vocabulary.
    Check filters_applied.query_effective and warnings (query_relaxed, query_spelling_corrected)
    to see exactly what was searched.

    Rows Are Listings: each result is one vendor listing, so the same physical part may appear
    once per vendor. Compare on mpn/specs instead of assuming one row per part.

    Price/Stock Freshness: before returning, price and stock are re-verified against the live
    vendors for the returned listings (only those two fields are updated, and the refreshed
    values are cached in the local catalog). When the network or a vendor is unavailable, the
    last cached price/stock is served instead and data.price_stock_refresh reports the status.

    Spec Fields: 'specs' contains only extracted keys. Keys that were never extracted are named
    in 'specs_not_extracted' (unknown is never reported as null) and are scoped to the product's
    own category. 'spec_extraction_scope' is "unclassified" for products outside the taxonomy
    (most of the catalog): for those, specs are not extracted at all — use the title,
    description/raw_attributes, and price/stock instead. 'low_confidence_specs' lists keys whose
    extraction confidence is below 1.0.

    Example Invocation: search_components(query="12v geared motor", category="motor", filters={"rpm": {"lte": 1000}}, limit=10)

    Behavior When No Matches: Returns ok:true with total_matches:0, data: {"results": []}, and
    rank-ordered suggestions if an enum value was unmatched (data.query_suggestions when the text
    query itself could be misspelled) — never an error.
    Errors: unknown_filter_key, unknown_category, unsupported_operator, operator_not_supported_for_type,
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

        category_norm: Optional[str] = None
        require_spec_scope = False
        if category is not None and str(category).strip():
            category_norm = str(category).strip().lower()
            valid_categories = _valid_categories(conn)
            if category_norm not in valid_categories:
                return make_error(
                    error_code="unknown_category",
                    message=f"Component category '{category}' is not present in the catalog.",
                    suggestions=[{"category": c} for c in valid_categories[:10]],
                    details={"valid_categories": valid_categories},
                )
            # Categories with filterable attributes keep their original scope:
            # only products whose specs were extracted for that category match.
            require_spec_scope = bool(_category_spec_keys(conn, category_norm))


        # --- query understanding: stop words, typo correction, AND -> OR relaxation ---
        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = db.resolve_query_plan(query, conn=conn)
        effective_query = query_plan.effective_query if query_plan else (query or "").strip()

        # --- cursor is bound to the query that produced it ---
        fingerprint = _query_fingerprint(
            norm_filters, effective_query, price_max, in_stock_only, sort, category_norm
        )
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

        filters_applied = {
            "filters": norm_filters or {},
            "query": (query or "").strip() or None,
            "price_max": price_max,
            "in_stock_only": bool(in_stock_only),
            "category": category_norm,
            "sort": sort,
            "limit": effective_limit,
            "offset": offset,
            "refresh": bool(refresh),
            "force_refresh": bool(force_refresh),
        }
        if query_plan is not None and query_plan.resolved:
            original = (query or "").strip()
            if query_plan.effective_query.lower() != original.lower():
                filters_applied["query_effective"] = query_plan.effective_query

        if query_plan is not None and not query_plan.resolved:
            # Nothing in the index resembles the query: do NOT silently drop the
            # text filter (that would return the whole catalog). Guide instead.
            warnings.extend(query_plan.warnings)
            suggestions = _enum_filter_suggestions(norm_filters, catalog_specs)
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter value matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            return make_response(
                data={
                    "results": [],
                    "suggestions": suggestions,
                    "query_suggestions": query_plan.suggestions,
                    "price_stock_refresh": None,
                },
                total_matches=0,
                returned=0,
                truncated=False,
                next_cursor=None,
                filters_applied=filters_applied,
                coverage=_build_coverage_and_warnings(
                    catalog_specs, total_products, norm_filters or {}, 0
                )[0],
                warnings=warnings
            )

        count_sql, count_params, select_sql, select_params = _build_sql_query(
            norm_filters or {},
            catalog_specs,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max,
            in_stock_only=in_stock_only,
            category=category_norm,
            category_spec_keys=(_category_spec_keys(conn, category_norm) if category_norm else None),
            require_spec_scope=require_spec_scope,
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
        if query_plan is not None:
            warnings.extend(query_plan.warnings)

        if total_matches == 0:
            suggestions: List[Dict[str, Any]] = []
            suggestions.extend(_enum_filter_suggestions(norm_filters, catalog_specs))
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter value matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            zero_data: Dict[str, Any] = {
                "results": [],
                "suggestions": suggestions,
                "price_stock_refresh": None,
            }
            if query_plan is not None and query_plan.suggestions:
                zero_data["query_suggestions"] = query_plan.suggestions
            return make_response(
                data=zero_data,
                total_matches=0,
                returned=0,
                truncated=False,
                next_cursor=None,
                filters_applied=filters_applied,
                coverage=coverage,
                warnings=warnings
            )

        refresh_info: Optional[PriceStockRefreshInfo] = None
        cur.execute(select_sql, select_params)
        rows = cur.fetchall()

        if refresh and rows:
            # Verify price/stock for the displayed page before the response is built;
            # only those two fields are updated, and the values are cached in SQLite.
            refresh_info = refresh_price_stock_for_products(
                [db.row_to_unified_product(r) for r in rows],
                query=effective_query or None,
                force=force_refresh,
            )
            warnings.extend(_price_stock_fallback_warning(refresh_info))
            if refresh_info.attempted and refresh_info.refreshed_products > 0:
                # Re-read the page so displayed prices/stock reflect the refresh.
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
        category_keys_map = _applicable_spec_keys(conn)
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

            row_keys = r.keys()
            row_category = r["spec_category"] if "spec_category" in row_keys else None
            applicable_keys = category_keys_map.get(row_category or "", all_catalog_keys)
            # unknown != absent != null: missing keys are enumerated, never filled
            # with null — scoped to keys that apply to this product's category.
            specs_not_extracted = [k for k in applicable_keys if k not in specs]

            stock_status = "in_stock" if r["available"] else "out_of_stock"
            results.append({
                "product_id": pid,
                "title": r["title"],
                "vendor": r["vendor_name"] or r["vendor"],
                "price": float(r["price"]),
                "currency": "INR",
                "available": bool(r["available"]),
                "stock_status": stock_status,
                "price_source": (r["price_stock_source"] if "price_stock_source" in row_keys else None) or "cache",
                "price_updated_at": (
                    str(r["price_stock_updated_at"])
                    if "price_stock_updated_at" in row_keys and r["price_stock_updated_at"] else None
                ),
                "category": r["category"],
                "spec_category": row_category,
                "spec_extraction_scope": "category_scoped" if row_category else "unclassified",
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
                "price_stock_refresh": refresh_info.model_dump() if refresh_info else None,
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
        category_coverage = _category_coverage(conn)
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
                "category_coverage": category_coverage.get(k, []),
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
            "read_only": False,
            "write_scope": "search-time price/stock refresh cache only",
            "price_stock_refresh": {
                "enabled": bool(settings.price_stock_refresh_enabled),
                "ttl_seconds": settings.price_stock_ttl_seconds,
                "timeout_seconds": settings.price_stock_refresh_timeout_seconds,
            },
        }
        return make_response(data=data, total_matches=counts.get("products") if isinstance(counts.get("products"), int) else 0, returned=1)
    except sqlite3.Error as exc:
        return make_error(error_code="db_unavailable", message=f"Database error: {exc}")
    finally:
        conn.close()


@mcp.tool()
def search_by_mpn(
    mpn: str,
    limit: int = 20,
    cursor: Optional[str] = None,
    refresh: bool = True,
    force_refresh: bool = False
) -> ToolResponseEnvelope:
    """
    Resolve a manufacturer part number (or vendor SKU) to its vendor listings, without fuzzy text matching.

    Recommended Call Order: Call when the user supplies a concrete part number rather than a spec sheet; follow with get_product for full detail.

    Parameter Syntax:
      - mpn: str (part number or SKU; matched case-insensitively and ignoring spaces/hyphens/underscores)
      - limit: int (max listings per page, 1 to 50 inclusive, default: 20)
      - cursor: str (opaque cursor from a previous page's next_cursor; bound to this mpn)
      - refresh: bool (default: true; verify these listings' price/stock live, TTL-aware)
      - force_refresh: bool (default: false; bypass the refresh TTL to re-verify now)

    Matching: 'MPU6050', 'mpu-6050' and 'MPU 6050' resolve to the same listings; each listing
    carries its extracted specs so fit questions can be answered without a second call.

    Price/Stock Freshness: price and stock for the returned listings are re-verified against
    live vendors before the response is built, and cached in the local catalog. When vendors
    are unreachable, the last cached values are served and data.price_stock_refresh reports it.

    Behavior When No Matches: Returns ok:true with total_matches:0 and data.suggested_mpns
    (closest catalog part numbers) rather than an error, plus a hint to fall back to free-text search.

    Example Invocation: search_by_mpn(mpn="RMCS-2077")
    """
    needle = (mpn or "").strip()
    if not needle:
        return make_error(
            error_code="invalid_filter_value",
            message="mpn must be a non-empty string.",
            suggestions=[{"mpn": "RMCS-2077"}]
        )

    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        return make_error(
            error_code="invalid_filter_value",
            message=f"limit must be an integer (got {limit!r}).",
            suggestions=[{"limit": 20}]
        )
    if limit_int < 1:
        return make_error(
            error_code="invalid_filter_value",
            message=f"limit must be >= 1 (got {limit_int}).",
            suggestions=[{"limit": 20}]
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

    normalized = db.normalize_identifier(needle)
    fingerprint = _query_fingerprint({"mpn": normalized}, None, None, False, "relevance")

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
                message="This cursor was issued for a different mpn.",
                suggestions=[{"cursor": None}],
                details={"hint": "Drop the cursor and restart pagination with the current arguments."}
            )
        if offset < 0:
            offset = 0

    conn = get_db()
    try:
        rows, total = db.find_products_by_mpn(needle, limit=effective_limit, offset=offset)

        refresh_info: Optional[PriceStockRefreshInfo] = None
        if refresh and rows:
            refresh_info = refresh_price_stock_for_products(
                db.get_products_by_ids([r["id"] for r in rows]),
                query=needle,
                force=force_refresh,
            )
            if refresh_info.attempted and refresh_info.refreshed_products > 0:
                rows, total = db.find_products_by_mpn(needle, limit=effective_limit, offset=offset)

        # Extracted specs for the page: lets an agent answer "will this fit?" in one call.
        specs_by_product: Dict[int, Dict[str, str]] = {}
        if rows:
            cur = conn.cursor()
            ids = [r["id"] for r in rows]
            placeholders = ", ".join(["?"] * len(ids))
            cur.execute(
                f"SELECT product_id, key, value FROM product_specs WHERE product_id IN ({placeholders});",
                ids,
            )
            for pid, spec_key, spec_value in cur.fetchall():
                specs_by_product.setdefault(pid, {})[spec_key] = spec_value

        listings = [
            {
                "product_id": r["id"],
                "vendor": r["vendor_name"] or r["vendor"],
                "title": r["title"],
                "price": float(r["price"]),
                "currency": "INR",
                "available": bool(r["available"]),
                "stock_status": "in_stock" if r["available"] else "out_of_stock",
                "price_source": r["price_stock_source"] or "cache",
                "price_updated_at": r["price_stock_updated_at"],
                "product_url": r["product_url"],
                "vendor_sku": r["sku"],
                "mpn": r["mpn"],
                "category": r["category"],
                "specs": specs_by_product.get(r["id"], {}),
            }
            for r in rows
        ]

        suggested_mpns: List[str] = []
        if not listings:
            suggested_mpns = db.suggest_mpns(needle)
            warnings.append({
                "code": "mpn_not_found",
                "message": (
                    "No listing carries this MPN/SKU (separators are ignored). MPN extraction is uneven "
                    "across vendors, so fall back to search_components with a free-text query before "
                    "concluding the part is absent from the catalog."
                ),
            })
            if suggested_mpns:
                warnings.append({
                    "code": "mpn_did_you_mean",
                    "message": f"No exact match; closest catalog MPNs: {', '.join(suggested_mpns[:3])}.",
                    "suggestions": suggested_mpns,
                })
        warnings.extend(_price_stock_fallback_warning(refresh_info))

        returned = len(listings)
        truncated = (offset + returned) < total
        next_cursor = None
        if truncated:
            next_cursor = base64.b64encode(
                f"{offset + returned}:{fingerprint}".encode("utf-8")
            ).decode("ascii")

        return make_response(
            data={
                "mpn": needle,
                "mpn_normalized": normalized,
                "listings": listings,
                "items_are": "listings",
                "suggested_mpns": suggested_mpns,
                "note": (
                    "Matched MPN or vendor SKU ignoring case, spaces and hyphens; no fuzzy title matching "
                    "was applied. Listings include extracted specs for fit checks."
                ),
                "price_stock_refresh": refresh_info.model_dump() if refresh_info else None,
            },
            total_matches=total,
            returned=returned,
            truncated=truncated,
            next_cursor=next_cursor,
            filters_applied={
                "mpn": needle,
                "mpn_normalized": normalized,
                "limit": effective_limit,
                "offset": offset,
                "refresh": bool(refresh),
                "force_refresh": bool(force_refresh),
            },
            warnings=warnings
        )
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Aggregate Spec-Driven MCP Server (Agent-First)")
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
