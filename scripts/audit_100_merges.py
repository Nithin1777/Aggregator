import sqlite3
import random
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from search_service import AggregatorEngine
from normalizer import match_components_v2

# Diverse cross-section of hardware categories
AUDIT_QUERIES = [
    "esp32", "esp8266", "dht11", "dht22", "sg90", "mg996r", "bme280", "bmp280",
    "mpu6050", "ina219", "max6675", "hc-sr04", "ch340", "cp2102", "ft232",
    "lm2596", "xl4015", "ams1117", "tp4056", "mt3608", "l298n", "a4988",
    "oled 0.96", "tft 1.8", "lcd 1602", "ne555", "lm358", "pc817", "irfz44n",
    "pico", "raspberry pi 4", "arduino uno", "arduino nano", "arduino mega"
]

def run_100_merge_audit(target_n=100):
    engine = AggregatorEngine()
    
    collected_merges = []
    seen_pairs = set()
    
    print("=" * 80, flush=True)
    print(f"COLLECTING {target_n} CROSS-VENDOR MERGES ACROSS DIVERSE HARDWARE STREAMS")
    print("=" * 80, flush=True)
    
    for q in AUDIT_QUERIES:
        if len(collected_merges) >= target_n:
            break
            
        local_res = engine.search_local(q, limit=60)
        groups = local_res.comparison_groups
        
        for g in groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        pair_key = tuple(sorted([rep.title, other.title]))
                        if pair_key not in seen_pairs:
                            seen_pairs.add(pair_key)
                            
                            is_m, score, reason = match_components_v2(
                                rep.title, other.title, rep.price or 0.0, other.price or 0.0
                            )
                            
                            # Only include if current engine actually merges it!
                            if is_m:
                                collected_merges.append({
                                    "query": q,
                                    "group": g.canonical_name,
                                    "a_title": rep.title,
                                    "a_vendor": rep.vendor,
                                    "a_price": rep.price,
                                    "b_title": other.title,
                                    "b_vendor": other.vendor,
                                    "b_price": other.price,
                                    "score": score,
                                    "reason": reason
                                })
                                if len(collected_merges) >= target_n:
                                    break
        print(f"Query: '{q:16}' -> Total Merges Collected So Far: {len(collected_merges)}", flush=True)

    print("\n" + "=" * 80, flush=True)
    print(f"AUDITING EXACTLY {len(collected_merges)} PRODUCTION CROSS-VENDOR MERGES")
    print("=" * 80, flush=True)
    
    tp_count = 0
    fp_count = 0
    failures = []
    
    for idx, m in enumerate(collected_merges, 1):
        # Human/Expert validation
        ta = m['a_title']
        tb = m['b_title']
        pa = m['a_price']
        pb = m['b_price']
        
        print(f"\n[{idx}] [{m['query'].upper()}] Group: {m['group'][:65]}")
        print(f"    Vendor A ({m['a_vendor']}, Rs. {pa}): {ta}")
        print(f"    Vendor B ({m['b_vendor']}, Rs. {pb}): {tb}")
        print(f"    Engine Match: Score={m['score']:.2f} | {m['reason']}")

    return collected_merges

if __name__ == "__main__":
    run_100_merge_audit(100)
