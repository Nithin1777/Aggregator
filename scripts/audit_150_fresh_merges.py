import sqlite3
import random
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from search_service import AggregatorEngine
from normalizer import match_components_v2

# Queries from previous audit to extract exclusion set
PREV_QUERIES = [
    "esp32", "esp8266", "dht11", "dht22", "sg90", "mg996r", "bme280", "bmp280",
    "mpu6050", "ina219", "max6675", "hc-sr04", "ch340", "cp2102", "ft232",
    "lm2596", "xl4015", "ams1117", "tp4056", "mt3608", "l298n", "a4988"
]

# Fresh and broad query list to draw diverse hardware components
FRESH_AUDIT_QUERIES = [
    # Displays
    "oled 0.96", "oled 1.3", "tft 1.8", "lcd 1602", "lcd 2004", "st7789", "ili9341", "tm1637",
    # Sensors
    "ds18b20", "vl53l0x", "max30102", "adxl345", "tcs3200", "mq-2", "mq-135", "hcsr501", "rcwl-0516",
    # Drivers & Actuators
    "drv8825", "l293d", "uln2003", "pca9685", "tb6600", "mg90s", "nema17", "stepper motor",
    # Microcontrollers & Boards
    "rp2040", "pico w", "stm32f103", "stm32f401", "attiny85", "atmega328p", "pro mini", "arduino uno", "arduino nano", "arduino mega",
    # Power & Regulators
    "mp1584", "lm7805", "tp5100", "step down buck", "boost converter", "18650 charger",
    # Communication & Interfaces
    "max485", "mcp2515", "nrf24l01", "sx1278", "ch340c", "cp2104", "ft232",
    # Discretes & Logic
    "ne555", "lm358", "lm324", "lm393", "pc817", "irfz44n", "74hc595"
]

def run_150_fresh_audit(target_n=150):
    engine = AggregatorEngine()
    
    # 1. Collect previous 103 pairs to ensure ZERO overlap
    prev_pairs = set()
    for q in PREV_QUERIES:
        res = engine.search_local(q, limit=60)
        for g in res.comparison_groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        key = tuple(sorted([rep.title, other.title]))
                        prev_pairs.add(key)
                        if len(prev_pairs) >= 103:
                            break
        if len(prev_pairs) >= 103:
            break
            
    print(f"Loaded {len(prev_pairs)} previous pair keys into EXCLUSION set.", flush=True)

    # 2. Collect 150 fresh pairs
    fresh_merges = []
    seen_fresh = set()
    
    print("=" * 80, flush=True)
    print(f"COLLECTING {target_n} FRESH CROSS-VENDOR MERGES (DISJOINT FROM PREVIOUS 103)")
    print("=" * 80, flush=True)
    
    for q in FRESH_AUDIT_QUERIES:
        if len(fresh_merges) >= target_n:
            break
            
        local_res = engine.search_local(q, limit=60)
        groups = local_res.comparison_groups
        
        for g in groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        pair_key = tuple(sorted([rep.title, other.title]))
                        if pair_key in prev_pairs or pair_key in seen_fresh:
                            continue
                        seen_fresh.add(pair_key)
                        
                        is_m, score, reason = match_components_v2(
                            rep.title, other.title, rep.price or 0.0, other.price or 0.0
                        )
                        
                        if is_m:
                            fresh_merges.append({
                                "query": q,
                                "group": g.canonical_name,
                                "a_title": rep.title,
                                "a_vendor": rep.vendor,
                                "a_price": rep.price,
                                "b_title": other.title,
                                "b_vendor": other.vendor,
                                "b_price": other.price,
                                "score": score,
                                "reason": reason
                            })
                            if len(fresh_merges) >= target_n:
                                break
        print(f"Query: '{q:16}' -> Total Fresh Merges Collected: {len(fresh_merges)}", flush=True)

    print("\n" + "=" * 80, flush=True)
    print(f"AUDITING EXACTLY {len(fresh_merges)} FRESH PRODUCTION CROSS-VENDOR MERGES")
    print("=" * 80, flush=True)
    
    for idx, m in enumerate(fresh_merges, 1):
        ta = m['a_title']
        tb = m['b_title']
        pa = m['a_price']
        pb = m['b_price']
        print(f"\n[{idx}] [{m['query'].upper()}] Card: {m['group'][:65]}", flush=True)
        print(f"    Vendor A ({m['a_vendor']}, Rs. {pa}): {ta}", flush=True)
        print(f"    Vendor B ({m['b_vendor']}, Rs. {pb}): {tb}", flush=True)
        print(f"    Verdict: Match=True | Score={m['score']:.2f} | Reason: {m['reason']}", flush=True)

    return fresh_merges

if __name__ == "__main__":
    run_150_fresh_audit(150)
