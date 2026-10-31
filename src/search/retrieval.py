"""Ranked catalog retrieval through the shared query policy and eligibility engine."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import db
from models import UnifiedProduct
from src.search.ranking import rank_products
from src.search.service import CatalogSearch


@dataclass
class CatalogResults:
    products: List[UnifiedProduct]
    total_matches: int
    query_contract: Dict[str, Any]


def retrieve_catalog(query: str, limit: int = 50, vendor: Optional[str] = None,
                     in_stock_only: bool = False, category: Optional[str] = None,
                     spec_filters: Optional[Dict[str, Any]] = None,
                     price_min: Optional[float] = None, price_max: Optional[float] = None,
                     listing_ids=None) -> CatalogResults:
    """Count and rank one prepared query on a consistent read snapshot.

    Canonical products receive ranking boosts only after eligibility. There is
    no second candidate injection or hidden alternate-query search.
    """
    connection = db.get_db_connection()
    try:
        connection.execute("BEGIN")
        search = CatalogSearch(connection)
        prepared = search.prepare(
            query, filters=spec_filters, category=category, vendor=vendor,
            in_stock_only=in_stock_only, include_related=not bool(spec_filters),
            price_min=price_min, price_max=price_max, listing_ids=listing_ids,
        )
        if prepared is None:
            return CatalogResults([], 0, {})
        total = search.count(prepared)
        rows = search.rows(prepared, limit=max(100, limit * 3)) if total else []
        products = [db.row_to_unified_product(row) for row in rows]
        plan = prepared.plan
        products = rank_products(products, query, limit=limit,
                                 preferences=plan.preference_tokens if plan else ())
        return CatalogResults(products, total, plan.contract() if plan else {})
    finally:
        connection.close()


def search_catalog_ranked(query: str, limit: int = 50, vendor: Optional[str] = None,
                          in_stock_only: bool = False, category: Optional[str] = None,
                          spec_filters: Optional[Dict[str, Any]] = None,
                          price_min: Optional[float] = None, price_max: Optional[float] = None) -> List[UnifiedProduct]:
    """Compatibility API for callers that only need the matching listings."""
    return retrieve_catalog(query, limit, vendor, in_stock_only, category,
                            spec_filters, price_min, price_max).products
