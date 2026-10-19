import sqlite3
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from normalizer import (
    match_components_v2, extract_mpn, canonical_base_mpn,
    extract_hardware_specs, classify_product_form, check_hardware_conflict
)

conn = sqlite3.connect('data/components.db')
c = conn.cursor()
rows = c.execute("SELECT title_a, price_a, title_b, price_b, risk_level, flags, reason FROM matcher_gray_zone WHERE decision='merged'").fetchall()

seen = set()
for ta, pa, tb, pb, risk, flags, reason in rows:
    pair_key = tuple(sorted([ta, tb]))
    if pair_key in seen:
        continue
    seen.add(pair_key)
    
    is_m, sc, r = match_components_v2(ta, tb, pa, pb)
    if is_m:
        print('=== PAIR ===')
        print('Risk in log:', risk)
        print('A:', ta, f'(Rs. {pa})')
        print('  MPN:', extract_mpn(ta), '-> base:', canonical_base_mpn(extract_mpn(ta)))
        print('  Specs:', extract_hardware_specs(ta))
        print('  Form:', classify_product_form(ta))
        print('B:', tb, f'(Rs. {pb})')
        print('  MPN:', extract_mpn(tb), '-> base:', canonical_base_mpn(extract_mpn(tb)))
        print('  Specs:', extract_hardware_specs(tb))
        print('  Form:', classify_product_form(tb))
        conflict, c_reason = check_hardware_conflict(
            ta, tb, extract_hardware_specs(ta), extract_hardware_specs(tb),
            canonical_base_mpn(extract_mpn(ta)), canonical_base_mpn(extract_mpn(tb)),
            pa, pb
        )
        print('Conflict check:', conflict, c_reason)
        print('Result:', is_m, sc, r)
        print()
