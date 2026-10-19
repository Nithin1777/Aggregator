import sqlite3

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

q1 = '("smart"* "elex"* "motor"*)'
c.execute('SELECT count(*) FROM products p JOIN products_fts fts ON p.id = fts.rowid WHERE products_fts MATCH ?', (q1,))
print('Original 3 tokens:', c.fetchone()[0])

q2 = '("smartelex"* "motor"*)'
c.execute('SELECT count(*) FROM products p JOIN products_fts fts ON p.id = fts.rowid WHERE products_fts MATCH ?', (q2,))
print('Merged bigram + motor:', c.fetchone()[0])

q3 = '("smart"* "elex"* "motor"*) OR ("smartelex"* "motor"*)'
c.execute('SELECT count(*) FROM products p JOIN products_fts fts ON p.id = fts.rowid WHERE products_fts MATCH ?', (q3,))
print('Combined:', c.fetchone()[0])
