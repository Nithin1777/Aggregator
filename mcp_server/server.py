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
import math
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
from normalizer import extract_pack_quantity
from search_service import AggregatorEngine
from src.llm.bucketing import get_family_tokens
from src.search.product_prior import coverage as query_title_coverage
from src.search.query_terms import (
    QueryPlan,
    detect_sort_intent,
    detect_unsupported_syntax,
    parse_price_constraints,
    relaxed_fts_expr,
    strict_fts_expr,
)
from src.search.retrieval import rank_products
from src.spec.filters import (
    ALIAS_GROUPS,
    _nearest_keys,
    alias_catalog_payload,
    build_alias_clauses,
    build_alias_expansion,
    build_hybrid_spec_clauses,
    build_spec_clauses,
    build_vendor_spec_clauses,
    canonical_enum_value,
    enum_is_numeric as _enum_is_numeric,
    filter_column as _filter_column,
    load_catalog_specs as _load_catalog_specs,
    normalize_scalar,
    parse_and_validate_filters,
    resolve_category,
    route_alias_key,
    validate_filter_scope,
)
from src.spec.vendor_index import (
    load_vendor_specs,
    normalize_vendor_key,
    vendor_category_keys,
    vendor_key_value_counts,
)
from src.spec.taxonomy_loader import get_taxonomy

logger = logging.getLogger(__name__)

SERVER_VERSION = "2.2.0"
SCHEMA_VERSION = "2.2.0"

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
    diagnosis: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Single machine-readable explanation for an empty, rejected, or low-confidence result. "
            "Branch on diagnosis.reason and diagnosis.severity (error/warning/info) instead of "
            "inferring from warnings."
        ),
    )

_description_schema_ready = False


def get_db() -> sqlite3.Connection:
    global _description_schema_ready
    conn = db.get_db_connection()
    if not _description_schema_ready:
        db.ensure_description_schema(conn)
        _description_schema_ready = True
    return conn

