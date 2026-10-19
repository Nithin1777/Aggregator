import sqlite3

conn = sqlite3.connect('data/components.db')
c = conn.cursor()
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)

for t in tables:
    try:
        cnt = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {cnt} rows")
    except Exception as e:
        print(f"  {t}: error {e}")

if 'products' in tables:
    c.execute("SELECT vendor, COUNT(*) FROM products GROUP BY vendor")
    for v, cnt in c.fetchall():
        print(f"  vendor '{v}': {cnt}")

conn.close()
