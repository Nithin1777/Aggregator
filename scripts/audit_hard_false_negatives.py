import sqlite3
import random
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from normalizer import (
    match_components_v2, extract_mpn, canonical_base_mpn,
    extract_hardware_specs, classify_product_form, calculate_token_similarity,
    apply_synonyms, normalize_title
)

def run_hard_fn_audit(sample_size=50, seed=42):
    random.seed(seed)
    conn = sqlite3.connect("data/components.db")
    c = conn.cursor()
    
    rows = c.execute("""
        SELECT id, query, vendor_a, title_a, price_a, vendor_b, title_b, price_b, score, flags, reason 
        FROM matcher_gray_zone 
        WHERE decision = 'rejected'
    """).fetchall()
    
    print(f"Total rejected rows in matcher_gray_zone: {len(rows)}", flush=True)
    
    hard_candidates = []
    seen_pairs = set()
    
    for r in rows:
        rid, query, va, ta, pa, vb, tb, pb, logged_score, flags, logged_reason = r
        if va == vb:
            continue
            
        pair_key = tuple(sorted([ta, tb]))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        
        # Calculate token similarity
        na = apply_synonyms(normalize_title(ta))
        nb = apply_synonyms(normalize_title(tb))
        sim = calculate_token_similarity(na, nb)
        
        # Check current engine decision
        is_m, cur_score, cur_reason = match_components_v2(ta, tb, pa or 0.0, pb or 0.0)
        
        # Filter for HARD candidate:
        # 1. High lexical similarity (sim >= 0.50 or logged_score >= 0.50 or cur_score >= 0.50)
        effective_sim = max(sim, logged_score or 0.0, cur_score or 0.0)
        if effective_sim < 0.50:
            continue
            
        # 2. Must still be rejected by current engine
        if is_m:
            continue
            
        # 3. Must be NON-MPN veto (not a differing canonical MPN like ESP32 vs ESP8266 or DHT11 vs DHT22)
        mpn_a = canonical_base_mpn(extract_mpn(ta))
        mpn_b = canonical_base_mpn(extract_mpn(tb))
        
        if mpn_a and mpn_b and mpn_a != mpn_b:
            # Different MPN -> skip, this is a standard MPN veto
            continue
            
        if "Tier A.5 MPN conflict" in cur_reason:
            continue
            
        specs_a = extract_hardware_specs(ta)
        specs_b = extract_hardware_specs(tb)
        form_a = classify_product_form(ta)
        form_b = classify_product_form(tb)
        
        hard_candidates.append({
            "id": rid,
            "query": query,
            "vendor_a": va,
            "title_a": ta,
            "price_a": pa,
            "vendor_b": vb,
            "title_b": tb,
            "price_b": pb,
            "sim": effective_sim,
            "mpn_a": mpn_a,
            "mpn_b": mpn_b,
            "form_a": form_a,
            "form_b": form_b,
            "specs_a": specs_a,
            "specs_b": specs_b,
            "reason": cur_reason
        })
        
    print(f"Total hard non-MPN veto candidate pairs with sim >= 0.50: {len(hard_candidates)}", flush=True)
    
    # Sort or sample 50 diverse candidates across various reasons
    # Group by reason category to ensure we sample across spec guards, form guards, price gaps, discriminators
    sampled = []
    by_category = {}
    for cand in hard_candidates:
        cat = cand["reason"].split(":")[0].strip()
        by_category.setdefault(cat, []).append(cand)
        
    print("\nCandidates available by veto category:")
    for cat, items in by_category.items():
        print(f"  - {cat:35}: {len(items)} pairs")
        
    # Balanced sampling across categories up to sample_size
    cats = list(by_category.keys())
    random.shuffle(cats)
    idx = 0
    while len(sampled) < sample_size and any(len(v) > 0 for v in by_category.values()):
        cat = cats[idx % len(cats)]
        if by_category[cat]:
            sampled.append(by_category[cat].pop(0))
        idx += 1
        
    print(f"\nSampled exactly {len(sampled)} hard non-MPN veto pairs for detailed inspection.", flush=True)
    return sampled

if __name__ == "__main__":
    sampled = run_hard_fn_audit(50)
    for idx, cand in enumerate(sampled, 1):
        print(f"\n[{idx}] Category: {cand['reason']}")
        print(f"    Sim: {cand['sim']:.2f} | Query: {cand['query']}")
        print(f"    Vendor A ({cand['vendor_a']}, Rs. {cand['price_a']}): {cand['title_a']}")
        print(f"    Vendor B ({cand['vendor_b']}, Rs. {cand['price_b']}): {cand['title_b']}")
        print(f"    MPN: {cand['mpn_a']} vs {cand['mpn_b']} | Form: {cand['form_a']} vs {cand['form_b']}")
        print(f"    Specs A: {cand['specs_a']}")
        print(f"    Specs B: {cand['specs_b']}")
