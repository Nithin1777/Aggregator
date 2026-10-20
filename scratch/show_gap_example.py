import re
import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "select id, vendor, title, category_canonical, spec_category, (select count(*) from product_specs ps where ps.product_id=products.id) n "
    "from products where vendor='quartzcomponents' and title like '%18AWG Solid Core PVC Wire%' limit 3"
).fetchall()
for r in rows:
    print(dict(r))
