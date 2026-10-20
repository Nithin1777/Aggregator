"""
Component title normalization, MPN extraction, and cross-vendor matching engine.
"""
import re
from functools import lru_cache
from typing import List, Optional, Set, Tuple

# Known electronic part number patterns and common component chips
COMMON_MPN_PATTERNS = [
    # Specific modules and boards first before generic base chips
    r"\b(NodeMCU(?:[- ]?32S)?)\b",
    r"\b(ESP-?32(?:[- ]?(?:[CSHP]\d+[A-Z0-9]*|WROVER[A-Z0-9-]*|WROOM[A-Z0-9-]*|CAM))?)\b",
    r"\b(ESP-?8266)\b",
    r"\b(ESP-?01S?)\b",

    # Servos & Actuators
    r"\b(SG-?90[A-Z]?)\b",
    r"\b(MG-?90S?)\b",
    r"\b(MG-?996R?|MG-?995)\b",

    # Microcontrollers & Boards
    r"\b(ATMEGA328P(?:-PU|-AU)?)\b",
    r"\b(ATMEGA2560)\b",
    r"\b(ATTINY85)\b",
    r"\b(STM32F103C8T6)\b",
    r"\b(STM32F401(?:CCU6)?)\b",
    r"\b(STM32F411(?:CEU6)?)\b",
    r"\b(RP2040|RP2350)\b",
    r"\b(Raspberry Pi (?:5|4|3B\+|Pico\s*2(?: W)?|Pico(?: W)?))\b",
    r"\b(Arduino (?:Uno(?: R[34])?|Nano(?:[- ]?(?:33|R4|ESP32|Every))?|Mega(?: 2560)?|Pro Mini))\b",

    # Popular Sensors
    r"\b(MPU-?6050)\b",
    r"\b(MPU-?9250)\b",
    r"\b(BME280)\b",
    r"\b(BMP280)\b",
    r"\b(BMP180)\b",
    r"\b(DHT11)\b",
    r"\b(DHT22)\b",
    r"\b(HC-?SR04)\b",
    r"\b(VL53L0X)\b",
    r"\b(MAX6675)\b",
    r"\b(MAX30102)\b",
    r"\b(ADXL345)\b",
    r"\b(INA219)\b",
    r"\b(DS18B20)\b",
    r"\b(MQ-?[23456789]|MQ-?135)\b",
    r"\b(TCS3200)\b",

    # Power Regulators & Converters
    r"\b(LM2596S?)\b",
    r"\b(AMS1117(?:-[0-9.]+)?)\b",
    r"\b(LM7805|LM7812)\b",
    r"\b(XL4015)\b",
    r"\b(TP4056)\b",
    r"\b(MT3608)\b",
    r"\b(MP1584EN)\b",

    # Drivers, USB & Interfaces
    r"\b(CH340[GCN]?)\b",
    r"\b(CP2102)\b",
    r"\b(FT232RL?)\b",
    r"\b(L298N)\b",
    r"\b(L293D)\b",
    r"\b(A4988)\b",
    r"\b(DRV8825)\b",
    r"\b(ULN2003)\b",
    r"\b(MAX485)\b",
    r"\b(MCP2515)\b",
    r"\b(PCA9685)\b",

    # Displays
    r"\b(SSD1306)\b",
    r"\b(ST7789)\b",
    r"\b(ILI9341)\b",
    r"\b(TM1637)\b",
    r"\b(LCD-?1602)\b",
    r"\b(LCD-?2004)\b",

    # General ICs & Discretes
    r"\b(NE555P?)\b",
    r"\b(LM358[PN]?)\b",
    r"\b(LM324[N]?)\b",
    r"\b(LM393)\b",
    r"\b(PC817)\b",
    r"\b(IRFZ44N)\b",
    r"\b(74HC595)\b",
]

# Compiled MPN patterns
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in COMMON_MPN_PATTERNS]

# General IC code fallback: e.g. ATMEGA328, STM32F103, ESP32, LM2596, VC-02, etc.
GENERAL_IC_REGEX = re.compile(r"\b([A-Z]{2,}-?\d{2,}[A-Z0-9-]*)\b", re.IGNORECASE)

# Marketing buzzwords & noise tokens to strip
NOISE_WORDS = {
    "original", "genuine", "authentic", "high", "quality", "best", "price",
    "pack", "of", "combo", "free", "shipping", "brand", "new", "100%",
    "compatible", "for", "with", "india", "online", "buy", "deal", "special",
    "popular", "products", "item", "standard", "type", "pcs", "piece", "pieces",
    "fast", "delivery", "warranty", "month", "months", "year"
}

