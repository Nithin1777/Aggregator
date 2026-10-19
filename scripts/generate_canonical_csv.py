import csv
import sqlite3
from pathlib import Path

db_path = Path('data/components.db')
conn = sqlite3.connect(str(db_path))
c = conn.cursor()

# Reset any prior canonical flags
c.execute("UPDATE products SET is_canonical = 0, canonical_for = NULL")
conn.commit()

# Definitions: (key, key_type, search_term, required_tokens, forbidden_tokens)
TARGET_KEYS = [
    # Brands (8)
    ('arduino', 'brand', 'Arduino Uno R3', ['arduino', 'uno'], ['kit', 'case', 'shield', 'cable']),
    ('esp32', 'brand', 'ESP32 Development Board WiFi+Bluetooth', ['esp32', 'development', 'board'], ['camera', 'screen', 'display', 'relay', 'shield', 'case', 'expansion']),
    ('raspberry pi', 'brand', 'Raspberry Pi 4 Model-B', ['raspberry', 'pi', '4', 'model'], ['case', 'fan', 'cooler', 'cable', 'power', 'heatsink', 'kit']),
    ('stm32', 'brand', 'STM32F103C8T6 Development Board', ['stm32f103c8t6', 'development'], ['case', 'cable']),
    ('rp2040', 'brand', 'Raspberry Pi Pico', ['raspberry', 'pi', 'pico'], ['case', 'cable', 'header', 'kit', 'uno']),
    ('esp8266', 'brand', 'NodeMCU ESP8266', ['nodemcu', 'esp8266'], ['relay', 'shield', 'motor', 'kit']),
    ('teensy', 'brand', 'Teensy 4.1 Development Board', ['teensy', '4.1'], ['cable', 'header', 'case']),
    ('beaglebone', 'brand', 'BeagleBone Black Rev C', ['beaglebone', 'black'], ['case', 'cable', 'cape', 'adapter']),

    # Category Terms (9)
    ('servo', 'category', 'SG90 Servo Motor', ['sg90', 'servo'], ['wheel', 'horn', 'claw', 'bracket', 'mount']),
    ('relay', 'category', '5V 1 Channel Relay Module', ['5v', '1', 'channel', 'relay'], ['solid', 'shield', 'esp8266', 'kit', '16']),
    ('stepper', 'category', 'NEMA 17 Stepper Motor', ['nema', '17', 'stepper'], ['bracket', 'mount', 'damper', 'cable']),
    ('buck converter', 'category', 'LM2596 DC-DC Buck Converter', ['lm2596', 'buck'], ['case', 'box', 'shell']),
    ('sensor', 'category', 'DHT11 Temperature and Humidity Sensor', ['dht11', 'temperature'], ['cable', 'probe']),
    ('oled', 'category', '0.96 inch I2C OLED Display Module', ['0.96', 'oled'], ['cable', 'case']),
    ('dc motor', 'category', 'BO Motor Dual Shaft', ['bo', 'motor'], ['clamp', 'wheel', 'bracket']),
    ('lithium battery', 'category', '', ['18650', 'rechargeable', 'battery'], ['tester', 'holder', 'case', 'box', 'shield', 'spacer']),
    ('bms', 'category', '3S 10A Battery Protection BMS Module', ['3s', 'bms'], ['cable', 'strip', 'smartelex']),


    # High-Volume MPNs (6)
    ('lm2596', 'mpn', 'LM2596 DC-DC Converter Step Down Module', ['lm2596', 'step', 'down'], ['case', 'box', 'shell']),
    ('dht11', 'mpn', 'DHT11 Humidity & Temperature Sensor Module', ['dht11', 'sensor'], ['case', 'cable']),
    ('bme280', 'mpn', 'BME280 Temperature Humidity Sensor Module', ['bme280', 'sensor'], ['case', 'adapter']),
    ('sg90', 'mpn', 'TowerPro SG90 9g Mini Servo Motor', ['sg90', 'servo'], ['wheel', 'horn', 'claw', 'bracket']),
    ('l298n', 'mpn', 'L298N 2A Dual Motor Driver Module', ['l298n', 'motor', 'driver'], ['case', 'cable', 'four']),
    ('tp4056', 'mpn', 'TP4056 1A Li-ion Battery Charging Board', ['tp4056', 'charging'], ['ic', 'smd', 'sop-8']),

    # Additional standard catalog electronics to reach >=40
    ('ch340', 'mpn', 'CH340G USB to TTL Converter Module', ['ch340', 'usb', 'ttl'], ['cable']),
    ('cp2102', 'mpn', 'CP2102 USB to TTL Serial Module', ['cp2102', 'usb', 'ttl'], ['cable', 'xbee']),
    ('ft232rl', 'mpn', 'FT232RL FTDI USB to TTL Serial Adapter Module', ['ft232rl', 'adapter'], ['ic', 'ssop', 'cable', 'xbee']),
    ('ds3231', 'mpn', 'DS3231 Real Time Clock RTC Module', ['ds3231', 'rtc'], ['cable', 'case']),
    ('ds1307', 'mpn', 'Tiny RTC DS1307 Clock Module', ['ds1307', 'rtc'], ['ic', 'dip', 'soic']),
    ('hx711', 'mpn', 'HX711 Load Cell Amplifier Module', ['hx711', 'amplifier'], ['scale', 'unit', 'm5stack']),
    ('ads1115', 'mpn', 'ADS1115 16-Bit I2C ADC Module', ['ads1115', 'adc'], ['ic', 'vssop']),
    ('pam8403', 'mpn', 'PAM8403 Mini 5V Digital Amplifier Board', ['pam8403', 'amplifier'], ['bluetooth']),
    ('tpa3116', 'mpn', 'TPA3116D2 Dual Channel Audio Amplifier Board', ['tpa3116', 'amplifier'], ['subwoofer', 'ic', 'dad']),
    ('hc-sr04', 'mpn', 'HC-SR04 Ultrasonic Sensor Module', ['hc-sr04', 'sensor'], ['bracket', 'mount']),
    ('mpu6050', 'mpn', 'MPU6050 3-Axis Gyroscope Accelerometer Module', ['mpu6050', 'sensor'], ['ic', 'qfn', '10dof', 'gy-87']),
    ('bmp280', 'mpn', 'BMP280 Barometric Pressure Sensor Module', ['bmp280', 'sensor'], ['gy-91', 'bno055']),
    ('max485', 'mpn', 'MAX485 TTL To RS485 Converter Module', ['max485', 'module'], ['ic', 'epa']),
    ('mcp2515', 'mpn', 'MCP2515 CAN Bus Module Board', ['mcp2515', 'can'], ['shield', 'featherwing']),
    ('uln2003', 'mpn', 'ULN2003 Stepper Motor Driver Module', ['uln2003', 'driver'], ['smd', 'board']),
    ('a4988', 'mpn', 'A4988 Stepper Motor Driver Module', ['a4988', 'stepper'], ['case', 'bracket']),
    ('drv8825', 'mpn', 'DRV8825 Stepper Motor Driver Module', ['drv8825', 'stepper'], ['base', 'm5stack']),
    ('xl4015', 'mpn', 'XL4015 5A Step Down Buck Converter Module', ['xl4015', 'step', 'down'], ['case']),
    ('mt3608', 'mpn', 'MT3608 2A DC-DC Step Up Boost Module', ['mt3608', 'step', 'up'], ['type-c']),
    ('vl53l0x', 'mpn', 'VL53L0X Laser Distance Sensor Module', ['vl53l0x', 'sensor'], ['gy-53']),
    ('ina219', 'mpn', 'INA219 I2C Current & Power Monitor Module', ['ina219', 'module'], ['ic', 'soic']),
    ('ne555', 'mpn', 'NE555 Timer IC', ['ne555', 'timer'], ['door', 'bell', 'kt955', 'kit']),
    ('lm358', 'mpn', 'LM358 Operational Amplifier Module', ['lm358', 'amplifier'], ['pcb', 'ir']),

    ('pc817', 'mpn', 'PC817 Optocoupler Isolation Module', ['pc817', 'optocoupler'], ['smd']),
    ('74hc595', 'mpn', '74HC595 Shift Register Breakout Module', ['74hc595', 'shift'], ['ic', 'soic16'])
]

