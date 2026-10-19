import sqlite3

c = sqlite3.connect("data/components.db")
for t in ("products", "product_specs", "spec_stats_global", "spec_definitions"):
    cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    print(t, "->", cols)
print()
print("spec rows:", c.execute("select count(*) from product_specs").fetchone()[0])
print("distinct keys:", c.execute("select count(distinct key) from product_specs").fetchone()[0])
print("stats rows:", c.execute("select count(*) from spec_stats_global").fetchone()[0])
print("spec_category nonnull:", c.execute("select count(*) from products where spec_category is not null").fetchone()[0])
print()
for r in c.execute("select key, value_type, applicable_count from spec_stats_global order by applicable_count desc"):
    print("  stat:", r)
