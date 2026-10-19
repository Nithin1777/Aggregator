import json
import random
import sqlite3
from normalizer import match_components_v2
from config import settings

conn = sqlite3.connect(str(settings.db_path))
conn.row_factory = sqlite3.Row

keywords = [
    'esp32', 'nodemcu', 'esp8266', 'ch340', 'cp2102',
    'lm2596', 'ams1117', 'xl4015', 'tp4056',
    'mpu6050', 'bme280', 'bmp280', 'dht11', 'dht22', 'hc-sr04',
    'sg90', 'a4988', 'drv8825', 'l298n', 'oled'
]
random.seed(101)
random.shuffle(keywords)

fresh_pairs = []
seen_pair_keys = set()
for kw in keywords:
    rows = conn.execute(
        "SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url FROM products WHERE title LIKE ? ORDER BY RANDOM() LIMIT 20;",
        (f"%{kw}%",)
    ).fetchall()
    pool = [dict(r) for r in rows]
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            p1, p2 = pool[i], pool[j]
            if p1['vendor'] != p2['vendor']:
                pk = tuple(sorted([f"{p1['vendor']}:{p1['external_id']}", f"{p2['vendor']}:{p2['external_id']}"]))
                if pk not in seen_pair_keys:
                    seen_pair_keys.add(pk)
                    fresh_pairs.append((p1, p2))
                    if len(fresh_pairs) >= 100:
                        break
        if len(fresh_pairs) >= 100:
            break
    if len(fresh_pairs) >= 100:
        break

conn.close()

merges = []
for p1, p2 in fresh_pairs:
    is_m, score, reason = match_components_v2(p1['title'], p2['title'])
    if is_m:
        merges.append((p1, p2, score, reason))

print(f"Total Merges to inspect: {len(merges)}")
for idx, (p1, p2, s, r) in enumerate(merges, 1):
    print(f"\n=== #{idx} [{s:.2f}] {r} ===")
    print(f"  A [{p1['vendor_name']} | Rs.{p1['price']}]: {p1['title']}")
    print(f"  B [{p2['vendor_name']} | Rs.{p2['price']}]: {p2['title']}")