@lru_cache(maxsize=16384)
def clean_html(text: str) -> str:
    """Strip any basic HTML markup from a string."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text).strip()


@lru_cache(maxsize=16384)
def strip_compatibility_clauses(text: str) -> str:
    """Removes trailing compatibility spam like 'Compatible with Arduino/ESP32', 'for Arduino/ESP32/sensors', or host accessory clauses."""
    m = re.search(
        r"\b(compatible\s+with|for\s*[:\s]+(?:arduino|esp32|esp8266|raspberry|rpi|stm32|pico|rc\s+(?:drone|plane|car|model)|drone|robotics?|mq-?[0-9]+|ds18b20|nema\s*\d+|sensor\b)|using\s+(?:arduino|esp32|esp8266|raspberry|rpi|stm32|pico)|(?:arduino|esp32|nano)\s+alternative)\b",
        text,
        re.IGNORECASE
    )
    if m and m.start() > 5:
        return text[:m.start()].strip()
    return text

INVALID_MPN_TOKENS = {
    "TYPE-C", "TYPEC", "USB2", "USB3", "V1", "V2", "V3", "PIN", "ROHS",
    "TTL", "UART", "I2C", "SPI", "GPIO", "DIP", "SMD", "PCB", "LED", "LCD", "OLED", "PWM"
}

@lru_cache(maxsize=16384)
def extract_mpn(title: str) -> Optional[str]:
    """
    Extracts the most canonical Manufacturer Part Number (MPN) or IC identifier from a product title.
    Returns uppercase standardized string (e.g., 'ESP32-CAM', 'LM2596', 'MPU-6050').
    Excludes mechanical frame standards like NEMA17/NEMA23.
    """
    clean = clean_html(title)
    clean = re.sub(r"\s*-\s*RS\d+\s*$", "", clean, flags=re.IGNORECASE)
    stripped = strip_compatibility_clauses(clean)

    # 1. Check stripped text first (both compiled patterns and general IC regex)
    for pat in COMPILED_PATTERNS:
        match = pat.search(stripped)
        if match:
            val = match.group(1).upper()
            val = re.sub(r"\s+", "-", val)
            val = val.replace("ESP-32", "ESP32").replace("ESP-8266", "ESP8266")
            val = val.replace("NODEMCU32S", "NODEMCU-32S").replace("NODEMCU-32-S", "NODEMCU-32S")
            val = val.replace("MPU6050", "MPU-6050").replace("MPU9250", "MPU-9250")
            val = val.replace("HCSR04", "HC-SR04").replace("LCD1602", "LCD-1602")
            if not re.match(r"^NEMA\d*$", val, re.IGNORECASE):
                return val

    match = GENERAL_IC_REGEX.search(stripped)
    if match:
        val = match.group(1).upper()
        if val not in INVALID_MPN_TOKENS and not re.match(r"^NEMA\d*$", val, re.IGNORECASE):
            return val

    # 2. Only if stripped text found no identifier AND compatibility was not stripped, check unstripped clean text as fallback
    was_stripped = (stripped != clean)
    if not was_stripped:
        for pat in COMPILED_PATTERNS:
            match = pat.search(clean)
            if match:
                val = match.group(1).upper()
                val = re.sub(r"\s+", "-", val)
                val = val.replace("ESP-32", "ESP32").replace("ESP-8266", "ESP8266")
                val = val.replace("NODEMCU32S", "NODEMCU-32S").replace("NODEMCU-32-S", "NODEMCU-32S")
                val = val.replace("MPU6050", "MPU-6050").replace("MPU9250", "MPU-9250")
                val = val.replace("HCSR04", "HC-SR04").replace("LCD1602", "LCD-1602")
                if not re.match(r"^NEMA\d*$", val, re.IGNORECASE):
                    return val

        match = GENERAL_IC_REGEX.search(clean)
        if match:
            val = match.group(1).upper()
            if val not in INVALID_MPN_TOKENS and not re.match(r"^NEMA\d*$", val, re.IGNORECASE):
                return val

    return None

@lru_cache(maxsize=16384)
def normalize_title(title: str) -> str:
    """
    Produces a clean, lowercased, canonical string representing the core component.
    Strips noise words, punctuation, and extraneous spacing.
    """
    text = clean_html(title)
    text = re.sub(r"\s*-\s*RS\d+\s*$", "", text, flags=re.IGNORECASE)
    text = text.lower()


    # Replace punctuation with spaces except hyphens inside words
    text = re.sub(r"[^\w\s-]", " ", text)
    tokens = text.split()

    filtered = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        # Handle "pack of X"
        if tok == "pack" and i + 2 < len(tokens) and tokens[i+1] == "of":
            skip_next = True
            continue
        if tok in NOISE_WORDS:
            continue
        # Skip isolated numbers unless part of model (handled separately)
        filtered.append(tok)

    normalized = " ".join(filtered)
    # Deduplicate repeated adjacent words
    deduped = []
    for word in normalized.split():
        if not deduped or deduped[-1] != word:
            deduped.append(word)

    return " ".join(deduped).strip()

_PLURAL_EXCEPTIONS = {
    "lens", "series", "species", "gas", "bus", "plus", "news", "analysis",
    "basis", "axis", "status", "alias", "canvas", "atlas", "cms", "rms",
}


def _singularize(token: str) -> str:
    """
    Light singularization for match similarity only.

    Vendor titles disagree on plurality ("Creality Display Kit" vs "Kits"), and
    exact-token Jaccard then drops below the merge threshold, producing duplicate
    comparison cards. Part numbers are left untouched.
    """
    if len(token) <= 3 or token in _PLURAL_EXCEPTIONS or any(ch.isdigit() for ch in token):
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "ics")):
        return token[:-1]
    return token


@lru_cache(maxsize=16384)
def get_token_set(text: str) -> Set[str]:
    """Returns normalized set of keywords (plural-tolerant)."""
    return {_singularize(t) for t in re.findall(r"\b\w{2,}\b", text.lower())}


@lru_cache(maxsize=32768)
def calculate_token_similarity(t1: str, t2: str) -> float:
    """Calculates Jaccard similarity between two token sets."""
    s1 = get_token_set(t1)
    s2 = get_token_set(t2)
    if not s1 or not s2:
        return 0.0
    intersection = s1.intersection(s2)
    union = s1.union(s2)
    return len(intersection) / len(union)

def get_canonical_cluster_key(title: str) -> str:
    """
    Returns a clustering key for grouping identical components across vendors (V1).
    Priority: MPN if present; otherwise the top 3-4 significant sorted tokens.
    """
    mpn = extract_mpn(title)
    if mpn:
        return f"mpn:{mpn}"
    
    norm = normalize_title(title)
    tokens = sorted(list(get_token_set(norm)))[:4]
    return f"tokens:{'_'.join(tokens)}"


# ---------------------------------------------------------------------------
# MATCHER V2: Multi-Tiered Matching, Spec Guards, and Domain Normalization
# ---------------------------------------------------------------------------

SYNONYM_REPLACEMENTS = [
    (r"\bwlan\b", "wifi"),
    (r"\bbt\b", "bluetooth"),
    (r"\bdevkit\b", "development board"),
    (r"\biic\b", "i2c"),
    (r"\b(\d+)\s*p\b", r"\1 pin"),
    (r"\b(\d+)\s*pins\b", r"\1 pin"),
    (r"\b(\d+)\s*gram(?:s)?\b", r"\1g"),
    (r"\b(\d+(?:\.\d+)?)\s*(?:inch(?:es)?|\")\b", r"\1 inch"),
    (r"\b12864\b", "128x64"),
    (r"\btype-c\b", "usb-c"),
    (r"\btypec\b", "usb-c"),
    (r"\blithium\b", "li-ion"),
    (r"\bwireless\b", "wifi"),
    (r"\bch340g\b", "ch340"),
    (r"\bstep-down\b", "buck"),
    (r"\bstep down\b", "buck"),
    (r"\bstep-up\b", "boost"),
    (r"\bstep up\b", "boost"),
    (r"\bsg-90\b", "sg90"),
    (r"\bhcsr04\b", "hc-sr04"),
]

@lru_cache(maxsize=16384)
def apply_synonyms(text: str) -> str:
    """Standardizes electronic synonyms, abbreviations, and units."""
    res = text.lower()
    for pat, rep in SYNONYM_REPLACEMENTS:
        res = re.sub(pat, rep, res, flags=re.IGNORECASE)
    return res

def canonical_base_mpn(mpn: Optional[str]) -> Optional[str]:
    """Normalizes part numbers to their core silicon identity."""
    if not mpn:
        return None
    m = mpn.upper().replace("-", "")
    if m.startswith("LM2596HV"):
        return "LM2596HV"
    if m.startswith("LM2596"):
        return "LM2596"
    if m.startswith("SG90"):
        return "SG90"
    if m.startswith("MG90"):
        return "MG90"
    if m.startswith("MG995") or m.startswith("MG996"):
        return "MG996R"
    if m.startswith("HCSR04"):
        return "HC-SR04"
    if m.startswith("CH340"):
        return "CH340"
    if "PICO" in m:
        if "PICO2" in m:
            return "PICO-2"
        if "PICOW" in m:
            return "PICO-W"
        return "PICO"
    if "NANO" in m and not m.startswith("INA"):
        if "NANO33" in m:
            return "ARDUINO-NANO-33"
        if "NANOR4" in m:
            return "ARDUINO-NANO-R4"
        if "NANOESP32" in m:
            return "ARDUINO-NANO-ESP32"
        if "NANOEVERY" in m:
            return "ARDUINO-NANO-EVERY"
        return "ARDUINO-NANO"
    if m.startswith("ESP32"):
        # Match subfamily: letter + digits, e.g. C3, C6, C61, S2, S3, H2, P4, C5, H4
        sub = re.search(r"^ESP32([A-Z]\d+)", m)
        if sub:
            return f"ESP32-{sub.group(1)}"
        if "WROVER" in m:
            return "ESP32-WROVER"
        if "WROOM" in m:
            return "ESP32-WROOM"
        if "CAM" in m:
            return "ESP32-CAM"
        if m in {"ESP32", "ESP32DEVKITC", "ESP3232S", "ESP32D0WD", "ESP32D0WDQ6", "ESP32S0WD", "ESP32D2WD"}:
            return "ESP32"
        # Inverted default: unknown suffix after ESP32 remains specific (deny-by-default)
        rest = m[5:].strip("-")
        if rest:
            return f"ESP32-{rest}"
        return "ESP32"
    return mpn.upper()

@lru_cache(maxsize=16384)
def extract_board_model(t: str) -> Optional[str]:
    """Extracts commercial microcontroller development board models."""
    # ICs with bootloaders (e.g. ATMega328P with Arduino Uno Bootloader) are bare ICs, not boards
    if re.search(r"\bboot-?loader\b", t):
        return None

    # 1. Raspberry Pi / RP2040 boards
    if re.search(r"\bpico[\s_-]*2[\s_-]*(?:w|wh)\b", t):
        return "pico_2_w"
    if re.search(r"\bpico[\s_-]*2\b", t):
        return "pico_2"
    if re.search(r"\bpico[\s_-]*(?:w|wh)\b", t):
        return "pico_w"
    if re.search(r"\b(?:w5500[\s_-]*evb[\s_-]*pico|wiznet[\s_-]*pico)\b", t):
        return "w5500_evb_pico"
    if re.search(r"\b(?:rp2040[\s_-]*zero|pico[\s_-]*zero)\b", t):
        return "rp2040_zero"
    if re.search(r"\bitsybitsy\b", t):
        return "itsybitsy"
    if re.search(r"\bfeather\b", t):
        return "feather"
    if re.search(r"\bxiao\b", t):
        return "xiao"
    if re.search(r"\bqt[\s_-]*py\b", t):
        return "qt_py"
    if re.search(r"\b(?:raspberry\s*pi\s*pico|rpi\s*pico|pico\s*h)\b", t):
        return "pico_base"

    # 2. Arduino boards
    if re.search(r"\bnano[\s_-]*matter\b", t):
        return "nano_matter"
    if re.search(r"\bnano[\s_-]*every\b", t):
        return "nano_every"
    if re.search(r"\bnano[\s_-]*33[\s_-]*ble\b", t):
        return "nano_33_ble"
    if re.search(r"\bnano[\s_-]*33[\s_-]*iot\b", t):
        return "nano_33_iot"
    if re.search(r"\bnano[\s_-]*33\b", t):
        return "nano_33"
    if re.search(r"\bnano[\s_-]*rp2040\b", t):
        return "nano_rp2040"
    if re.search(r"\bnano[\s_-]*esp32\b", t):
        return "nano_esp32"
    if re.search(r"\bpro[\s_-]*micro\b", t):
        return "pro_micro"
    if re.search(r"\bpro[\s_-]*mini\b", t):
        return "pro_mini"
    if re.search(r"\blilypad\b", t):
        return "lilypad"
    if re.search(r"\bleonardo\b", t):
        return "leonardo"
    if re.search(r"\bdue\b", t):
        return "due"
    if re.search(r"\buno[\s_-]*r4\b", t) or "minima" in t:
        return "uno_r4"
    if re.search(r"\buno\b", t):
        return "uno_r3"
    if re.search(r"\bnano\b", t):
        return "nano_classic"
    if re.search(r"\bmega\b", t):
        return "mega_2560"

    # 3. ESP32 boards
    if re.search(r"\besp32[\s_-]*cam\b", t):
        return "esp32_cam"
    if re.search(r"\bnodemcu\b", t):
        return "nodemcu"
    if re.search(r"\bwemos[\s_-]*d1\b", t):
        return "wemos_d1"

    # 4. STM32 boards
    if re.search(r"\bblack[\s_-]*pill\b", t):
        return "black_pill"
    if re.search(r"\bblue[\s_-]*pill\b", t):
        return "blue_pill"
    if re.search(r"\bnucleo\b", t):
        return "nucleo"
    if re.search(r"\bdiscovery\b", t):
        return "discovery"

    return None

@lru_cache(maxsize=16384)
def extract_bundles(title: str) -> Set[str]:
    """Detects bundled accessories, combo items, or peripheral add-ons."""
    t = clean_html(title).lower()
    bundles = set()
    # Bundle connectors / cables
    if re.search(r"\b(?:\+|with)\s*(?:[\w\s-]*\s+)?(?:cables?|wires?|cords?)\b", t):
        bundles.add("cable")
    # Bundle cases / enclosures
    if re.search(r"\b(?:\+|with)\s*(?:[\w\s-]*\s+)?(?:cases?|enclosures?|shells?|box)\b", t):
        bundles.add("case")
    # Bundle motor drivers
    if re.search(r"\b(?:\+|with|and)\s*(?:[\w\s-]*\s+)?(?:stepper\s*drivers?|motor\s*drivers?|drivers?|tb6600|dm542|uln2003|a4988|drv8825)\b", t):
        bundles.add("driver")
    # Bundle motors
    if re.search(r"\b(?:\+|with|and)\s*(?:[\w\s-]*\s+)?(?:stepper\s*motors?|servos?|motors?|28byj-?48)\b", t):
        bundles.add("motor")
    # Bundle displays
    if re.search(r"\b(?:\+|with)\s*(?:[\w\s-]*\s+)?(?:oled|lcd|screens?|displays?)\b", t):
        bundles.add("display")
    # Bundle heatsinks
    if re.search(r"\b(?:\+|with)\s*(?:[\w\s-]*\s+)?(?:heat\s*sinks?)\b", t):
        bundles.add("heatsink")
    return bundles

@lru_cache(maxsize=16384)
def extract_hardware_specs(title: str) -> dict:
    """Extracts distinctive hardware specs (pin count, dimensions, RAM, direction)."""
    t = apply_synonyms(title.lower())
    specs = {}

    # Board Model (Architectural general field)
    bm = extract_board_model(t)
    if bm:
        specs["board_model"] = bm

    # Pins
    pm = re.search(r"\b(30|38|40|28|16|8)[-\s]*pins?\b", t)
    if pm:
        specs["pins"] = pm.group(1)

    # RAM capacity
    rm = re.search(r"\b([1248]gb)\b", t)
    if rm:
        specs["ram"] = rm.group(1).upper()

    # Dimensions
    dm = re.search(r"\b(\d{2,4})\s*mm\b", t)
    if dm:
        specs["dimension_mm"] = dm.group(1)

    # Dynamic Screen size (handles decimals and integers: 0.96", 1.3", 2", etc.)
    sm = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:inch|\")\b", t)
    if sm:
        specs["screen_size"] = sm.group(1)

    # Display color / backlight
    if re.search(r"\b(oled|lcd|tft|display|screen)\b", t):
        cm = re.search(r"\b(blue|white|yellow[- ]?blue|yellow|green|red|dual[- ]?color)\s*(?:backlight|light|color|colour|screen|display|oled|lcd)?\b", t)
        if cm:
            color = cm.group(1).replace("-", " ")
            if color in {"blue", "white", "yellow blue", "yellow", "green", "red", "dual color"}:
                specs["display_color"] = color

    # Operating voltage (3.3V vs 5V)
    vm = re.search(r"\b(3\.3v|5v)\b", t)
    if vm:
        specs["operating_voltage"] = vm.group(1)

    # Clock speed (8MHz vs 16MHz)
    fm = re.search(r"\b(8mhz|16mhz)\b", t)
    if fm:
        specs["clock_speed"] = fm.group(1)

    # NEMA frame size (17, 23, etc.)
    nm = re.search(r"\b(nema\s*\d{2})\b", t)
    if nm:
        specs["nema_frame"] = re.sub(r"\s+", "", nm.group(1))

    # RPM
    rpm = re.search(r"\b(\d{2,5})\s*rpm\b", t)
    if rpm:
        specs["rpm"] = rpm.group(1)

    # Direction / Conversion topology
    if "3.3v to 5v" in t or "3.3v-5v" in t:
        specs["direction"] = "3.3v_to_5v"
    elif "5v to 3.3v" in t or "5v-3.3v" in t:
        specs["direction"] = "5v_to_3.3v"
    elif "220v to 12v" in t or "ac to dc" in t or "ac-dc" in t:
        specs["direction"] = "ac_to_dc"
    elif "12v to 220v" in t or "dc to ac" in t or "dc-ac" in t or "inverter" in t:
        specs["direction"] = "dc_to_ac"

    if "boost" in t:
        specs["topology"] = "boost"
    elif "buck" in t:
        specs["topology"] = "buck"

    # Specific module antennas / subvariants
    if re.search(r"\b32u\b", t) or "ipex" in t or "u.fl" in t:
        specs["antenna"] = "u.fl"
    elif re.search(r"\b32d\b", t) or "pcb antenna" in t:
        specs["antenna"] = "pcb"

    # Wireless (with word boundary to prevent 'pico with' false matches)
    if re.search(r"\bpico[\s_-]*(?:w|wh)\b", t):
        specs["wireless"] = "pico_w"
    elif re.search(r"\bpico\b", t):
        specs["wireless"] = "pico_base"

    if "r4" in t or "minima" in t:
        specs["arduino_gen"] = "r4"
    elif "r3" in t:
        specs["arduino_gen"] = "r3"

    # Voltage rating (capacitors, etc.)
    v_rat = re.search(r"\b(10v|16v|25v|35v|50v|63v|100v|250v|400v)\b", t)
    if v_rat:
        specs["voltage_rating"] = v_rat.group(1)

    # USB bridge chip
    if re.search(r"\bch340[gcn]?\b", t):
        specs["usb_chip"] = "ch340"
    elif re.search(r"\bcp2102\b", t):
        specs["usb_chip"] = "cp2102"
    elif re.search(r"\bft232r?l?\b", t):
        specs["usb_chip"] = "ft232"

    # USB connector / interface port
    if re.search(r"\b(type[- ]?c|usb[- ]?c)\b", t):
        specs["usb_port"] = "type_c"
    elif re.search(r"\b(micro[- ]?usb|micro\s*b)\b", t):
        specs["usb_port"] = "micro_usb"
    elif re.search(r"\bmini[- ]?usb\b", t):
        specs["usb_port"] = "mini_usb"

    # Ecosystem
    if re.search(r"\bgravity\b", t):
        specs["ecosystem"] = "gravity"
    elif re.search(r"\bgrove\b", t):
        specs["ecosystem"] = "grove"
    elif re.search(r"\bcrowtail\b", t):
        specs["ecosystem"] = "crowtail"
    elif re.search(r"\bcrowbits\b", t):
        specs["ecosystem"] = "crowbits"
    elif re.search(r"\beasy[- ]plug\b", t):
        specs["ecosystem"] = "easy_plug"

    # Integrated Subsystems / Major Peripherals
    if re.search(r"\b(lora|sx127[68])\b", t):
        specs["has_lora"] = "lora"
    if re.search(r"\b(ov2640|camera)\b", t):
        specs["has_camera"] = "camera"
    if re.search(r"\b(0\.96|1\.28|1\.3|1\.44|1\.8|2\.4|2\.8|3\.5)\s*inch\b", t) or re.search(r"\b(oled|tft|lcd|voltmeter|(?:with|digital)\s+(?:voltmeter|display|screen))\b", t):
        specs["has_display"] = "display"

    return specs

# ---------------------------------------------------------------------------
# PRODUCT FORM & COMPOSITE TITLE CLASSIFICATION
# ---------------------------------------------------------------------------

PRODUCT_FORMS = [
    ("kit", re.compile(r"\b(kits?|combos?|starter\s*packs?|learning\s*packs?|starter\s*kits?|learning\s*kits?|project\s*kits?|make\s+an?|kt\d{3,4}|(?:measure|display|monitor|build)\b.*\busing\b)\b", re.IGNORECASE)),
    ("composite", re.compile(r"\b(\d+\s*in\s*\d+|\d+-way|\d+\s*channel|multi[- ]?sensor|multi[- ]?function)\b", re.IGNORECASE)),
    ("accessory", re.compile(r"\b(brackets?|mount(?:ing)?(?:\s*(?:brackets?|stands?|holders?))?|holders?|stands?|wheels?|pulleys?|standoffs?|screws?(?:\s*set)?|antennas?|cables?|wires?|cords?|sleeves?|casing\s*pipes?|sockets?|shock\s*absorbers?|vibration\s*dampers?|heat\s*sinks?|heatsinks?)\b", re.IGNORECASE)),
    ("enclosure", re.compile(r"\b(cases?|enclosures?|box(?:es)?|housings?|covers?|acrylic\s*shells?)\b", re.IGNORECASE)),
    ("robotics", re.compile(r"\b(drone|quadcopter|robot\s*chassis|smart\s*car|robotic\s*arm)\b", re.IGNORECASE)),
    ("shield", re.compile(r"\b(shield|[- ]?hat|bonnet|featherwing)\b", re.IGNORECASE)),
    ("expansion", re.compile(r"\b(exp[ae]nsion|expander|breakout\s*board|terminal\s*adapter|adapter\s*(?:plate|board|programmer)|carrier|base(?:\s*board|\s*plate)?|motherboard|quad\s*gpio)\b", re.IGNORECASE)),
    ("proprietary", re.compile(r"\b(gravity|stemma|qwiic|grove|ph2\.0|jst|crowbits|easy-plug|crowtail)\b", re.IGNORECASE)),
    ("chip", re.compile(r"\b(bare\s*ic|ic\s*chip|regulator\s*ic|switching\s*regulator\s*ic|interface\s*ic|driver\s*ic|darlington|transistor\s*arrays?|microcontroller\s*(?:ic|chip|mcu|unit)|microcontroller-mcu|boot-?loader(?:\s*ic)?|(?:power[- ]?|p)?dip-?\d+|lqfp-?\d*|tqfp-?\d*|tssop-?\d*|msop-?\d*|qfn-?\d*|soic-?\d*|sop-?\d*|to-\d+|smd\s*package|smd\s*chip|smd\s*ic|reel|tape)\b", re.IGNORECASE)),
    ("dev_board", re.compile(r"\b(dev(?:elopment)?\s*(?:board|kit)|devkit|core\s*board|minimum\s*system\s*(?:board|module)|mainboard)\b|\b(esp32|nodemcu|arduino|pico|stm32|rpi|raspberry)\b.*\b(board)\b|\b(uno|nano|mega|pro\s*mini|pro\s*micro|pico(?:\s*2)?(?:\s*w)?|nodemcu|feather|itsybitsy)\b", re.IGNORECASE)),
    ("module", re.compile(r"\b(module|breakout|sensor(?:\s*board)?|transducer|motor|servo|display|screen|driver|controller|converter|regulator|relay|switch|buzzer|board)\b", re.IGNORECASE)),
]

EXPANSION_ACTIVE_TOKENS = re.compile(r"\b(usb|ch340|cp2102|programmer|converter|uart|usb-to-ttl|bridge|active)\b", re.IGNORECASE)
EXPANSION_BOARD_TOKENS = re.compile(r"\b(module|board|converter|programmer)\b", re.IGNORECASE)
EXPANSION_PASSIVE_TOKENS = re.compile(r"\b(adapter|plate|breakout|breadboard|pinout|socket|pitch)\b", re.IGNORECASE)

def are_forms_incompatible(fa: Optional[str], fb: Optional[str]) -> bool:
    """Checks whether two hardware packaging/form-factors are mutually exclusive."""
    if fa == fb:
        return False
    
    # expansion sub-forms compatibility:
    # expansion_active and expansion_passive are hard-incompatible with each other.
    # Both are compatible with generic unclassified expansion.
    if fa in ("expansion_active", "expansion_passive") and fb in ("expansion_active", "expansion_passive"):
        return True
    if (fa == "expansion" and fb in ("expansion_active", "expansion_passive")) or \
       (fb == "expansion" and fa in ("expansion_active", "expansion_passive")):
        return False

    # module and dev_board are board/breakout forms that frequently interchange in vendor titles
    # (e.g. 'ESP32-CAM Module' vs 'ESP32-CAM Development Board').
    # They are guarded by board_model, spec guards, and subsystem presence checks.
    generic_board_forms = {"module", "dev_board"}
    if fa in generic_board_forms and fb in generic_board_forms:
        return False

    # Deny by default: None (unknown form) cannot merge with specialized forms or boards/modules,
    # and specialized forms (chip, accessory, enclosure, shield, expansion, kit, robotics) never merge across forms.
    return True

@lru_cache(maxsize=16384)
def classify_product_form(title: str) -> Optional[str]:
    """Classifies physical product packaging, architecture, and form factor."""
    clean = strip_compatibility_clauses(clean_html(title))
    for form_name, pat in PRODUCT_FORMS:
        if pat.search(clean):
            if form_name == "expansion":
                has_active = bool(EXPANSION_ACTIVE_TOKENS.search(clean) and EXPANSION_BOARD_TOKENS.search(clean))
                if has_active:
                    return "expansion_active"
                if EXPANSION_PASSIVE_TOKENS.search(clean):
                    return "expansion_passive"
                return "expansion"
            return form_name
    return None

@lru_cache(maxsize=16384)
def count_title_mpns(title: str) -> int:
    """Counts distinct major IC/module identifiers present in a title."""
    clean = clean_html(title).upper()
    found = set()
    for pat in COMPILED_PATTERNS:
        matches = pat.findall(clean)
        for m in matches:
            canonical = canonical_base_mpn(m) or m
            found.add(canonical)
    return len(found)

def check_hardware_conflict(
    title_a: str,
    title_b: str,
    specs_a: dict,
    specs_b: dict,
    base_mpn_a: Optional[str] = None,
    base_mpn_b: Optional[str] = None,
    price_a: float = 0.0,
    price_b: float = 0.0
) -> Tuple[bool, str]:
    """
    Evaluates hard negative constraints:
    1. Board model: Distinct commercial board models (Pico vs ItsyBitsy, Uno vs Nano) NEVER merge.
    2. Spec guards: Conflicting pin count, voltage, dimensions, RAM, direction, USB bridge, ecosystem, screen size, display color, clock speed.
    3. Bundle contents guard: Single board vs board+cable+case bundle.
    4. Subsystem presence guards: Display, LoRa radio, Camera.
    5. Tier A.5: MPN conflict veto (different chips can NEVER merge).
    6. Product Form Mismatch Veto (module vs shield vs kit vs expansion vs chip vs accessory).
    7. Multi-component Kit vs Kit & Accessory vs Accessory token similarity.
    8. Extreme Price Gap Veto.
    """
    # 1. Board model conflict (general field across all microcontroller ecosystems)
    bm_a = specs_a.get("board_model")
    bm_b = specs_b.get("board_model")
    if bm_a and bm_b and bm_a != bm_b:
        return True, f"Conflicting board model: '{bm_a}' vs '{bm_b}'"
    if (bm_a is not None) != (bm_b is not None):
        specific_bm = bm_a or bm_b
        return True, f"Hardware board model specificity mismatch: '{specific_bm}' vs unspecified"

    # 2. Spec guards
    guarded_keys = [
        "pins", "ram", "dimension_mm", "screen_size", "display_color", "operating_voltage", "clock_speed", "nema_frame", "rpm",
        "direction", "topology", "antenna", "wireless", "arduino_gen", "voltage_rating", "usb_chip", "usb_port", "ecosystem"
    ]
    for key in guarded_keys:
        if key in specs_a and key in specs_b:
            if specs_a[key] != specs_b[key]:
                return True, f"Conflicting spec '{key}': '{specs_a[key]}' vs '{specs_b[key]}'"

    # 3. Bundle contents guard (e.g. Bare item vs Item + Driver or Item + Cable + Case)
    bundles_a = extract_bundles(title_a)
    bundles_b = extract_bundles(title_b)
    if bundles_a != bundles_b:
        diff = bundles_a.symmetric_difference(bundles_b)
        return True, f"Bundle contents mismatch: '{bundles_a}' vs '{bundles_b}' (difference: {diff})"

    # 4. Integrated subsystem presence guards (Display, LoRa radio, Camera)
    for sub in ("has_display", "has_lora", "has_camera"):
        if (sub in specs_a) != (sub in specs_b):
            return True, f"Hardware feature presence mismatch: '{sub}'"

    # 5. Tier A.5 MPN conflict veto
    if base_mpn_a and base_mpn_b and base_mpn_a != base_mpn_b:
        return True, f"Tier A.5 MPN conflict: '{base_mpn_a}' != '{base_mpn_b}'"

    # 6. Product Form Mismatch Veto
    form_a = classify_product_form(title_a)
    form_b = classify_product_form(title_b)
    if are_forms_incompatible(form_a, form_b):
        return True, f"Product form mismatch: '{form_a}' vs '{form_b}'"

    # 7. Multi-Component Kit vs Kit Veto & Accessory vs Accessory Veto
    if (form_a == "kit" and form_b == "kit") or (form_a == "accessory" and form_b == "accessory"):
        sim = calculate_token_similarity(
            apply_synonyms(normalize_title(title_a)),
            apply_synonyms(normalize_title(title_b))
        )
        if sim < 0.60:
            cat = "kits" if form_a == "kit" else "hardware accessories"
            return True, f"Distinct {cat} (token overlap {sim:.2f} < 0.60)"

    # 8. Extreme Price Gap Veto
    if price_a > 0 and price_b > 0:
        ratio = max(price_a, price_b) / max(min(price_a, price_b), 1.0)
        if ratio > 3.5:
            return True, f"Extreme price disparity ({ratio:.1f}x difference)"

    return False, ""

def match_components_v2(
    title_a: str,
    title_b: str,
    price_a: float = 0.0,
    price_b: float = 0.0,
    threshold: float = 0.58
) -> Tuple[bool, float, str]:
    """
    Matcher V2 Pipeline:
    - Hard Veto Checks: Tier A.5 MPN conflict, Spec Guards, Form Incompatibility, Kit Veto, Price Gap Veto
    - Tier A: Exact Canonical MPN Match
    - Tier B: Domain-normalized token similarity
    """
    raw_mpn_a = extract_mpn(title_a)
    raw_mpn_b = extract_mpn(title_b)
    base_a = canonical_base_mpn(raw_mpn_a)
    base_b = canonical_base_mpn(raw_mpn_b)

    specs_a = extract_hardware_specs(title_a)
    specs_b = extract_hardware_specs(title_b)

    # 1. HARD VETO CHECK (Runs BEFORE Tier A!)
    has_conflict, reason = check_hardware_conflict(
        title_a, title_b, specs_a, specs_b, base_a, base_b, price_a, price_b
    )
    if has_conflict:
        return False, 0.0, f"Hard Veto: {reason}"

    # 2. TIER A: EXACT CANONICAL MPN MATCH
    if base_a and base_b and base_a == base_b:
        return True, 1.0, f"Tier A: Canonical MPN match '{base_a}'"

    # 3. TIER B: SYNONYM-NORMALIZED SIMILARITY
    # Asymmetric MPN check: An item with an identifiable canonical MPN can NEVER merge
    # with a title lacking an MPN via Tier B.
    if (base_a is not None) != (base_b is not None):
        return False, 0.0, "Tier B asymmetric MPN veto: Cannot merge specific MPN with generic title"

    syn_a = apply_synonyms(normalize_title(title_a))
    syn_b = apply_synonyms(normalize_title(title_b))
    sim = calculate_token_similarity(syn_a, syn_b)

    if sim >= threshold:
        return True, sim, f"Tier B: Similarity {sim:.3f} >= {threshold:.2f}"

    return False, sim, f"Below threshold: {sim:.3f} < {threshold:.2f}"

def get_canonical_cluster_key_v2(title: str) -> str:
    """Generates precise clustering key incorporating MPN, form factor, and critical hardware discriminators."""
    raw_mpn = extract_mpn(title)
    base_mpn = canonical_base_mpn(raw_mpn)
    specs = extract_hardware_specs(title)
    form = classify_product_form(title)
    
    spec_suffix = []
    # Distinctive packaging / form factors that should never share a cluster bucket
    if form in ("shield", "expansion", "expansion_active", "expansion_passive", "kit", "composite", "proprietary", "chip"):
        spec_suffix.append(f"form={form}")
    if "pins" in specs:
        spec_suffix.append(f"pins={specs['pins']}")
    if "direction" in specs:
        spec_suffix.append(f"dir={specs['direction']}")
    if "usb_chip" in specs:
        spec_suffix.append(f"usb={specs['usb_chip']}")
    if "ram" in specs:
        spec_suffix.append(f"ram={specs['ram']}")
    if "wireless" in specs:
        spec_suffix.append(f"w={specs['wireless']}")
    if "antenna" in specs:
        spec_suffix.append(f"ant={specs['antenna']}")
    if "screen_size" in specs:
        spec_suffix.append(f"size={specs['screen_size']}")
    if "dimension_mm" in specs:
        spec_suffix.append(f"dim={specs['dimension_mm']}")
    if "voltage_rating" in specs:
        spec_suffix.append(f"v={specs['voltage_rating']}")
    
    spec_str = f":{'_'.join(sorted(spec_suffix))}" if spec_suffix else ""

    if base_mpn:
        return f"mpn:{base_mpn}{spec_str}"

    syn_norm = apply_synonyms(normalize_title(title))
    tokens = sorted(list(get_token_set(syn_norm)))[:4]
    return f"tokens:{'_'.join(tokens)}{spec_str}"


# ---------------------------------------------------------------------------
# OPTION B: CONSERVATIVE MERGE & SPEC SIGNATURE PIPELINE
# ---------------------------------------------------------------------------

def extract_spec_signature(title: str, form: Optional[str] = None) -> str:
    """
    Extracts standardized spec signature across the 7 canonical dimensions:
    channels, voltage, current, capacity, screen_size, chemistry, interface.
    Populated only when ALL required fields for the category are present.
    Any missing -> empty string.
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
        elif "single channel" in t or "1-channel" in t or "1 channel" in t or "1 way" in t:
            channels = "1ch"
        elif "dual channel" in t or "2-channel" in t or "2 channel" in t or "2 way" in t:
            channels = "2ch"
        elif "4-channel" in t or "4 channel" in t or "4 way" in t:
            channels = "4ch"
        elif "6-channel" in t or "6 channel" in t or "6 way" in t:
            channels = "6ch"
        elif "8-channel" in t or "8 channel" in t or "8 way" in t:
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
    elif "5.0" in t or '5"' in t or "5 inch" in t:
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

    return ""


