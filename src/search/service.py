"""Local catalog search execution, independent of web and MCP transports.

Connections are supplied by the caller. Planning and eligibility are
shared by row retrieval and counting; execution errors never broaden the query.
"""
from dataclasses import dataclass
from typing import Optional

from src.search.catalog_query import CatalogQuery, FTS_ORDER
from src.search.filter_plan import prepare_filters
from src.search.planner import resolve_query_plan
from src.search.query_terms import QueryPlan


@dataclass(frozen=True)
class PreparedSearch:
    query: CatalogQuery
    plan: Optional[QueryPlan] = None
    original_text: Optional[str] = None

class CatalogSearch:
    def __init__(self, connection):
        self.connection = connection

    def prepare(self, query=None, *, filters=None, category=None, vendor=None,
                in_stock_only=False, include_related=False, price_min=None,
                price_max=None, listing_ids=None):
        text_plan = None
        expression = None
        original = query.strip() if query is not None else None
        if query is not None:
            if not original:
                return None
            text_plan = resolve_query_plan(original, self.connection, category=category)
            if not text_plan.resolved:
                return PreparedSearch(CatalogQuery(query_plan=text_plan), text_plan, original)
            expression = text_plan.simple_expr
            category = category or text_plan.inferred_category
        normalized = prepare_filters(
            self.connection, filters, category=category,
            metadata=None if filters else {},
        )
        catalog_query = CatalogQuery.from_filters(
            normalized.filters, normalized.metadata,
            vendor_filters=normalized.vendor_filters, vendor_metadata=normalized.vendor_metadata,
            aliases=normalized.aliases, text_expression=expression, category=category,
            vendor=vendor, include_related=include_related, in_stock_only=in_stock_only,
            price_min=price_min, price_max=price_max, listing_ids=listing_ids,
            query_plan=text_plan,
        )
        return PreparedSearch(catalog_query, text_plan, original)

    def rows(self, prepared, *, limit=50, offset=0, order_by=None):
        if prepared is None:
            return []
        if order_by is None:
            order_by = f"{FTS_ORDER}, p.id ASC" if prepared.query.text_expression else "p.available DESC, p.price ASC, p.id ASC"
        sql, params = prepared.query.select_statement(order_by=order_by, limit=limit, offset=offset)
        return self.connection.execute(sql, params).fetchall()

    def count(self, prepared):
        if prepared is None:
            return 0
        sql, params = prepared.query.count_statement()
        row = self.connection.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
