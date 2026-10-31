import sqlite3

con = sqlite3.connect("data/components_copy.db")
print(con.execute(
    "select key, count(*) from product_specs ps join products p on p.id=ps.product_id "
    "where p.spec_category='integrated-circuits' group by key order by 2 desc"
).fetchall())
print(con.execute(
    "select value, count(*) from product_specs ps join products p on p.id=ps.product_id "
    "where p.spec_category='integrated-circuits' and key='package' group by value order by 2 desc limit 10"
).fetchall())
print(con.execute(
    "select value, count(*) from product_specs ps join products p on p.id=ps.product_id "
    "where p.spec_category='integrated-circuits' and key='pins' group by value order by 2 desc limit 10"
).fetchall())
