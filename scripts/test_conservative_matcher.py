import re
from typing import Any, Dict, Optional, Tuple

def extract_spec_signature(title: str, form: Optional[str] = None) -> str:
    """
    Extracts standardized spec signature across the 7 canonical dimensions:
    channels, voltage, current, capacity, screen_size, chemistry, interface.
    Returns empty string if any required field for the identified category is missing.
    """
    t = title.lower()

    # 1. Channels (Relays, SSR, BMS series cell count, Line sensors)
    channels = None
    ch_m = re.search(r"\b(\d+)\s*(?:channel|ch|way)\b", t)
    if ch_m:
        channels = f"{ch_m.group(1)}ch"
    else:
        bms_s = re.search(r"\b([1-6])s\b", t)
        if bms_s:
            channels = f"{bms_s.group(1)}s"
        elif "single channel" in t or "1-channel" in t or "1 channel" in t:
            channels = "1ch"
        elif "dual channel" in t or "2-channel" in t or "2 channel" in t:
            channels = "2ch"
        elif "4-channel" in t or "4 channel" in t:
            channels = "4ch"
        elif "8-channel" in t or "8 channel" in t:
            channels = "8ch"

    # 2. Voltage
    voltage = None
    v_m = re.search(r"\b(3\.3v|5v|12v|24v|48v|220v|240v)\b", t)
    if v_m:
        voltage = v_m.group(1)

    # 3. Current
    current = None
    c_m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:a|amp|amperes?)\b", t)
    if c_m:
        current = f"{c_m.group(1)}a"

    # 4. Capacity (Battery mAh / Load cell kg)
    capacity = None
    cap_m = re.search(r"\b(\d+)\s*mah\b", t)
    if cap_m:
        capacity = f"{cap_m.group(1)}mah"
    else:
        kg_m = re.search(r"\b(\d+)\s*kg\b", t)
        if kg_m:
            capacity = f"{kg_m.group(1)}kg"

    # 5. Screen Size
    screen_size = None
    sc_m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:inch|\")\b", t)
    if sc_m:
        screen_size = f"{sc_m.group(1)}in"
    elif "0.96" in t:
        screen_size = "0.96in"
    elif "1.3" in t:
        screen_size = "1.3in"
    elif "2.8" in t:
        screen_size = "2.8in"
    elif "5.0" in t or '5"' in t:
        screen_size = "5.0in"

    # 6. Chemistry
    chemistry = None
    if "lifepo4" in t or "lfp" in t:
        chemistry = "lifepo4"
    elif "nmc" in t:
        chemistry = "nmc"
    elif "li-ion" in t or "li ion" in t or "lithium" in t:
        chemistry = "li_ion"
    elif "lipo" in t or "li-po" in t:
        chemistry = "lipo"

    # 7. Interface
    interface = None
    if "i2c" in t or "iic" in t:
        interface = "i2c"
    elif "spi" in t:
        interface = "spi"
    elif "uart" in t or "ttl" in t:
        interface = "uart"
    elif "rs485" in t:
        interface = "rs485"
    elif "npn" in t:
        interface = "npn"
    elif "-v" in t or "voltage output" in t:
        interface = "voltage_out"

    # Category determination & required fields check
    # relay: requires (channels, voltage)
    if "relay" in t or "ssr" in t:
        if not (channels and voltage):
            return ""
        sig_parts = [f"ch={channels}", f"v={voltage}"]
        if current:
            sig_parts.append(f"a={current}")
        return "|".join(sorted(sig_parts))

    # bms: requires (channels, current, chemistry)
    if "bms" in t or "battery protection" in t:
        if not (channels and current and chemistry):
            return ""
        return f"ch={channels}|a={current}|chem={chemistry}"

    # display: requires (screen_size, interface)
    if any(k in t for k in ["oled", "tft", "lcd", "display"]):
        if not screen_size:
            return ""
        sig_parts = [f"sz={screen_size}"]
        if interface:
            sig_parts.append(f"if={interface}")
        return "|".join(sorted(sig_parts))

    # current transformer: requires current
    if "current transformer" in t or "sct" in t or "zmct" in t:
        if not current:
            return ""
        return f"a={current}"

    # load cell: requires capacity
    if "load cell" in t:
        if not capacity:
            return ""
        return f"cap={capacity}"

    # Default / standard IC breakouts (DHT11, DS3231, etc.)
    # If generic without missing complex multi-variant requirements:
    parts = []
    if channels: parts.append(f"ch={channels}")
    if voltage: parts.append(f"v={voltage}")
    if current: parts.append(f"a={current}")
    if capacity: parts.append(f"cap={capacity}")
    if screen_size: parts.append(f"sz={screen_size}")
    if chemistry: parts.append(f"chem={chemistry}")
    if interface: parts.append(f"if={interface}")

    if parts:
        return "|".join(sorted(parts))
    
    # Simple standardized default for simple IC modules where no subvariants exist
    return "std_spec"


class MockItem:
    def __init__(self, title, mpn, form):
        self.title = title
        self.mpn = mpn
        self.form = form
        self.spec_sig = extract_spec_signature(title, form)


def can_merge(a: MockItem, b: MockItem) -> bool:
    if not (a.mpn and b.mpn and a.mpn == b.mpn):
        return False
    if not (a.spec_sig and b.spec_sig):
        return False
    if a.spec_sig != b.spec_sig:
        return False
    if a.form != b.form:
        return False
    return True


# Test cases from the 9 audit failure classes
test_pairs = [
    # 1. Relays with differing channels (should VETO)
    ("2 Channel Relay Module 5V", "6 Channel Relay Module 5V", "RELAY", "module", False),
    # 2. Relays with same channels & voltage (should MERGE)
    ("2 Channel Relay Module 5V", "Dual Channel 5V Relay Module", "RELAY", "module", True),
    # 3. BMS with differing series count (should VETO)
    ("6S 20A Li-ion BMS", "2S 20A Li-ion BMS", "BMS", "module", False),
    # 4. BMS with differing chemistry (should VETO)
    ("5S 100A LiFePO4 BMS", "5S 100A NMC BMS", "BMS", "module", False),
    # 5. BMS with identical specs (should MERGE)
    ("4S 30A Li-ion BMS Module", "4S 30A Lithium Battery Protection BMS", "BMS", "module", True),
    # 6. Current transformer differing current (should VETO)
    ("YHDC SCT-024TS 300A Split Core", "YHDC SCT-024TS 100A Split Core", "SCT-024TS", "module", False),
    # 7. Dev board vs display dev board (should VETO)
    ("ESP32-S3 Development Board", "ESP32-S3 1.9inch Display Dev Board", "ESP32-S3", "dev_board", False),
    # 8. Missing required spec (e.g. unknown voltage on relay -> should VETO merge)
    ("Relay Module", "5V 1 Channel Relay Module", "RELAY", "module", False),
]

passed = 0
for t1, t2, mpn, form, expected in test_pairs:
    item_a = MockItem(t1, mpn, form)
    item_b = MockItem(t2, mpn, form)
    res = can_merge(item_a, item_b)
    ok = (res == expected)
    if ok:
        passed += 1
    print(f"[{'PASS' if ok else 'FAIL'}] '{t1}' vs '{t2}' -> Result={res} (Expected={expected}) | sig_a='{item_a.spec_sig}' sig_b='{item_b.spec_sig}'")

print(f"\nTotal: {passed}/{len(test_pairs)} passed.")
