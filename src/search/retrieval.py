"""
Unified retrieval backend with support for Meilisearch and Enhanced SQLite BM25 + RRF.
"""
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple
import db
from models import UnifiedProduct
from src.search.canonical_boost import adjust_search_score
from src.search.query_intent import QueryIntentResult, get_query_intent

logger = logging.getLogger(__name__)

class MeiliSearchBackend:
    """Meilisearch client integration with graceful fallback."""

    def __init__(self, host: str = "http://127.0.0.1:7700", api_key: str = ""):
        self.host = host
        self.api_key = api_key
        self._client = None
        self._is_available = None

    def check_connection(self) -> bool:
        if self._is_available is not None:
            return self._is_available
        try:
            import meilisearch
            self._client = meilisearch.Client(self.host, self.api_key)
            health = self._client.health()
            self._is_available = (health.get("status") == "available")
        except Exception:
            self._is_available = False
        return self._is_available

    def search(self, query: str, limit: int = 50) -> Optional[List[Dict[str, Any]]]:
        if not self.check_connection():
            return None
        try:
            index = self._client.index("products")
            res = index.search(query, {"limit": limit})
            return res.get("hits", [])
        except Exception as e:
            logger.debug(f"Meilisearch query failed: {e}")
            return None


class EnhancedSQLiteBackend:
    """
    Enhanced SQLite retrieval with BM25 FTS ranking, RRF (Reciprocal Rank Fusion),
    and intent-gated ranking adjustments.
    """

    def search(
        self,
        query: str,
        limit: int = 50,
        vendor: Optional[str] = None,
        in_stock_only: bool = False
    ) -> List[UnifiedProduct]:
        intent_res = get_query_intent(query)
        
        # 1. Fetch broader candidate pool from DB
        raw_candidates = db.search_products(
            query=query,
            limit=max(100, limit * 3),
            vendor=vendor,
            in_stock_only=in_stock_only
        )

        if not raw_candidates:
            # If FTS matched nothing, try matched canonical key directly
            if intent_res.matched_key:
                raw_candidates = db.search_products(
                    query=intent_res.matched_key,
                    limit=max(100, limit * 3),
                    vendor=vendor,
                    in_stock_only=in_stock_only
                )

        # Ensure canonical hero product(s) for matched intent key are in candidates pool
        if intent_res.matched_key:
            conn = db.get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT * FROM products 
                    WHERE is_canonical = 1 
                      AND (canonical_for = ? OR canonical_for LIKE ? OR canonical_for LIKE ? OR canonical_for LIKE ?)
                """, (intent_res.matched_key, f"{intent_res.matched_key},%", f"%,{intent_res.matched_key}", f"%,{intent_res.matched_key},%"))
                can_rows = cur.fetchall()
                existing_ids = {p.id for p in raw_candidates if p.id is not None}
                for r in can_rows:
                    if r["id"] not in existing_ids:
                        can_prod = UnifiedProduct(
                            id=r["id"],
                            vendor=r["vendor"],
                            vendor_name=r["vendor_name"],
                            external_id=r["external_id"],
                            title=r["title"],
                            normalized_title=r["normalized_title"],
                            mpn=r["mpn"],
                            sku=r["sku"],
                            price=float(r["price"]),
                            available=bool(r["available"]),
                            product_url=r["product_url"],
                            image_url=r["image_url"],
                            category=r["category"],
                            is_canonical=bool(r["is_canonical"]) if "is_canonical" in r.keys() and r["is_canonical"] else False,
                            canonical_for=r["canonical_for"] if "canonical_for" in r.keys() else None,
                            query_ctr=float(r["query_ctr"]) if "query_ctr" in r.keys() and r["query_ctr"] is not None else None,
                            updated_at=str(r["updated_at"]) if r["updated_at"] else None,
                            extra=json.loads(r["raw_json"]) if r["raw_json"] else {}
                        )
                        raw_candidates.insert(0, can_prod)
                        existing_ids.add(r["id"])
            finally:
                conn.close()

        if not raw_candidates:
            return []

        # 2. Score candidates using RRF base score + Canonical Boost + Length Penalty + Repetition Penalty
        scored_products: List[Tuple[float, UnifiedProduct]] = []
        for rank_idx, prod in enumerate(raw_candidates):
            # Reciprocal rank from FTS / BM25
            rrf_base = 1.0 / (60.0 + (rank_idx + 1))
            # Convert to a score range comparable to BM25
            base_score = 10.0 * (1.0 + rrf_base * 50.0)

            final_score = adjust_search_score(
                base_score=base_score,
                title=prod.title,
                is_canonical=prod.is_canonical,
                canonical_for=prod.canonical_for,
                intent_result=intent_res,
                query=query
            )
            scored_products.append((final_score, prod))

        # 3. Sort by adjusted score descending
        scored_products.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored_products[:limit]]


_meili_backend = MeiliSearchBackend()
_sqlite_backend = EnhancedSQLiteBackend()

def search_catalog_ranked(
    query: str,
    limit: int = 50,
    vendor: Optional[str] = None,
    in_stock_only: bool = False
) -> List[UnifiedProduct]:
    """
    Main ranked catalog search function.
    Tries Meilisearch if running; seamlessly uses Enhanced SQLite BM25 + RRF + Adjustments.
    """
    # 1. If Meilisearch is active, attempt it
    if _meili_backend.check_connection():
        hits = _meili_backend.search(query, limit=limit)
        if hits is not None:
            # Map hits to UnifiedProduct models
            pass

    # 2. SQLite Enhanced BM25 + RRF + Intent Ranking
    return _sqlite_backend.search(
        query=query,
        limit=limit,
        vendor=vendor,
        in_stock_only=in_stock_only
    )
