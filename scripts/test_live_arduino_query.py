import sys
from pathlib import Path

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.search.retrieval import search_catalog_ranked
from src.search.query_intent import get_query_intent

# Force UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

query = "arduino"
intent = get_query_intent(query)
print(f"Query: '{query}'")
print(f"Intent Result: {intent}")

results = search_catalog_ranked(query=query, limit=10)
print(f"\nTop 10 Live Search Results from 50K Catalog for '{query}':")
print("=" * 80)
for idx, p in enumerate(results, start=1):
    canonical_tag = f"[CANONICAL for {p.canonical_for}]" if p.is_canonical else ""
    print(f"#{idx:02d} | ID:{p.id:<6} | {p.vendor:<16} | Rs.{p.price:<7.1f} | {canonical_tag} {p.title}")
print("=" * 80)

# Check if Uno R3 / canonical is in top 3
top_3_is_canonical = any(p.is_canonical and p.canonical_for == "arduino" for p in results[:3])
top_3_has_uno = any("uno" in p.title.lower() for p in results[:3])
print(f"\nCanonical hero in Top 3: {top_3_is_canonical}")
print(f"Uno board in Top 3:      {top_3_has_uno}")
