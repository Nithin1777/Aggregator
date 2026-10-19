import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/components.db')
c = conn.cursor()

keys = [
    # Brand tokens
    'arduino', 'esp32', 'raspberry pi', 'stm32', 'rp2040', 'esp8266', 'teensy', 'beaglebone',
    # Category terms
    'servo', 'relay', 'stepper', 'buck converter', 'sensor', 'oled', 'dc motor', 'lithium battery', 'bms',
    # High-volume MPNs
    'lm2596', 'dht11', 'bme280', 'sg90', 'l298n', 'tp4056',
    # Additional components to reach >=40
    'ch340', 'cp2102', 'ft232rl', 'ds3231', 'ds1307', 'hx711', 'ads1115',
    'pam8403', 'tpa3116', 'hc-sr04', 'mpu6050', 'bmp280', 'max485', 'mcp2515',
    'uln2003', 'a4988', 'drv8825', 'xl4015', 'mt3608', 'vl53l0x', 'ina219',
    'ne555', 'lm358', 'pc817', '74hc595'
]

for k in keys:
    c.execute("SELECT id, title, mpn, vendor, price FROM products WHERE title LIKE ? ORDER BY LENGTH(title) ASC LIMIT 3", (f'%{k}%',))
    rows = c.fetchall()
    print(f"=== KEY: '{k}' ===")
    for r in rows:
        print(f"  [{r[0]}] {r[1]} | MPN: {r[2]} | {r[3]} Rs.{r[4]}")

conn.close()
