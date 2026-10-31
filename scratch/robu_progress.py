import sqlite3

con = sqlite3.connect("data/components.db")
print(con.execute("select status, count(*) from robu_catalog_registry group by status").fetchall())
print("descriptions", con.execute("select count(*) from product_descriptions").fetchone()[0])
print("attrs", con.execute("select count(*) from product_attributes").fetchone()[0])
print("products", con.execute("select count(*) from products where vendor='robu'").fetchone()[0])
