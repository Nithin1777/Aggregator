import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

clean_q = "raspberry pi"
fts_query = ' "raspberry"* "pi"* '

c.execute("SELECT COUNT(*) FROM products p JOIN products_fts fts ON p.id = fts.rowid WHERE products_fts MATCH ?", (fts_query,))
total = c.fetchone()[0]
print(f"Total matching 'raspberry pi' in FTS: {total}")

# Find row rank of 4468
c.execute("""
    WITH ranked AS (
        SELECT p.id, p.title, p.is_canonical, p.canonical_for,
               ROW_NUMBER() OVER (ORDER BY fts.rank) as rnk
        FROM products p
        JOIN products_fts fts ON p.id = fts.rowid
        WHERE products_fts MATCH ?
    )
    SELECT * FROM ranked WHERE is_canonical = 1 OR id = 4468;
""", (fts_query,))
print("Ranked position of canonical product(s):", c.fetchall())

conn.close()
