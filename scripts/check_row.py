import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


conn = db.get_db_connection()
c = conn.cursor()
c.execute('SELECT p.* FROM products p JOIN products_fts fts ON p.id = fts.rowid LIMIT 1')
row = c.fetchone()
print('Row keys:', row.keys())
print('is_canonical in keys?:', 'is_canonical' in row.keys())
print('Row is_canonical value:', row['is_canonical'])
conn.close()
