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
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

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
)
from src.search.ranking import (rank_products, spec_completeness_bonus,
                                category_spec_bonus)
from src.search.planner import resolve_query_plan
from src.search.filter_plan import FilterPlanError, prepare_filters
from src.search.catalog_query import CatalogQuery, build_catalog_statements
from src.search.product_role import classify_role, identify_product, spec_subject, identity_policy_summary
from src.search.listing_identity import sold_form, comparison_evidence
from src.search.requirements import extract_requirements
from src.spec.constraints import compile_predicates, summarize_states, discover_candidates, individual_match_counts
from src.spec.introspection import filter_population
from src.spec.filters import (
    _nearest_keys,
    alias_catalog_payload,
    canonical_enum_value,
    enum_is_numeric as _enum_is_numeric,
    load_catalog_specs as _load_catalog_specs,
    resolve_category,
    validate_filter_scope,
)
from src.spec.vendor_index import (
    VENDOR_KEY_ALIASES,
    load_vendor_specs,
    normalize_vendor_key,
    vendor_category_keys,
    vendor_alias_is_current,
)
from src.spec.taxonomy_loader import get_taxonomy

logger = logging.getLogger(__name__)

SERVER_VERSION = "2.4.0"
SCHEMA_VERSION = "2.4.0"

# Hard ceiling on page size. An agent asking for 1000 rows is almost always
# paginating by accident, and a negative limit becomes SQL `LIMIT -1` (= no limit).
MAX_PAGE_SIZE = 50

# Initialize FastMCP Server
class EnvelopeMCP(FastMCP):
    """Validate transport arguments before FastMCP turns failures into exceptions."""

    def add_tool(self, fn, *args, **kwargs):
        result = super().add_tool(fn, *args, **kwargs)
        tool = self._tool_manager.get_tool(kwargs.get("name") or fn.__name__)
        if tool is not None:
            tool.parameters["additionalProperties"] = False
        return result

    async def call_tool(self, name, arguments):
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            try:
                tool.fn_metadata.arg_model.model_validate(arguments, strict=True, extra="forbid")
            except ValidationError as exc:
                result = make_error(
                    error_code="invalid_arguments", message="Tool arguments do not match the advertised schema.",
                    details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
                )
                return tool.fn_metadata.convert_result(result)
        return await super().call_tool(name, arguments)


