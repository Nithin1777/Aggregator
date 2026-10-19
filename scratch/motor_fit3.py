import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

print("=== all 'Mega Torque' listings")
for r in cur.execute(
    "select title, price, vendor_name, available, product_url from products "
    "where lower(title) like '%mega torque%' order by price asc"
):
    print(f"  Rs {r['price']:>7.0f} | {'IN ' if r['available'] else 'OUT'} | {r['title'][:110]}")
    print(f"       {r['product_url']}")

print("\n=== any product whose title has 24V AND (12mm or keyway or encoder servo)")
for r in cur.execute(
    "select title, price, vendor_name, available, product_url from products "
    "where lower(title) like '%24v%' and (lower(title) like '%12mm%' or lower(title) like '%keyway%') "
    "order by price asc"
):
    print(f"  Rs {r['price']:>7.0f} | {'IN ' if r['available'] else 'OUT'} | {r['title'][:110]}")
    print(f"       {r['product_url']}")

print("\n=== samples of the 'Pro-Range High Torque Planetary Gears Motors' category")
for r in cur.execute(
    "select title, price, available from products "
    "where category like '%High Torque Planetary%' order by price asc limit 20"
):
    print(f"  Rs {r['price']:>7.0f} | {'IN ' if r['available'] else 'OUT'} | {r['title'][:100]}")
