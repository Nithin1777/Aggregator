import json
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('audit_153_parsed.json', 'r', encoding='utf-8') as f:
    entries = json.load(f)

# Complete manual / heuristic classification of each of the 153 items
audit_results = []

failure_classes = {
    'channel_count': [],
    'rating_capacity': [],
    'battery_spec': [],
    'devboard_subsystem_display': [],
    'module_vs_board': [],
    'protocol_as_mpn': [],
    'ic_package_footprint': [],
    'audio_topology_submodel': [],
    'sensor_output_interface': []
}

for item in entries:
    idx = int(item['idx'])
    t_a = item['title_a'].lower()
    t_b = item['title_b'].lower()
    reason = item['reason']
    
    f_class = None
    note = ""
    
    # Check 1: IC Package footprint (DIP vs SOIC)
    if idx == 7:
        f_class = 'ic_package_footprint'
        note = "DS1307 DIP-8 through-hole IC vs SOIC-8 SMD IC (Incompatible PCB footprints)"
    # Check 2: Audio amplifier topology & sub-models
    elif idx == 24:
        f_class = 'audio_topology_submodel'
        note = "XH-M543 60W Mono/Dual vs Vire-2C50 50W+50W Stereo Class D"
    elif idx == 26:
        f_class = 'audio_topology_submodel'
        note = "XH-M543 vs XH-M542 Amplifier board models"
    elif idx == 28:
        f_class = 'audio_topology_submodel'
        note = "2x50W with Remote/FM vs 2x100W Audio Amplifier"
    elif idx == 29:
        f_class = 'audio_topology_submodel'
        note = "2.1 Channel Subwoofer vs 2.0 Channel Stereo with Tone Control"
    # Check 3: Channel count
    elif idx == 38:
        f_class = 'channel_count'
        note = "2 Channel Relay vs 6 Channel Relay"
    elif idx == 40:
        f_class = 'channel_count'
        note = "1 Channel SSR vs 2 Channel SSR"
    elif idx == 53:
        f_class = 'channel_count'
        note = "1 Channel PC817 Optocoupler vs 4 Channel PC817 Optocoupler"
    elif idx == 54:
        f_class = 'channel_count'
        note = "2 Channel Modbus RS485 Relay vs 4 Channel Modbus RS485 Relay"
    elif idx == 138:
        f_class = 'channel_count'
        note = "4-Channel IR Line Tracking vs 3-Channel IR Line Tracking"
    # Check 4: Electrical ratings (Amps / Kg / Ratio)
    elif idx == 57:
        f_class = 'rating_capacity'
        note = "Standard 10A 1-channel relay vs 30A High-Power SLA relay"
    elif idx == 59:
        f_class = 'rating_capacity'
        note = "1S 1A 1MOS BMS vs 1S 4.5A 3MOS BMS"
    elif idx == 65:
        f_class = 'rating_capacity'
        note = "2S 16A BMS vs 2S 20A BMS"
    elif idx == 66:
        f_class = 'rating_capacity'
        note = "5S 15A BMS vs 5S 20A BMS"
    elif idx == 113:
        f_class = 'rating_capacity'
        note = "YZC-516C 200kg Load Cell vs 100kg Load Cell"
    elif idx in [114, 115]:
        f_class = 'rating_capacity'
        note = "ZMCT103C 10A current sensor vs 5A range current sensor"
    elif idx == 116:
        f_class = 'rating_capacity'
        note = "YHDC SCT-024TS 300A/5A vs 100A/1A Split Core CT"
    elif idx == 117:
        f_class = 'rating_capacity'
        note = "YHDC SCT006 1A to 1mA vs 5A to 2.5mA Split Core CT"
    elif idx == 119:
        f_class = 'rating_capacity'
        note = "YHDC SCT016S 200A to 100mA vs 100A to 1A Split Core CT"
    # Check 5: Battery Series and Chemistry
    elif idx == 61:
        f_class = 'battery_spec'
        note = "6S (22.2V-25.2V) BMS vs 2S (7.4V-8.4V) BMS"
    elif idx == 67:
        f_class = 'battery_spec'
        note = "5S 100A LiFePO4 (3.2V cell) vs 5S 100A NMC (3.7V cell) chemistry"
    # Check 6: Protocol collision as MPN
    elif idx == 104:
        f_class = 'protocol_as_mpn'
        note = "Protocol 'RS485' matched DYP-L062M4W vs DYP-L07AYY4W (two distinct sensor models)"
    # Check 7: Dev Board Subsystems & Displays
    elif idx == 121:
        f_class = 'devboard_subsystem_display'
        note = "Bare ESP32-S3-WROOM-1 Dev Board vs Waveshare ESP32-S3 with 1.9\" Display"
    elif idx == 122:
        f_class = 'devboard_subsystem_display'
        note = "Waveshare ESP32-S3 with 5\" 1024x600 Touch LCD vs 2.8\" Round Display"
    elif idx == 123:
        f_class = 'devboard_subsystem_display'
        note = "DFRobot Beetle ESP32-C3 Dev Board vs Waveshare ESP32-C3 with 0.71\" Display"
    elif idx == 126:
        f_class = 'devboard_subsystem_display'
        note = "SmartElex ESP32-C6 Pico Dev Board vs Waveshare ESP32-C6 Pico Dev Board"
    # Check 8: Module vs Dev Board
    elif idx in [124, 125]:
        f_class = 'module_vs_board'
        note = "Bare SMD Castellation Module (ESP32-C6-MINI) vs Dev Board with USB & pin headers"
    # Check 9: Sensor Output Interface
    elif idx in [107, 108]:
        f_class = 'sensor_output_interface'
        note = "XKC-Y25/Y26-V (Analog Voltage output) vs XKC-Y25/Y26-NPN (Open-Collector output)"

    is_tp = (f_class is None)
    if not is_tp:
        failure_classes[f_class].append(idx)

    audit_results.append({
        'idx': idx,
        'is_tp': is_tp,
        'f_class': f_class,
        'note': note,
        'title_a': item['title_a'],
        'title_b': item['title_b'],
        'v_a': item['v_a'],
        'v_b': item['v_b'],
        'price_a': item['p_a'],
        'price_b': item['p_b'],
        'score': item['score'],
        'reason': item['reason']
    })

total = len(audit_results)
tp_num = sum(1 for r in audit_results if r['is_tp'])
fp_num = sum(1 for r in audit_results if not r['is_tp'])
precision = (tp_num / total) * 100

print(f"AUDIT TOTALS:")
print(f"Total Fresh Pairs Audited: {total}")
print(f"True Positives: {tp_num}")
print(f"False Positives: {fp_num}")
print(f"Empirical Precision: {precision:.2f}%")
print("\nBREAKDOWN BY FAILURE CLASS:")
for k, v in failure_classes.items():
    print(f"  - {k} ({len(v)} occurrences): IDs {v}")
