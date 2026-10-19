import sqlite3

c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

def show(label, sql, params=(), limit_print=60):
    rows = cur.execute(sql, params).fetchall()
    print(f"\n=== {label}  (n={len(rows)})")
    for r in rows[:limit_print]:
        avail = "IN " if r["available"] else "OUT"
        print(f"  {r['price']:>7.0f} | {avail} | {r['vendor_name']:<18} | {r['title'][:88]}")
        print(f"          {r['product_url']}")

base = (
    "select p.id, p.vendor_name, p.title, p.price, p.available, p.product_url "
    "from products p join product_specs s on s.product_id = p.id "
)

show(
    "voltage=24, price 4000-7000 (any title)",
    base
    + "where s.key='voltage' and s.value='24' and p.price between 4000 and 7000 "
    "order by p.price asc",
)

show(
    "titles with gearbox/worm/reduction, price 3000-8000",
    "select p.id,p.vendor_name,p.title,p.price,p.available,p.product_url from products p "
    "where (lower(p.title) like '%gearbox%' or lower(p.title) like '%worm%' "
    "or lower(p.title) like '%reduction gear%' or lower(p.title) like '%gear motor%') "
    "and p.price between 3000 and 8000 order by p.price asc",
)

show(
    "titles mentioning 24V and gear/box, price 2000-9000",
    "select p.id,p.vendor_name,p.title,p.price,p.available,p.product_url from products p "
    "where lower(p.title) like '%24v%' and (lower(p.title) like '%gear%' or lower(p.title) like '%motor%') "
    "and p.price between 2000 and 9000 order by p.price asc",
)

# which vendors stock high-torque DC gear motors at all
print("\n=== vendors with titles like '%gear motor%' or '%geared motor%'")
for r in cur.execute(
    "select vendor_name, count(*) n, min(price) lo, max(price) hi from products "
    "where lower(title) like '%gear motor%' or lower(title) like '%geared motor%' "
    "group by vendor_name order by n desc limit 12"
):
    print(f"  {r['vendor_name']:<20} n={r['n']:<4} price {r['lo']:.0f}-{r['hi']:.0f}")
