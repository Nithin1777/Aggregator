import json
import re
import sys

# Force UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')


with open('audit_153_parsed.json', 'r', encoding='utf-8') as f:
    entries = json.load(f)

# Categories of False Positives:
# 1. Channel Count Mismatch (e.g. 1-ch vs 2-ch vs 4-ch vs 6-ch)
# 2. Electrical Rating Mismatch (e.g. 10A vs 5A, 300A vs 100A, 16A vs 20A, 200kg vs 100kg)
# 3. Battery Spec Mismatch (e.g. 6S vs 2S, LiFePO4 vs NMC)
# 4. Dev Board vs Embedded Display / Subsystem (e.g. bare dev board vs board with integrated 1.9" LCD, or 5" vs 2.8" LCD)
# 5. Non-MPN Protocol Collision (e.g. 'RS485' extracted as canonical MPN matching two completely different DYP distance sensors)
# 6. Module vs Dev Board (e.g. bare SMD stamp module ESP32-C6-MINI vs development board with USB/regulator)
# 7. Package Footprint Mismatch (e.g. PDIP-8 through-hole IC vs SOIC-8 SMD IC)
# 8. Audio Power / Topology Mismatch (e.g. 2.1ch Subwoofer vs 2.0ch stereo, or XH-M543 vs XH-M542)

results = []
fp_count = 0
tp_count = 0