def extract_spec_discriminators(title: str) -> dict:
    """
    Extracts the 7 core spec discriminators:
    RAM, voltage, current, channels, chemistry, screen_size, package.
    """
    t = title.lower()
    specs = {}

    # 1. RAM (handles '3GB', '4GB', '16GB', '2 GB RAM', etc.)
    m_ram = re.search(r'\b(\d+)\s*(?:gb|g)\s*(?:ram)?\b', t)
    if m_ram and 'flash' not in t and 'micro' not in t and 'tf' not in t and 'emmc' not in t:
        specs['ram'] = f"{m_ram.group(1)}gb"

    # 2. Voltage (e.g. 3.3v, 5v, 12v, 24v, 48v, 80v, 220v)
    m_v = re.search(r'\b(3\.3|5|12|24|48|80|220|240)\s*v\b', t)
    if m_v:
        specs['voltage'] = f"{m_v.group(1)}v"

    # 3. Current (e.g. 1a, 2a, 10a, 20a, 30a, 40a, 43a)
    m_a = re.search(r'\b(\d+(?:\.\d+)?)\s*(?:a|amp|amperes?)\b', t)
    if m_a and 'type' not in t and 'class' not in t:
        specs['current'] = f"{m_a.group(1)}a"

    # 4. Channels / series cells (e.g. 1ch, 2ch, 4ch, 1s, 2s, 3s, 4s)
    m_ch = re.search(r'\b(\d+)\s*(?:channel|ch|way)\b', t)
    if m_ch:
        specs['channels'] = f"{m_ch.group(1)}ch"
    else:
        m_s = re.search(r'\b([1-6])s\b', t)
        if m_s:
            specs['channels'] = f"{m_s.group(1)}s"

    # 5. Chemistry
    if 'lifepo4' in t:
        specs['chemistry'] = 'lifepo4'
    elif 'nmc' in t:
        specs['chemistry'] = 'nmc'
    elif 'lipo' in t or 'li-po' in t:
        specs['chemistry'] = 'lipo'
    elif 'li-ion' in t or 'lithium' in t:
        specs['chemistry'] = 'li_ion'

    # 6. Screen size
    m_sc = re.search(r'\b(\d+(?:\.\d+)?)\s*(?:inch|\")\b', t)
    if m_sc:
        specs['screen_size'] = f"{m_sc.group(1)}in"

    # 7. Package / product form
    form = classify_product_form(title)
    if form:
        specs['package'] = form

    return specs


