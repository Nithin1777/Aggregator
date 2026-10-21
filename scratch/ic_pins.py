import sqlite3

con = sqlite3.connect("data/components_copy.db")
for row in con.execute(
    "select p.title, ps.value, ps.value_num, ps.value_type "
    "from product_specs ps join products p on p.id=ps.product_id "
    "where p.spec_category='integrated-circuits' and key='pins' limit 5"
):
    print(row)
print(con.execute(
    "select count(*) from product_specs ps join products p on p.id=ps.product_id "
    "where p.spec_category='integrated-circuits' and key='pins' and value_num is not null"
).fetchone())
