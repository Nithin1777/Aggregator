"""
Automated Search & Matching Corpus Audit
Runs targeted queries across diverse categories against http://127.0.0.1:8000/search
and analyzes False Positives (over-merges), False Negatives (missed merges),
price discrepancies, and relevance anomalies.
"""

import urllib.request
import urllib.parse
import json
import re
import time
from typing import List, Dict, Any, Tuple
from collections import defaultdict

TAXONOMY = {
    "Motor Drivers & Controllers": [
        "l298n motor driver",
        "tb6612fng",
        "a4988 stepper driver",
        "drv8825",
        "bts7960 43a"
    ],
    "ESC & BLDC / Drone Parts": [
        "bldc esc 30a",
        "simonk 30a esc",
        "bidirectional esc",
        "2212 1400kv bldc motor",
        "propeller 1045",
        "f405 flight controller"
    ],
    "Batteries & Power": [
        "18650 3.7v battery",
        "lipo battery 3s 2200mah",
        "4s lipo battery",
        "cr2032 battery"
    ],
    "BMS & Protection": [
        "bms 3s 20a",
        "bms 1s 3.7v",
        "bms 4s 40a"
    ],
    "Wheels & Robotics Mechanics": [
        "mecanum wheel",
        "omni wheel 100mm",
        "robot wheel 65mm",
        "mg996r servo",
        "sg90 servo",
        "nema 17 stepper motor"
    ],
    "Encoders & Feedback": [
        "rotary encoder ky-040",
        "as5600 magnetic encoder",
        "optical encoder module"
    ],
    "Passives & Discretes": [
        "10k resistor",
        "100uf capacitor",
        "1n4007 diode",
        "5mm red led"
    ],
    "Kits & Starter Packs": [
        "resistor assortment kit",
        "capacitor kit",
        "beginner arduino starter kit",
        "electronic component kit"
    ],
    "Sensors": [
        "mpu6050",
        "bme280 sensor",
        "dht22 temperature sensor",
        "vl53l0x time of flight",
        "hc-sr04 ultrasonic"
    ],
    "Microcontrollers & SBCs": [
        "arduino uno r3",
        "esp32",
        "esp32 cam",
        "stm32f103c8t6",
        "rp2040",
        "raspberry pi 4",
        "raspberry pi 5 8gb"
    ],
    "Displays": [
        "0.96 oled i2c",
        "1602 lcd i2c",
        "st7789 display"
    ],
    "Relays & Switches": [
        "single channel relay",
        "relay module 4 channel",
        "asw-15d switch",
        "endstop limit switch"
    ]
}


def fetch_search(query: str) -> List[Dict[str, Any]]:
    url = f"http://127.0.0.1:8000/search?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "AuditScript/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("comparison_groups", [])


def extract_spec_tokens(title: str) -> Dict[str, str]:
    """Extract key discriminatory spec tokens from title."""
    t = title.lower()
    specs = {}
    
    # Cell count: 1s, 2s, 3s, 4s, 6s
    m_cell = re.search(r'\b([1-6]s)\b', t)
    if m_cell:
        specs["cells"] = m_cell.group(1)
        
    # Current: e.g. 20a, 30a, 40a, 43a
    m_curr = re.search(r'\b(\d+(?:\.\d+)?)\s*a\b', t)
    if m_curr:
        specs["current"] = f"{m_curr.group(1)}a"
        
    # Voltage: e.g. 3.3v, 5v, 12v, 24v
    m_volt = re.search(r'\b(\d+(?:\.\d+)?)\s*v\b', t)
    if m_volt:
        specs["voltage"] = f"{m_volt.group(1)}v"
        
    # KV rating: e.g. 1000kv, 1400kv, 2200kv
    m_kv = re.search(r'\b(\d+)\s*kv\b', t)
    if m_kv:
        specs["kv"] = f"{m_kv.group(1)}kv"
        
    # RAM: e.g. 2gb, 4gb, 8gb, 16gb
    m_ram = re.search(r'\b(\d+)\s*(?:gb|g)\s*(?:ram)?\b', t)
    if m_ram and "flash" not in t:
        specs["ram"] = f"{m_ram.group(1)}gb"
        
    # Wheel size: e.g. 60mm, 65mm, 100mm
    m_dim = re.search(r'\b(\d+)\s*mm\b', t)
    if m_dim:
        specs["dim"] = f"{m_dim.group(1)}mm"
        
    # Pack count: e.g. 10pcs, 50pcs, 100pcs, pack of 10
    m_pack = re.search(r'\b(?:pack\s*of\s*(\d+)|(\d+)\s*pcs|\((\d+)\s*pcs\))\b', t)
    if m_pack:
        pcs = m_pack.group(1) or m_pack.group(2) or m_pack.group(3)
        specs["pack"] = f"{pcs}pcs"

    # Resistance: e.g. 10k, 1k, 100k, 220r
    m_res = re.search(r'\b(\d+(?:\.\d+)?\s*[k|m|r]?(?:ohm)?)\b', t)
    # Only if contains ohm or k/m/r specifically
    m_res2 = re.search(r'\b(\d+(?:\.\d+)?)\s*(k|m|r|ohm|kohm)\b', t)
    if m_res2:
        specs["resistance"] = f"{m_res2.group(1)}{m_res2.group(2)}"
        
    return specs