def make_response(
    data: Any,
    total_matches: int = 0,
    returned: int = 0,
    truncated: bool = False,
    next_cursor: Optional[str] = None,
    filters_applied: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    diagnosis: Optional[Dict[str, Any]] = None,
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
        "diagnosis": diagnosis,
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
    diagnosis: Optional[Dict[str, Any]] = None,
) -> ToolResponseEnvelope:
    """Builds a structured error envelope without throwing bare ValueErrors."""
    if diagnosis is not None:
        diagnosis = {"severity": "error", **diagnosis}
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "valid_keys": valid_keys,
        "suggestions": suggestions or [],
        "details": details or {},
        "diagnosis": diagnosis or {"reason": error_code, "severity": "error", **(details or {})},
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
    vendor_filters: Optional[Dict[str, Any]] = None,
) -> str:
    """Stable hash binding a pagination cursor to the exact query that produced it."""
    payload = json.dumps(
        {
            "filters": norm_filters or {},
            "vendor_filters": vendor_filters or {},
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
        requested_text = canonical_enum_value(requested, spec_meta)
        declared = [str(v) for v in (spec_meta.get("enum_values") or [])]
        declared_present = [v for v in declared if v in counts]
        near = difflib.get_close_matches(
            str(requested_text),
            [str(v) for v in counts],
            n=limit,
            cutoff=0.5,
        )

        def rank(item: Tuple[str, int]) -> Tuple[int, float, int]:
            value = str(item[0])
            if value == requested_text:
                return (0, 0.0, -item[1])
            if value in declared_present:
                return (1, float(declared_present.index(value)), -item[1])
            if value in near:
                return (2, float(near.index(value)), -item[1])
            return (3, -float(item[1]), -item[1])

        items.sort(key=rank)

    return [{"key": key, "value": v, "count": c} for v, c in items[:limit]]


def _enum_filter_suggestions(
    norm_filters: Optional[Dict[str, Any]],
    catalog_specs: Dict[str, Dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Closest populated enum values for every unmatched-filter key.

    When a category is supplied, suggestions come from that category's observed
    values (spec_stats), not from the global distribution: suggesting `lipo` to a
    motor query is worse than suggesting nothing.
    """
    suggestions: List[Dict[str, Any]] = []
    for fk in (norm_filters or {}):
        if catalog_specs[fk]["type"] != "enum":
            continue
        requested = norm_filters[fk]
        if isinstance(requested, dict):
            requested = requested.get("eq", requested.get("in"))

        meta = catalog_specs[fk]
        if conn is not None and category:
            row = conn.execute(
                "SELECT value_counts FROM spec_stats WHERE category = ? AND key = ?;",
                (category, fk),
            ).fetchone()
            scoped = None
            if row and row[0]:
                try:
                    scoped = json.loads(row[0])
                except (TypeError, ValueError):
                    scoped = None
            if scoped:
                meta = {**meta, "value_counts": scoped}

        suggestions.extend(_enum_suggestions(fk, meta, requested))
    return suggestions


def _build_coverage_and_warnings(
    catalog_specs: Dict[str, Dict[str, Any]],
    total_products: int,
    norm_filters: Dict[str, Any],
    total_matches: int,
    result_spec_counts: Optional[Dict[str, Dict[str, int]]] = None,
    category: Optional[str] = None,
    category_total: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
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
        # Two explicit denominators: the whole catalog, and the page actually
        # returned. The old result_present/result_of pair divided a page count
        # by total_matches, which made every filtered key look 100% covered.
        coverage[fk] = {
            "catalog": {"present": present, "of": total_products, "pct": pct},
        }
        if category and conn is not None:
            rows = conn.execute(
                "SELECT category, filled_count, applicable_count FROM spec_stats WHERE key = ?;",
                (fk,),
            ).fetchall()
            row = next((r for r in rows if str(r[0]) == str(category)), None)
            filled = int(row[1]) if row and row[1] is not None else 0
            # A key owned by exactly one category can use the global applicable
            # count: spec_stats can lag behind after category reclassification,
            # while the global count is recomputed by the same promotion run.
            if len(rows) == 1 and str(rows[0][0]) == str(category):
                filled = max(filled, int(meta.get("count") or 0))
            denominator = int(category_total or (row[2] if row and row[2] else 0) or 0)
            coverage[fk]["category"] = {
                "present": filled,
                "of": denominator,
                "pct": round(filled / max(denominator, 1) * 100, 2) if denominator else 0.0,
            }

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


def _compute_filter_recall(
    conn: sqlite3.Connection,
    catalog_specs: Dict[str, Dict[str, Any]],
    norm_filters: Dict[str, Any],
    query_plan: Optional[QueryPlan],
    price_max: Optional[float],
    in_stock_only: bool,
    category: Optional[str],
    active_category_keys: Optional[List[str]],
    require_spec_scope: bool,
    total_matches: int,
    alias_filters: Optional[List[Dict[str, Any]]] = None,
    vendor_filters: Optional[Dict[str, Any]] = None,
    vendor_specs_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    include_vendor_specs: bool = True,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Counts products excluded because a filtered key was never extracted."""
    norm_filters = norm_filters or {}
    vendor_filters = vendor_filters or {}
    if not norm_filters and not vendor_filters:
        return None, []
    base_count_sql, base_params, _, _ = _build_sql_query(
        {},
        catalog_specs,
        fts_tokens=(query_plan.effective_tokens if query_plan else None),
        fts_relaxed=(query_plan.is_relaxed if query_plan else False),
        price_max=price_max,
        in_stock_only=in_stock_only,
        category=category,
        category_spec_keys=active_category_keys,
        require_spec_scope=require_spec_scope,
    )
    inner_sql = base_count_sql.replace("SELECT COUNT(DISTINCT p.id)", "SELECT p.id", 1).rstrip().rstrip(";")
    base_total = int(conn.execute(f"SELECT COUNT(*) FROM ({inner_sql}) base;", base_params).fetchone()[0])
    warnings: List[Dict[str, Any]] = []
    recall: Dict[str, Any] = {
        "scope": "extracted_only",
        "mode": "hybrid" if include_vendor_specs else "promoted",
        "base_matches": base_total,
    }

    if norm_filters:
        keys = list(norm_filters.keys())
        promoted_sql, promoted_params, _, _ = _build_sql_query(
            norm_filters,
            catalog_specs,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max,
            in_stock_only=in_stock_only,
            category=category,
            category_spec_keys=active_category_keys,
            require_spec_scope=require_spec_scope,
            hybrid_vendor=False,
            vendor_filters=vendor_filters,
            vendor_specs=vendor_specs_meta,
        )
        promoted_total = int(conn.execute(promoted_sql, promoted_params).fetchone()[0])
        missing_sql = " OR ".join(
            "(NOT EXISTS (SELECT 1 FROM product_specs ps WHERE ps.product_id = base.id AND ps.key = ?) "
            "AND NOT EXISTS (SELECT 1 FROM product_vendor_specs pvs "
            "WHERE pvs.product_id = base.id AND pvs.key = ?))"
            for _ in keys
        )
        missing_params: List[Any] = []
        for key in keys:
            missing_params.extend([key, key])
        missing_spec = int(conn.execute(
            f"SELECT COUNT(*) FROM ({inner_sql}) base WHERE {missing_sql};",
            base_params + missing_params,
        ).fetchone()[0])
        promoted_missing = int(conn.execute(
            f"SELECT COUNT(*) FROM ({inner_sql}) base WHERE "
            + " OR ".join(
                "NOT EXISTS (SELECT 1 FROM product_specs ps "
                "WHERE ps.product_id = base.id AND ps.key = ?)"
                for _ in keys
            )
            + ";",
            base_params + keys,
        ).fetchone()[0])
        filtered_out_value = max(0, base_total - total_matches - missing_spec)
        recall.update({
            "keys": keys,
            "missing_spec": missing_spec,
            "promoted_matches": promoted_total,
            "hybrid_matches": total_matches,
            "vendor_fallback_matches": max(0, total_matches - promoted_total),
            "promoted_missing": promoted_missing,
            "vendor_available_but_value_mismatch": max(0, promoted_missing - missing_spec),
            "filtered_out_for_value": filtered_out_value,
        })
        if missing_spec:
            warnings.append({
                "code": "filter_recall_gap",
                "message": (
                    f"{missing_spec} matching product(s) have no extracted value for "
                    f"{keys} in either the promoted or vendor layer and are excluded. "
                    "Treat the count as 'products with usable specs', not all products."
                ),
                "missing_spec": missing_spec,
                "base_matches": base_total,
                "vendor_fallback_matches": recall["vendor_fallback_matches"],
            })

    if vendor_filters:
        vendor_keys = list(vendor_filters.keys())
        vendor_missing_sql = " OR ".join(
            "NOT EXISTS (SELECT 1 FROM product_vendor_specs pvs "
            "WHERE pvs.product_id = base.id AND pvs.key = ?)"
            for _ in vendor_keys
        )
        vendor_missing = int(conn.execute(
            f"SELECT COUNT(*) FROM ({inner_sql}) base WHERE {vendor_missing_sql};",
            base_params + vendor_keys,
        ).fetchone()[0])
        recall["vendor_filters"] = {
            "keys": vendor_keys,
            "missing_spec": vendor_missing,
            "base_matches": base_total,
        }
        if vendor_missing:
            warnings.append({
                "code": "vendor_filter_recall_gap",
                "message": (
                    f"{vendor_missing} matching product(s) have no vendor attribute "
                    f"for {vendor_keys}; the vendor tier is incomplete."
                ),
                "missing_spec": vendor_missing,
                "base_matches": base_total,
                "tier": "vendor",
            })
    return recall, warnings


def _diagnosis(reason: str, severity: str = "warning", **payload: Any) -> Dict[str, Any]:
    """Single ranked explanation; agents branch on `.reason`."""
    return {"reason": reason, "severity": severity, **payload}


def _diagnose_empty_result(
    norm_filters: Dict[str, Any],
    category: Optional[str],
    query_original: Optional[str],
    total_matches: int,
    base_total: int,
    missing_spec: int,
    filter_recall: Optional[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    individual_counts: Optional[List[int]] = None,
    vendor_filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Implements the documented diagnosis precedence for a 0-row result."""
    if total_matches > 0:
        raise ValueError("_diagnose_empty_result is only for empty results")
    vendor_filters = vendor_filters or {}
    if vendor_filters and not norm_filters:
        vendor_recall = (filter_recall or {}).get("vendor_filters") or {}
        vendor_missing = int(vendor_recall.get("missing_spec") or 0)
        vendor_base = int(vendor_recall.get("base_matches") or base_total or 0)
        if vendor_base > 0 and vendor_missing >= vendor_base:
            return _diagnosis(
                "filter_key_not_extracted",
                "warning",
                filters=vendor_filters,
                tier="vendor",
                category=category,
                missing_spec=vendor_missing,
                base_matches=vendor_base,
                hint=(
                    "The vendor layer has no value for this key on any candidate. "
                    "This is an extraction gap; try a promoted filter or text search."
                ),
            )
        return _diagnosis(
            "filter_value_not_present",
            "warning",
            filters=vendor_filters,
            tier="vendor",
            category=category,
            missing_spec=vendor_missing,
            base_matches=vendor_base,
        )

    promoted_missing = int((filter_recall or {}).get("promoted_missing") or 0)
    vendor_values_present = max(0, base_total - missing_spec)
    if base_total > 0 and promoted_missing >= base_total:
        return _diagnosis(
            "filter_key_not_extracted",
            "warning",
            filters=norm_filters,
            category=category,
            tier="promoted",
            missing_spec=promoted_missing,
            base_matches=base_total,
            vendor_values_present=vendor_values_present,
            hint=(
                "The authoritative promoted layer has no value for this key on any "
                "candidate. Vendor-reported values may exist but did not satisfy the "
                "requested value; try vendor_filters or a broader value."
            ),
        )
    if base_total > 0 and missing_spec >= base_total:
        return _diagnosis(
            "filter_key_not_extracted",
            "warning",
            filters=norm_filters,
            category=category,
            missing_spec=missing_spec,
            base_matches=base_total,
            hint=(
                "All matching products lack this attribute in the catalog. "
                "This is an extraction gap, not evidence that the part does not exist; "
                "try search_components without the filter or use a text query."
            ),
        )
    if norm_filters and individual_counts and all(c > 0 for c in individual_counts):
        return _diagnosis(
            "no_matching_combination",
            "info",
            filters=norm_filters,
            category=category,
            per_key_matches=individual_counts,
            hint="Each filter is valid on its own, but no product satisfies all of them at once.",
        )
    if suggestions:
        return _diagnosis(
            "filter_value_not_present",
            "warning",
            filters=norm_filters,
            category=category,
            suggested_value=suggestions[0].get("value"),
            suggestions=suggestions[:5],
            missing_spec=missing_spec,
            base_matches=base_total,
        )
    if norm_filters and len(norm_filters) > 1:
        return _diagnosis(
            "no_matching_combination",
            "info",
            filters=norm_filters,
            category=category,
            per_key_matches=individual_counts or [],
            hint="Each filter is valid on its own, but no product satisfies all of them at once.",
        )
    if filter_recall and filter_recall.get("missing_spec"):
        return _diagnosis(
            "filter_recall_gap",
            "warning",
            filters=norm_filters,
            category=category,
            missing_spec=filter_recall["missing_spec"],
            base_matches=filter_recall["base_matches"],
        )
    if norm_filters:
        return _diagnosis(
            "filter_value_not_present",
            "warning",
            filters=norm_filters,
            category=category,
            missing_spec=missing_spec,
            base_matches=base_total,
        )
    if query_original:
        return _diagnosis(
            "no_catalog_match",
            "info",
            query=query_original,
            filters=norm_filters,
            category=category,
        )
    return _diagnosis("no_catalog_match", "info", filters=norm_filters, category=category)


def _vendor_coverage(
    conn: sqlite3.Connection,
    vendor_meta: Dict[str, Dict[str, Any]],
    category: Optional[str],
    category_total: int,
    total_products: int,
) -> Dict[str, Any]:
    coverage: Dict[str, Any] = {}
    for key, meta in (vendor_meta or {}).items():
        global_count = int(meta.get("global_count") or meta.get("count") or 0)
        coverage[key] = {
            "catalog": {
                "present": global_count,
                "of": total_products,
                "pct": round(global_count / max(total_products, 1) * 100, 2),
            },
            "tier": "vendor",
        }
        if category:
            row = conn.execute(
                "SELECT filled_count FROM vendor_spec_stats WHERE category = ? AND key = ?;",
                (category, key),
            ).fetchone()
            filled = int(row[0]) if row and row[0] is not None else 0
            coverage[key]["category"] = {
                "present": filled,
                "of": category_total,
                "pct": round(filled / max(category_total, 1) * 100, 2) if category_total else 0.0,
            }
    return coverage


def _result_diagnosis(
    query_plan: Optional[QueryPlan],
    filter_recall: Optional[Dict[str, Any]],
    norm_filters: Dict[str, Any],
    category: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Non-empty result diagnosis: relaxed query, or partial filter recall."""
    if query_plan is not None and query_plan.mode in ("relaxed", "corrected_relaxed"):
        return _diagnosis(
            "query_relaxed",
            "warning",
            query=query_plan.original,
            query_effective=query_plan.effective_query,
            dropped_tokens=list(query_plan.dropped_tokens),
            match_mode=query_plan.match_mode,
        )
    if filter_recall and filter_recall.get("missing_spec"):
        return _diagnosis(
            "filter_recall_gap",
            "warning",
            filters=norm_filters,
            category=category,
            missing_spec=filter_recall["missing_spec"],
            base_matches=filter_recall["base_matches"],
        )
    return None


def _parse_and_validate_filters(
    filters: Optional[Dict[str, Any]],
    advertised_specs: Dict[str, Dict[str, Any]],
    category: Optional[str] = None,
    coercions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[ToolResponseEnvelope]]:
    """Validates filter keys and operators, rejecting deprecated string range forms."""
    normalized, error = parse_and_validate_filters(
        filters, advertised_specs, category=category, coercions=coercions
    )
    if error is None:
        return normalized, None
    return None, make_error(
        error_code=error["error_code"],
        message=error["message"],
        valid_keys=error.get("valid_keys"),
        suggestions=error.get("suggestions"),
        details=error.get("details"),
        diagnosis={
            "reason": error["error_code"],
            "severity": "error",
            **(error.get("details") or {}),
        },
    )


def _route_filter_aliases(
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
            routed[target] = value
            applied.append({"group": "voltage", "from": key, "to": target})
        else:
            routed[key] = value
    return routed, applied


GLOBAL_VENDOR_KEYS = {
    "manufacturer", "brand", "operating_temperature", "mounting_type",
    "material", "color", "colour", "weight",
}


def _split_filter_tiers(
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
        applicable_to_category = (not category) or (bool(category_keys) and key in category_keys)
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


def _load_vendor_filter_meta(
    conn: sqlite3.Connection,
    vendor_filters: Dict[str, Any],
    category: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    keys = [normalize_vendor_key(k) for k in (vendor_filters or {})]
    if not keys:
        return {}
    return load_vendor_specs(conn, category=category, min_count=1, keys=keys)


def _parse_vendor_filters(
    filters: Optional[Dict[str, Any]],
    vendor_specs: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[ToolResponseEnvelope]]:
    normalized, error = parse_and_validate_filters(filters, vendor_specs)
    if error is None:
        return normalized, None
    details = dict(error.get("details") or {})
    details.setdefault("tier", "vendor")
    return None, make_error(
        error_code=error["error_code"],
        message=error["message"],
        valid_keys=error.get("valid_keys"),
        suggestions=error.get("suggestions"),
        details=details,
        diagnosis={
            "reason": error["error_code"],
            "severity": "error",
            "tier": "vendor",
            **details,
        },
    )


def _expand_unscoped_aliases(
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
        if existing is None:
            alias_filters.append({"group": group, "keys": keys, "value": value})
        else:
            # Both aliases were supplied: union their values when possible.
            old = existing["value"]
            if old == value:
                pass
            elif isinstance(old, dict) or isinstance(value, dict):
                existing["value"] = {"in": [old, value]}
            else:
                existing["value"] = {"in": [old, value]}
        disclosed[group] = keys
    return expanded, alias_filters, disclosed


def _coercion_payload(coercions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicates coercion notices for filters_applied."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in coercions or []:
        marker = (str(item.get("key")), str(item.get("from")), str(item.get("to")))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


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


def _resolve_category_or_error(
    conn: sqlite3.Connection,
    category: Optional[str],
) -> Tuple[Optional[str], Optional[ToolResponseEnvelope]]:
    """
    Resolves a category name (case/plural/typo tolerant) or returns a structured error.

    The previous implementation did an exact lower-case membership check and then
    suggested the first ten categories alphabetically, so 'motors' failed and
    'motr' never suggested 'motor'.
    """
    if category is None or not str(category).strip():
        return None, None

    valid_categories = _valid_categories(conn)
    resolved, suggestions = resolve_category(category, valid_categories)
    if resolved is not None:
        return resolved, None

    return None, make_error(
        error_code="unknown_category",
        message=(
            f"Component category '{category}' is not present in the catalog. "
            f"Closest valid categories: {suggestions}."
        ),
        suggestions=[{"category": c} for c in suggestions],
        details={"valid_categories": valid_categories},
        diagnosis={
            "reason": "unknown_category",
            "provided_category": str(category),
            "nearest_categories": suggestions,
        },
    )


def _validate_price_max(price_max: Optional[float]) -> Tuple[Optional[float], Optional[ToolResponseEnvelope]]:
    """Rejects negative / non-finite budgets instead of silently turning them into 0 matches."""
    if price_max is None:
        return None, None
    try:
        value = float(price_max)
    except (TypeError, ValueError):
        return None, make_error(
            error_code="invalid_filter_value",
            message=f"price_max must be a number in INR; got {price_max!r}.",
            suggestions=[{"price_max": 500}],
            details={"key": "price_max", "provided_value": price_max},
        )
    if not math.isfinite(value) or value < 0:
        return None, make_error(
            error_code="invalid_filter_value",
            message=f"price_max must be a finite number >= 0; got {price_max!r}.",
            suggestions=[{"price_max": 500}],
            details={"key": "price_max", "provided_value": price_max, "expected": ">= 0"},
        )
    return value, None


def _category_filter_support(conn: sqlite3.Connection, category: Optional[str]) -> str:
    """'none' | 'partial' for the requested category's filterable attributes."""
    if not category:
        return "unknown"
    if _category_spec_keys(conn, category):
        return "partial"
    try:
        if vendor_category_keys(conn, category):
            return "partial"
    except sqlite3.Error:
        pass
    return "none"


def _unsupported_query_error(query: Optional[str]) -> Optional[ToolResponseEnvelope]:
    """Rejects FTS5 boolean/phrase syntax instead of silently rewriting it."""
    syntax = detect_unsupported_syntax(query)
    if not syntax:
        return None
    return make_error(
        error_code="unsupported_query_syntax",
        message=syntax["message"],
        suggestions=[
            {"query": "esp32"},
            {"query": "esp8266"},
            {"hint": "Run one query per alternative, or use filters/category to narrow."},
        ],
        details={
            "operators": syntax["operators"],
            "characters": syntax["characters"],
        },
        diagnosis={
            "reason": "unsupported_query_syntax",
            "operators": syntax["operators"],
            "characters": syntax["characters"],
        },
    )


def _query_status(plan: Optional[QueryPlan]) -> str:
    """Coarse search-mode label exposed to agents."""
    if plan is None:
        return "filters_only"
    if plan.mode in ("strict", "corrected"):
        return "exact"
    if plan.mode in ("relaxed", "corrected_relaxed"):
        return "relaxed" if plan.match_mode == "all_terms" else "low_confidence"
    if plan.mode == "unresolved":
        return "unresolved"
    return plan.mode


def _scoped_query_warning(
    warnings: List[Dict[str, Any]],
    total_matches: int,
) -> None:
    """Makes query_narrowed report the post-category-scope count, not the pre-scope one."""
    for warning in warnings:
        if warning.get("code") != "query_narrowed":
            continue
        warning["total_matches"] = total_matches
        warning["matched_all_terms"] = total_matches
        warning["message"] = (
            f"Only {total_matches} listing(s) matched all query terms in this scope, but "
            f"{warning.get('head_matches')} match '{warning.get('head_token')}' alone. "
            "This may be over-narrow: drop a modifier or add spec filters to narrow deliberately."
        )


def _category_inference_warning(
    category: Optional[str],
    category_source: Optional[str],
    scoped_total: int,
    unscoped_total: int,
) -> Optional[Dict[str, Any]]:
    if not category or category_source != "query_head" or scoped_total == unscoped_total:
        return None
    return {
        "code": "category_inferred",
        "message": (
            f"The query head implied category '{category}', which changed the result count "
            f"from {unscoped_total} to {scoped_total}. Pass category explicitly to override."
        ),
        "category": category,
        "total_with_category": scoped_total,
        "total_without_category": unscoped_total,
    }


def _match_ratio(query: str, title: str) -> float:
    """Fraction of content query tokens present in a title (prefix aware)."""
    try:
        return float(query_title_coverage(query, title))
    except Exception:  # noqa: BLE001 - diagnostics must never break a search
        return 0.0


def _price_basis_fields(title: str, price: float) -> Dict[str, Any]:
    """Pack quantity / unit price / quote flag for one listing."""
    quantity = extract_pack_quantity(title)
    is_quote = float(price) <= 0
    unit_price = None
    if quantity and quantity > 1 and not is_quote:
        unit_price = round(float(price) / quantity, 4)
    return {
        "price_is_quote": is_quote,
        "pack_quantity": quantity,
        "unit_price": unit_price,
        "price_basis": (
            "per_pack" if quantity and quantity > 1 else ("quote" if is_quote else "per_unit")
        ),
        "price_comparable": bool(not is_quote and (quantity is None or quantity == 1)),
    }


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
    offset: int = 0,
    head_token: Optional[str] = None,
    alias_filters: Optional[List[Dict[str, Any]]] = None,
    zero_price_only: bool = False,
    hybrid_vendor: bool = False,
    vendor_filters: Optional[Dict[str, Any]] = None,
    vendor_specs: Optional[Dict[str, Dict[str, Any]]] = None,
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

    if hybrid_vendor and normalized_filters:
        hybrid_where, hybrid_params = build_hybrid_spec_clauses(
            normalized_filters, advertised_specs
        )
        where_clauses.extend(hybrid_where)
        where_params.extend(hybrid_params)
    else:
        spec_joins, spec_join_params, spec_where, spec_where_params = build_spec_clauses(
            normalized_filters, advertised_specs
        )
        joins.extend(spec_joins)
        join_params.extend(spec_join_params)
        where_clauses.extend(spec_where)
        where_params.extend(spec_where_params)

    if vendor_filters and vendor_specs:
        vendor_joins, vendor_join_params, vendor_where, vendor_where_params = (
            build_vendor_spec_clauses(vendor_filters, vendor_specs)
        )
        joins.extend(vendor_joins)
        join_params.extend(vendor_join_params)
        where_clauses.extend(vendor_where)
        where_params.extend(vendor_where_params)

    alias_where, alias_params = build_alias_clauses(alias_filters, advertised_specs)
    if alias_where:
        where_clauses.extend(alias_where)
        where_params.extend(alias_params)

    if zero_price_only:
        where_clauses.append("p.price <= 0")

    if price_max is not None:
        where_clauses.append("p.price <= ?")
        where_params.append(float(price_max))

    if sort in ("price_asc", "price_desc"):
        # Price 0 rows are quote/fabrication services, not components. Sorting
        # them first makes "cheapest X" return a service, not the part.
        where_clauses.append("p.price > 0")

    if head_token and sort == "price_asc":
        # A compatibility clause ("... for Arduino ESP32") is not the product.
        # For a single-term query, require the term to appear in the first ~30
        # characters rather than anywhere in the title.
        where_clauses.append("instr(lower(p.title), lower(?)) BETWEEN 1 AND 30")
        where_params.append(head_token)

    if in_stock_only:
        where_clauses.append("p.available = 1")

    if category:
        # Always use the canonical catalog category. `spec_category` is the
        # extraction scope, and it contains accessories filed by vendors under
        # "Battery Accessories" (a 9V snap connector is not a battery). Spec
        # joins still require extracted values when filters are supplied.
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


def _spec_completeness_bonus(
    conn: sqlite3.Connection,
    product_ids: List[int],
) -> Dict[int, float]:
    """
    Small engineering-quality tie-break: prefer products with more extracted
    specifications. Mandatory filters already decided compatibility; this only
    orders equally compatible candidates.
    """
    if not product_ids:
        return {}
    id_ph = ", ".join(["?"] * len(product_ids))
    promoted = {
        int(pid): int(count)
        for pid, count in conn.execute(
            f"SELECT product_id, COUNT(1) FROM product_specs "
            f"WHERE product_id IN ({id_ph}) GROUP BY product_id;",
            product_ids,
        ).fetchall()
    }
    vendor = {
        int(pid): int(count)
        for pid, count in conn.execute(
            f"SELECT product_id, COUNT(1) FROM product_vendor_specs "
            f"WHERE product_id IN ({id_ph}) GROUP BY product_id;",
            product_ids,
        ).fetchall()
    }
    return {
        pid: 2.0 * min(promoted.get(pid, 0), 10) + 0.2 * min(vendor.get(pid, 0), 25)
        for pid in product_ids
    }


def _category_spec_bonus(
    conn: sqlite3.Connection,
    product_ids: List[int],
    spec_keys: Optional[List[str]],
) -> Dict[int, float]:
    """
    Reproduces the category spec-coverage ORDER BY as a score bonus.

    The SQL path puts products carrying more of the category's extracted specs
    first. When the shared Python ranker is used instead, the same signal is
    applied as a large per-spec bonus so it still dominates raw BM25.
    """
    if not product_ids or not spec_keys:
        return {}
    id_ph = ", ".join(["?"] * len(product_ids))
    key_ph = ", ".join(["?"] * len(spec_keys))
    rows = conn.execute(
        f"SELECT product_id, COUNT(1) FROM product_specs "
        f"WHERE product_id IN ({id_ph}) AND key IN ({key_ph}) "
        f"GROUP BY product_id;",
        list(product_ids) + list(spec_keys),
    ).fetchall()
    return {int(pid): 100.0 * int(count) for pid, count in rows}

# =====================================================================
# 6 AGENT-FIRST TOOLS
# =====================================================================

@mcp.tool()
def describe_catalog(summary: bool = False) -> ToolResponseEnvelope:
    """
    Return comprehensive catalog-level metadata, total product counts, vendor distributions, category counts, queryable spec keys with coverage percentages, and data freshness timestamps.

    Recommended Call Order: Call describe_catalog first before running any searches to inspect available attributes and catalog size.

    Using The Output: counts.products_by_spec_category lists the values accepted by the
    `category` parameter of search_components/count_components (e.g. "battery", "motor",
    "relay"). spec_keys lists every filterable key with coverage; `values` is null for
    open-ended enums (e.g. battery voltage), where describe_spec shows observed distributions.

    Parameter Syntax: summary: bool (default: false). summary=true returns a compact
    columnar key catalog under ~6 KB instead of the full taxonomy/vendor payload.

    Example Invocation: describe_catalog(summary=True)

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
        spec_index_updated_at = cur.execute(
            "SELECT MAX(updated_at) FROM spec_stats_global;"
        ).fetchone()[0]
        spec_keys_list = []
        for k, s in catalog_specs.items():
            cov_pct = round(s["count"] / max(total_products, 1) * 100, 2)
            spec_keys_list.append({
                "key": k,
                "display_name": s["display_name"],
                "type": s["type"],
                "unit": s["unit"],
                "coverage_pct": cov_pct,
                "backfill_pct": cov_pct,
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
                "filter_support": "partial" if cat.attributes else "none",
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

        vendor_spec_stats = db.description_stats()
        vendor_keys = load_vendor_specs(conn, min_count=5)
        vendor_key_payload = [
            {
                "key": k,
                "tier": "vendor",
                "type": v["type"],
                "unit": v["unit"],
                "count": v["count"],
                "range": (
                    {"min": v.get("min"), "max": v.get("max")}
                    if v["type"] != "enum" else None
                ),
            }
            for k, v in list(vendor_keys.items())[:100]
        ]
        if summary:
            # Columnar payload keeps the documented catalog -> count -> search
            # preamble under ~6 KB while retaining key/type/unit/coverage/values.
            columns = [
                "key", "type", "unit", "coverage_pct", "backfill_pct",
                "values", "category_coverage",
            ]
            summary_rows = []
            for k, s in catalog_specs.items():
                cov_pct = round(s["count"] / max(total_products, 1) * 100, 2)
                values = None
                if s["type"] == "enum":
                    values = s["enum_values"]
                    if not values:
                        values = [v for v, _ in sorted(
                            (s.get("value_counts") or {}).items(), key=lambda kv: -kv[1]
                        )][:20]
                cc = [c for c in category_coverage.get(k, []) if c.get("filled")][:1]
                cc_payload = (
                    {c["category"]: [c["filled"], c["applicable"]] for c in cc}
                    if cc else None
                )
                summary_rows.append([
                    k, s["type"], s["unit"], cov_pct, cov_pct, values, cc_payload,
                ])
            data = {
                "total_products": total_products,
                "spec_columns": columns,
                "spec_keys": summary_rows,
                "aliases": alias_catalog_payload(),
                "spec_index_updated_at": spec_index_updated_at,
                "vendor_spec_key_count": len(vendor_keys),
                "summary": True,
            }
            return make_response(data=data, total_matches=total_products, returned=1)

        data = {
            "total_products": total_products,
            "counts": {
                "products": total_products,
                "spec_rows": cur.execute("SELECT COUNT(1) FROM product_specs;").fetchone()[0],
                "spec_keys_advertised": len(spec_keys_list),
                "vendor_attribute_rows": vendor_spec_stats["attribute_rows"],
                "products_with_vendor_attributes": vendor_spec_stats["products_with_attributes"],
                "products_with_descriptions": vendor_spec_stats["descriptions"],
                "vendor_spec_rows": cur.execute(
                    "SELECT COUNT(1) FROM product_vendor_specs;"
                ).fetchone()[0],
                "vendor_spec_keys": len(vendor_keys),
            },
            "products_by_vendor": products_by_vendor,
            "products_by_spec_category": products_by_spec_category,
            "taxonomy": taxonomy_payload,
            "spec_keys": spec_keys_list,
            "aliases": alias_catalog_payload(),
            "vendor_spec_key_count": len(vendor_keys),
            "vendor_spec_keys": vendor_key_payload,
            "currency": "INR",
            "freshness": {
                "oldest_crawl": oldest_crawl,
                "newest_crawl": newest_crawl,
                "spec_index_updated_at": spec_index_updated_at,
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
def describe_spec(key: str, vendor: bool = False) -> ToolResponseEnvelope:
    """
    Return detailed metadata, value distributions, ranges, and coverage diagnostics for a single spec attribute.

    Recommended Call Order: Call after describe_catalog to introspect a specific spec key before building filters.

    Using The Output: `coverage_pct` is global (all products, most of which are unclassified);
    `category_coverage` breaks the same key down per component category (e.g. voltage is
    extracted on ~61% of batteries but only ~22% of motors), which is the number to trust
    when deciding whether a filter is meaningful for a category.

    Parameter Syntax: key: str (e.g. "rpm", "voltage", "cells", "capacity_mah");
    vendor: bool (default false; force the normalized vendor layer, including keys
    that are not in the promoted taxonomy).

    Example Invocation: describe_spec(key="rpm")

    Behavior When No Matches: If key is not in the taxonomy, returns ok:false with error_code='unknown_filter_key' and a list of valid keys.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM products;")
        total_products = cur.fetchone()[0]

        catalog_specs = _load_catalog_specs(conn)
        vendor_key = normalize_vendor_key(key)
        vendor_meta = load_vendor_specs(
            conn, min_count=1, keys=[vendor_key]
        ).get(vendor_key)
        if vendor or (key not in catalog_specs and vendor_meta):
            if not vendor_meta:
                all_vendor = load_vendor_specs(conn, min_count=5)
                return make_error(
                    error_code="unknown_filter_key",
                    message=f"Vendor spec key '{key}' is not in the normalized vendor layer.",
                    valid_keys=sorted(catalog_specs.keys()),
                    suggestions=[
                        {"key": k, "tier": "vendor"}
                        for k in list(all_vendor.keys())[:5]
                    ],
                    details={"tier": "vendor"},
                )
            counts = vendor_key_value_counts(conn, vendor_key, limit=30)
            category_rows = conn.execute(
                "SELECT category, filled_count, applicable_count FROM vendor_spec_stats "
                "WHERE key = ? ORDER BY filled_count DESC LIMIT 12;",
                (vendor_key,),
            ).fetchall()
            data = {
                "key": vendor_key,
                "tier": "vendor",
                "status": "vendor_reported",
                "display_name": str(vendor_key).replace("_", " ").title(),
                "type": vendor_meta["type"],
                "unit": vendor_meta["unit"],
                "coverage_pct": round(
                    int(vendor_meta.get("global_count") or 0) / max(total_products, 1) * 100, 2
                ),
                "coverage_count": int(vendor_meta.get("global_count") or 0),
                "category_coverage": [
                    {
                        "category": str(row[0]),
                        "filled": int(row[1] or 0),
                        "applicable": int(row[2] or 0),
                        "pct": round(
                            int(row[1] or 0) / max(int(row[2] or 0), 1) * 100, 1
                        ),
                    }
                    for row in category_rows
                ],
                "values": counts if vendor_meta["type"] == "enum" else None,
                "range": (
                    {"min": vendor_meta.get("min"), "max": vendor_meta.get("max")}
                    if vendor_meta["type"] != "enum" else None
                ),
                "sample_raw_strings": list(counts.keys())[:5],
                "note": (
                    "Vendor-reported attribute normalized from product descriptions. "
                    "Lower confidence than a taxonomy-promoted specification."
                ),
            }
            return make_response(
                data=data,
                total_matches=int(vendor_meta.get("global_count") or 0),
                returned=1,
                warnings=[{
                    "code": "vendor_spec_tier",
                    "message": (
                        "This key is vendor-reported, not taxonomy-promoted. "
                        "Use it through filters/vendor_filters; values may be incomplete."
                    ),
                }],
            )
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
    category: Optional[str] = None,
    vendor_filters: Optional[Dict[str, Any]] = None,
    include_vendor_specs: bool = True,
) -> ToolResponseEnvelope:
    """
    Count matching components and inspect filter coverage without fetching product rows.

    Recommended Call Order: Call after describe_catalog and before search_components to size query results and verify filter coverage.

    Parameter Syntax:
      - filters: dict (supports scalar equality or operators in, gte, lte, gt, lt, eq, tolerance;
        tolerance is a fraction of eq: 0.05 means +/-5%, must be > 0 and <= 1. Numeric keys accept
        engineering strings in the declared unit, e.g. resistance_ohm="10k", capacitance_uf="100nF".
        Default: null)
      - query: str (FTS text search string, default: null)
      - price_max: float (maximum price in INR, must be >= 0; default: null)
      - in_stock_only: bool (default: false)
      - category: str (restrict to a catalog category such as 'battery', 'motor', 'resistors';
        case-insensitive, plural tolerant; see describe_catalog data.taxonomy, default: null)
      - vendor_filters: dict (query normalized vendor attributes that were never
        promoted, e.g. {"max_discharge_current_a": {"gte": 20}}; default: null)
      - include_vendor_specs: bool (default: true; fall back to normalized vendor
        values only where the promoted value is missing)

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

        price_max_norm, price_err = _validate_price_max(price_max)
        if price_err:
            return price_err

        catalog_specs = _load_catalog_specs(conn)
        category_norm, category_err = _resolve_category_or_error(conn, category)
        if category_err:
            return category_err
        category_keys = _category_spec_keys(conn, category_norm) if category_norm else []
        coercions: List[Dict[str, Any]] = []
        promoted_input, auto_vendor, tier_routes = _split_filter_tiers(
            filters, catalog_specs, conn, category_norm, category_keys,
            allow_vendor=include_vendor_specs,
        )
        explicit_vendor = {
            normalize_vendor_key(k): v for k, v in (vendor_filters or {}).items()
        }
        requested_vendor_input = {**auto_vendor, **explicit_vendor}
        vendor_meta = _load_vendor_filter_meta(
            conn, requested_vendor_input, category_norm
        )
        missing_vendor_keys = [
            k for k in requested_vendor_input if k not in vendor_meta
        ]
        if missing_vendor_keys:
            return make_error(
                error_code="unknown_filter_key",
                message=(
                    f"Unknown vendor filter key(s) {missing_vendor_keys}. "
                    "Use describe_spec(key=..., vendor=True) to inspect vendor attributes."
                ),
                valid_keys=sorted(vendor_meta.keys())[:50],
                suggestions=[{"key": k} for k in missing_vendor_keys[:3]],
                details={"tier": "vendor", "provided_keys": missing_vendor_keys},
            )
        norm_vendor_filters, vendor_err = _parse_vendor_filters(
            requested_vendor_input, vendor_meta
        )
        if vendor_err:
            return vendor_err
        norm_vendor_filters = norm_vendor_filters or {}
        routed_filters, alias_applied = _route_filter_aliases(
            promoted_input, catalog_specs, category_norm, category_keys
        )
        norm_filters, err = _parse_and_validate_filters(
            routed_filters, catalog_specs, category=category_norm, coercions=coercions
        )
        if err:
            return err
        requested_filters = dict(norm_filters or {})
        if category_norm:
            alias_filters: List[Dict[str, Any]] = []
            alias_disclosed: Dict[str, List[str]] = {}
        else:
            norm_filters, alias_filters, alias_disclosed = _expand_unscoped_aliases(
                norm_filters or {}, catalog_specs
            )
        scope_error = validate_filter_scope(norm_filters or {}, category_norm, category_keys)
        if scope_error:
            return make_error(
                error_code=scope_error["error_code"],
                message=scope_error["message"],
                suggestions=scope_error.get("suggestions"),
                details=scope_error.get("details"),
                diagnosis={"reason": scope_error["error_code"], **(scope_error.get("details") or {})},
            )

        require_spec_scope = bool(category_keys)

        if query is not None and not str(query).strip():
            return make_error(
                error_code="empty_query",
                message=(
                    "query was provided but is empty/whitespace. Omit it to count without a "
                    "full-text filter, or pass actual search terms."
                ),
                suggestions=[{"query": "motor"}],
            )

        syntax_error = _unsupported_query_error(query)
        if syntax_error:
            return syntax_error

        query_original = (query or "").strip() or None
        price_max_source = "argument" if price_max_norm is not None else None
        category_source = "argument" if category_norm else None
        if query is not None:
            cleaned_query, _price_min, query_price_max = parse_price_constraints(str(query))
            if price_max_norm is None and query_price_max is not None:
                price_max_norm = query_price_max
                price_max_source = "query"
            query = cleaned_query or None

        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = db.resolve_query_plan(query, conn=conn)

        # Natural-language queries often name the category in the head term
        # ("battery to power an arduino"). Infer the category only when the
        # caller did not supply one and no explicit spec filters are in play,
        # so inferred scoping can never override deliberate constraints.
        if (
            category_norm is None
            and not norm_filters
            and query_plan is not None
            and query_plan.resolved
            and query_plan.head_token
        ):
            inferred_category, _ = resolve_category(
                query_plan.head_token, _valid_categories(conn)
            )
            if inferred_category:
                category_norm = inferred_category
                category_source = "query_head"
                category_keys = _category_spec_keys(conn, category_norm)
                require_spec_scope = bool(category_keys)

        if query_plan is not None and not query_plan.resolved:
            coverage, warnings = _build_coverage_and_warnings(
                catalog_specs,
                total_products,
                requested_filters,
                0,
                category=category_norm,
                category_total=int(cur.execute(
                    "SELECT COUNT(1) FROM products WHERE category_canonical = ?;",
                    (category_norm,),
                ).fetchone()[0]) if category_norm else 0,
                conn=conn,
            )
            warnings.extend(query_plan.warnings)
            suggestions = _enum_filter_suggestions(
                norm_filters, catalog_specs, conn=conn, category=category_norm
            )
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
                    "query_status": "unresolved",
                    "match_mode": query_plan.match_mode,
                },
                total_matches=0,
                returned=0,
                filters_applied={
                    "filters": requested_filters,
                    "query": query_original,
                    "query_effective": query_plan.effective_query,
                    "price_max": price_max_norm,
                    "price_max_source": price_max_source,
                    "in_stock_only": bool(in_stock_only),
                    "category": category_norm,
                    "category_source": category_source,
                    "filter_support": _category_filter_support(conn, category_norm),
                    "filter_scope": "extracted_only" if (norm_filters or alias_filters or norm_vendor_filters) else "all_products",
                    "query_status": "unresolved",
                    "match_mode": query_plan.match_mode,
                    "price_zero_excluded": False,
                },
                coverage=coverage,
                warnings=warnings,
                diagnosis={
                    "reason": "query_unresolved",
                    "severity": "warning",
                    "query": query_original,
                    "query_suggestions": query_plan.suggestions,
                    "dropped_tokens": list(query_plan.dropped_tokens),
                },
            )

        count_sql, params, _, _ = _build_sql_query(
            norm_filters or {},
            catalog_specs,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max_norm,
            in_stock_only=in_stock_only,
            category=category_norm,
            require_spec_scope=require_spec_scope,
            alias_filters=alias_filters,
            hybrid_vendor=bool(include_vendor_specs),
            vendor_filters=norm_vendor_filters,
            vendor_specs=vendor_meta,
        )
        cur.execute(count_sql, params)
        total_count = cur.fetchone()[0]

        category_total = 0
        if category_norm:
            category_total = int(cur.execute(
                "SELECT COUNT(1) FROM products WHERE category_canonical = ?;",
                (category_norm,),
            ).fetchone()[0])
        coverage, warnings = _build_coverage_and_warnings(
            catalog_specs,
            total_products,
            requested_filters,
            total_count,
            category=category_norm,
            category_total=category_total,
            conn=conn,
        )

        filter_recall, recall_warnings = _compute_filter_recall(
            conn,
            catalog_specs,
            norm_filters or {},
            query_plan,
            price_max_norm,
            in_stock_only,
            category_norm,
            _category_spec_keys(conn, category_norm) if category_norm else None,
            require_spec_scope,
            total_count,
            alias_filters=alias_filters,
            vendor_filters=norm_vendor_filters,
            vendor_specs_meta=vendor_meta,
            include_vendor_specs=include_vendor_specs,
        )
        warnings.extend(recall_warnings)

        suggestions: List[Dict[str, Any]] = []
        diagnosis: Optional[Dict[str, Any]] = None
        if total_count == 0:
            individual_counts: List[int] = []
            for only_key, only_value in (requested_filters or {}).items():
                only_sql, only_params, _, _ = _build_sql_query(
                    {only_key: only_value},
                    catalog_specs,
                    fts_tokens=(query_plan.effective_tokens if query_plan else None),
                    fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                    price_max=price_max_norm,
                    in_stock_only=in_stock_only,
                    category=category_norm,
                    require_spec_scope=require_spec_scope,
                )
                cur.execute(only_sql, only_params)
                individual_counts.append(int(cur.fetchone()[0]))
            zero_keys = {
                key: value for (key, value), count in zip(
                    (requested_filters or {}).items(), individual_counts
                ) if count == 0
            }
            if zero_keys:
                suggestions.extend(
                    _enum_filter_suggestions(
                        zero_keys, catalog_specs, conn=conn, category=category_norm
                    )
                )
            diagnosis = _diagnose_empty_result(
                requested_filters,
                category_norm,
                query_original,
                total_count,
                filter_recall["base_matches"] if filter_recall else 0,
                filter_recall["missing_spec"] if filter_recall else 0,
                filter_recall,
                suggestions,
                individual_counts,
                vendor_filters=norm_vendor_filters,
            )
            if suggestions and diagnosis["reason"] == "filter_value_not_present":
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
        else:
            diagnosis = _result_diagnosis(query_plan, filter_recall, requested_filters, category_norm)

        if query_plan is not None:
            warnings.extend(query_plan.warnings)
            _scoped_query_warning(warnings, total_count)
        if category_source == "query_head" and category_norm:
            unscoped_sql, unscoped_params, _, _ = _build_sql_query(
                norm_filters or {},
                catalog_specs,
                fts_tokens=(query_plan.effective_tokens if query_plan else None),
                fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                price_max=price_max_norm,
                in_stock_only=in_stock_only,
                category=None,
                require_spec_scope=False,
                alias_filters=alias_filters,
                hybrid_vendor=bool(include_vendor_specs),
                vendor_filters=norm_vendor_filters,
                vendor_specs=vendor_meta,
            )
            cur.execute(unscoped_sql, unscoped_params)
            unscoped_total = int(cur.fetchone()[0])
            inferred_warning = _category_inference_warning(
                category_norm, category_source, total_count, unscoped_total
            )
            if inferred_warning:
                warnings.append(inferred_warning)

        filters_applied = {
            "filters": requested_filters,
            "vendor_filters": norm_vendor_filters or {},
            "query": query_original,
            "price_max": price_max_norm,
            "price_max_source": price_max_source,
            "in_stock_only": bool(in_stock_only),
            "category": category_norm,
            "category_source": category_source,
            "filter_support": _category_filter_support(conn, category_norm),
            "filter_scope": (
                "extracted_only"
                if (norm_filters or alias_filters or norm_vendor_filters)
                else "all_products"
            ),
            "filter_mode": "hybrid" if include_vendor_specs else "promoted",
            "include_vendor_specs": bool(include_vendor_specs),
            "query_status": _query_status(query_plan),
            "match_mode": query_plan.match_mode if query_plan else "all_terms",
            "price_zero_excluded": False,
            "price_zero_policy": "count_includes_zero_price_rows; price-sorted search excludes them",
        }
        if tier_routes:
            filters_applied["filter_routed_to_vendor"] = tier_routes
        if tier_routes:
            filters_applied["filter_routed_to_vendor"] = tier_routes
        if alias_applied:
            filters_applied["filter_alias_applied"] = alias_applied
        if alias_disclosed:
            per_key_counts: Dict[str, int] = {}
            for entry in alias_filters:
                for key in entry.get("keys") or []:
                    key_sql, key_params, _, _ = _build_sql_query(
                        {key: entry.get("value")},
                        catalog_specs,
                        fts_tokens=(query_plan.effective_tokens if query_plan else None),
                        fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                        price_max=price_max_norm,
                        in_stock_only=in_stock_only,
                        category=None,
                        require_spec_scope=False,
                    )
                    cur.execute(key_sql, key_params)
                    per_key_counts[key] = int(cur.fetchone()[0])
            filters_applied["filter_expanded"] = {
                group: keys for group, keys in alias_disclosed.items()
            }
            filters_applied["filter_expanded"]["per_key_counts"] = per_key_counts
        coerced = _coercion_payload(coercions)
        if coerced:
            filters_applied["filter_value_coerced"] = coerced
        if query_plan is not None and query_plan.resolved:
            original = (query_original or "")
            if query_plan.effective_query.lower() != original.lower():
                filters_applied["query_effective"] = query_plan.effective_query
            if query_plan.dropped_tokens:
                filters_applied["query_tokens_dropped"] = list(query_plan.dropped_tokens)

        vendor_cov = _vendor_coverage(
            conn, vendor_meta, category_norm, category_total, total_products
        )
        return make_response(
            data={
                "count": total_count,
                "suggestions": suggestions,
                "query_status": _query_status(query_plan),
                "match_mode": query_plan.match_mode if query_plan else "all_terms",
                "query_tokens_dropped": list(query_plan.dropped_tokens) if query_plan else [],
                "filter_recall": filter_recall,
                "filter_provenance": {
                    "mode": (filter_recall or {}).get("mode"),
                    "promoted_matches": (filter_recall or {}).get("promoted_matches"),
                    "vendor_fallback_matches": (filter_recall or {}).get("vendor_fallback_matches"),
                    "missing_spec": (filter_recall or {}).get("missing_spec"),
                },
                "vendor_coverage": vendor_cov,
                "vendor_filter_count": len(norm_vendor_filters or {}),
                "zero_price_rows_in_catalog": int(cur.execute(
                    "SELECT COUNT(1) FROM products WHERE price <= 0;"
                ).fetchone()[0]),
            },
            total_matches=total_count,
            returned=0,
            filters_applied=filters_applied,
            coverage=coverage,
            warnings=warnings,
            diagnosis=diagnosis,
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

    Spec Fields: 'specs' is the flat {key: value} map (same shape as search results);
    'spec_details' adds value_num/type/confidence/evidence; 'extracted_specs' is retained as an
    alias. 'spec_category' is the extraction scope and 'spec_extraction_scope' is
    "category_scoped", "category_no_attributes" or "unclassified". For unclassified or
    attribute-less categories, 'specs_not_extracted' is empty (never every catalog key), and
    'raw_attributes' / 'description' are the authoritative details.

    'vendor_specs' is the much larger free-form attribute set parsed from the vendor's
    description table/list (tolerance, package, dielectric, dimensions, ...). It is the
    layer to use for fit questions when the taxonomy has no filter for that field.

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
                **_price_basis_fields(o[3], float(o[4])),
                "available": bool(o[5]),
                "stock_status": "in_stock" if o[5] else "out_of_stock",
                "product_url": o[6],
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
        # Scope "not extracted" to the product's category. An unclassified product
        # has no applicable taxonomy keys at all; listing all nine catalog keys
        # made an Arduino Uno look like it was missing battery chemistry.
        if spec_category:
            applicable_keys = _applicable_spec_keys(conn).get(spec_category, [])
            spec_extraction_scope = (
                "category_scoped" if applicable_keys else "category_no_attributes"
            )
            specs_not_extracted = [k for k in applicable_keys if k not in extracted_specs]
        else:
            applicable_keys = []
            spec_extraction_scope = "unclassified"
            specs_not_extracted = []

        stored_description = db.get_product_description(product_id)
        if stored_description and stored_description.get("description"):
            description = stored_description["description"]
            description_source = f"vendor_page:{stored_description.get('source') or 'stored'}"
        elif raw_meta.get("body"):
            description, description_source = raw_meta["body"], "vendor_page"
        elif raw_meta.get("short_description"):
            description, description_source = raw_meta["short_description"], "vendor_page_short"
        else:
            # Never present the title as if it were a description.
            description, description_source = p.get("title"), "title_fallback"

        vendor_specs = db.get_product_attributes(product_id)
        normalized_vendor_specs: Dict[str, Any] = {}
        normalized_vendor_details: Dict[str, Any] = {}
        for vr in cur.execute(
            "SELECT key, value, value_num, value_type, unit, raw_key, confidence, source "
            "FROM product_vendor_specs WHERE product_id = ? ORDER BY key;",
            (product_id,),
        ).fetchall():
            normalized_vendor_specs[str(vr[0])] = vr[1]
            normalized_vendor_details[str(vr[0])] = {
                "value": vr[1],
                "value_num": vr[2],
                "type": vr[3],
                "unit": vr[4],
                "raw_key": vr[5],
                "confidence": vr[6],
                "source": vr[7],
                "tier": "vendor",
            }

        data = {
            "product_id": p["id"],
            "title": p["title"],
            "normalized_title": p.get("normalized_title"),
            "mpn": p.get("mpn"),
            "sku": p.get("sku"),
            "spec_category": spec_category,
            "spec_extraction_scope": spec_extraction_scope,
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
            "specs": {key: str(value["value"]) for key, value in extracted_specs.items()},
            "spec_details": extracted_specs,
            "extracted_specs": extracted_specs,
            "vendor_specs": vendor_specs,
            "vendor_spec_count": len(vendor_specs),
            "normalized_vendor_specs": normalized_vendor_specs,
            "normalized_vendor_details": normalized_vendor_details,
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
    category: Optional[str] = None,
    compact: bool = False,
    vendor_filters: Optional[Dict[str, Any]] = None,
    include_vendor_specs: bool = True,
) -> ToolResponseEnvelope:
    """
    Search electronic components by parametric spec filters and/or full-text query with side-by-side vendor offers and cursor pagination.

    Recommended Call Order: Call after sizing the query with count_components to retrieve paginated component comparison cards.

    Parameter Syntax:
      - filters: dict (supports scalar equality or operators in, gte, lte, gt, lt, eq, tolerance;
        tolerance is a fraction of eq: 0.05 means +/-5%, must be > 0 and <= 1. Numeric keys accept
        engineering strings in the declared unit, e.g. resistance_ohm="10k", capacitance_uf="100nF".
        Default: null)
      - query: str (FTS query string, default: null)
      - price_max: float (max price in INR, must be >= 0; default: null)
      - in_stock_only: bool (default: false; listings flag out-of-stock rather than silently omitting)
      - sort: str (one of 'relevance', 'price_asc', 'price_desc', default: 'relevance')
      - limit: int (page size, 1 to 50 inclusive; values >50 are clamped with a warning, <1 is rejected, default: 10)
      - cursor: str (opaque cursor from previous page's next_cursor; bound to the query, default: null)
      - refresh: bool (default: true; verify price/stock live before returning, TTL-aware)
      - force_refresh: bool (default: false; bypass the refresh TTL to re-verify now)
      - category: str (restrict to a catalog category, e.g. 'battery', 'motor', 'resistors';
        case-insensitive, plural tolerant; see describe_catalog data.taxonomy, default: null)
      - compact: bool (default: false; return only product_id/title/vendor/price/currency/
        stock_status/mpn/specs/product_url per row, keeping 50 rows under 25 KB)

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
    invalid_filter_value, filter_not_applicable_to_category, empty_query, invalid_cursor, stale_cursor.
    """
    sort_intent = detect_sort_intent(query)
    sort_inferred = bool(sort_intent and sort == "relevance")
    if sort_inferred:
        # "cheapest relay" is a price request, not a relevance request. Applying
        # relevance order here produced top hits up to 162x above the cheapest item.
        sort = sort_intent

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
    if sort_inferred:
        warnings.append({
            "code": "sort_inferred",
            "message": (
                "The query asks for the cheapest/lowest-priced item, so results are sorted by "
                "price ascending. Pass sort explicitly to override."
            ),
            "sort": sort,
        })
    elif sort_intent:
        warnings.append({
            "code": "superlative_ignored",
            "message": (
                f"The query asks for the cheapest item, but sort={sort!r} was requested explicitly; "
                "the explicit sort wins."
            ),
            "sort": sort,
        })
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

        price_max_norm, price_err = _validate_price_max(price_max)
        if price_err:
            return price_err

        catalog_specs = _load_catalog_specs(conn)
        all_catalog_keys = sorted(catalog_specs.keys())

        category_norm, category_err = _resolve_category_or_error(conn, category)
        if category_err:
            return category_err
        category_keys = _category_spec_keys(conn, category_norm) if category_norm else []
        coercions: List[Dict[str, Any]] = []
        promoted_input, auto_vendor, tier_routes = _split_filter_tiers(
            filters, catalog_specs, conn, category_norm, category_keys,
            allow_vendor=include_vendor_specs,
        )
        explicit_vendor = {
            normalize_vendor_key(k): v for k, v in (vendor_filters or {}).items()
        }
        requested_vendor_input = {**auto_vendor, **explicit_vendor}
        vendor_meta = _load_vendor_filter_meta(
            conn, requested_vendor_input, category_norm
        )
        missing_vendor_keys = [
            k for k in requested_vendor_input if k not in vendor_meta
        ]
        if missing_vendor_keys:
            return make_error(
                error_code="unknown_filter_key",
                message=(
                    f"Unknown vendor filter key(s) {missing_vendor_keys}. "
                    "Use describe_spec(key=..., vendor=True) to inspect vendor attributes."
                ),
                valid_keys=sorted(vendor_meta.keys())[:50],
                suggestions=[{"key": k} for k in missing_vendor_keys[:3]],
                details={"tier": "vendor", "provided_keys": missing_vendor_keys},
            )
        norm_vendor_filters, vendor_err = _parse_vendor_filters(
            requested_vendor_input, vendor_meta
        )
        if vendor_err:
            return vendor_err
        norm_vendor_filters = norm_vendor_filters or {}
        routed_filters, alias_applied = _route_filter_aliases(
            promoted_input, catalog_specs, category_norm, category_keys
        )
        norm_filters, err = _parse_and_validate_filters(
            routed_filters, catalog_specs, category=category_norm, coercions=coercions
        )
        if err:
            return err
        requested_filters = dict(norm_filters or {})
        if category_norm:
            alias_filters: List[Dict[str, Any]] = []
            alias_disclosed: Dict[str, List[str]] = {}
        else:
            norm_filters, alias_filters, alias_disclosed = _expand_unscoped_aliases(
                norm_filters or {}, catalog_specs
            )
        scope_error = validate_filter_scope(norm_filters or {}, category_norm, category_keys)
        if scope_error:
            return make_error(
                error_code=scope_error["error_code"],
                message=scope_error["message"],
                suggestions=scope_error.get("suggestions"),
                details=scope_error.get("details"),
                diagnosis={"reason": scope_error["error_code"], **(scope_error.get("details") or {})},
            )

        require_spec_scope = bool(category_keys)

        syntax_error = _unsupported_query_error(query)
        if syntax_error:
            return syntax_error

        query_original = (query or "").strip() or None
        price_max_source = "argument" if price_max_norm is not None else None
        category_source = "argument" if category_norm else None
        if query is not None:
            cleaned_query, _price_min, query_price_max = parse_price_constraints(str(query))
            if price_max_norm is None and query_price_max is not None:
                price_max_norm = query_price_max
                price_max_source = "query"
            query = cleaned_query or None

        # --- query understanding: stop words, typo correction, AND -> OR relaxation ---
        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = db.resolve_query_plan(query, conn=conn)
        effective_query = query_plan.effective_query if query_plan else (query or "").strip()
        head_token = (
            effective_query
            if query_plan is not None and len(query_plan.effective_tokens) == 1
            else None
        )

        # Infer the component category from the query's head term only when no
        # category and no explicit spec filters were supplied.
        if (
            category_norm is None
            and not norm_filters
            and query_plan is not None
            and query_plan.resolved
            and query_plan.head_token
        ):
            inferred_category, _ = resolve_category(
                query_plan.head_token, _valid_categories(conn)
            )
            if inferred_category:
                category_norm = inferred_category
                category_source = "query_head"
                category_keys = _category_spec_keys(conn, category_norm)
                require_spec_scope = bool(category_keys)

        # --- cursor is bound to the query that produced it ---
        fingerprint = _query_fingerprint(
            requested_filters,
            effective_query,
            price_max_norm,
            in_stock_only,
            sort,
            category_norm,
            vendor_filters=norm_vendor_filters,
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
            "filters": requested_filters,
            "vendor_filters": norm_vendor_filters or {},
            "query": query_original,
            "price_max": price_max_norm,
            "price_max_source": price_max_source,
            "in_stock_only": bool(in_stock_only),
            "category": category_norm,
            "category_source": category_source,
            "filter_support": _category_filter_support(conn, category_norm),
            "filter_mode": "hybrid" if include_vendor_specs else "promoted",
            "include_vendor_specs": bool(include_vendor_specs),
            "sort": sort,
            "sort_source": "query_intent" if sort_inferred else "argument",
            "limit": effective_limit,
            "offset": offset,
            "refresh": bool(refresh),
            "force_refresh": bool(force_refresh),
            "price_zero_excluded": sort in ("price_asc", "price_desc"),
            "price_zero_policy": (
                "price-sorted search excludes zero-price quote rows"
                if sort in ("price_asc", "price_desc")
                else "relevance search may include zero-price quote rows"
            ),
            "filter_scope": "extracted_only" if (norm_filters or alias_filters or norm_vendor_filters) else "all_products",
        }
        if tier_routes:
            filters_applied["filter_routed_to_vendor"] = tier_routes
        if alias_applied:
            filters_applied["filter_alias_applied"] = alias_applied
        if alias_disclosed:
            filters_applied["filter_expanded"] = {g: k for g, k in alias_disclosed.items()}
        coerced = _coercion_payload(coercions)
        if coerced:
            filters_applied["filter_value_coerced"] = coerced
        if query_plan is not None and query_plan.resolved:
            original = (query_original or "")
            if query_plan.effective_query.lower() != original.lower():
                filters_applied["query_effective"] = query_plan.effective_query
            filters_applied["query_status"] = _query_status(query_plan)
            filters_applied["match_mode"] = query_plan.match_mode
            if query_plan.dropped_tokens:
                filters_applied["query_tokens_dropped"] = list(query_plan.dropped_tokens)

        if query_plan is not None and not query_plan.resolved:
            # Nothing in the index resembles the query: do NOT silently drop the
            # text filter (that would return the whole catalog). Guide instead.
            warnings.extend(query_plan.warnings)
            suggestions = _enum_filter_suggestions(
                norm_filters, catalog_specs, conn=conn, category=category_norm
            )
            if suggestions:
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter value matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            return make_response(
                data={
                    "items": [],
                    "results": [],
                    "suggestions": suggestions,
                    "query_suggestions": query_plan.suggestions,
                    "query_status": "unresolved",
                    "match_mode": query_plan.match_mode,
                    "price_stock_refresh": None,
                },
                total_matches=0,
                returned=0,
                truncated=False,
                next_cursor=None,
                filters_applied=filters_applied,
                coverage=_build_coverage_and_warnings(
                    catalog_specs,
                    total_products,
                    requested_filters,
                    0,
                    category=category_norm,
                    category_total=int(cur.execute(
                        "SELECT COUNT(1) FROM products WHERE category_canonical = ?;",
                        (category_norm,),
                    ).fetchone()[0]) if category_norm else 0,
                    conn=conn,
                )[0],
                warnings=warnings,
                diagnosis={
                    "reason": "query_unresolved",
                    "severity": "warning",
                    "query": (query or "").strip(),
                    "query_suggestions": query_plan.suggestions,
                    "dropped_tokens": list(query_plan.dropped_tokens),
                },
            )

        active_category_keys = _category_spec_keys(conn, category_norm) if category_norm else None
        count_sql, count_params, select_sql, select_params = _build_sql_query(
            norm_filters or {},
            catalog_specs,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max_norm,
            in_stock_only=in_stock_only,
            category=category_norm,
            category_spec_keys=active_category_keys,
            require_spec_scope=require_spec_scope,
            sort=sort,
            limit=effective_limit,
            offset=offset,
            head_token=head_token,
            alias_filters=alias_filters,
            hybrid_vendor=bool(include_vendor_specs),
            vendor_filters=norm_vendor_filters,
            vendor_specs=vendor_meta,
        )

        cur.execute(count_sql, count_params)
        total_matches = cur.fetchone()[0]

        filter_recall, recall_warnings = _compute_filter_recall(
            conn,
            catalog_specs,
            norm_filters or {},
            query_plan,
            price_max_norm,
            in_stock_only,
            category_norm,
            active_category_keys,
            require_spec_scope,
            total_matches,
            alias_filters=alias_filters,
            vendor_filters=norm_vendor_filters,
            vendor_specs_meta=vendor_meta,
            include_vendor_specs=include_vendor_specs,
        )
        warnings.extend(recall_warnings)

        category_total = 0
        if category_norm:
            category_total = int(cur.execute(
                "SELECT COUNT(1) FROM products WHERE category_canonical = ?;",
                (category_norm,),
            ).fetchone()[0])

        coverage, cov_warnings = _build_coverage_and_warnings(
            catalog_specs,
            total_products,
            requested_filters,
            total_matches,
            category=category_norm,
            category_total=category_total,
            conn=conn,
        )
        vendor_cov = _vendor_coverage(
            conn, vendor_meta, category_norm, category_total, total_products
        )
        warnings.extend(cov_warnings)
        if query_plan is not None:
            warnings.extend(query_plan.warnings)
            _scoped_query_warning(warnings, total_matches)
        if category_source == "query_head" and category_norm:
            unscoped_sql, unscoped_params, _, _ = _build_sql_query(
                norm_filters or {},
                catalog_specs,
                fts_tokens=(query_plan.effective_tokens if query_plan else None),
                fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                price_max=price_max_norm,
                in_stock_only=in_stock_only,
                category=None,
                require_spec_scope=False,
                alias_filters=alias_filters,
                hybrid_vendor=bool(include_vendor_specs),
                vendor_filters=norm_vendor_filters,
                vendor_specs=vendor_meta,
            )
            cur.execute(unscoped_sql, unscoped_params)
            unscoped_total = int(cur.fetchone()[0])
            inferred_warning = _category_inference_warning(
                category_norm, category_source, total_matches, unscoped_total
            )
            if inferred_warning:
                warnings.append(inferred_warning)

        if total_matches == 0:
            suggestions: List[Dict[str, Any]] = []
            individual_counts: List[int] = []
            for only_key, only_value in (requested_filters or {}).items():
                only_sql, only_params, _, _ = _build_sql_query(
                    {only_key: only_value},
                    catalog_specs,
                    fts_tokens=(query_plan.effective_tokens if query_plan else None),
                    fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                    price_max=price_max_norm,
                    in_stock_only=in_stock_only,
                    category=category_norm,
                    require_spec_scope=require_spec_scope,
                )
                cur.execute(only_sql, only_params)
                individual_counts.append(int(cur.fetchone()[0]))
            zero_keys = {
                key: value for (key, value), count in zip(
                    (requested_filters or {}).items(), individual_counts
                ) if count == 0
            }
            if zero_keys:
                suggestions.extend(
                    _enum_filter_suggestions(
                        zero_keys, catalog_specs, conn=conn, category=category_norm
                    )
                )
            diagnosis = _diagnose_empty_result(
                requested_filters,
                category_norm,
                query_original,
                total_matches,
                filter_recall["base_matches"] if filter_recall else 0,
                filter_recall["missing_spec"] if filter_recall else 0,
                filter_recall,
                suggestions,
                individual_counts,
                vendor_filters=norm_vendor_filters,
            )
            if suggestions and diagnosis.get("reason") == "filter_value_not_present":
                warnings.append({
                    "code": "filter_value_not_present",
                    "message": "Filter value matched 0 catalog rows; see data.suggestions for the closest populated values."
                })
            zero_data: Dict[str, Any] = {
                "items": [],
                "results": [],
                "suggestions": suggestions,
                "query_status": _query_status(query_plan),
                "match_mode": query_plan.match_mode if query_plan else "all_terms",
                "price_stock_refresh": None,
                "filter_recall": filter_recall,
                "filter_provenance": {
                    "mode": (filter_recall or {}).get("mode"),
                    "promoted_matches": (filter_recall or {}).get("promoted_matches"),
                    "vendor_fallback_matches": (filter_recall or {}).get("vendor_fallback_matches"),
                    "missing_spec": (filter_recall or {}).get("missing_spec"),
                },
                "vendor_coverage": vendor_cov,
                "vendor_filters": norm_vendor_filters,
            }
            if query_plan is not None and query_plan.suggestions:
                zero_data["query_suggestions"] = query_plan.suggestions
            if alias_applied:
                filters_applied["filter_alias_applied"] = alias_applied
            if alias_disclosed:
                filters_applied["filter_expanded"] = {g: k for g, k in alias_disclosed.items()}
            coerced = _coercion_payload(coercions)
            if coerced:
                filters_applied["filter_value_coerced"] = coerced
            return make_response(
                data=zero_data,
                total_matches=0,
                returned=0,
                truncated=False,
                next_cursor=None,
                filters_applied=filters_applied,
                coverage=coverage,
                warnings=warnings,
                diagnosis=diagnosis,
            )

        refresh_info: Optional[PriceStockRefreshInfo] = None

        # Text relevance uses the same ranker as the web UI. The SQL path gives
        # the BM25 candidate order; Python applies canonical/length/role
        # adjustments, then the requested cursor offset slices that ranking.
        rank_mode = bool(
            query is not None
            and query_plan is not None
            and query_plan.resolved
            and sort == "relevance"
        )
        if rank_mode:
            pool_limit = min(1000, max(400, offset + effective_limit + 200))
            _, _, rank_sql, rank_params = _build_sql_query(
                norm_filters or {},
                catalog_specs,
                fts_tokens=query_plan.effective_tokens,
                fts_relaxed=query_plan.is_relaxed,
                price_max=price_max_norm,
                in_stock_only=in_stock_only,
                category=category_norm,
                category_spec_keys=active_category_keys,
                require_spec_scope=require_spec_scope,
                sort=sort,
                limit=pool_limit,
                offset=0,
                head_token=head_token,
                alias_filters=alias_filters,
                hybrid_vendor=bool(include_vendor_specs),
                vendor_filters=norm_vendor_filters,
                vendor_specs=vendor_meta,
            )
            cur.execute(rank_sql, rank_params)
            candidate_rows = cur.fetchall()
            candidate_products = [db.row_to_unified_product(r) for r in candidate_rows]
            bonus = _category_spec_bonus(
                conn,
                [p.id for p in candidate_products if p.id is not None],
                active_category_keys,
            )
            if norm_filters or norm_vendor_filters:
                ids_for_bonus = [p.id for p in candidate_products if p.id is not None]
                completeness = _spec_completeness_bonus(conn, ids_for_bonus)
                bonus = {
                    pid: bonus.get(pid, 0.0) + completeness.get(pid, 0.0)
                    for pid in set(bonus) | set(completeness)
                }
            ranked = rank_products(
                candidate_products,
                effective_query,
                bonus_by_id=bonus or None,
            )
            page_products = ranked[offset:offset + effective_limit]
            row_by_id = {r["id"]: r for r in candidate_rows}
            rows = [
                row_by_id[p.id] for p in page_products
                if p.id is not None and p.id in row_by_id
            ]
        else:
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
                page_ids = [r["id"] for r in rows]
                if page_ids:
                    id_ph = ", ".join(["?"] * len(page_ids))
                    cur.execute(
                        f"SELECT * FROM products WHERE id IN ({id_ph});", page_ids
                    )
                    row_by_id = {r["id"]: r for r in cur.fetchall()}
                    rows = [row_by_id[i] for i in page_ids if i in row_by_id]

        ids = [r["id"] for r in rows]
        attribute_counts = db.get_attribute_counts(ids)
        result_spec_counts: Dict[str, int] = {}
        if ids:
            placeholders = ", ".join(["?"] * len(ids))
            cur.execute(
                f"SELECT key, COUNT(1) FROM product_specs WHERE product_id IN ({placeholders}) GROUP BY key;",
                ids
            )
            for spec_key, spec_count in cur.fetchall():
                result_spec_counts[spec_key] = spec_count
        for group, keys in (alias_disclosed or {}).items():
            result_spec_counts[group] = sum(result_spec_counts.get(k, 0) for k in keys)

        vendor_keys_for_page = sorted(
            set((norm_vendor_filters or {}).keys())
            | set((requested_filters or {}).keys())
        )
        vendor_specs_by_product: Dict[int, Dict[str, Any]] = {}
        if ids and vendor_keys_for_page:
            id_ph = ", ".join(["?"] * len(ids))
            key_ph = ", ".join(["?"] * len(vendor_keys_for_page))
            cur.execute(
                f"SELECT product_id, key, value FROM product_vendor_specs "
                f"WHERE product_id IN ({id_ph}) AND key IN ({key_ph});",
                list(ids) + vendor_keys_for_page,
            )
            for pid, key, value in cur.fetchall():
                vendor_specs_by_product.setdefault(int(pid), {})[str(key)] = value
        for vk in vendor_keys_for_page:
            fallback_present = sum(
                1 for row_vendor in vendor_specs_by_product.values() if vk in row_vendor
            )
            result_spec_counts[vk] = max(result_spec_counts.get(vk, 0), fallback_present)

        # Spec presence across the returned page: tells the caller whether a field
        # is genuinely absent from the part or simply never extracted for it.
        page_coverage = {
            k: {
                "present": result_spec_counts.get(k, 0),
                "of": len(ids),
                "pct": round(result_spec_counts.get(k, 0) / max(len(ids), 1) * 100, 1),
            }
            for k in all_catalog_keys
        }
        for fk, entry in coverage.items():
            entry["page"] = {
                "present": result_spec_counts.get(fk, 0),
                "of": len(ids),
                "pct": round(result_spec_counts.get(fk, 0) / max(len(ids), 1) * 100, 1),
            }
        for vk in (norm_vendor_filters or {}):
            vendor_present = sum(
                1 for row_vendor in vendor_specs_by_product.values() if vk in row_vendor
            )
            page_coverage[vk] = {
                "present": vendor_present,
                "of": len(ids),
                "pct": round(vendor_present / max(len(ids), 1) * 100, 1),
                "tier": "vendor",
            }

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

            vendor_for_row = vendor_specs_by_product.get(pid, {})
            spec_sources = {k: "promoted" for k in specs}
            for vk, vv in vendor_for_row.items():
                if vk not in specs:
                    specs[vk] = vv
                    spec_sources[vk] = "vendor"

            row_keys = r.keys()
            row_category = r["spec_category"] if "spec_category" in row_keys else None
            if row_category:
                applicable_keys = category_keys_map.get(row_category, [])
                row_scope = (
                    "category_scoped" if applicable_keys else "category_no_attributes"
                )
                # unknown != absent != null: scoped to keys that apply to this category.
                specs_not_extracted = [k for k in applicable_keys if k not in specs]
            else:
                row_scope = "unclassified"
                specs_not_extracted = []

            stock_status = "in_stock" if r["available"] else "out_of_stock"
            results.append({
                "product_id": pid,
                "title": r["title"],
                "vendor": r["vendor_name"] or r["vendor"],
                "price": float(r["price"]),
                "currency": "INR",
                **_price_basis_fields(r["title"], float(r["price"])),
                "available": bool(r["available"]),
                "stock_status": stock_status,
                "price_source": (r["price_stock_source"] if "price_stock_source" in row_keys else None) or "cache",
                "price_updated_at": (
                    str(r["price_stock_updated_at"])
                    if "price_stock_updated_at" in row_keys and r["price_stock_updated_at"] else None
                ),
                "category": r["category"],
                "category_canonical": (
                    r["category_canonical"] if "category_canonical" in row_keys else None
                ),
                "spec_category": row_category,
                "spec_extraction_scope": row_scope,
                "mpn": r["mpn"],
                "vendor_sku": r["sku"],
                "product_url": r["product_url"],
                "specs": specs,
                "spec_sources": spec_sources,
                "spec_count": len(specs),
                "vendor_specs": vendor_for_row,
                "vendor_spec_count": attribute_counts.get(pid, 0),
                "specs_not_extracted": specs_not_extracted,
                "low_confidence_specs": low_confidence_specs,
            })

        if compact:
            compact_keys = (
                "product_id", "title", "vendor", "price", "currency",
                "stock_status", "mpn", "specs", "product_url",
            )
            results = [{k: row.get(k) for k in compact_keys} for row in results]

        returned = len(results)
        truncated = (offset + returned) < total_matches
        next_cursor = None
        if truncated:
            next_cursor = base64.b64encode(
                f"{offset + returned}:{fingerprint}".encode("utf-8")
            ).decode("ascii")

        query_status = _query_status(query_plan)
        match_ratios = [_match_ratio(effective_query, r["title"]) for r in results] if results else []
        match_ratio_top = match_ratios[0] if match_ratios else None
        match_ratio_avg = round(sum(match_ratios) / len(match_ratios), 3) if match_ratios else None
        low_confidence_reason = None
        if (
            query_plan is not None
            and query_plan.mode in ("relaxed", "corrected_relaxed")
            and match_ratio_top is not None
            and match_ratio_top < 0.5
        ):
            low_confidence_reason = (
                f"The best result covers only {match_ratio_top:.0%} of the query terms; "
                "these results are a broad match."
            )
        elif (
            query_plan is not None
            and query_plan.mode in ("relaxed", "corrected_relaxed")
            and len(query_plan.effective_tokens) == 1
            and total_matches >= 1000
        ):
            low_confidence_reason = (
                f"Relaxation reduced the query to the single generic term "
                f"'{effective_query}', which matches {total_matches:,} listings."
            )

        if low_confidence_reason:
            query_status = "low_confidence"
            warnings.append({
                "code": "query_low_confidence",
                "message": (
                    f"{low_confidence_reason} Add a category or spec filters before quoting "
                    "these results as the answer."
                ),
                "match_ratio_top": match_ratio_top,
                "match_ratio_avg": match_ratio_avg,
                "query_effective": effective_query,
                "total_matches": total_matches,
            })

        if alias_disclosed:
            per_key_counts: Dict[str, int] = {}
            for entry in alias_filters:
                for key in entry.get("keys") or []:
                    key_sql, key_params, _, _ = _build_sql_query(
                        {key: entry.get("value")},
                        catalog_specs,
                        fts_tokens=(query_plan.effective_tokens if query_plan else None),
                        fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                        price_max=price_max_norm,
                        in_stock_only=in_stock_only,
                        category=None,
                        require_spec_scope=False,
                    )
                    cur.execute(key_sql, key_params)
                    per_key_counts[key] = int(cur.fetchone()[0])
            filters_applied["filter_expanded"] = {g: k for g, k in alias_disclosed.items()}
            filters_applied["filter_expanded"]["per_key_counts"] = per_key_counts

        if compact:
            data_payload: Dict[str, Any] = {
                "results": results,
                "query_status": query_status,
                "total_listings": total_matches,
                "compact": True,
            }
        else:
            data_payload = {
                "items": results,
                "results": results,
                "items_are": "listings",
                "total_listings": total_matches,
                "query_status": query_status,
                "match_mode": query_plan.match_mode if query_plan else "all_terms",
                "match_ratio_top": match_ratio_top,
                "match_ratio_avg": match_ratio_avg,
                "query_tokens_used": list(query_plan.effective_tokens) if query_plan else [],
                "query_tokens_dropped": list(query_plan.dropped_tokens) if query_plan else [],
                "note": (
                    "Each result is one vendor listing. The same physical part can appear once per vendor; "
                    "compare on mpn/specs rather than assuming one row per part."
                ),
                "page_coverage": page_coverage,
                "vendor_coverage": vendor_cov,
                "vendor_filters": norm_vendor_filters,
                "filter_recall": filter_recall,
                "filter_provenance": {
                    "mode": (filter_recall or {}).get("mode"),
                    "promoted_matches": (filter_recall or {}).get("promoted_matches"),
                    "vendor_fallback_matches": (filter_recall or {}).get("vendor_fallback_matches"),
                    "missing_spec": (filter_recall or {}).get("missing_spec"),
                },
                "price_stock_refresh": refresh_info.model_dump() if refresh_info else None,
            }

        return make_response(
            data=data_payload,
            total_matches=total_matches,
            returned=returned,
            truncated=truncated,
            next_cursor=next_cursor,
            filters_applied=filters_applied,
            coverage=coverage,
            warnings=warnings,
            diagnosis=_result_diagnosis(query_plan, filter_recall, requested_filters, category_norm),
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
        spec_index_updated_at = cur.execute(
            "SELECT MAX(updated_at) FROM spec_stats_global;"
        ).fetchone()[0]
        spec_keys_list = []
        for k, s in catalog_specs.items():
            cov_pct = round(s["count"] / max(total_products, 1) * 100, 2)
            spec_keys_list.append({
                "key": k,
                "display_name": s["display_name"],
                "type": s["type"],
                "unit": s["unit"],
                "coverage_pct": cov_pct,
                "backfill_pct": cov_pct,
                "count": s["count"],
                "category_coverage": category_coverage.get(k, []),
                "values": s["enum_values"] if s["type"] == "enum" else None,
                "range": {"min": s["min"], "max": s["max"]} if s["type"] in ("integer", "float") else None,
                "confidence": "low" if k == "current_a" else "high",
                "precision_note": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)" if k == "current_a" else None
            })

        return make_response(
            data={
                "spec_keys": spec_keys_list,
                "spec_key_names": [s["key"] for s in spec_keys_list],
                "deprecated": True,
                "replacement": "describe_catalog(summary=True)",
            },
            total_matches=len(spec_keys_list),
            returned=len(spec_keys_list),
            warnings=[{
                "code": "deprecated_tool",
                "message": (
                    "list_spec_keys is retained for backward compatibility. Use "
                    "describe_catalog(summary=True) for a compact key/type/unit/coverage catalog."
                ),
            }],
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
            "vendor_spec_counts": {
                "rows": cur.execute("SELECT COUNT(1) FROM product_vendor_specs;").fetchone()[0],
                "keys": cur.execute("SELECT COUNT(1) FROM vendor_spec_stats_global;").fetchone()[0],
            },
            "spec_index_updated_at": cur.execute(
                "SELECT MAX(updated_at) FROM spec_stats_global;"
            ).fetchone()[0],
            "spec_backfill_pct": round(
                (cur.execute("SELECT COUNT(1) FROM product_specs;").fetchone()[0] or 0)
                / max(cur.execute("SELECT COUNT(1) FROM products;").fetchone()[0] or 1, 1)
                * 100, 2
            ),
            "spec_backfill_pct_by_key": {
                str(row[0]): round(
                    float(row[1] or 0)
                    / max(int(cur.execute("SELECT COUNT(1) FROM products;").fetchone()[0] or 1), 1)
                    * 100, 2
                )
                for row in cur.execute(
                    "SELECT key, applicable_count FROM spec_stats_global ORDER BY key;"
                ).fetchall()
            },
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
    force_refresh: bool = False,
    allow_ambiguous: bool = False,
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
      - allow_ambiguous: bool (default: false; return listings even when the "MPN" is a family
        token or is shared by dozens of unrelated titles, with an mpn_ambiguous warning)

    Matching: 'MPU6050', 'mpu-6050' and 'MPU 6050' resolve to the same listings; each listing
    carries its extracted specs so fit questions can be answered without a second call.

    Ambiguity: family tokens (ESP32, ESP8266, STM32, ...) are not part numbers. When one MPN
    value maps to dozens of distinct titles, the tool returns error_code='mpn_family_token' or
    'mpn_ambiguous' instead of presenting 429 unrelated listings as an exact match.

    Price/Stock Freshness: price and stock for the returned listings are re-verified against
    live vendors before the response is built, and cached in the local catalog. When vendors
    are unreachable, the last cached values are served and data.price_stock_refresh reports it.

    Behavior When No Matches: Returns ok:true with total_matches:0 and data.suggested_mpns
    (closest catalog part numbers) rather than an error, plus a hint to fall back to free-text search.

    Errors: mpn_family_token (ESP32, ESP8266, ... are chip families, not part numbers),
    mpn_ambiguous (one identifier shared by dozens of distinct titles), stale_cursor,
    invalid_filter_value.

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
    family_token = normalized in get_family_tokens()
    if family_token and not allow_ambiguous:
        return make_error(
            error_code="mpn_family_token",
            message=(
                f"'{needle}' is a chip/platform family token, not a manufacturer part number. "
                "It is shared by many unrelated products, so an exact MPN lookup would return "
                "a misleading list. Use search_components with a text query instead."
            ),
            suggestions=[{"tool": "search_components", "query": needle}],
            details={"mpn_normalized": normalized, "family_tokens": sorted(get_family_tokens())},
            diagnosis={
                "reason": "mpn_family_token",
                "mpn": needle,
                "mpn_normalized": normalized,
            },
        )

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

        mpn_stats = db.mpn_match_stats(needle)
        ambiguous = bool(
            mpn_stats["total"] >= 30
            and mpn_stats["distinct_titles"] >= 25
            and mpn_stats["distinct_titles"] / max(mpn_stats["total"], 1) >= 0.8
        )
        if ambiguous and not allow_ambiguous:
            return make_error(
                error_code="mpn_ambiguous",
                message=(
                    f"'{needle}' matches {mpn_stats['total']} listings across "
                    f"{mpn_stats['distinct_titles']} distinct titles. That is a shared/family "
                    "identifier, not an exact part number. Use search_components or pass "
                    "allow_ambiguous=True if you really want these listings."
                ),
                suggestions=[
                    {"tool": "search_components", "query": needle},
                    {"allow_ambiguous": True},
                ],
                details={
                    "mpn_normalized": normalized,
                    "total": mpn_stats["total"],
                    "distinct_titles": mpn_stats["distinct_titles"],
                    "distinct_vendors": mpn_stats["distinct_vendors"],
                    "sample_titles": mpn_stats["sample_titles"],
                },
                diagnosis={
                    "reason": "mpn_ambiguous",
                    "mpn": needle,
                    **mpn_stats,
                },
            )

        ambiguity_warning: Optional[Dict[str, Any]] = None
        if ambiguous or family_token:
            ambiguity_warning = {
                "code": "mpn_ambiguous",
                "message": (
                    f"'{needle}' is a shared/family identifier ({mpn_stats['total']} listings, "
                    f"{mpn_stats['distinct_titles']} distinct titles). These are not one part."
                ),
                "total": mpn_stats["total"],
                "distinct_titles": mpn_stats["distinct_titles"],
                "distinct_vendors": mpn_stats["distinct_vendors"],
            }
            warnings.append(ambiguity_warning)

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
        attribute_counts = db.get_attribute_counts([r["id"] for r in rows]) if rows else {}

        listings = [
            {
                "product_id": r["id"],
                "vendor": r["vendor_name"] or r["vendor"],
                "title": r["title"],
                "price": float(r["price"]),
                "currency": "INR",
                **_price_basis_fields(r["title"], float(r["price"])),
                "available": bool(r["available"]),
                "stock_status": "in_stock" if r["available"] else "out_of_stock",
                "price_source": r["price_stock_source"] or "cache",
                "price_updated_at": r["price_stock_updated_at"],
                "product_url": r["product_url"],
                "vendor_sku": r["sku"],
                "mpn": r["mpn"],
                "category": r["category"],
                "specs": specs_by_product.get(r["id"], {}),
                "vendor_spec_count": attribute_counts.get(r["id"], 0),
            }
            for r in rows
        ]

        suggested_mpns: List[str] = []
        diagnosis: Optional[Dict[str, Any]] = None
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
            diagnosis = _diagnosis(
                "mpn_not_found",
                "warning",
                mpn=needle,
                mpn_normalized=normalized,
                suggested_mpns=suggested_mpns,
            )
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
                "items": listings,
                "results": listings,
                "listings": listings,
                "listings_deprecated": "Use data.results; data.listings is a compatibility alias.",
                "total_listings": total,
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
            warnings=warnings,
            diagnosis=diagnosis,
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
