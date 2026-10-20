"""Same golden set through the MCP text-search path (must match web ordering)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server.server as server

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
    res = server.search_components(query=query, limit=5, refresh=False)
    rows = (res.data or {}).get("results") or []
    title = rows[0]["title"] if rows else ""
    ok = res.ok and token in title.lower()
    passed += ok
    print(f"[{'PASS' if ok else 'FAIL'}] {query:18s} -> {title[:90]}")

print(f"\n{passed}/{len(GOLDEN)} MCP top-1 core matches")
