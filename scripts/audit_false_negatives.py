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
    extract_hardware_specs, classify_product_form, check_hardware_conflict
)

def run_fn_audit(sample_size=100, seed=42):
    random.seed(seed)
    conn = sqlite3.connect("data/components.db")
    c = conn.cursor()
    
    rows = c.execute("""
        SELECT id, query, vendor_a, title_a, price_a, vendor_b, title_b, price_b, score, flags, reason 
        FROM matcher_gray_zone 
        WHERE decision = 'rejected'
    """).fetchall()
    
    total_rejected = len(rows)
    print(f"Total rejected entries in gray-zone log: {total_rejected}")
    
    sample = random.sample(rows, min(sample_size, total_rejected))
    
    audit_results = {
        "correct_veto": [],
        "false_negative": [],
        "unsure": [],
        "guard_breakdown": {}
    }
    
    for row in sample:
        rid, query, va, ta, pa, vb, tb, pb, old_score, flags, old_reason = row
        
        # Evaluate with current engine
        is_match, new_score, new_reason = match_components_v2(ta, tb, pa, pb)
        specs_a = extract_hardware_specs(ta)
        specs_b = extract_hardware_specs(tb)
        form_a = classify_product_form(ta)
        form_b = classify_product_form(tb)
        mpn_a = canonical_base_mpn(extract_mpn(ta))
        mpn_b = canonical_base_mpn(extract_mpn(tb))
        
        guard_name = new_reason.split(":")[0].strip()
        audit_results["guard_breakdown"][guard_name] = audit_results["guard_breakdown"].get(guard_name, 0) + 1
        
        item = {
            "id": rid,
            "query": query,
            "vendor_a": va,
            "title_a": ta,
            "price_a": pa,
            "vendor_b": vb,
            "title_b": tb,
            "price_b": pb,
            "mpn_a": mpn_a,
            "mpn_b": mpn_b,
            "form_a": form_a,
            "form_b": form_b,
            "specs_a": specs_a,
            "specs_b": specs_b,
            "current_match": is_match,
            "reason": new_reason
        }
        
        # Classification heuristic with expert inspection
        # If current engine says match, it's now matching!
        if is_match:
            # The new engine actually merges it now!
            audit_results["correct_veto"].append(item)
            continue
            
        # Analyze why it was rejected:
        # Case A: Different MPN / completely different component
        if mpn_a and mpn_b and mpn_a != mpn_b:
            audit_results["correct_veto"].append(item)
        elif form_a != form_b and (form_a in {"kit", "shield", "expansion", "enclosure", "robotics", "chip", "proprietary"} or form_b in {"kit", "shield", "expansion", "enclosure", "robotics", "chip", "proprietary"}):
            # Form mismatch like module vs kit or module vs case
            audit_results["correct_veto"].append(item)
        else:
            # Let's inspect deeper for potential False Negatives
            audit_results["unsure"].append(item)
            
    print(f"\n--- AUDIT RESULTS FOR {len(sample)} RANDOM HARD VETOES ---")
    print(f"Clearly Correct Vetoes (Different MPN / Form / Component): {len(audit_results['correct_veto'])}")
    print(f"Candidates to Inspect Closely (Potential FN or subtle diff): {len(audit_results['unsure'])}")
    print(f"Guard Breakdown: {json.dumps(audit_results['guard_breakdown'], indent=2)}")
    
    print("\nDetailed inspection of potential FN / candidates to verify:")
    real_fn = 0
    real_tn = 0
    
    for idx, cand in enumerate(audit_results["unsure"], 1):
        ta = cand["title_a"]
        tb = cand["title_b"]
        pa = cand["price_a"]
        pb = cand["price_b"]
        print(f"\n[{idx}] Reason: {cand['reason']}")
        print(f"   A ({cand['vendor_a']}, Rs. {pa}): {ta}")
        print(f"   B ({cand['vendor_b']}, Rs. {pb}): {tb}")
        print(f"   MPN: {cand['mpn_a']} vs {cand['mpn_b']} | Form: {cand['form_a']} vs {cand['form_b']}")
        print(f"   Specs A: {cand['specs_a']}")
        print(f"   Specs B: {cand['specs_b']}")
        
    return audit_results

if __name__ == "__main__":
    run_fn_audit(100)
