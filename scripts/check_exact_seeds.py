import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

def find_best(query, where="1=1", limit=2):
    c.execute(f"SELECT id, title, mpn, vendor, price FROM products WHERE {where} AND title LIKE ? ORDER BY LENGTH(title) ASC LIMIT ?", (f'%{query}%', limit))
    return c.fetchall()

print("Brand tokens:")
for b in ['arduino uno', 'esp32 dev', 'raspberry pi 4', 'stm32f103c8t6', 'raspberry pi pico', 'nodemcu esp8266', 'teensy', 'beaglebone']:
    res = find_best(b, limit=2)
    print(f" {b}:", res)

print("\nCategories:")
for cat, term in [('servo', 'sg90'), ('relay', 'relay module'), ('stepper', 'nema 17'), ('buck converter', 'lm2596'), ('sensor', 'dht11'), ('oled', '0.96'), ('dc motor', 'bo motor'), ('lithium battery', '18650'), ('bms', 'bms')]:
    res = find_best(term, limit=2)
    print(f" {cat} ({term}):", res)

conn.close()