def tokenize_title(title: str) -> set:
    words = re.findall(r'[a-zA-Z0-9]+', title.lower())
    stop = {"the", "and", "for", "with", "pcs", "pack", "in", "of", "to", "board", "module", "original"}
    return {w for w in words if w not in stop and len(w) > 1}

def audit():
    print(f"Starting End-to-End Audit across {sum(len(v) for v in TAXONOMY.values())} queries...")
    
    total_queries = 0
    total_groups = 0
    total_merged_cards = 0
    
    false_positives = []
    suspicious_merges = []
    potential_false_negatives = []
    ranking_anomalies = []
    
    category_summary = defaultdict(lambda: {"queries": 0, "groups": 0, "merged": 0, "fp": 0, "fn": 0})
    
    for category, queries in TAXONOMY.items():
        print(f"\n==================== {category} ====================")
        for q in queries:
            total_queries += 1
            category_summary[category]["queries"] += 1
            try:
                t0 = time.time()
                groups = fetch_search(q)
                dt = (time.time() - t0) * 1000
            except Exception as e:
                print(f"  [ERROR] Query '{q}' failed: {e}")
                continue
                
            num_groups = len(groups)
            total_groups += num_groups
            category_summary[category]["groups"] += num_groups
            
            merged_groups = [g for g in groups if len(g.get("offers", [])) > 1]
            total_merged_cards += len(merged_groups)
            category_summary[category]["merged"] += len(merged_groups)
            
            print(f"  Query: '{q}' -> {num_groups} cards ({len(merged_groups)} multi-vendor merges) in {dt:.1f}ms")
            
            # 1. Check Relevance of Top Card
            if groups:
                top_card = groups[0]
                top_title = (top_card.get("canonical_name") or "").lower()
                q_words = [w for w in q.lower().split() if len(w) > 2 and w not in ["sensor", "module", "driver", "board", "parts"]]
                missing_keywords = [w for w in q_words if w not in top_title]
                if missing_keywords and len(missing_keywords) == len(q_words):
                    ranking_anomalies.append({
                        "query": q,
                        "category": category,
                        "top_title": top_card.get("canonical_name"),
                        "issue": f"Top result does not contain keywords: {missing_keywords}"
                    })
            
            # 2. Check False Positives in Merged Groups
            for g in merged_groups:
                offers = g.get("offers", [])
                specs_list = [(o.get("vendor"), o.get("title", ""), extract_spec_tokens(o.get("title", "")), float(o.get("price", 0.0) or 0.0)) for o in offers]
                
                # Check conflicting specs
                for i in range(len(specs_list)):
                    for j in range(i + 1, len(specs_list)):
                        v1, t1, s1, p1 = specs_list[i]
                        v2, t2, s2, p2 = specs_list[j]
                        
                        conflicts = []
                        for key in ["cells", "current", "voltage", "kv", "ram", "dim", "pack"]:
                            if key in s1 and key in s2 and s1[key] != s2[key]:
                                conflicts.append(f"{key}: '{s1[key]}' vs '{s2[key]}'")
                                
                        if conflicts:
                            fp_record = {
                                "query": q,
                                "category": category,
                                "canonical_name": g.get("canonical_name"),
                                "merge_reason": g.get("merge_reason"),
                                "source": g.get("source"),
                                "conflicts": conflicts,
                                "item_a": f"[{v1}] {t1} (₹{p1})",
                                "item_b": f"[{v2}] {t2} (₹{p2})"
                            }
                            false_positives.append(fp_record)
                            category_summary[category]["fp"] += 1
                            print(f"    [FALSE POSITIVE DETECTED] {g.get('canonical_name')}")
                            print(f"      Conflict: {', '.join(conflicts)}")
                            print(f"      Offer A: [{v1}] {t1}")
                            print(f"      Offer B: [{v2}] {t2}")
                            
                        # Price ratio check (> 3.0x ratio on non-zero prices)
                        if p1 > 0 and p2 > 0:
                            ratio = max(p1, p2) / min(p1, p2)
                            if ratio >= 3.0 and not conflicts:
                                suspicious_merges.append({
                                    "query": q,
                                    "category": category,
                                    "canonical_name": g.get("canonical_name"),
                                    "ratio": round(ratio, 2),
                                    "item_a": f"[{v1}] {t1} (₹{p1})",
                                    "item_b": f"[{v2}] {t2} (₹{p2})"
                                })

            # 3. Check for Potential False Negatives (unmerged identical products in top 10 singletons)
            singletons = [g for g in groups[:10] if len(g.get("offers", [])) == 1]
            for i in range(len(singletons)):
                for j in range(i + 1, len(singletons)):
                    g1 = singletons[i]
                    g2 = singletons[j]
                    o1 = g1["offers"][0]
                    o2 = g2["offers"][0]
                    
                    if o1.get("vendor") == o2.get("vendor"):
                        continue # Same vendor, naturally not merged if separate items
                        
                    t1 = o1.get("title", "")
                    t2 = o2.get("title", "")
                    
                    tokens1 = tokenize_title(t1)
                    tokens2 = tokenize_title(t2)
                    
                    if not tokens1 or not tokens2:
                        continue
                        
                    intersection = tokens1.intersection(tokens2)
                    union = tokens1.union(tokens2)
                    jaccard = len(intersection) / len(union)
                    
                    # If high token similarity, verify specs don't conflict
                    s1 = extract_spec_tokens(t1)
                    s2 = extract_spec_tokens(t2)
                    has_conflict = any(k in s1 and k in s2 and s1[k] != s2[k] for k in ["cells", "current", "voltage", "kv", "ram", "dim", "pack"])
                    
                    if jaccard >= 0.70 and not has_conflict:
                        fn_record = {
                            "query": q,
                            "category": category,
                            "similarity": round(jaccard, 2),
                            "item_a": f"[{o1.get('vendor')}] {t1} (₹{o1.get('price')})",
                            "item_b": f"[{o2.get('vendor')}] {t2} (₹{o2.get('price')})"
                        }
                        potential_false_negatives.append(fn_record)
                        category_summary[category]["fn"] += 1
                        
    # Write full JSON audit results
    audit_data = {
        "summary": {
            "total_queries": total_queries,
            "total_groups": total_groups,
            "total_merged_cards": total_merged_cards,
            "false_positives_count": len(false_positives),
            "suspicious_merges_count": len(suspicious_merges),
            "potential_false_negatives_count": len(potential_false_negatives),
            "ranking_anomalies_count": len(ranking_anomalies)
        },
        "category_summary": dict(category_summary),
        "false_positives": false_positives,
        "suspicious_merges": suspicious_merges,
        "potential_false_negatives": potential_false_negatives,
        "ranking_anomalies": ranking_anomalies
    }
    
    with open("data/audit_results.json", "w", encoding="utf-8") as f:
        json.dump(audit_data, f, indent=2)
        
    print("\n=======================================================")
    print("                    AUDIT SUMMARY                      ")
    print("=======================================================")
    print(f"Total Queries Evaluated:       {total_queries}")
    print(f"Total Cards Examined:          {total_groups}")
    print(f"Total Merged Multi-vendor:     {total_merged_cards}")
    print(f"Confirmed False Positives:     {len(false_positives)}")
    print(f"Suspicious Price Ratios (>3x): {len(suspicious_merges)}")
    print(f"Potential Missed Merges (FN):  {len(potential_false_negatives)}")
    print(f"Ranking Anomalies:             {len(ranking_anomalies)}")
    print("=======================================================")
    print("Audit results written to data/audit_results.json")

if __name__ == "__main__":
    audit()
