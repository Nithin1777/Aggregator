import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

keys = ['arduino', 'esp32', 'raspberry pi', 'servo', 'relay', 'lm2596', 'dht11']
for k in keys:
    c.execute("SELECT id, title, is_canonical, canonical_for FROM products WHERE canonical_for = ?", (k,))
    row = c.fetchone()
    print(f"Key '{k}': {row}")

conn.close()
