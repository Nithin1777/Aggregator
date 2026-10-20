import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row


def q(sql, params=()):
    return con.execute(sql, params).fetchall()


print("TABLES")
for r in q("select name from sqlite_master where type='table' order by name"):
    print(" ", r["name"])

print("\nROW COUNTS")
for t in ("products", "product_specs", "spec_definitions", "spec_stats_global", "spec_stats"):
    try:
        print(f"  {t}: {con.execute(f'select count(*) from {t}').fetchone()[0]:,}")
    except Exception as e:
        print(f"  {t}: MISSING ({e})")

print("\nPRODUCT COLUMNS")
print([r["name"] for r in q("PRAGMA table_info(products)")])

print("\nPRODUCTS BY VENDOR")
for r in q("select vendor, count(*) n from products group by vendor order by n desc"):
    print(f"  {r['vendor']}: {r['n']:,}")

print("\nSPECS COVERAGE BY VENDOR (products with >=1 spec row)")
rows = q("""
    select p.vendor,
           count(distinct p.id) total,
           count(distinct ps.product_id) with_specs,
           count(ps.product_id) spec_rows
    from products p left join product_specs ps on ps.product_id = p.id
    group by p.vendor order by total desc
""")
for r in rows:
    pct = 100.0 * r["with_specs"] / r["total"] if r["total"] else 0
    print(f"  {r['vendor']:<22} {r['with_specs']:>7,}/{r['total']:<7,} = {pct:5.1f}%   rows={r['spec_rows']:,}")

print("\nSPECS COVERAGE BY CATEGORY")
rows = q("""
    select coalesce(p.category,'(null)') cat,
           count(distinct p.id) total,
           count(distinct ps.product_id) with_specs
    from products p left join product_specs ps on ps.product_id = p.id
    group by cat order by total desc
""")
for r in rows:
    pct = 100.0 * r["with_specs"] / r["total"] if r["total"] else 0
    print(f"  {r['cat']:<26} {r['with_specs']:>7,}/{r['total']:<7,} = {pct:5.1f}%")

print("\nSPEC KEYS")
for r in q("select key, count(*) n, count(distinct product_id) prods from product_specs group by key order by n desc"):
    print(f"  {r['key']:<24} {r['n']:>7,}  ({r['prods']:,} products)")

print("\nSPEC DEFINITIONS")
for r in q("select * from spec_definitions limit 200"):
    print(" ", dict(r))

print("\nSPEC STATS GLOBAL")
for r in q("select * from spec_stats_global limit 200"):
    print(" ", dict(r))
