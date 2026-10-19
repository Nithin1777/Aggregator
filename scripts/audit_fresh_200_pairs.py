import random
import sqlite3
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from config import settings
from normalizer import match_components_v2, extract_spec_signature, canonical_base_mpn, extract_mpn, ComponentItem

random.seed(42)
conn = sqlite3.connect(str(settings.db_path))
conn.row_factory = sqlite3.Row
cur = conn.cursor()
candidate_keys = [
    'esp32', 'esp8266', 'rp2040', 'stm32', 'arduino', 'relay', 'servo', 'bms',
    'oled', 'tft', 'dht11', 'dht22', 'bme280', 'bmp280', 'mpu6050', 'ina219',
    'lm2596', 'xl4015', 'tp4056', 'l298n', 'a4988', 'drb8825', 'ds3231', 'hx711',
    'ads1115', 'max6675', 'ch340', 'cp2102', 'ft232', 'mcp2515', 'max485'
]
candidate_pairs = []
seen = set()
for key in candidate_keys:
    if len(candidate_pairs) >= 200: break
    cur.execute('SELECT id, vendor, title, price, mpn, sku, category FROM products WHERE title LIKE ? AND price > 10.0 LIMIT 40', (f'%{key}%',))
    rows = cur.fetchall()
    robu = [r for r in rows if r['vendor'] == 'robu']
    other = [r for r in rows if r['vendor'] != 'robu']
    for ra in robu:
        for rb in other:
            p_id = tuple(sorted([ra['id'], rb['id']]))
            if p_id in seen: continue
            seen.add(p_id)
            candidate_pairs.append((dict(ra), dict(rb)))
            if len(candidate_pairs) >= 200: break
        if len(candidate_pairs) >= 200: break
conn.close()

def clean_sig(title):
    sig = extract_spec_signature(title)
    return '' if sig == 'std_spec' else sig

def test_can_merge(ca, cb, ta, tb, pa, pb):
    if ca.mpn and cb.mpn and ca.mpn != cb.mpn:
        return False, 'mpn_conflict'
    sig_a = clean_sig(ta)
    sig_b = clean_sig(tb)
    if sig_a and sig_b and sig_a == sig_b:
        return True, 'spec_exact'
    if sig_a and sig_b and sig_a != sig_b:
        return False, 'spec_variant'
    merged, score, reason = match_components_v2(ta, tb, pa, pb)
    return merged, f'deferred_tier_b:{reason}'

print('=== ALL MERGES PERMITTED ON FRESH 200 SET ===')
merges = []
for idx, (ra, rb) in enumerate(candidate_pairs):
    ta, tb = ra['title'], rb['title']
    pa, pb = float(ra['price'] or 0.0), float(rb['price'] or 0.0)
    ca = ComponentItem(ta, pa, mpn=ra['mpn'])
    cb = ComponentItem(tb, pb, mpn=rb['mpn'])
    m, r = test_can_merge(ca, cb, ta, tb, pa, pb)
    if m:
        merges.append((idx, ra, rb, r))
        print(f"[{idx:03d}] {r}")
        print(f"   Vendor A ({ra['vendor']}, Rs. {pa}): {ta}")
        print(f"   Vendor B ({rb['vendor']}, Rs. {pb}): {tb}")

print(f"\nTotal Merges Permitted: {len(merges)} out of 200 pairs.")
