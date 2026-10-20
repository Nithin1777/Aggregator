import sqlite3

con = sqlite3.connect("data/components.db")
q = con.execute

print("products:", q("select count(*) from products").fetchone()[0])
print("products with >=1 spec:", q("select count(distinct product_id) from product_specs").fetchone()[0])
print("spec rows:", q("select count(*) from product_specs").fetchone()[0])
print("classified (spec_category not null):", q("select count(*) from products where spec_category is not null").fetchone()[0])
print()
print("spec rows per key:")
for k, n, prods in q("select key, count(*), count(distinct product_id) from product_specs group by key order by 2 desc"):
    print(f"  {k:<16} rows={n:<6} products={prods}")
print()
print("fill rate within the 3 attribute-bearing categories:")
for cat, total in q("select spec_category, count(*) from products where spec_category is not null group by spec_category"):
    filled = q("select count(distinct product_id) from product_specs ps join products p on p.id=ps.product_id where p.spec_category=?", (cat,)).fetchone()[0]
    print(f"  {cat:<10} {filled}/{total} = {100*filled/total:.1f}%")
print()
print("raw_json containing description-ish keys:")
for key in ("body", "body_html", "description", "short_description", "attributes", "specifications"):
    n = q("select count(*) from products where raw_json like ?", (f'%"{key}"%',)).fetchone()[0]
    print(f"  {key:<18} {n}")
