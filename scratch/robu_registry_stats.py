import sqlite3

con = sqlite3.connect("data/components.db")
cols = [r[1] for r in con.execute("pragma table_info(robu_catalog_registry)")]
print("columns", cols)
print("registry rows", con.execute("select count(*) from robu_catalog_registry").fetchone()[0])
for col in ("indexed", "is_indexed", "status"):
    if col in cols:
        print(col, con.execute(f"select {col}, count(*) from robu_catalog_registry group by {col}").fetchall())
