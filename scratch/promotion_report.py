import sqlite3

con = sqlite3.connect("data/components_copy.db")
con.row_factory = sqlite3.Row
print("spec_definitions", con.execute("select count(*) from spec_definitions").fetchone()[0])
print("product_specs", con.execute("select count(*) from product_specs").fetchone()[0])
print("products with specs", con.execute("select count(distinct product_id) from product_specs").fetchone()[0])
print("\ncategory counts")
for r in con.execute(
    "select coalesce(spec_category,'(null)'), count(*) from products group by spec_category order by 2 desc limit 20"
):
    print(" ", r[0], r[1])
print("\ntop keys")
for r in con.execute(
    "select key, count(*) n, count(distinct product_id) p from product_specs group by key order by n desc limit 30"
):
    print(f"  {r[0]:<24} {r[1]:>7} {r[2]:>7}")
print("\nspec_stats_global")
for r in con.execute("select key, applicable_count from spec_stats_global order by applicable_count desc limit 50"):
    print(" ", r[0], r[1])
