"""Parameterized catalog eligibility shared by MCP, web search and browsing.

This module owns SQL composition, not connection lifetime, request validation,
ranking policy or response envelopes. Every query uses the products alias ``p``.
"""
from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional, Tuple
from src.search.query_terms import QueryPlan, relaxed_fts_expr, strict_fts_expr

from src.spec.constraints import Predicate, compile_predicates

FTS_ORDER = "bm25(products_fts, 5.0, 2.0, 12.0, 10.0, 0.0)"

@dataclass(frozen=True)
class CatalogQuery:
    predicates: Tuple[Predicate, ...] = ()
    text_expression: Optional[str] = None
    category: Optional[str] = None
    vendor: Optional[str] = None
    in_stock_only: bool = False
    include_related: bool = False
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    zero_price_only: bool = False
    listing_ids: Optional[Tuple[int, ...]] = None
    query_plan: Optional[QueryPlan] = None

    @classmethod
    def from_filters(cls, filters, metadata, *, vendor_filters=None,
                     vendor_metadata=None, hybrid=True, aliases=None, **scope):
        predicates = compile_predicates(
            filters, metadata, vendor_filters=vendor_filters,
            vendor_metadata=vendor_metadata, hybrid=hybrid, aliases=aliases,
        )
        return cls(predicates=tuple(predicates), **scope)

    def relation(self):
        clauses, params = [], []
        joins = ""
        expression = self.query_plan.simple_expr if self.query_plan else self.text_expression
        if expression:
            joins = " JOIN products_fts fts ON p.id = fts.rowid"
            clauses.append("products_fts MATCH ?")
            params.append(expression)
        for predicate in (*self.predicates, *(self.query_plan.predicates if self.query_plan else [])):
            clauses.append(predicate.match_sql)
            params.extend(predicate.match_params)
        if self.query_plan:
            if not self.query_plan.resolved:
                clauses.append("0")
            if self.query_plan.identity_policy:
                clauses.append("aggregate_query_identity(p.title, p.mpn, p.sku, ?) = 1")
                params.append(json.dumps(self.query_plan.identity_policy, sort_keys=True))
            if self.query_plan.price_min is not None:
                clauses.append("p.price >= ?")
                params.append(self.query_plan.price_min)
            if self.query_plan.price_max is not None:
                clauses.append("p.price <= ?")
                params.append(self.query_plan.price_max)
        if self.category:
            clauses.append("p.category_canonical = ?")
            params.append(self.category)
            if not self.include_related:
                clauses.append("aggregate_identity_matches(p.title, p.category_canonical, ?) = 1")
                params.append(self.category)
        if self.vendor:
            clauses.append("p.vendor = ?")
            params.append(self.vendor)
        if self.in_stock_only:
            clauses.append("p.available = 1")
        if self.price_min is not None:
            clauses.append("p.price >= ?")
            params.append(float(self.price_min))
        if self.price_max is not None:
            clauses.append("p.price <= ?")
            params.append(float(self.price_max))
        if self.zero_price_only:
            clauses.append("p.price <= 0")
        if self.listing_ids is not None:
            clauses.append("p.id IN (" + ",".join("?" for _ in self.listing_ids) + ")" if self.listing_ids else "0")
            params.extend(self.listing_ids)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return "FROM products p" + joins + where, params

    def count_statement(self):
        relation, params = self.relation()
        return f"SELECT COUNT(DISTINCT p.id) {relation};", params

    def ids_statement(self):
        """Composable population for diagnostics, without rewriting count SQL."""
        relation, params = self.relation()
        return f"SELECT DISTINCT p.id {relation}", params

    def select_statement(self, *, order_by="p.id ASC", order_params=(), limit=10, offset=0):
        # order_by comes only from application policy, never request text.
        relation, params = self.relation()
        return (f"SELECT p.* {relation} ORDER BY {order_by} LIMIT ? OFFSET ?;",
                params + list(order_params) + [limit, offset])


def build_catalog_statements(
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
    include_related: bool = False,
    query_plan: Optional[QueryPlan] = None,
) -> Tuple[str, List[Any], str, List[Any]]:
    """Builds parameterized SQL for count and select queries.

    Active callers supply the shared query_plan, which owns required terms,
    identity and inferred engineering predicates. Token arguments remain for
    compatibility with older internal callers.
    """
    text_expression = None
    if fts_tokens:
        text_expression = relaxed_fts_expr(fts_tokens) if fts_relaxed else strict_fts_expr(fts_tokens)
    catalog_query = CatalogQuery.from_filters(
        normalized_filters, advertised_specs, hybrid=hybrid_vendor,
        vendor_filters=vendor_filters, vendor_metadata=vendor_specs, aliases=alias_filters,
        text_expression=text_expression, category=category, include_related=include_related,
        price_max=price_max, in_stock_only=in_stock_only, zero_price_only=zero_price_only,
        query_plan=query_plan,
    )

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

    has_text = bool(query_plan.simple_expr) if query_plan else bool(fts_tokens)
    if sort == "price_asc":
        order_by = "ORDER BY p.price ASC"
    elif sort == "price_desc":
        order_by = "ORDER BY p.price DESC"
    elif spec_boost and has_text:
        order_by = f"ORDER BY {spec_boost}, {FTS_ORDER}"
    elif spec_boost:
        order_by = f"ORDER BY {spec_boost}, p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"
    elif has_text:
        order_by = f"ORDER BY {FTS_ORDER}"
    else:
        order_by = "ORDER BY p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"

    order_by += ", p.id ASC"
    count_sql, params = catalog_query.count_statement()
    select_sql, select_params = catalog_query.select_statement(
        order_by=order_by.removeprefix("ORDER BY "), order_params=order_params,
        limit=limit, offset=offset,
    )
    return count_sql, params, select_sql, select_params
