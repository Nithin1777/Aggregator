import json
import sqlite3
from collections import Counter, defaultdict

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
con.execute("PRAGMA temp_store=MEMORY")

print("CATEGORY_CANONICAL DISTRIBUTION")
rows = con.execute("""
    select coalesce(category_canonical,'(null)') c, count(*) n
    from products group by c order by n desc
""").fetchall()
for r in rows:
    print(f"  {r['c']:<26} {r['n']:,}")

print("\nSPEC_COVERAGE PER CANONICAL CATEGORY")
rows = con.execute("""
    select coalesce(p.category_canonical,'(null)') c,
           count(distinct p.id) total,
           count(distinct ps.product_id) with_specs
    from products p left join product_specs ps on ps.product_id = p.id
    group by c order by total desc
""").fetchall()
for r in rows:
    pct = 100.0 * r["with_specs"] / r["total"] if r["total"] else 0
    print(f"  {r['c']:<26} {r['with_specs']:>6,}/{r['total']:<7,} {pct:5.1f}%")

print("\nSPEC_CATEGORY COLUMN")
rows = con.execute("""
    select coalesce(spec_category,'(null)') s, count(*) n
    from products group by s order by n desc
""").fetchall()
for r in rows:
    print(f"  {r['s']:<20} {r['n']:,}")

print("\nRAW_JSON KEY COVERAGE BY VENDOR")
for vendor in [r["vendor"] for r in con.execute("select distinct vendor from products order by vendor")]:
    rows = con.execute("select raw_json from products where vendor=? limit 400", (vendor,)).fetchall()
    key_counts = Counter()
    n = 0
    for r in rows:
        n += 1
        try:
            d = json.loads(r["raw_json"]) if r["raw_json"] else {}
        except Exception:
            d = {}
        if isinstance(d, dict):
            key_counts.update(d.keys())
    keys = ", ".join(f"{k}" for k, _ in key_counts.most_common(30))
    print(f"\n  {vendor} (n={n})")
    print(f"    {keys}")