def check_spec_discriminator_conflict(title_a: str, title_b: str) -> Tuple[bool, Optional[str]]:
    """
    Evaluates conflicting specs across the 7 discriminators.
    Returns (True, conflict_reason) if any present spec conflicts.
    Returns (False, None) if specs match, are absent, or are equal.
    """
    s_a = extract_spec_discriminators(title_a)
    s_b = extract_spec_discriminators(title_b)

    for k in ('ram', 'voltage', 'current', 'channels', 'chemistry', 'screen_size'):
        if k in s_a and k in s_b and s_a[k] != s_b[k]:
            return True, f"conflicting {k}: '{s_a[k]}' vs '{s_b[k]}'"

    if 'package' in s_a and 'package' in s_b:
        if are_forms_incompatible(s_a['package'], s_b['package']):
            return True, f"incompatible package forms: '{s_a['package']}' vs '{s_b['package']}'"

    return False, None


class ComponentItem:

    """Wrapper holding MPN, form factor, and spec signature for conservative matching."""
    def __init__(self, title: str, price: float = 0.0, mpn: Optional[str] = None, form: Optional[str] = None):
        self.title = title
        self.price = price
        self.mpn = mpn or canonical_base_mpn(extract_mpn(title))
        raw_form = form or classify_product_form(title)
        if raw_form in ("dev_board", "module", "composite"):
            self.form = "module"
        else:
            self.form = raw_form
        self.spec_sig = extract_spec_signature(title, self.form)




