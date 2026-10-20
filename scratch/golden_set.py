"""Small golden set of core-part queries; checks the top card/listing is the part."""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
GOLDEN = {
    "IG52": "motor",
    "NEMA17": "motor",
    "Raspberry Pi 5": "model",
    "resistor": "resistor",
    "display": "display",
    "CH340": "converter",
    "TP4056": "module",
    "DRV8825": "driver",
    "LM2596": "module",
    "DHT11": "sensor",
    "BME280": "sensor",
    "SG90": "servo",
    "Arduino Uno": "uno",
    "ESP32": "esp32",
    "Raspberry Pi Pico": "pico",
    "A4988": "driver",
    "NE555": "555",
    "MPU6050": "mpu6050",
    "MAX485": "max485",
    "PC817": "pc817",
    "74HC595": "74hc595",
    "XL4015": "xl4015",
    "MT3608": "mt3608",
    "INA219": "ina219",
}

passed = 0
for query, token in GOLDEN.items():
    url = f"{BASE}/api/search?" + urllib.parse.urlencode(
        {"q": query, "page": 1, "page_size": 5, "refresh": "off"}
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    groups = data.get("comparison_groups") or []
    title = groups[0]["canonical_name"] if groups else ""
    ok = token in title.lower()
    passed += ok
    print(f"[{'PASS' if ok else 'FAIL'}] {query:18s} -> {title[:90]}")

print(f"\n{passed}/{len(GOLDEN)} top-1 core matches")
