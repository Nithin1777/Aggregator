import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.search.retrieval import search_catalog_ranked
from src.search.query_intent import get_query_intent

sys.stdout.reconfigure(encoding='utf-8')

test_queries = [
    ("arduino", "brand", "Arduino Uno"),
    ("raspberry pi", "brand", "Raspberry Pi"),
    ("esp32", "brand", "ESP32"),
    ("servo", "category", "SG90"),
    ("relay", "category", "Relay"),
    ("lm2596", "mpn", "LM2596"),
    ("dht11", "mpn", "DHT11"),
    ("sg90", "mpn", "SG90")
]

for q, expected_type, hero_substring in test_queries:
    intent = get_query_intent(q)
    results = search_catalog_ranked(query=q, limit=5)
    
    top_1 = results[0] if results else None
    top_3 = results[:3]
    
    # Check canonical presence
    has_canonical_in_top_3 = any(p.is_canonical and p.canonical_for == q for p in top_3)
    rank_of_canonical = None
    for idx, p in enumerate(results, start=1):
        if p.is_canonical and p.canonical_for == q:
            rank_of_canonical = idx
            break

    print(f"\nQUERY: '{q}' (Intent: {intent.intent}, Matched Key: {intent.matched_key})")
    print(f"  Canonical Rank in Live 50K DB: {rank_of_canonical}")
    for idx, p in enumerate(top_3, start=1):
        c_mark = " [★ CANONICAL HERO]" if p.is_canonical and p.canonical_for == q else ""
        print(f"    #{idx}: {p.title} (Rs.{p.price}, {p.vendor}){c_mark}")
