import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "select id,title,category,category_canonical,spec_category from products where title like '%9V Battery Snap%' limit 6"
).fetchall()
for r in rows:
    print(dict(r))
