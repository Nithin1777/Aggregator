import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from search_service import AggregatorEngine
from normalizer import match_components_v2

KEYWORDS = ["sg90", "hc-sr04", "bme280", "ina219", "raspberry pi", "nano", "mega", "pico"]

def run_fresh_keyword_test():
    engine = AggregatorEngine()
    
    total_cards = 0
    total_multivendor_groups = 0
    pairwise_merges = []
    
    print("=" * 80, flush=True)
    print("FRESH KEYWORD CROSS-VENDOR MERGE AUDIT", flush=True)
    print(f"Keywords: {', '.join(KEYWORDS)}", flush=True)
    print("=" * 80, flush=True)
    
    for kw in KEYWORDS:
        local_res = engine.search_local(kw, limit=50)
        groups = local_res.comparison_groups
        total_cards += len(groups)
        kw_merges = 0
        
        for g in groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                total_multivendor_groups += 1
                kw_merges += 1
                
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        is_m, score, reason = match_components_v2(
                            rep.title, other.title, rep.price or 0.0, other.price or 0.0
                        )
                        pairwise_merges.append({
                            "keyword": kw,
                            "group_name": g.canonical_name,
                            "a_title": rep.title,
                            "a_vendor": rep.vendor,
                            "a_price": rep.price,
                            "b_title": other.title,
                            "b_vendor": other.vendor,
                            "b_price": other.price,
                            "match": is_m,
                            "score": score,
                            "reason": reason
                        })
                        
        print(f"Keyword: '{kw:14}' -> {local_res.total_results:3} local products, {len(groups):3} cards, {kw_merges:2} multi-vendor cards", flush=True)

    print("\n" + "=" * 80, flush=True)
    print(f"SUMMARY:", flush=True)
    print(f"  Total Cards Across 8 Keywords: {total_cards}", flush=True)
    print(f"  Multi-Vendor Cards: {total_multivendor_groups}", flush=True)
    print(f"  Cross-Vendor Item Pairs Merged: {len(pairwise_merges)}", flush=True)
    print("=" * 80, flush=True)
    
    print("\n--- DETAILED AUDIT OF EVERY CROSS-VENDOR MERGE ---", flush=True)
    for idx, m in enumerate(pairwise_merges, 1):
        print(f"\n[{idx}] [{m['keyword'].upper()}] Group: {m['group_name']}", flush=True)
        print(f"    Vendor A ({m['a_vendor']}, Rs. {m['a_price']}): {m['a_title']}", flush=True)
        print(f"    Vendor B ({m['b_vendor']}, Rs. {m['b_price']}): {m['b_title']}", flush=True)
        print(f"    Engine Verdict: {m['match']} | Score: {m['score']:.2f} | Reason: {m['reason']}", flush=True)

    return pairwise_merges

if __name__ == "__main__":
    run_fresh_keyword_test()
