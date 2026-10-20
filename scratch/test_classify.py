import sys

sys.path.insert(0, ".")
from src.spec.extractor import classify_category
from src.spec.taxonomy_loader import get_taxonomy

tax = get_taxonomy()
samples = [
    "100 Ohm 1/4W Carbon Film Resistor (Pack of 10)",
    "10uF 25V Electrolytic Capacitor",
    "Arduino Uno R3 Development Board",
    "NEMA17 Stepper Motor 1.8 Degree 2A",
    "3.7V 1300mAh LiPo Battery",
    "5V 1 Channel Relay Module",
    "ESP32 WROOM 32 WiFi Module",
    "M3 x 15mm Nylon Spacer",
]
for s in samples:
    print(f"{s!r:<60} -> {classify_category(s, tax)}")
