import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

clean_q = "raspberry pi"
fts_query = " ".join([f'"{tok}"*' for tok in clean_q.split() if tok])
print("FTS Query:", fts_query)

c.execute("""
    SELECT p.id, p.title, p.is_canonical, p.canonical_for, fts.rank
    FROM products p
    JOIN products_fts fts ON p.id = fts.rowid
    WHERE products_fts MATCH ?
    ORDER BY rank
    LIMIT 200
""", (fts_query,))
rows = c.fetchall()
print(f"Total rows fetched: {len(rows)}")

found = [r for r in rows if r[0] == 4468]
print("Is canonical ID 4468 in the 200 rows?", found)

# Where does 4468 appear?
c.execute("""
    SELECT p.id, p.title, p.is_canonical, p.canonical_for, fts.rank
    FROM products p
    JOIN products_fts fts ON p.id = fts.rowid
    WHERE products_fts MATCH ? AND p.id = 4468
""", (fts_query,))
print("Direct check on 4468:", c.fetchall())

conn.close()
