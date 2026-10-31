"""Shared relevance ordering and engineering completeness signals.

Eligibility is decided before this module receives candidates.
"""
from typing import Dict, List, Optional, Tuple
from models import UnifiedProduct
from src.search.canonical_boost import adjust_search_score
from src.search.product_prior import query_category_hint
from src.search.query_intent import get_query_intent

def rank_products(
    products: List[UnifiedProduct],
    query: str,
    limit: Optional[int] = None,
    bonus_by_id: Optional[Dict[int, float]] = None,
    preferences=(),
) -> List[UnifiedProduct]:
    """
    Shared relevance ranking for every text-search surface (web and MCP).

    Input order is the retrieval order (BM25). Each candidate gets a reciprocal-position base
    score plus the canonical/length/repetition/product-role adjustments, then
    the list is sorted. `bonus_by_id` lets a caller preserve an extra signal
    (the MCP category spec-coverage boost) without duplicating the ranker.
    """
    intent_res = get_query_intent(query)
    category_hint = query_category_hint(query)
    scored_products: List[Tuple[float, UnifiedProduct]] = []
    for rank_idx, prod in enumerate(products):
        # Reciprocal rank from FTS / BM25
        position_score = 1.0 / (60.0 + (rank_idx + 1))
        base_score = 10.0 * (1.0 + position_score * 50.0)

        final_score = adjust_search_score(
            base_score=base_score,
            title=prod.title,
            is_canonical=prod.is_canonical,
            canonical_for=prod.canonical_for,
            intent_result=intent_res,
            query=query,
        )
        if bonus_by_id and prod.id is not None:
            final_score += bonus_by_id.get(prod.id, 0.0)
        if category_hint and prod.category_canonical == category_hint:
            final_score += 8.0
        if preferences:
            from src.search.query_intent import contains_identity_token
            final_score += min(1.0, sum(0.1 for term in preferences
                                       if contains_identity_token(prod.title, term)))
        scored_products.append((final_score, prod))

    scored_products.sort(key=lambda item: item[0], reverse=True)
    ranked = [product for _, product in scored_products]
    return ranked[:limit] if limit is not None else ranked


def spec_completeness_bonus(
    conn,
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


def category_spec_bonus(
    conn,
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