class MergeResult(tuple):
    """Tuple subclass (merged: bool, reason: str) with boolean truthiness equal to merged."""
    def __new__(cls, merged: bool, reason: str):
        return super().__new__(cls, (bool(merged), str(reason)))

    @property
    def merged(self) -> bool:
        return self[0]

    @property
    def reason(self) -> str:
        return self[1]

    def __bool__(self) -> bool:
        return self[0]


def can_merge_three_outcome(a: Any, b: Any) -> Tuple[bool, str]:
    """
    Three-outcome Conservative Merge Rule with Tier B fallback:
      1. MPN conflict → hard reject ("mpn_conflict")
      2. Both specs present AND equal → confident merge ("spec_exact")
      3. Both specs present AND differ → variant family (reject merge) ("spec_variant")
      4. Otherwise (missing spec on either side) → defer to Tier B with existing discriminator guards ("deferred_tier_b:<reason>")
    """
    # Duck-typing: if strings passed, convert to ComponentItem
    if isinstance(a, str):
        a = ComponentItem(a)
    if isinstance(b, str):
        b = ComponentItem(b)

    title_a = getattr(a, "title", str(a))
    title_b = getattr(b, "title", str(b))
    price_a = float(getattr(a, "price", 0.0) or 0.0)
    price_b = float(getattr(b, "price", 0.0) or 0.0)

    mpn_a = getattr(a, "mpn", None)
    mpn_b = getattr(b, "mpn", None)

    # 1. MPN conflict → hard reject
    if mpn_a and mpn_b and mpn_a != mpn_b:
        return MergeResult(False, "mpn_conflict")

    sig_a = getattr(a, "spec_sig", "") or ""
    sig_b = getattr(b, "spec_sig", "") or ""

    # 2. Both specs present AND equal → confident merge
    if sig_a and sig_b and sig_a == sig_b:
        return MergeResult(True, "spec_exact")

    # 3. Both specs present AND differ → variant family (reject merge)
    if sig_a and sig_b and sig_a != sig_b:
        return MergeResult(False, "spec_variant")

    # 4. Otherwise (missing spec on either side) → defer to Tier B
    #    with the existing spec discriminator guards.
    #    Do NOT hard-reject on missing data.
    merged, score, reason = match_components_v2(title_a, title_b, price_a, price_b)
    return MergeResult(merged, f"deferred_tier_b:{reason}")


def can_merge(a: Any, b: Any) -> Tuple[bool, str]:
    if isinstance(a, str):
        a = ComponentItem(a)
    if isinstance(b, str):
        b = ComponentItem(b)
    title_a = getattr(a, "title", str(a))
    title_b = getattr(b, "title", str(b))
    price_a = float(getattr(a, "price", 0.0) or 0.0)
    price_b = float(getattr(b, "price", 0.0) or 0.0)
    merged, score, reason = match_components_v2(title_a, title_b, price_a, price_b)
    return MergeResult(merged, f"v2_matcher:{reason}")