mcp = EnvelopeMCP("aggregate-agent-first")
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
    if refresh_info is None:
        return []
    if refresh_info.status == "partial":
        return [{"code": "price_refresh_partial", **refresh_info.model_dump()}]
    if refresh_info.status in ("cached", "offline_cooldown", "timeout", "error"):
        return [{
            "code": "price_stock_cache_fallback",
            "status": refresh_info.status,
            "message": refresh_info.message or "Serving cached price/stock.",
        }]
    if refresh_info.status in {"disabled", "fresh_cache"}:
        return [{"code": "price_stock_cached", "status": refresh_info.status,
                 "message": refresh_info.message or "Serving cached price/stock; no live request was made."}]
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
    query_plan: Optional[QueryPlan] = None,
) -> ToolResponseEnvelope:
    """Builds a successful uniform response envelope."""
    filters_applied = dict(filters_applied or {})
    warnings = list(warnings or [])
    if query_plan is not None and isinstance(data, dict):
        from src.search.query_intent import retrieval_response_fields
        data.update(retrieval_response_fields(query_plan.contract(), total_matches))
        filters_applied["price_min"] = query_plan.price_min
        filters_applied["query_contract"] = query_plan.contract()
    original_query = filters_applied.get("query")
    if original_query and isinstance(data, dict):
        _, requirements = extract_requirements(original_query)
        if query_plan is not None:
            requirements = query_plan.unresolved_constraints
        data["original_query"] = original_query
        data["unresolved_constraints"] = requirements
        data["requirements_verified"] = False if requirements else None
        if requirements:
            filters_applied["unresolved_constraints"] = requirements
            if not any(w.get("code") == "unresolved_engineering_constraints" for w in warnings):
                warnings.append({"code": "unresolved_engineering_constraints",
                                 "message": "Retrieved listings do not verify the preserved application requirements.",
                                 "constraints": requirements})
            if total_matches > 0:
                diagnosis = {"reason": "unresolved_engineering_constraints", "severity": "warning",
                             "unresolved_constraints": requirements,
                             "hint": "Resolve the preserved requirements before making an engineering recommendation."}
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
    include_related: bool = False,
    include_vendor_specs: bool = True,
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
            "include_related": include_related,
            "include_vendor_specs": include_vendor_specs,
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
            filled = int(conn.execute(
                "SELECT COUNT(DISTINCT ps.product_id) FROM product_specs ps "
                "JOIN products p ON p.id = ps.product_id WHERE p.category_canonical = ? AND ps.key = ?",
                (category, fk),
            ).fetchone()[0])
            denominator = int(category_total or 0)
            coverage[fk]["category"] = {
                "scope": "discovery_category",
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


def _constraint_scope(
    conn, catalog_specs, norm_filters, query_plan, price_max, in_stock_only,
    category, active_category_keys, require_spec_scope, alias_filters=None,
    vendor_filters=None, vendor_specs_meta=None, include_vendor_specs=True,
    include_related=False, sort="relevance", head_token=None,
):
    scope = CatalogQuery(
        query_plan=query_plan,
        price_max=price_max, in_stock_only=in_stock_only, category=category,
        include_related=include_related,
    )
    base_sql, params = scope.ids_statement()
    predicates = compile_predicates(
        norm_filters, catalog_specs, hybrid=include_vendor_specs,
        vendor_filters=vendor_filters, vendor_metadata=vendor_specs_meta,
        aliases=alias_filters,
    )
    return base_sql, params, predicates


def _compute_filter_recall(
    conn, catalog_specs, norm_filters, query_plan, price_max, in_stock_only,
    category, active_category_keys, require_spec_scope, total_matches,
    alias_filters=None, vendor_filters=None, vendor_specs_meta=None,
    include_vendor_specs=True, include_related=False, sort="relevance", head_token=None,
):
    """Diagnostics use exactly the predicates and evidence states used by retrieval."""
    base_sql, params, predicates = _constraint_scope(
        conn, catalog_specs, norm_filters, query_plan, price_max, in_stock_only,
        category, active_category_keys, require_spec_scope, alias_filters,
        vendor_filters, vendor_specs_meta, include_vendor_specs, include_related,
        sort, head_token,
    )
    recall = summarize_states(conn, base_sql, params, predicates)
    if recall is None:
        return None, []
    state_summary = dict(recall)
    promoted = compile_predicates(
        norm_filters, catalog_specs, hybrid=False, aliases=alias_filters,
        vendor_filters=vendor_filters, vendor_metadata=vendor_specs_meta,
    )
    promoted_recall = (
        summarize_states(conn, base_sql, params, promoted)
        if include_vendor_specs and (norm_filters or alias_filters)
        else state_summary
    )
    recall.update({
        "scope": "usable_evidence", "mode": "hybrid" if include_vendor_specs else "promoted",
        "promoted_matches": promoted_recall["confirmed_matches"],
        "hybrid_matches": recall["confirmed_matches"],
        "vendor_fallback_matches": max(0, recall["confirmed_matches"] - promoted_recall["confirmed_matches"]),
        "promoted_missing": promoted_recall["any_unknown"],
        "vendor_filled_unknown": max(0, promoted_recall["any_unknown"] - recall["any_unknown"]),
    })
    if vendor_filters:
        vendor_predicates = compile_predicates(
            {}, catalog_specs, vendor_filters=vendor_filters, vendor_metadata=vendor_specs_meta,
        )
        vendor_recall = (
            summarize_states(conn, base_sql, params, vendor_predicates)
            if norm_filters or alias_filters else state_summary
        )
        recall["vendor_filters"] = vendor_recall
    warnings = []
    if recall["any_unknown"]:
        warnings.append({
            "code": "filter_recall_gap", "missing_spec": recall["missing_spec"],
            "any_unknown": recall["any_unknown"], "base_matches": recall["base_matches"],
            "vendor_fallback_matches": recall["vendor_fallback_matches"],
            "message": "Some listings have unknown constraints. Unknown never satisfies a filter; explicit failures take precedence in the count partition.",
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


def _populated_categories(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT category_canonical FROM products "
        "WHERE category_canonical IS NOT NULL AND category_canonical != '' "
        "ORDER BY category_canonical;"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _valid_categories(conn: sqlite3.Connection) -> List[str]:
    """Supported categories include empty ones; inference requires actual data."""
    return sorted(set(_populated_categories(conn)) | set(get_taxonomy().categories))


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
    if query and "\x00" in query:
        return make_error(error_code="invalid_query", message="query must not contain a NUL character.")
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
    if plan.mode == "strict":
        return "all_terms"
    if plan.mode == "corrected":
        return "corrected"
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


# =====================================================================
# 6 AGENT-FIRST TOOLS
# =====================================================================

@mcp.tool()
def describe_catalog(summary: bool = False) -> ToolResponseEnvelope:
    """
    Return comprehensive catalog-level metadata, total product counts, vendor distributions, category counts, queryable spec keys with coverage percentages, and data freshness timestamps.

    Recommended Call Order: Call describe_catalog first before running any searches to inspect available attributes and catalog size.

    Using The Output: taxonomy[].id lists accepted categories. taxonomy[].products
    and products_component match default category counts; products_with_related is
    broad discovery. products_by_spec_category describes extraction assignments.
    Key coverage statistics describe stored promoted rows before identity/ownership
    checks. Use describe_spec for usable filter coverage and observed distributions.

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
        component_counts = {str(r[0]): int(r[1]) for r in conn.execute(
            "SELECT category_canonical, SUM(aggregate_identity_matches(title,category_canonical,category_canonical)) "
            "FROM products GROUP BY category_canonical")}
        observed_values = {}
        for cat_id, key, value in conn.execute(
            "SELECT DISTINCT p.category_canonical,s.key,aggregate_canonical_value(s.value,s.key) "
            "FROM product_specs s JOIN products p ON p.id=s.product_id "
            "WHERE aggregate_identity_matches(p.title,p.category_canonical,p.category_canonical)=1 "
            "AND aggregate_spec_subject(p.title,p.category_canonical,s.key)='sold_item'"):
            observed_values.setdefault((cat_id, key), set()).add(value)

        taxonomy = get_taxonomy()
        taxonomy_payload = []
        for cat in taxonomy.categories.values():
            taxonomy_payload.append({
                "id": cat.id,
                "display": cat.display,
                "description": cat.description,
                "products": component_counts.get(cat.id, 0),
                "products_component": component_counts.get(cat.id, 0),
                "products_with_related": products_by_category.get(cat.id, 0),
                "filter_support": "partial" if cat.attributes else "none",
                "filterable_attributes": [
                    {
                        "key": attr.key,
                        "display_name": attr.display_name,
                        "type": attr.type,
                        "unit": attr.unit,
                        "values": sorted(observed_values.get((cat.id, attr.key), set())) if attr.type in {"enum", "string"} else None,
                        "values_scope": "populated_promoted_component_evidence",
                        "schema_values": attr.values,
                        "range": {"min": attr.range[0], "max": attr.range[1]} if attr.range else None,
                    }
                    for attr in cat.attributes.values() if attr.key in catalog_specs
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
                "vendor_spec_key_count": int(conn.execute("SELECT COUNT(*) FROM vendor_spec_stats_global").fetchone()[0]),
                "vendor_spec_discovery_min_count": 5,
                "summary": True,
                "spec_statistics_scope": "stored_promoted_rows; describe_spec gives filterable coverage",
                "identity_policy": {"version": 1, "details": "describe_catalog(summary=False)"},
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
                "vendor_spec_keys": int(conn.execute("SELECT COUNT(*) FROM vendor_spec_stats_global").fetchone()[0]),
            },
            "products_by_vendor": products_by_vendor,
            "products_by_spec_category": products_by_spec_category,
            "products_by_spec_category_scope": "extraction_assignment; not the category search population",
            "products_by_category": component_counts,
            "category_count_policy": "taxonomy.products and products_by_category match default count_components(category); products_with_related is broad discovery",
            "spec_statistics_scope": "stored_promoted_rows_before_identity_and_ownership; use describe_spec for filterable coverage",
            "taxonomy": taxonomy_payload,
            "identity_policy": identity_policy_summary(),
            "spec_keys": spec_keys_list,
            "aliases": alias_catalog_payload(),
            "vendor_spec_key_count": int(conn.execute("SELECT COUNT(*) FROM vendor_spec_stats_global").fetchone()[0]),
                "vendor_spec_discovery_min_count": 5,
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
def describe_spec(key: str, vendor: bool = False, include_vendor_specs: bool = True,
                  category: Optional[str] = None, include_related: bool = False) -> ToolResponseEnvelope:
    """Inspect a filter's usable values, range and coverage using search semantics.

    Example Invocation: describe_spec(key="rpm")
    Keys are case/whitespace normalized. Default counts use promoted evidence with
    vendor fallback, just like count_components. Set include_vendor_specs=False for
    promoted-only counts, or vendor=True for explicit vendor_filters. category and
    include_related have the same meaning as in search. Stored extraction coverage
    is reported separately. values is a bounded list of populated equality values;
    each count can be reproduced with the same filter options.
    """
    key = str(key).strip().lower()
    conn = get_db()
    try:
        catalog = _load_catalog_specs(conn)
        category_norm, error = _resolve_category_or_error(conn, category)
        if error:
            return error
        vkey = normalize_vendor_key(key)
        vendor_meta = load_vendor_specs(conn, min_count=1, keys=[vkey])
        use_vendor = vendor or (key not in catalog and vkey in vendor_meta)
        metadata = vendor_meta if use_vendor else catalog
        key = vkey if use_vendor else key
        if key not in metadata:
            return make_error(error_code="unknown_filter_key", message=f"Unknown spec key '{key}'.",
                              valid_keys=sorted(catalog), suggestions=_nearest_keys(key, sorted(catalog)))
        if category_norm and not use_vendor:
            scope_error = validate_filter_scope({key: {}}, category_norm, _category_spec_keys(conn, category_norm))
            if scope_error:
                return make_error(**scope_error)
        meta = metadata[key]
        population = filter_population(conn, key, catalog, vendor_metadata=vendor_meta,
                                       hybrid=include_vendor_specs, category=category_norm,
                                       include_related=include_related, vendor=use_vendor)
        total = int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])
        count = population["coverage_count"]
        data = {"key": key, "tier": "vendor" if use_vendor else "promoted",
                "status": "vendor_reported" if use_vendor else "key_healthy" if count else "key_no_usable_data",
                "display_name": meta["display_name"], "type": meta["type"], "unit": meta["unit"],
                **population, "coverage_pct": round(count / max(total, 1) * 100, 2),
                "coverage_denominator": {"products": total, "scope": "global_catalog"},
                "stored_coverage_count": meta.get("global_count", meta.get("count", 0)),
                "stored_coverage_scope": "indexed_rows_before_identity_and_ownership",
                "category_coverage": _category_coverage(conn).get(key, []) if not use_vendor else []}
        warnings = []
        if use_vendor:
            warnings.append({"code": "vendor_spec_tier", "message": "Vendor-reported evidence; use vendor_filters."})
        if key == "current_a":
            warnings.append({"code": "low_precision_advisory", "message": "Current extraction may describe a driver or module; verify the item."})
        return make_response(data=data, total_matches=count, returned=1, warnings=warnings)
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
    include_related: bool = False,
) -> ToolResponseEnvelope:
    """
    Count matching components and inspect filter coverage without fetching product rows.

    Identity: modeled component families require sold-item identity evidence by default.
    include_related=True browses the broader vendor category, including accessories and
    unknown identities. See describe_catalog.identity_policy for modeled families.

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

    Query policy: required terms are conjoined and never dropped automatically.
    Brand/model/variant identity is protected. Safe contextual engineering values
    become typed predicates; ambiguous requirements remain unresolved. Suggestions
    are separate from confirmed results and require a new request.

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
        try:
            filter_plan = prepare_filters(
                conn, filters, metadata=catalog_specs, category=category_norm,
                category_keys=category_keys, vendor_filters=vendor_filters,
                include_vendor_specs=include_vendor_specs,
            )
        except FilterPlanError as exc:
            return make_error(**exc.error)
        coercions = filter_plan.coercions
        norm_filters = filter_plan.filters
        requested_filters = filter_plan.requested_filters
        norm_vendor_filters = filter_plan.vendor_filters
        vendor_meta = filter_plan.vendor_metadata
        alias_filters = filter_plan.aliases
        alias_disclosed = filter_plan.alias_disclosed
        alias_applied = filter_plan.alias_routes
        tier_routes = filter_plan.tier_routes

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

        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = resolve_query_plan(query, conn=conn, category=category_norm)
            if query_plan.price_max is not None and (price_max_norm is None or query_plan.price_max < price_max_norm):
                price_max_norm = query_plan.price_max
                price_max_source = "query"

        # Natural-language queries often name the category in the head term
        # ("battery to power an arduino"). Infer the category only when the
        # caller did not supply one and no explicit spec filters are in play,
        # so inferred scoping can never override deliberate constraints.
        if (
            category_norm is None
            and not norm_filters
            and query_plan is not None
            and query_plan.resolved
            and (query_plan.head_token or query_plan.inferred_category)
        ):
            inferred_category, _ = resolve_category(
                query_plan.inferred_category or query_plan.head_token, _populated_categories(conn)
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
                query_plan=query_plan,
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

        count_sql, params, _, _ = build_catalog_statements(
            norm_filters or {},
            catalog_specs,
            query_plan=query_plan,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max_norm,
            in_stock_only=in_stock_only,
            category=category_norm,
            include_related=include_related,
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
            include_related=include_related,
        )
        warnings.extend(recall_warnings)

        suggestions: List[Dict[str, Any]] = []
        diagnosis: Optional[Dict[str, Any]] = None
        if total_count == 0:
            base_sql, base_params, predicates = _constraint_scope(
                conn, catalog_specs, norm_filters or {}, query_plan, price_max_norm,
                in_stock_only, category_norm, _category_spec_keys(conn, category_norm) if category_norm else None, require_spec_scope,
                alias_filters, norm_vendor_filters, vendor_meta, include_vendor_specs,
                include_related, "relevance", None,
            )
            per_constraint_counts = individual_match_counts(conn, base_sql, base_params, predicates)
            individual_counts = list(per_constraint_counts.values())
            zero_keys = {key: value for key, value in (requested_filters or {}).items()
                         if per_constraint_counts.get(key) == 0}
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
            unscoped_sql, unscoped_params, _, _ = build_catalog_statements(
                norm_filters or {},
                catalog_specs,
                query_plan=query_plan,
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
            "include_related": bool(include_related),
            "identity_policy": "discovery" if include_related else "component_when_modeled",
            "query_status": _query_status(query_plan),
            "match_mode": query_plan.match_mode if query_plan else "all_terms",
            "price_zero_excluded": False,
            "price_zero_policy": "zero_price_rows_included_in_all_sort_orders",
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
                    key_sql, key_params, _, _ = build_catalog_statements(
                        {key: entry.get("value")},
                        catalog_specs,
                        query_plan=query_plan,
                        fts_tokens=(query_plan.effective_tokens if query_plan else None),
                        fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                        price_max=price_max_norm,
                        in_stock_only=in_stock_only,
                        category=category_norm,
                        include_related=include_related,
                        hybrid_vendor=bool(include_vendor_specs),
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
            query_plan=query_plan,
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

    Related vendor_offers retain individual listing IDs and grouping reasons. A
    shared extracted MPN alone does not prove interchangeability; compare prices
    only when price_comparable is true and inspect comparison_reasons otherwise.

    Behavior When No Matches: Returns ok:false with error_code='product_not_found' if the product ID does not exist in the catalog.
    """
    if isinstance(product_id, bool) or not isinstance(product_id, int) or not 0 < product_id <= 2**63 - 1:
        return make_error(error_code="invalid_filter_value", message="product_id must be a positive SQLite integer ID.")
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
        for offer in offers:
            offer.update(comparison_evidence(p, offer))

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
            extracted_specs[k]["subject"] = spec_subject(p["title"], p.get("category_canonical") or "", k)
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
            if not vendor_alias_is_current(p.get("category_canonical"), vr[0], vr[5], vr[7]):
                continue
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
                "subject": spec_subject(p["title"], p.get("category_canonical") or "", str(vr[0])),
            }

        spec_sources = {key: "promoted" for key in extracted_specs}
        for key, detail in normalized_vendor_details.items():
            target = (VENDOR_KEY_ALIASES.get(p.get("category_canonical") or "") or {}).get(key, key)
            if target != key:
                continue
            if key not in extracted_specs:
                extracted_specs[key] = detail
                spec_sources[key] = "vendor"
        if refresh_info is None:
            refresh_info = PriceStockRefreshInfo(status="skipped", cached_products=len(offers),
                                                message="Live refresh was not requested; serving cached values.")

        data = {
            "product_id": p["id"],
            "title": p["title"],
            "product_role": classify_role(p["title"], p.get("category_canonical") or ""),
            "product_identity": identify_product(p["title"], p.get("category_canonical") or "").to_dict(),
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
            "specs": {key: str(value["value"]) for key, value in extracted_specs.items() if value["subject"] == "sold_item"},
            "related_specs": {key: str(value["value"]) for key, value in extracted_specs.items() if value["subject"] != "sold_item"},
            "spec_details": extracted_specs,
            "spec_sources": {key: source for key, source in spec_sources.items() if extracted_specs[key]["subject"] == "sold_item"},
            "spec_subjects": {key: detail["subject"] for key, detail in extracted_specs.items()},
            "extracted_specs": extracted_specs,
            "vendor_specs": vendor_specs,
            "vendor_spec_count": len(vendor_specs),
            "normalized_vendor_specs": normalized_vendor_specs,
            "available_vendor_spec_keys": sorted(normalized_vendor_specs),
            "normalized_vendor_details": normalized_vendor_details,
            "vendor_spec_details": normalized_vendor_details,
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
    include_related: bool = False,
    include_partial_candidates: bool = False,
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

    Identity: modeled families require sold-item identity evidence. include_related=True
    browses the broad category; product_identity and spec_subjects disclose what each
    listing sells and which entity its attributes describe. Title rules are evidence,
    not independent verification. describe_catalog.identity_policy lists modeled families.

    Natural-Language Queries: runtime requirements (e.g. "power an Arduino for 8 hours")
    are preserved in data.unresolved_constraints separately from retrieval terms.
    requirements_verified=False means the results cannot establish the application fit.
    Required terms are never dropped or switched to any-term matching. Only
    missing generic category words may be corrected automatically. Identity
    changes are suggestions requiring a new request, never confirmed results.

    Partial Discovery: include_partial_candidates=True optionally returns a separate
    data.partial_candidates section when strict compound filters match nothing. Candidates
    satisfy at least one constraint, have unknown constraints, and explicitly fail none.
    They never contribute to total_matches, returned, or pagination.

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
        try:
            filter_plan = prepare_filters(
                conn, filters, metadata=catalog_specs, category=category_norm,
                category_keys=category_keys, vendor_filters=vendor_filters,
                include_vendor_specs=include_vendor_specs,
            )
        except FilterPlanError as exc:
            return make_error(**exc.error)
        coercions = filter_plan.coercions
        norm_filters = filter_plan.filters
        requested_filters = filter_plan.requested_filters
        norm_vendor_filters = filter_plan.vendor_filters
        vendor_meta = filter_plan.vendor_metadata
        alias_filters = filter_plan.aliases
        alias_disclosed = filter_plan.alias_disclosed
        alias_applied = filter_plan.alias_routes
        tier_routes = filter_plan.tier_routes

        require_spec_scope = bool(category_keys)

        syntax_error = _unsupported_query_error(query)
        if syntax_error:
            return syntax_error

        query_original = (query or "").strip() or None
        price_max_source = "argument" if price_max_norm is not None else None
        category_source = "argument" if category_norm else None

        # --- shared identity-preserving text and engineering query plan ---
        query_plan: Optional[QueryPlan] = None
        if query is not None:
            query_plan = resolve_query_plan(query, conn=conn, category=category_norm)
            if query_plan.price_max is not None and (price_max_norm is None or query_plan.price_max < price_max_norm):
                price_max_norm = query_plan.price_max
                price_max_source = "query"
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
            and (query_plan.head_token or query_plan.inferred_category)
        ):
            inferred_category, _ = resolve_category(
                query_plan.inferred_category or query_plan.head_token, _populated_categories(conn)
            )
            if inferred_category:
                category_norm = inferred_category
                category_source = "query_head"
                category_keys = _category_spec_keys(conn, category_norm)
                require_spec_scope = bool(category_keys)

        # --- cursor is bound to the query that produced it ---
        fingerprint = _query_fingerprint(
            requested_filters,
            query_original,
            price_max_norm,
            in_stock_only,
            sort,
            category_norm,
            vendor_filters=norm_vendor_filters,
            include_related=include_related,
            include_vendor_specs=include_vendor_specs,
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
            "include_related": bool(include_related),
            "identity_policy": "discovery" if include_related else "component_when_modeled",
            "sort": sort,
            "sort_source": "query_intent" if sort_inferred else "argument",
            "limit": effective_limit,
            "offset": offset,
            "refresh": bool(refresh),
            "force_refresh": bool(force_refresh),
            "price_zero_excluded": False,
            "price_zero_policy": "zero_price_rows_included_in_all_sort_orders",
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
                query_plan=query_plan,
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
        count_sql, count_params, select_sql, select_params = build_catalog_statements(
            norm_filters or {},
            catalog_specs,
            query_plan=query_plan,
            fts_tokens=(query_plan.effective_tokens if query_plan else None),
            fts_relaxed=(query_plan.is_relaxed if query_plan else False),
            price_max=price_max_norm,
            in_stock_only=in_stock_only,
            category=category_norm,
            include_related=include_related,
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
            include_related=include_related,
            sort=sort, head_token=head_token,
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
            unscoped_sql, unscoped_params, _, _ = build_catalog_statements(
                norm_filters or {},
                catalog_specs,
                query_plan=query_plan,
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
            base_sql, base_params, predicates = _constraint_scope(
                conn, catalog_specs, norm_filters or {}, query_plan, price_max_norm,
                in_stock_only, category_norm, _category_spec_keys(conn, category_norm) if category_norm else None, require_spec_scope,
                alias_filters, norm_vendor_filters, vendor_meta, include_vendor_specs,
                include_related, sort, head_token,
            )
            per_constraint_counts = individual_match_counts(conn, base_sql, base_params, predicates)
            individual_counts = list(per_constraint_counts.values())
            zero_keys = {key: value for key, value in (requested_filters or {}).items()
                         if per_constraint_counts.get(key) == 0}
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
            if include_partial_candidates:
                base_sql, base_params, predicates = _constraint_scope(
                    conn, catalog_specs, norm_filters or {}, query_plan, price_max_norm,
                    in_stock_only, category_norm, active_category_keys, require_spec_scope,
                    alias_filters, norm_vendor_filters, vendor_meta, include_vendor_specs,
                    include_related, sort, head_token,
                )
                zero_data["confirmed_matches"] = []
                zero_data["partial_candidates"] = discover_candidates(
                    conn, base_sql, base_params, predicates, effective_limit,
                )
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
                query_plan=query_plan,
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
            # Fixed SQL windows give pagination a stable ordering without
            # loading/ranking the entire catalog on every page. A window's
            # membership never changes with page size or cursor offset.
            rank_window = 400
            pool_offset = (offset // rank_window) * rank_window
            pool_limit = (((offset + effective_limit - 1) // rank_window) + 1) * rank_window - pool_offset
            _, _, rank_sql, rank_params = build_catalog_statements(
                norm_filters or {},
                catalog_specs,
                query_plan=query_plan,
                fts_tokens=query_plan.effective_tokens,
                fts_relaxed=query_plan.is_relaxed,
                price_max=price_max_norm,
                in_stock_only=in_stock_only,
                category=category_norm,
                include_related=include_related,
                category_spec_keys=active_category_keys,
                require_spec_scope=require_spec_scope,
                sort=sort,
                limit=pool_limit,
                offset=pool_offset,
                head_token=head_token,
                alias_filters=alias_filters,
                hybrid_vendor=bool(include_vendor_specs),
                vendor_filters=norm_vendor_filters,
                vendor_specs=vendor_meta,
            )
            cur.execute(rank_sql, rank_params)
            candidate_rows = cur.fetchall()
            candidate_products = [db.row_to_unified_product(r) for r in candidate_rows]
            bonus = category_spec_bonus(
                conn,
                [p.id for p in candidate_products if p.id is not None],
                active_category_keys,
            )
            if norm_filters or norm_vendor_filters:
                ids_for_bonus = [p.id for p in candidate_products if p.id is not None]
                completeness = spec_completeness_bonus(conn, ids_for_bonus)
                bonus = {
                    pid: bonus.get(pid, 0.0) + completeness.get(pid, 0.0)
                    for pid in set(bonus) | set(completeness)
                }
            ranked = []
            for window_start in range(0, len(candidate_products), rank_window):
                ranked.extend(rank_products(
                    candidate_products[window_start:window_start + rank_window],
                    query_original or effective_query, bonus_by_id=bonus or None,
                    preferences=query_plan.preference_tokens,
                ))
            relative_offset = offset - pool_offset
            page_products = ranked[relative_offset:relative_offset + effective_limit]
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
        vendor_specs_by_product: Dict[int, Dict[str, Any]] = {}
        vendor_details_by_product: Dict[int, Dict[str, Any]] = {}
        categories_by_id = {int(row["id"]): row["category_canonical"] for row in rows}
        if ids and include_vendor_specs:
            id_ph = ", ".join(["?"] * len(ids))
            cur.execute(
                f"SELECT product_id, key, value, value_num, value_type, unit, raw_key, confidence, source FROM product_vendor_specs "
                f"WHERE product_id IN ({id_ph}) ORDER BY product_id, key;",
                list(ids),
            )
            for pid, key, value, number, value_type, unit, raw_key, confidence, source in cur.fetchall():
                if not vendor_alias_is_current(categories_by_id[int(pid)], key, raw_key, source):
                    continue
                vendor_specs_by_product.setdefault(int(pid), {})[str(key)] = value
                vendor_details_by_product.setdefault(int(pid), {})[str(key)] = {
                    "value": value, "value_num": number, "type": value_type,
                    "unit": unit, "raw_key": raw_key, "confidence": confidence,
                    "source": source, "tier": "vendor",
                }
        results = []
        category_keys_map = _applicable_spec_keys(conn)
        for r in rows:
            pid = r["id"]
            cur.execute(
                "SELECT key, value, confidence, source, evidence, value_num, value_type FROM product_specs WHERE product_id = ?;", (pid,)
            )
            specs: Dict[str, Any] = {}
            low_confidence_specs: List[str] = []
            spec_details: Dict[str, Any] = {}
            for spec_key, spec_value, conf, source, evidence, number, value_type in cur.fetchall():
                specs[spec_key] = spec_value
                spec_details[spec_key] = {"value": spec_value, "value_num": number,
                                          "type": value_type, "confidence": conf, "source": source,
                                          "evidence": evidence, "tier": "promoted"}
                try:
                    if conf is not None and float(conf) < 1.0:
                        low_confidence_specs.append(spec_key)
                except (TypeError, ValueError):
                    pass

            vendor_for_row = vendor_specs_by_product.get(pid, {})
            spec_sources = {k: "promoted" for k in specs}
            visible_requested_keys = set(requested_filters) | set(norm_vendor_filters or {})
            for group in alias_filters:
                visible_requested_keys.update(group.get("keys") or [])
            vendor_key_map = VENDOR_KEY_ALIASES.get(r["category_canonical"] or "") or {}
            visible_requested_keys.update(vendor_key_map.get(key, key) for key in list(visible_requested_keys))
            for vk, vv in vendor_for_row.items():
                target = (VENDOR_KEY_ALIASES.get(r["category_canonical"] or "") or {}).get(vk, vk)
                if target != vk:
                    continue
                if compact and vk not in visible_requested_keys:
                    continue
                if vk not in specs:
                    specs[vk] = vv
                    spec_sources[vk] = "vendor"
                    spec_details[vk] = vendor_details_by_product[pid][vk]
                    if float(spec_details[vk].get("confidence") or 0) < 1.0:
                        low_confidence_specs.append(vk)

            identity = identify_product(r["title"], r["category_canonical"] or "")
            subjects = {key: spec_subject(r["title"], r["category_canonical"] or "", key) for key in specs}
            for key, details in spec_details.items():
                details["subject"] = subjects[key]
            related_specs = {key: specs.pop(key) for key in list(specs) if subjects[key] != "sold_item"}
            spec_sources = {key: value for key, value in spec_sources.items() if key in specs}

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
                "product_role": classify_role(r["title"], r["category_canonical"] or ""),
                "product_identity": identity.to_dict(),
                "spec_subjects": subjects,
                "related_specs": related_specs,
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
                "spec_details": spec_details,
                "spec_count": len(specs),
                "vendor_specs": vendor_for_row,
                "vendor_spec_details": vendor_details_by_product.get(pid, {}),
                "available_vendor_spec_keys": sorted(vendor_for_row),
                "vendor_spec_count": attribute_counts.get(pid, 0),
                "specs_not_extracted": specs_not_extracted,
                "low_confidence_specs": low_confidence_specs,
            })

        # Page coverage measures serialized sold-item key presence, not raw
        # rows or a sum of overlapping promoted/vendor/alias populations.
        coverage_keys = set(all_catalog_keys) | set(norm_vendor_filters or {}) | set(alias_disclosed)
        page_coverage = {}
        for key in coverage_keys:
            equivalent = set(alias_disclosed.get(key, [key]))
            present = sum(bool(equivalent.intersection(row["specs"])) for row in results)
            page_coverage[key] = {"present": present, "of": len(results),
                                  "pct": round(present / max(len(results), 1) * 100, 1),
                                  "measure": "sold_item_key_presence"}
        for key, entry in coverage.items():
            entry["page"] = page_coverage.get(key, {"present": 0, "of": len(results), "pct": 0.0})

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
                    key_sql, key_params, _, _ = build_catalog_statements(
                        {key: entry.get("value")},
                        catalog_specs,
                        query_plan=query_plan,
                        fts_tokens=(query_plan.effective_tokens if query_plan else None),
                        fts_relaxed=(query_plan.is_relaxed if query_plan else False),
                        price_max=price_max_norm,
                        in_stock_only=in_stock_only,
                        category=category_norm,
                        include_related=include_related,
                        hybrid_vendor=bool(include_vendor_specs),
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
            query_plan=query_plan,
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

        product_count = int(cur.execute("SELECT COUNT(*) FROM products").fetchone()[0])
        spec_product_count = int(cur.execute(
            "SELECT COUNT(DISTINCT ps.product_id) FROM product_specs ps JOIN products p ON p.id = ps.product_id"
        ).fetchone()[0])
        spec_row_count = int(cur.execute("SELECT COUNT(*) FROM product_specs").fetchone()[0])
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
                spec_product_count / max(product_count, 1) * 100, 2
            ),
            "spec_coverage": {"present": spec_product_count, "of": product_count,
                              "unit": "products", "definition": "Products with at least one promoted specification"},
            "spec_rows_per_product": round(spec_row_count / max(product_count, 1), 4),
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

    Matching: separators and case are ignored. 'MPU6050', 'mpu-6050' and 'MPU 6050'
    have the same identity decision; this mixed chip/module identifier needs
    allow_ambiguous=True for broad listing inspection. Related accessories are
    excluded by default. Each returned listing carries its sold form and specs.

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

    Example Invocation: search_by_mpn(mpn="MY1016ZL")
    """
    needle = (mpn or "").strip()
    if not needle:
        return make_error(
            error_code="invalid_filter_value",
            message="mpn must be a non-empty string.",
            suggestions=[{"mpn": "MY1016ZL"}]
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

    fingerprint = _query_fingerprint({"mpn": normalized, "allow_ambiguous": allow_ambiguous}, None, None, False, "relevance")

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
        all_rows, raw_total = db.find_products_by_mpn(needle, limit=1_000_000, offset=0)
        eligible_rows = all_rows if allow_ambiguous else [r for r in all_rows if sold_form(r["title"]) not in {"accessory", "enclosure", "bundle", "charger"}]
        forms = {}
        for r in eligible_rows:
            forms.setdefault(sold_form(r["title"]), []).append(int(r["id"]))
        if len(set(forms) - {"unknown"}) > 1 and not allow_ambiguous:
            return make_error(error_code="mpn_ambiguous",
                              message="This identifier is shared by different sold forms; select a listing or use allow_ambiguous=True to inspect them.",
                              details={"sold_forms": forms, "raw_matches": raw_total},
                              suggestions=[{"allow_ambiguous": True}, {"tool": "search_components", "query": needle}])
        total = len(eligible_rows)
        rows = eligible_rows[offset:offset + effective_limit]
        if total != raw_total:
            warnings.append({"code": "mpn_related_listings_excluded", "excluded": raw_total - total,
                             "message": "Accessory, enclosure, charger and kit listings are excluded from exact lookup."})

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
                refreshed, _ = db.find_products_by_mpn(needle, limit=1_000_000, offset=0)
                selected_ids = {int(r["id"]) for r in rows}
                rows = [r for r in refreshed if int(r["id"]) in selected_ids]

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
        for listing in listings:
            title = listing["title"]
            identity = identify_product(title)
            listing["product_role"] = classify_role(title)
            listing["product_identity"] = identity.to_dict()
            listing["sold_form"] = sold_form(title)
            subjects = {key: spec_subject(title, "", key) for key in listing["specs"]}
            listing["spec_subjects"] = subjects
            listing["related_specs"] = {key: value for key, value in listing["specs"].items() if subjects[key] != "sold_item"}
            listing["specs"] = {key: value for key, value in listing["specs"].items() if subjects[key] == "sold_item"}

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
