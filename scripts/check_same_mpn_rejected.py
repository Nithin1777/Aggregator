import sqlite3
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from normalizer import match_components_v2, extract_mpn, canonical_base_mpn, extract_hardware_specs, classify_product_form

conn = sqlite3.connect('data/components.db')
c = conn.cursor()
rows = c.execute("SELECT vendor_a, title_a, price_a, vendor_b, title_b, price_b, flags, reason FROM matcher_gray_zone WHERE decision='rejected'").fetchall()

same_mpn_rejected = []
for va, ta, pa, vb, tb, pb, flags, reason in rows:
    ma = canonical_base_mpn(extract_mpn(ta))
    mb = canonical_base_mpn(extract_mpn(tb))
    if ma and mb and ma == mb:
        same_mpn_rejected.append((ma, va, ta, pa, vb, tb, pb, reason))

print(f"Total rejected pairs with IDENTICAL canonical MPN: {len(same_mpn_rejected)}")

unique_same_mpn = {}
for ma, va, ta, pa, vb, tb, pb, reason in same_mpn_rejected:
    key = tuple(sorted([ta, tb]))
    if key not in unique_same_mpn:
        unique_same_mpn[key] = (ma, va, ta, pa, vb, tb, pb, reason)

print(f"Unique pairs with IDENTICAL canonical MPN rejected: {len(unique_same_mpn)}")

now_matches = 0
now_vetoed = 0
rejection_reasons = {}

for key, (ma, va, ta, pa, vb, tb, pb, reason) in unique_same_mpn.items():
    is_m, sc, r = match_components_v2(ta, tb, pa, pb)
    if is_m:
        now_matches += 1
    else:
        now_vetoed += 1
        r_clean = r.split(':')[0]
        rejection_reasons[r_clean] = rejection_reasons.get(r_clean, 0) + 1

print(f"\nUnder current engine:")
print(f"Now Matches (Previously rejected, now allowed): {now_matches}")
print(f"Still Vetoed: {now_vetoed}")
print(f"Still Vetoed Reasons Breakdown: {rejection_reasons}")

print("\n--- SAMPLE OF STILL VETOED IDENTICAL-MPN PAIRS ---")
sample_vetoed = []
for key, (ma, va, ta, pa, vb, tb, pb, reason) in unique_same_mpn.items():
    is_m, sc, r = match_components_v2(ta, tb, pa, pb)
    if not is_m:
        sample_vetoed.append((ma, va, ta, pa, vb, tb, pb, r))

for idx, (ma, va, ta, pa, vb, tb, pb, r) in enumerate(sample_vetoed[:25], 1):
    print(f"\n[{idx}] MPN: {ma} | Veto Reason: {r}")
    print(f"   A ({va}, Rs. {pa}): {ta}")
    print(f"   B ({vb}, Rs. {pb}): {tb}")
    print(f"   Specs: {extract_hardware_specs(ta)} vs {extract_hardware_specs(tb)}")
    print(f"   Forms: {classify_product_form(ta)} vs {classify_product_form(tb)}")
