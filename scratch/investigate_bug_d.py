import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from normalizer import (
    extract_spec_signature,
    match_components_v2,
    extract_hardware_specs,
    extract_mpn,
    canonical_base_mpn,
)

title_a = "Raspberry Pi 5 Model B - 4GB RAM"
title_b = "Raspberry Pi 5 Model 16GB RAM"
price_a = 11200.0
price_b = 34449.0

print("\n--- 1. SPEC SIGNATURE & RAM EXTRACTION ---")
sig_a = extract_spec_signature(title_a)
sig_b = extract_spec_signature(title_b)
specs_a = extract_hardware_specs(title_a)
specs_b = extract_hardware_specs(title_b)

print(f"Title A: '{title_a}'")
print(f"  extract_spec_signature(title_a): '{sig_a}'")
print(f"  extract_hardware_specs(title_a): {specs_a}")
print(f"  RAM field A: {specs_a.get('ram', 'not extracted')}")

print(f"\nTitle B: '{title_b}'")
print(f"  extract_spec_signature(title_b): '{sig_b}'")
print(f"  extract_hardware_specs(title_b): {specs_b}")
print(f"  RAM field B: {specs_b.get('ram', 'not extracted')}")

print("\n--- 2. MPN EXTRACTION ---")
mpn_a = extract_mpn(title_a)
mpn_b = extract_mpn(title_b)
base_a = canonical_base_mpn(mpn_a) if mpn_a else None
base_b = canonical_base_mpn(mpn_b) if mpn_b else None
print(f"MPN A: {mpn_a} (base: {base_a})")
print(f"MPN B: {mpn_b} (base: {base_b})")

print("\n--- 3. MATCH_COMPONENTS_V2 ---")
matched, score, reason = match_components_v2(title_a, title_b, price_a, price_b)
print(f"match_components_v2 result: matched={matched}, score={score}")
print(f"reason: '{reason}'")

print("\n--- 4. SYNTHETIC PAIR TEST WITH KNOWN RAM ---")
# Test a synthetic pair where both have extracted RAM: 4GB vs 8GB with matching MPN
title_4gb = "Raspberry Pi 5 4GB RAM Development Board"
title_8gb = "Raspberry Pi 5 8GB RAM Development Board"
specs_4gb = extract_hardware_specs(title_4gb)
specs_8gb = extract_hardware_specs(title_8gb)
print(f"specs_4gb: {specs_4gb}")
print(f"specs_8gb: {specs_8gb}")
matched_syn, score_syn, reason_syn = match_components_v2(title_4gb, title_8gb, 5000, 7000)
print(f"Synthetic 4GB vs 8GB match_components_v2: matched={matched_syn}, reason='{reason_syn}'")

# Test synthetic pair with 4GB vs 16GB
specs_16gb = extract_hardware_specs("Raspberry Pi 5 16GB RAM Development Board")
print(f"specs_16gb: {specs_16gb}")
matched_16, score_16, reason_16 = match_components_v2(title_4gb, "Raspberry Pi 5 16GB RAM Development Board", 5000, 15000)
print(f"Synthetic 4GB vs 16GB match_components_v2: matched={matched_16}, reason='{reason_16}'")

print("\n--- 5. CHECK EXACT LIVE PRODUCTS ---")
conn = db.get_db_connection()
rows = conn.execute("SELECT vendor, title, price, mpn FROM products WHERE title LIKE '%Raspberry Pi 5%' AND (title LIKE '%4GB%' OR title LIKE '%16GB%')").fetchall()
for r in rows:
    print(f"  DB: {r['vendor']} | {r['title']} | Rs. {r['price']} | mpn: {r['mpn']}")

if len(rows) >= 2:
    r0, r1 = rows[0], rows[1]
    m_db, s_db, re_db = match_components_v2(r0['title'], r1['title'], r0['price'], r1['price'])
    print(f"\nLive pair test between:")
    print(f"  [A] {r0['title']} (Rs. {r0['price']})")
    print(f"  [B] {r1['title']} (Rs. {r1['price']})")
    print(f"  Result: matched={m_db}, score={s_db}, reason='{re_db}'")