out_rows = []

for key, ktype, fallback_title, req_tokens, forbid_tokens in TARGET_KEYS:
    # Query candidates matching search
    query_pat = f"%{key}%"
    c.execute("SELECT id, title, mpn, vendor, price, available FROM products WHERE title LIKE ? OR normalized_title LIKE ?", (query_pat, query_pat))
    candidates = c.fetchall()
    
    best_id = None
    best_title = ""
    best_mpn = ""
    best_vendor = ""
    
    # Filter candidates
    valid_cands = []
    for cid, ctitle, cmpn, cvendor, cprice, cavail in candidates:
        low_title = ctitle.lower()
        if not all(t.lower() in low_title for t in req_tokens):
            continue
        if any(f.lower() in low_title for f in forbid_tokens):
            continue
        valid_cands.append((cid, ctitle, cmpn, cvendor, cprice, cavail))
    
    # Sort by in-stock first, then shortest clean title
    if valid_cands:
        valid_cands.sort(key=lambda x: (not x[5], len(x[1])))
        chosen = valid_cands[0]
        best_id = chosen[0]
        best_title = chosen[1]
        best_mpn = chosen[2] or ""
        best_vendor = chosen[3]
        
        # Mark in DB (append key to canonical_for if already set)
        c.execute("""
            UPDATE products 
            SET is_canonical = 1, 
                canonical_for = CASE 
                    WHEN canonical_for IS NULL OR canonical_for = '' THEN ?
                    ELSE canonical_for || ',' || ?
                END 
            WHERE id = ?
        """, (key, key, best_id))
    else:
        # Fallback to broader candidate or leave blank if truly none
        c.execute("SELECT id, title, mpn, vendor, price FROM products WHERE title LIKE ? ORDER BY LENGTH(title) ASC LIMIT 1", (f"%{fallback_title}%",))
        fb = c.fetchone()
        if fb:
            best_id = fb[0]
            best_title = fb[1]
            best_mpn = fb[2] or ""
            best_vendor = fb[3]
            c.execute("""
                UPDATE products 
                SET is_canonical = 1, 
                    canonical_for = CASE 
                        WHEN canonical_for IS NULL OR canonical_for = '' THEN ?
                        ELSE canonical_for || ',' || ?
                    END 
                WHERE id = ?
            """, (key, key, best_id))

    
    out_rows.append({
        'key': key,
        'type': ktype,
        'product_id': best_id if best_id else "",
        'title': best_title,
        'mpn': best_mpn,
        'vendor': best_vendor
    })

conn.commit()

# Ensure seeds directory exists
seeds_dir = Path('seeds')
seeds_dir.mkdir(parents=True, exist_ok=True)
csv_file = seeds_dir / 'canonical_products.csv'

with open(csv_file, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['key', 'type', 'product_id', 'title', 'mpn', 'vendor'])
    writer.writeheader()
    for r in out_rows:
        writer.writerow(r)

print(f"Generated {len(out_rows)} canonical seed products in {csv_file}.")
conn.close()
