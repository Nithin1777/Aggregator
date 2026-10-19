"""Confirm root causes of the two silent-failure bugs."""
import sqlite3

c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

print("=== A. value_num populated for enum vs numeric keys?")
for k in ("voltage", "rpm"):
    tot = cur.execute("select count(*) from product_specs where key=?", (k,)).fetchone()[0]
    nn = cur.execute("select count(*) from product_specs where key=? and value_num is not null", (k,)).fetchone()[0]
    print(f"  {k}: {nn}/{tot} rows have value_num")

print("\n=== B. simulate the failing predicate (voltage gte 24) exactly as built")
q = ("select count(distinct p.id) from products p "
     "join product_specs ps_0 on p.id = ps_0.product_id and ps_0.key = 'voltage' "
     "where ps_0.value_num >= 24.0")
print("  rows:", cur.execute(q).fetchone()[0], "  <- server returns 0 because value_num is NULL for enums")

print("\n=== C. same intent done correctly (cast the enum text)")
q2 = ("select count(distinct p.id) from products p "
      "join product_specs ps_0 on p.id = ps_0.product_id and ps_0.key = 'voltage' "
      "where cast(ps_0.value as real) >= 24.0")
print("  rows:", cur.execute(q2).fetchone()[0])

print("\n=== D. raw_json fields actually available (why description==title)")
for r in cur.execute("select raw_json from products where raw_json is not null limit 3"):
    print("  ", (r[0] or "")[:160])

print("\n=== E. datasheet_url / body present anywhere in raw_json?")
n_body = cur.execute("select count(*) from products where raw_json like '%\"body\"%'").fetchone()[0]
n_ds = cur.execute("select count(*) from products where raw_json like '%datasheet%'").fetchone()[0]
print(f"  products with body in raw_json: {n_body} | with datasheet: {n_ds}")

print("\n=== F. cross-vendor duplicate exposure (same MPN, multiple vendors)")
for r in cur.execute(
    "select mpn, count(*) n, group_concat(vendor_name, ' | ') v from products "
    "where mpn is not null and mpn != '' group by mpn having n > 1 order by n desc limit 5"
):
    print(f"  {r['mpn']}: {r['n']} listings ({r['v']})")
print("  duplicate-MPN groups:", cur.execute(
    "select count(*) from (select mpn from products where mpn is not null and mpn != '' "
    "group by mpn having count(*) > 1)").fetchone()[0])
