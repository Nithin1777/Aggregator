import json
import sqlite3

c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

for k in ["voltage", "rpm", "cells", "current_a"]:
    r = cur.execute(
        "select value_type,value_counts,value_min,value_max from spec_stats_global where key=?",
        (k,),
    ).fetchone()
    if not r:
        print(k, "MISSING")
        continue
    print(k, r["value_type"], "minmax", r["value_min"], r["value_max"])
    vc = json.loads(r["value_counts"]) if r["value_counts"] else {}
    print("  top values:", sorted(vc.items(), key=lambda x: -x[1])[:14])
print()

print("categories with motor/gear in name:")
rows = cur.execute(
    "select category, count(*) n from products "
    "where lower(category) like '%motor%' or lower(category) like '%gear%' "
    "group by category order by n desc limit 15"
).fetchall()
for r in rows:
    print("  ", r["category"], r["n"])
print()

print("24V products in price band 4500-6000, motor-ish titles:")
rows = cur.execute(
    "select p.id, p.vendor_name, p.title, p.price, p.available, p.product_url "
    "from products p join product_specs s on s.product_id = p.id "
    "where s.key = 'voltage' and s.value in ('24V','24 V','24v','DC 24V') "
    "and p.price between 4500 and 6000 "
    "and (lower(p.title) like '%motor%' or lower(p.title) like '%gearmotor%') "
    "order by p.price asc limit 40"
).fetchall()
print("  count:", len(rows))
for r in rows:
    print(f"  {r['price']:>7.0f} | {r['vendor_name']} | {r['available']} | {r['title'][:95]}")
    print(f"          {r['product_url']}")
