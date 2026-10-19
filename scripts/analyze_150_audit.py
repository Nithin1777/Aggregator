import re
import sys
import os

log_file = r"C:\Users\nithi\.gemini\antigravity\brain\54ea6d64-2aa6-4ae5-9ae9-2904735ca905\.system_generated\tasks\task-1752.log"

with open(log_file, "r", encoding="utf-8") as f:
    text = f.read()

# Pattern to extract blocks
pattern = re.compile(
    r"\[(\d+)\]\s+\[(.*?)\]\s+Card:\s+(.*?)\n"
    r"\s+Vendor A \((.*?), Rs\. (.*?)\):\s+(.*?)\n"
    r"\s+Vendor B \((.*?), Rs\. (.*?)\):\s+(.*?)\n"
    r"\s+Verdict:\s+Match=(.*?)\s+\|\s+Score=(.*?)\s+\|\s+Reason:\s+(.*?)\n"
)

matches = pattern.findall(text)
print(f"Total parsed merges: {len(matches)}")

# Known expert judgment criteria for each pair:
# A merge is a True Positive if both products represent the same physical electronic component, module, or assembly.
# It is a False Positive if:
# 1. Accessory vs Host (e.g. cable vs dev board, socket vs sensor, sleeve vs sensor)
# 2. Bare silicon IC vs Dev Board / Breakout Module
# 3. Completely different board architectures sharing an MCU (e.g. Pico W vs Adafruit ItsyBitsy, Feather vs Wiznet, Uno vs Nano)
# 4. Mechanical frame standard collision (NEMA17 cable vs damper, NEMA17 motor vs combo)
# 5. Distinct display sizes/technologies (2-inch vs 1.3-inch)
# 6. Voltage / clock incompatible variants (Pro Mini 3.3V/8M vs 5V/16M)

# Let's inspect each:
fp_list = []
tp_list = []

