import sqlite3

con = sqlite3.connect("data/components.db")
print("definitions", con.execute("select count(*) from spec_definitions").fetchone()[0])
print("advertised", con.execute("select count(*) from spec_stats_global").fetchone()[0])
print("promoted rows", con.execute("select count(*) from product_specs where source='promoted'").fetchone()[0])
print("products with specs", con.execute("select count(distinct product_id) from product_specs").fetchone()[0])
print("pending robu", con.execute("select count(*) from robu_catalog_registry where status='pending'").fetchone()[0])