for item in entries:
    idx = item['idx']
    t_a = item['title_a'].lower()
    t_b = item['title_b'].lower()
    score = item['score']
    reason = item['reason']
    
    issues = []
    
    # 1. Channel count
    ch_a = re.findall(r'\b(\d+)\s*(?:channel|ch|way)\b', t_a)
    ch_b = re.findall(r'\b(\d+)\s*(?:channel|ch|way)\b', t_b)
    if ch_a and ch_b:
        if ch_a[0] != ch_b[0]:
            issues.append(f"Channel mismatch: {ch_a[0]}ch vs {ch_b[0]}ch")
            
    # 2. Battery Series (e.g. 1S, 2S, 3S, 4S, 5S, 6S)
    s_a = re.findall(r'\b([1-6])s\b', t_a)
    s_b = re.findall(r'\b([1-6])s\b', t_b)
    if s_a and s_b:
        if s_a[0] != s_b[0]:
            issues.append(f"Battery series mismatch: {s_a[0]}S vs {s_b[0]}S")
            
    # 3. Battery Chemistry (LiFePO4 vs NMC/Li-ion)
    if ('lifepo4' in t_a and 'nmc' in t_b) or ('nmc' in t_a and 'lifepo4' in t_b):
        issues.append("Battery chemistry conflict: LiFePO4 vs NMC")
        
    # 4. Current rating (Amps) e.g. 10A vs 5A, 300A vs 100A, 1A vs 5A
    # Specifically for current transformers, BMS, or relays where rating is specified
    a_amps = set(re.findall(r'\b(\d+(?:\.\d+)?)\s*a\b', t_a))
    b_amps = set(re.findall(r'\b(\d+(?:\.\d+)?)\s*a\b', t_b))
    # Filter common non-ratings or check if there's an obvious discrepancy
    # E.g. in current transformer: 300a vs 100a
    if a_amps and b_amps and not (a_amps & b_amps):
        # Check if one is CT or BMS or Relay
        if any(k in t_a for k in ['transformer', 'sct', 'bms', 'relay', 'current']):
            issues.append(f"Current rating mismatch: {a_amps} vs {b_amps}")
            
    # 5. Weight / Load cell rating (kg)
    kg_a = re.findall(r'\b(\d+)\s*kg\b', t_a)
    kg_b = re.findall(r'\b(\d+)\s*kg\b', t_b)
    if kg_a and kg_b and kg_a[0] != kg_b[0]:
        issues.append(f"Load cell rating mismatch: {kg_a[0]}kg vs {kg_b[0]}kg")
        
    # 6. Display presence on dev board
    disp_a = bool(re.search(r'\b(?:display|lcd|oled|screen|touch)\b', t_a))
    disp_b = bool(re.search(r'\b(?:display|lcd|oled|screen|touch)\b', t_b))
    if any(k in t_a for k in ['esp32', 'pico', 'arduino', 'stm32']):
        if disp_a != disp_b:
            issues.append(f"Dev board display mismatch: has_disp_A={disp_a} vs has_disp_B={disp_b}")
            
    # 7. Screen size conflict if both have display
    sz_a = re.findall(r'(\d+(?:\.\d+)?)\s*(?:inch|\")', t_a)
    sz_b = re.findall(r'(\d+(?:\.\d+)?)\s*(?:inch|\")', t_b)
    if sz_a and sz_b and sz_a[0] != sz_b[0]:
        issues.append(f"Screen size mismatch: {sz_a[0]}\" vs {sz_b[0]}\"")
        
    # 8. Module vs Dev board form
    if ('module' in t_a and 'development board' in t_b and 'esp32-c6-mini' in t_a and 'wroom' in t_b):
        issues.append("SMD Module vs Dev Board with different core")
        
    # 9. False canonical MPN: RS485 on DYP ultrasonic sensors
    if 'rs485' in reason.lower() and ('dyp' in t_a or 'dyp' in t_b):
        if 'dyp-l062' in t_a and 'dyp-l07a' in t_b:
            issues.append("False MPN match on protocol 'RS485': DYP-L062 vs DYP-L07A")
            
    # 10. IC Package: PDIP vs SOIC
    if ('pdip' in t_a or 'dip-8' in t_a) and ('soic' in t_b or 'smd' in t_b):
        issues.append("IC Package footprint mismatch: DIP vs SOIC/SMD")
    elif ('pdip' in t_b or 'dip-8' in t_b) and ('soic' in t_a or 'smd' in t_a):
        issues.append("IC Package footprint mismatch: DIP vs SOIC/SMD")

    # 11. Audio channel / topology: 2.1 vs 2.0
    if ('2.1' in t_a and '2.0' in t_b) or ('2.0' in t_a and '2.1' in t_b):
        issues.append("Audio channel mismatch: 2.1ch vs 2.0ch")

    # 12. Audio amplifier board models: XH-M543 vs Vire-2C50 vs XH-M542
    if 'xh-m543' in t_a and 'xh-m542' in t_b:
        issues.append("Amp sub-model mismatch: XH-M543 vs XH-M542")
    if 'xh-m543' in t_a and 'vire-2c50' in t_b:
        issues.append("Amp sub-model mismatch: XH-M543 vs Vire-2C50")

    # 13. Brand dev board mismatch: DFRobot Beetle vs Waveshare
    if 'beetle' in t_a and 'waveshare' in t_b:
        issues.append("Board model mismatch: DFRobot Beetle vs Waveshare")

    # 14. SmartElex Pico vs Waveshare Pico
    if 'smartelex' in t_a and 'waveshare' in t_b and 'pico' in t_a:
        issues.append("Vendor board model conflict: SmartElex Pico vs Waveshare Pico")

    if issues:
        fp_count += 1
        results.append({
            'idx': idx,
            'is_tp': False,
            'issues': issues,
            'title_a': item['title_a'],
            'title_b': item['title_b'],
            'reason': reason
        })
    else:
        tp_count += 1
        results.append({
            'idx': idx,
            'is_tp': True,
            'issues': [],
            'title_a': item['title_a'],
            'title_b': item['title_b'],
            'reason': reason
        })

print(f"Total Merges Evaluated: {len(entries)}")
print(f"True Positives (Legitimate identical products across vendors): {tp_count}")
print(f"False Positives / Incompatible merges: {fp_count}")
precision = (tp_count / len(entries)) * 100
print(f"Raw Empirical Precision: {precision:.2f}%")

print("\n--- Summary of False Positives ---")
for r in results:
    if not r['is_tp']:
        print(f"[{r['idx']}] Issues: {', '.join(r['issues'])}")
        print(f"   A: {r['title_a']}")
        print(f"   B: {r['title_b']}")
        print(f"   Why: {r['reason']}\n")