for m in matches:
    idx, query, card, va, pa, ta, vb, pb, tb, verdict, score, reason = m
    idx = int(idx)
    pa = float(pa)
    pb = float(pb)
    
    # Hand-audited classifications:
    is_fp = False
    fp_reason = ""
    
    # Pair 2: 4-Pin I2C Blue vs 6-Pin SPI Yellow Blue Dual Color
    # While both use SSD1306, 4-pin I2C vs 6-pin SPI are different pinout/interface modules
    if idx == 2:
        is_fp = True
        fp_reason = "Interface / Pinout conflict: 4-Pin I2C vs 6-Pin SPI Yellow-Blue"
        
    # Pair 6: Waveshare (A) vs Waveshare (B)
    elif idx == 6:
        is_fp = True
        fp_reason = "Product variant mismatch: Waveshare 0.96 OLED (A) vs (B)"
        
    # Pair 10: Blue OLED vs White OLED
    elif idx == 10:
        is_fp = True
        fp_reason = "Display color conflict: Blue OLED vs White OLED screen"
        
    # Pair 13: Yellow Backlight vs Blue Backlight
    elif idx == 13:
        is_fp = True
        fp_reason = "Display backlight color: Yellow Backlight vs Blue Backlight LCD shield"
        
    # Pairs 19, 20, 21, 22: Casing pipe / sleeve vs actual DS18B20 sensor
    elif idx in [19, 20, 21, 22]:
        is_fp = True
        fp_reason = "Accessory vs Sensor: Stainless Steel Casing Pipe / Protective Sleeve vs actual DS18B20 sensor"
        
    # Pairs 38, 39, 40: Universal Sensor Socket vs MQ-2 sensor
    elif idx in [38, 39, 40]:
        is_fp = True
        fp_reason = "Connector vs Sensor: 7-Pin Universal Sensor Socket vs actual MQ-2 sensor"
        
    # Pairs 53, 54, 55: L293D module PCB vs L293D DIP-16 IC
    elif idx in [53, 54, 55]:
        is_fp = True
        fp_reason = "Form conflict: Motor Driver Module / PCB vs Bare DIP-16 IC"
        
    # Pairs 59, 60: ULN2003 Bare IC vs ULN2003 Stepper Driver Board / Module
    elif idx in [59, 60]:
        is_fp = True
        fp_reason = "Form conflict: Bare Darlington Array IC vs Stepper Driver Module Board"
        
    # Pair 77: Pure copper cable vs vibration damper
    elif idx == 77:
        is_fp = True
        fp_reason = "Frame size standard collision: NEMA17 Cable vs NEMA17 Shock Absorber / Damper"
        
    # Pair 78: NEMA17 motor + driver combo vs NEMA17 bare motor
    elif idx == 78:
        is_fp = True
        fp_reason = "Bundle vs Single: NEMA17 Motor with DM542 Driver Combo vs bare NEMA17 Motor"
        
    # Pairs 80, 81: NEMA23 motor + driver combo vs bare motor
    elif idx in [80, 81]:
        is_fp = True
        fp_reason = "Bundle vs Single: NEMA23 Motor with TB6600 Driver Combo vs bare NEMA23 Motor"
        
    # Pair 84: ULN2003 Driver Board vs 28BYJ-48 Stepper Motor + ULN2003 Driver Combo
    elif idx == 84:
        is_fp = True
        fp_reason = "Bundle vs Single: Bare ULN2003 Driver Board vs Stepper Motor + Driver Board Combo"
        
    # Pair 85: ULN2003 Driver vs Smoother Module for Stepper
    elif idx == 85:
        is_fp = True
        fp_reason = "Lexical false positive: ULN2003 Driver vs TL-Smoother Filter Module"
        
    # Pairs 88, 90: Raspberry Pi Pico vs RP2040-Zero
    elif idx in [88, 90]:
        is_fp = True
        fp_reason = "Form-factor / pinout conflict: Raspberry Pi Pico (40-pin) vs RP2040-Zero (micro-board)"
        
    # Pair 92: Pico W vs Adafruit ItsyBitsy RP2040
    elif idx == 92:
        is_fp = True
        fp_reason = "Board architecture conflict: Raspberry Pi Pico W vs Adafruit ItsyBitsy RP2040"
        
    # Pair 93: Adafruit Feather RP2040 vs Wiznet W5500-EVB-Pico RP2040
    elif idx == 93:
        is_fp = True
        fp_reason = "Board architecture conflict: Adafruit Feather RP2040 vs Wiznet W5500 Ethernet Pico"
        
    # Pair 96: Pico 2 W vs Wiznet W5500-EVB-Pico
    elif idx == 96:
        is_fp = True
        fp_reason = "Board architecture conflict: Pico 2 W vs Wiznet W5500 Ethernet Pico"
        
    # Pair 98: Waveshare 2inch LCD vs 1.3inch LCD
    elif idx == 98:
        is_fp = True
        fp_reason = "Screen size spec conflict: 2-inch LCD vs 1.3-inch LCD"
        
    # Pair 100: Pico base vs Pico WH
    elif idx == 100:
        is_fp = True
        fp_reason = "Wireless capability conflict: Non-wireless Pico vs WiFi-enabled Pico WH"
        
    # Pairs 102, 103: STM32F103C8T6 Dev Board vs bare LQFP-48 IC
    elif idx in [102, 103]:
        is_fp = True
        fp_reason = "Form conflict: STM32F103C8T6 Development Board vs Bare LQFP-48 MCU IC"
        
    # Pairs 110, 111, 113: ATmega328P-MUR bare QFN IC vs Pro Mini / Nano / LilyPad dev boards
    elif idx in [110, 111, 113]:
        is_fp = True
        fp_reason = "Form conflict: ATMEGA328P bare IC vs Dev Board (Pro Mini / Nano / LilyPad)"
        
    # Pair 114: Uno R3 SMD Board vs ATmega328P TQFP-32 bare IC
    elif idx == 114:
        is_fp = True
        fp_reason = "Form conflict: Arduino Uno R3 Development Board vs Bare TQFP-32 IC"
        
    # Pair 117: Waveshare ATmega328P Uno-compatible Board vs Official Arduino Nano
    elif idx == 117:
        is_fp = True
        fp_reason = "Arduino model conflict: Uno form-factor board vs Nano form-factor board"
        
    # Pairs 118, 119: Pro Mini 3.3V/8MHz vs 5V/16MHz
    elif idx in [118, 119]:
        is_fp = True
        fp_reason = "Electrical / Clock conflict: Pro Mini 3.3V/8MHz vs Pro Mini 5V/16MHz"
        
    # Pairs 124, 125: ATMega328-PU Bootloader IC vs Maker Uno / Motor Driver Uno
    elif idx in [124, 125]:
        is_fp = True
        fp_reason = "Form conflict: ATmega328-PU Bare Bootloader IC vs Complete Dev Board / Motor Driver"
        
    # Pair 131: USB Cable vs Arduino Nano dev board
    elif idx == 131:
        is_fp = True
        fp_reason = "Accessory vs Host: USB Programming Cable vs Arduino Nano Development Board"
        
    # Pair 136: Official Arduino Nano Matter vs Official Arduino Nano classic A000005
    elif idx == 136:
        is_fp = True
        fp_reason = "Subfamily / Architecture conflict: Nano Matter (MGM240S) vs Nano Classic (ATmega328P)"
        
    # Pair 150: Arduino Mega bare board vs Arduino Mega + Cable + Acrylic Case bundle
    elif idx == 150:
        is_fp = True
        fp_reason = "Bundle vs Single: Arduino Mega Board vs Arduino Mega + Cable + Acrylic Case Kit"
        
    if is_fp:
        fp_list.append((idx, query, ta, tb, va, vb, pa, pb, score, reason, fp_reason))
    else:
        tp_list.append((idx, query, ta, tb, va, vb, pa, pb, score, reason))

total = len(matches)
tp_count = len(tp_list)
fp_count = len(fp_list)
precision = (tp_count / total) * 100

print(f"\nAUDIT SUMMARY FOR N={total} FRESH MERGES:")
print(f"True Positives (Correct Merges):  {tp_count} / {total} ({precision:.2f}%)")
print(f"False Positives (Erroneous Merges): {fp_count} / {total} ({(fp_count/total)*100:.2f}%)")
print(f"\nDETAILED BREAKDOWN OF ALL {fp_count} FALSE MERGES:")
for item in fp_list:
    idx, query, ta, tb, va, vb, pa, pb, score, reason, fp_reason = item
    print(f"\n[#{idx:03d}] [{query.upper()}] Error: {fp_reason}")
    print(f"      A ({va}, Rs {pa}): {ta}")
    print(f"      B ({vb}, Rs {pb}): {tb}")
    print(f"      Engine: {reason}")
