import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
for r in con.execute("select id,title,product_url,mpn,sku from products where vendor='robokits' limit 8"):
    print(dict(r))
