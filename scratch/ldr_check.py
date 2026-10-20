import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
for r in con.execute(
    "select title, price, available from products where title like '%LDR%' and lower(title) like '%esp32%' limit 5"
):
    print(dict(r))
