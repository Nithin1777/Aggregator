import sqlite3
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from normalizer import (
    calculate_token_similarity, apply_synonyms, normalize_title,
    match_components_v2, extract_mpn, canonical_base_mpn,
    extract_hardware_specs, classify_product_form
)

conn = sqlite3.connect("data/components.db")
c = conn.cursor()

rows = c.execute("SELECT id, vendor_a, title_a, price_a, vendor_b, title_b, price_b, flags, reason FROM matcher_gray_zone WHERE decision='rejected'").fetchall()
print(f"Total rejected pairs in log: {len(rows)}")

# Find pairs with non-trivial token similarity (e.g. sim >= 0.25)
overlapping_pairs = []
for row in rows:
    rid, va, ta, pa, vb, tb, pb, flags, reason = row
    na = apply_synonyms(normalize_title(ta))
    nb = apply_synonyms(normalize_title(tb))
    sim = calculate_token_similarity(na, nb)
    if sim >= 0.20:
        overlapping_pairs.append((sim, row))

overlapping_pairs.sort(key=lambda x: x[0], reverse=True)
print(f"Pairs with token similarity >= 0.20: {len(overlapping_pairs)}")

# Inspect the top 100 highest-similarity vetoes!
print("\n--- AUDITING TOP 100 HIGHEST SIMILARITY VETOES ---")
fn_count = 0
tn_count = 0
unsure_count = 0

sample_to_inspect = overlapping_pairs[:100]

for idx, (sim, row) in enumerate(sample_to_inspect, 1):
    rid, va, ta, pa, vb, tb, pb, flags, reason = row
    is_match, score, cur_reason = match_components_v2(ta, tb, pa, pb)
    
    # Classify
    mpn_a = canonical_base_mpn(extract_mpn(ta))
    mpn_b = canonical_base_mpn(extract_mpn(tb))
    specs_a = extract_hardware_specs(ta)
    specs_b = extract_hardware_specs(tb)
    form_a = classify_product_form(ta)
    form_b = classify_product_form(tb)
    
    # Print out
    print(f"\n[{idx}] Sim: {sim:.2f} | Current Result: Match={is_match} ({cur_reason})")
    print(f"   A ({va}, Rs. {pa}): {ta}")
    print(f"   B ({vb}, Rs. {pb}): {tb}")
    print(f"   MPN: {mpn_a} vs {mpn_b} | Form: {form_a} vs {form_b}")
    print(f"   Specs: {specs_a} vs {specs_b}")
