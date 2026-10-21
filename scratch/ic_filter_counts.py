import sqlite3

con = sqlite3.connect("data/components_copy.db")
queries = {
    "dip": "select count(*) from product_specs ps join products p on p.id=ps.product_id where p.category_canonical='integrated-circuits' and key='package' and value='DIP'",
    "pins8": "select count(*) from product_specs ps join products p on p.id=ps.product_id where p.category_canonical='integrated-circuits' and key='pins' and value_num>=8",
    "both": "select count(*) from product_specs ps join products p on p.id=ps.product_id where p.category_canonical='integrated-circuits' and key='package' and value='DIP' and ps.product_id in (select product_id from product_specs where key='pins' and value_num>=8)",
}
for name, sql in queries.items():
    print(name, con.execute(sql).fetchone()[0])
print(con.execute("select key, count(*) from product_specs ps join products p on p.id=ps.product_id where p.category_canonical='integrated-circuits' group by key order by 2 desc").fetchall())
