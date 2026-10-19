import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.search.retrieval import search_catalog_ranked
from src.search.query_intent import get_query_intent
from src.search.canonical_boost import adjust_search_score
import db

sys.stdout.reconfigure(encoding='utf-8')

query = "raspberry pi"
intent_res = get_query_intent(query)
print("Intent:", intent_res)

raw_candidates = db.search_products(query=query, limit=150)
print(f"Total candidates: {len(raw_candidates)}")

for rank_idx, prod in enumerate(raw_candidates):
    if prod.id == 4468 or prod.is_canonical:
        rrf_base = 1.0 / (60.0 + (rank_idx + 1))
        base_score = 10.0 * (1.0 + rrf_base * 50.0)
        final_score = adjust_search_score(
            base_score=base_score,
            title=prod.title,
            is_canonical=prod.is_canonical,
            canonical_for=prod.canonical_for,
            intent_result=intent_res,
            query=query
        )
        print(f"Rank in FTS: {rank_idx+1} | Prod: {prod.title} | is_can={prod.is_canonical} | can_for={prod.canonical_for}")
        print(f"   Base: {base_score:.2f} | Final: {final_score:.2f}")

# Compare with the top scored product
top_prod = raw_candidates[0]
rrf_base = 1.0 / (60.0 + 1)
base_score = 10.0 * (1.0 + rrf_base * 50.0)
final_score = adjust_search_score(
    base_score=base_score,
    title=top_prod.title,
    is_canonical=top_prod.is_canonical,
    canonical_for=top_prod.canonical_for,
    intent_result=intent_res,
    query=query
)
print(f"Top FTS product: {top_prod.title} | Final: {final_score:.2f}")
