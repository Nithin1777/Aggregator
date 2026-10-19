import sqlite3
import random
import sys
import os
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from search_service import AggregatorEngine
from normalizer import match_components_v2

# 1. Load all previous pairs to ensure STRICTLY DISJOINT sampling
def load_all_previous_pairs():
    exclusion_set = set()
    
    # From task-1752.log (the fresh 153 merges audit)
    log_1752 = r"C:\Users\nithi\.gemini\antigravity\brain\54ea6d64-2aa6-4ae5-9ae9-2904735ca905\.system_generated\tasks\task-1752.log"
    if os.path.exists(log_1752):
        with open(log_1752, "r", encoding="utf-8") as f:
            content = f.read()
        pat = re.compile(r"Vendor A \([^,]+, Rs\. [0-9.]+\):\s*(.*?)\n\s*Vendor B \([^,]+, Rs\. [0-9.]+\):\s*(.*?)\n")
        for ta, tb in pat.findall(content):
            exclusion_set.add(tuple(sorted([ta.strip(), tb.strip()])))
            
    # Also load pairs from the first 103 merges audit
    PREV_QUERIES = [
        "esp32", "esp8266", "dht11", "dht22", "sg90", "mg996r", "bme280", "bmp280",
        "mpu6050", "ina219", "max6675", "hc-sr04", "ch340", "cp2102", "ft232",
        "lm2596", "xl4015", "ams1117", "tp4056", "mt3608", "l298n", "a4988"
    ]
    engine = AggregatorEngine()
    for q in PREV_QUERIES:
        res = engine.search_local(q, limit=50)
        for g in res.comparison_groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        exclusion_set.add(tuple(sorted([rep.title.strip(), other.title.strip()])))
                        
    return exclusion_set

# Brand new, highly diverse queries representing untouched hardware domains
NEW_DOMAIN_QUERIES = [
    # RTC & Clock
    "ds3231", "ds1307", "pcf8563",
    # ADCs & Amplifiers
    "ads1115", "hx711", "mcp4725", "max31865", "ina3221",
    # Audio & Sound
    "dfplayer mini", "pam8403", "tpa3116", "max98357", "i2s dac",
    # Relays & Power Switching
    "relay 5v", "solid state relay", "mosfet module", "ssr-25da", "optocoupler module",
    # Battery Management (BMS) & Chargers
    "18650 bms", "bms 3s", "bms 4s", "bms 2s", "bms 5s", "tp5100",
    # Industrial & CAN Bus
    "mcp2515 can", "tja1050", "sn65hvd230", "max485 module", "rs485 ttl",
    # Storage & Memory
    "sd card module", "micro sd adapter", "at24c256", "w25q64",
    # Magnetic, Proximity & Specialized Sensors
    "as5600", "apds9960", "hall sensor", "soil moisture", "rain sensor", "water level sensor",
    "load cell", "current transformer",
    # Next-Gen Wireless Microcontrollers
    "esp32-s3", "esp32-c3", "esp32-c6", "esp32-s2", "rp2350",
    # Optoelectronics & Barometric
    "vl53l1x", "bmp388", "bno055", "gy-521", "gy-87", "gy-271",
    # Additional Unseen Sensor & Module Domains
    "bme680", "ttp223", "ir sensor module", "sound sensor module", "flame sensor",
    "tilt sensor", "laser diode module", "passive buzzer", "active buzzer", "traffic light module"
]

def run_fresh_150_re_audit(target_n=150):
    engine = AggregatorEngine()
    exclusions = load_all_previous_pairs()
    print(f"Loaded {len(exclusions)} previous cross-vendor pairs into STRICT EXCLUSION set.", flush=True)

    collected = []
    seen = set()

    print("=" * 80, flush=True)
    print(f"COLLECTING {target_n} FRESH, FULLY DISJOINT CROSS-VENDOR MERGES")
    print("=" * 80, flush=True)

    for q in NEW_DOMAIN_QUERIES:
        if len(collected) >= target_n:
            break
        res = engine.search_local(q, limit=60)
        for g in res.comparison_groups:
            vendors = set(o.vendor for o in g.offers)
            if len(vendors) > 1:
                rep = g.offers[0]
                for other in g.offers[1:]:
                    if other.vendor != rep.vendor:
                        pair_key = tuple(sorted([rep.title.strip(), other.title.strip()]))
                        if pair_key in exclusions or pair_key in seen:
                            continue
                        seen.add(pair_key)
                        
                        is_m, score, reason = match_components_v2(
                            rep.title, other.title, rep.price or 0.0, other.price or 0.0
                        )
                        if is_m:
                            collected.append({
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
                            if len(collected) >= target_n:
                                break
        print(f"Query: '{q:20}' -> Total Fresh Disjoint Merges: {len(collected)}", flush=True)

    print("\n" + "=" * 80, flush=True)
    print(f"AUDITING EXACTLY {len(collected)} FRESH PRODUCTION CROSS-VENDOR MERGES")
    print("=" * 80, flush=True)

    for idx, m in enumerate(collected, 1):
        print(f"\n[{idx:03d}] [{m['query'].upper()}] Card: {m['group'][:65]}", flush=True)
        print(f"      Vendor A ({m['a_vendor']}, Rs. {m['a_price']}): {m['a_title']}", flush=True)
        print(f"      Vendor B ({m['b_vendor']}, Rs. {m['b_price']}): {m['b_title']}", flush=True)
        print(f"      Verdict: Match=True | Score={m['score']:.2f} | Reason: {m['reason']}", flush=True)

    return collected

if __name__ == "__main__":
    run_fresh_150_re_audit(150)
