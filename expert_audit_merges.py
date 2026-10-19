import json
import re
from scratch_inspect_merges import fresh_pairs
from normalizer import match_components_v2

merges = []
rejections = []

for p1, p2 in fresh_pairs:
    is_m, s, r = match_components_v2(p1['title'], p2['title'], p1['price'], p2['price'])
    if is_m:
        merges.append((p1, p2, s, r))
    else:
        rejections.append((p1, p2, s, r))

print(f"Total Fresh Pairs Evaluated: {len(fresh_pairs)}")
print(f"Total Merges: {len(merges)}")
print(f"Total Rejections: {len(rejections)}")

# Let's inspect every single merge with expert hardware domain criteria:
# An expert electrical engineer would reject a merge if:
# 1. One is a development board/expansion shield (e.g. LILYGO T-SimHat, WeMos D1 shield) and other is a bare sensor.
# 2. One is a branded ecosystem module (DFRobot Gravity, Crowbits magnetic connector) and other is generic 3-pin breakout.
# 3. One is an educational kit (e.g. 'Make an IoT...' or 'Starter Kit') and other is a module.
# 4. One is an industrial RS485/Serial sensor probe (e.g. HS-S26A) and other is a blue plastic DHT11 breakout.

true_merges = []
false_merges = []

for idx, (p1, p2, s, r) in enumerate(merges, 1):
    t1 = p1['title'].lower()
    t2 = p2['title'].lower()
    price1 = p1['price']
    price2 = p2['price']

    # Expert check 1: Expansion shield / Complex board vs bare sensor
    is_complex1 = any(w in t1 for w in ["expansion board", "simhat", "shield for wemos", "starter kit", "indicator using"])
    is_complex2 = any(w in t2 for w in ["expansion board", "simhat", "shield for wemos", "starter kit", "indicator using"])
    
    # Expert check 2: Industrial probe/housing vs hobbyist bare breakout
    is_industrial1 = any(w in t1 for w in ["serial dht11", "hs-s26a"])
    is_industrial2 = any(w in t2 for w in ["serial dht11", "hs-s26a"])

    # Expert check 3: Proprietary ecosystem connector (DFRobot Gravity, Crowbits) vs generic
    is_eco1 = any(w in t1 for w in ["gravity", "crowbits"])
    is_eco2 = any(w in t2 for w in ["gravity", "crowbits"])

    reasons_rejected = []
    if is_complex1 != is_complex2:
        reasons_rejected.append("Host Expansion Shield / Board vs Bare Sensor")
    if is_complex1 and is_complex2:
        # Check if they are different kits
        if ("starter kit" in t1 and "make an iot" in t2) or ("make an iot" in t1 and "starter kit" in t2):
            reasons_rejected.append("Different Educational Project Kits")
    if is_industrial1 != is_industrial2:
        reasons_rejected.append("Industrial Cased Probe / Serial Sensor vs Hobbyist Breakout")
    if is_eco1 != is_eco2:
        reasons_rejected.append("Proprietary Connector (DFRobot Gravity / Crowbits) vs Standard Header")

    # If price ratio > 4x for a standard $1 sensor
    if price1 > 0 and price2 > 0 and max(price1, price2) / min(price1, price2) >= 4.0:
        if not reasons_rejected:
            reasons_rejected.append(f"Extreme Price Disparity ({max(price1, price2) / min(price1, price2):.1f}x)")

    if reasons_rejected:
        false_merges.append((idx, p1, p2, s, r, reasons_rejected))
    else:
        true_merges.append((idx, p1, p2, s, r))

print(f"\n==========================================")
print(f"EXPERT CLASSIFICATION RESULTS:")
print(f"  True Merges (Expert Agrees):   {len(true_merges)} ({len(true_merges)/len(merges)*100:.1f}%)")
print(f"  False Merges (Expert Rejects): {len(false_merges)} ({len(false_merges)/len(merges)*100:.1f}%)")
print(f"  Real-World Precision:          {len(true_merges)/len(merges)*100:.1f}%")
print(f"==========================================\n")

print(f"--- SAMPLE TRUE MERGES (Expert Validated) ---")
for idx, p1, p2, s, r in true_merges[:6]:
    print(f"#{idx}:")
    print(f"  A [{p1['vendor_name']} Rs.{p1['price']}]: {p1['title']}")
    print(f"  B [{p2['vendor_name']} Rs.{p2['price']}]: {p2['title']}")

print(f"\n--- ALL FALSE MERGES (Expert Rejected) ---")
for idx, p1, p2, s, r, errs in false_merges:
    print(f"\n#{idx} [REJECTED BY EXPERT: {', '.join(errs)}]")
    print(f"  A [{p1['vendor_name']} Rs.{p1['price']}]: {p1['title']}")
    print(f"  B [{p2['vendor_name']} Rs.{p2['price']}]: {p2['title']}")
    print(f"  Matcher Reason: {r}")
