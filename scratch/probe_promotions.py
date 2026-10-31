import sys

sys.path.insert(0, ".")
from src.spec.taxonomy_loader import load_taxonomy
from src.spec.vendor_specs import build_promoted_specs

tax, _ = load_taxonomy(reload=True)
samples = [
    ("100 Ohm 1/4W 1% 0603 Resistor", "resistors"),
    ("100uF 25V 10% Electrolytic Capacitor", "capacitors"),
    ("10mH 2A Inductor", "inductors"),
    ("1N4007 1A 1000V Rectifier Diode DO-41", "diodes"),
    ("BC547 NPN 45V 100mA TO-92 Transistor", "transistors"),
    ("IRFZ44N N-Channel MOSFET 55V 49A TO-220", "semiconductors"),
    ("NE555 DIP-8 Timer IC", "integrated-circuits"),
    ("2.54mm 40 Pin Header Connector", "connectors"),
    ("DHT22 Temperature Humidity Sensor 3.3V", "sensors"),
    ("5 inch 800x480 TFT Display SPI", "displays"),
    ("5mm Red LED 2V", "leds"),
    ("5V 4 Channel Relay Module 10A", "relay"),
    ("2A 250V Glass Fuse", "fuses"),
    ("16MHz Crystal HC-49", "crystals"),
    ("18AWG 0.8sq.mm Wire 1 Meter", "wires"),
    ("12V 5A 60W Power Supply", "power"),
    ("M3 15mm Nylon Spacer", "hardware"),
]
for title, cat in samples:
    specs = build_promoted_specs(title, cat, {}, tax)
    print("\n", title)
    for s in specs:
        print("  ", s["key"], "=", s["value"], "|", s["value_num"], s["value_type"])
