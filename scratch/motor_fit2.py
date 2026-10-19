import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

for term in ["keyway", "pcd", "12mm shaft", "52mm", "ig52"]:
    print(f"\n=== title LIKE %{term}%")
    for r in cur.execute(
        "select title, price, vendor_name, available, product_url from products "
        "where lower(title) like ? order by price asc limit 14",
        (f"%{term}%",),
    ):
        print(f"  Rs {r['price']:>7.0f} | {'IN ' if r['available'] else 'OUT'} | {r['vendor_name']:<16} | {r['title'][:92]}")
        print(f"       {r['product_url']}")

print("\n=== all 24V IG52 / IG55 variants (any price)")
for r in cur.execute(
    "select title, price, vendor_name, available, product_url from products "
    "where (lower(title) like '%ig52%' or lower(title) like '%ig55%') "
    "and lower(title) like '%24v%' order by price asc"
):
    print(f"  Rs {r['price']:>7.0f} | {'IN ' if r['available'] else 'OUT'} | {r['vendor_name']:<16} | {r['title'][:92]}")
