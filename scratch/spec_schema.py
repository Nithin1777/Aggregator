import sqlite3

con = sqlite3.connect("data/components.db")
print(con.execute("select sql from sqlite_master where name = 'product_specs'").fetchone()[0])
print(con.execute("select sql from sqlite_master where name = 'spec_definitions'").fetchone()[0])
